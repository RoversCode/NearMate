"""验证 Skill 偏好与后台工作配置使用同一个原子提交边界。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from companion.background_agent.config_commit_lock import (
    BackgroundAgentConfigCommitLock,
)
from companion.background_agent.settings import BackgroundAgentSettings
from companion.background_agent.skill_registry import SkillRegistry
from companion.core.application.user_settings_reconfiguration import (
    UserSettingsCoordinator,
)
from companion.core.settings import Settings
from companion.records.bootstrap_secret_store import BootstrapSecretStore


class RecordingSettingsStore:
    """记录设置事务写入的完整快照。

    Attributes:
        snapshots: 按保存顺序记录的 JSON 安全快照。
    """

    def __init__(self) -> None:
        """初始化空的快照记录。

        Returns:
            无返回值。
        """

        self.snapshots: list[dict[str, object]] = []

    def save(self, snapshot: dict[str, object]) -> None:
        """记录一次原子设置保存。

        Args:
            snapshot: 本次应写入用户设置文件的完整快照。

        Returns:
            无返回值。
        """

        self.snapshots.append(snapshot)


class SkillSettingsHub:
    """提供 Skill 设置事务需要的最小控制面端口。

    Args:
        settings: 测试共享的可变设置对象。
        secret_store: 后台配置接口读取凭据状态使用的仓储。
        fail_broadcast: 是否让设置广播失败以验证回滚。
        pause_failing_broadcast: 是否在失败前暂停，供并发读取测试制造稳定交错。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        secret_store: BootstrapSecretStore,
        fail_broadcast: bool = False,
        pause_failing_broadcast: bool = False,
    ) -> None:
        """保存测试依赖和初始设置版本。

        Args:
            settings: 测试共享的可变设置对象。
            secret_store: 后台配置接口读取凭据状态使用的仓储。
            fail_broadcast: 是否让设置广播失败。
            pause_failing_broadcast: 是否等测试放行后再抛出广播错误。

        Returns:
            无返回值。
        """

        self.settings = settings
        self.settings_revision = 3
        self.bootstrap_secret_store = secret_store
        self.user_settings_store = RecordingSettingsStore()
        self.fail_broadcast = fail_broadcast
        self.pause_failing_broadcast = pause_failing_broadcast
        self.broadcast_entered = asyncio.Event()
        self.allow_broadcast_to_finish = asyncio.Event()

    def current_settings_payload(
        self,
        *,
        settings_revision: int | None = None,
    ) -> dict[str, object]:
        """按当前内存设置生成可保存快照。

        Args:
            settings_revision: 本次提交准备写入的新版本。

        Returns:
            包含版本和后台工作设置的安全字典。
        """

        return {
            "settings_revision": (
                self.settings_revision
                if settings_revision is None
                else settings_revision
            ),
            "show_speech_bubbles": self.settings.show_speech_bubbles,
            "background_agent": self.settings.background_agent.model_dump(mode="json"),
        }

    def publisher(self, domain: str) -> "SkillSettingsHub":
        """返回测试对象自身充当设置事件发布器。

        Args:
            domain: 生产代码申请的公共事件领域。

        Returns:
            同时实现 ``publish`` 的当前对象。
        """

        assert domain == "settings"
        return self

    async def publish(self, event: dict[str, object]) -> None:
        """记录发布边界或模拟广播失败。

        Args:
            event: 准备公开的设置变化事件。

        Returns:
            无返回值。

        Raises:
            RuntimeError: 测试要求模拟广播失败时抛出。
        """

        assert event["type"] == "settings.changed"
        if self.fail_broadcast:
            self.broadcast_entered.set()
            if self.pause_failing_broadcast:
                await self.allow_broadcast_to_finish.wait()
            raise RuntimeError("broadcast failed")


@pytest.mark.asyncio
async def test_skill_update_persists_preference_and_returns_new_revision(
    tmp_path: Path,
) -> None:
    """成功提交后，内存、持久化快照和响应必须描述同一个开关。"""

    coordinator, settings, hub, registry = _coordinator(tmp_path)
    expected_revision = registry.revision_for_preferences({})

    response = await coordinator.update_skill_enabled(
        skill_id="sample-skill",
        enabled=False,
        expected_revision=expected_revision,
    )

    assert settings.background_agent.skills == {"sample-skill": False}
    assert response["skill"]["enabled"] is False
    assert response["revision"] == registry.revision_for_preferences(
        {"sample-skill": False}
    )
    assert response["settings"]["settings_revision"] == hub.settings_revision
    assert hub.user_settings_store.snapshots[-1]["background_agent"]["skills"] == {
        "sample-skill": False
    }


@pytest.mark.asyncio
async def test_skill_update_keeps_durable_commit_when_broadcast_fails(
    tmp_path: Path,
) -> None:
    """Skill 已落盘后，即使广播失败也不能倒退 revision。"""

    coordinator, settings, hub, registry = _coordinator(
        tmp_path,
        initial_preferences={"sample-skill": True},
        fail_broadcast=True,
    )
    expected_revision = registry.revision_for_preferences(
        {"sample-skill": True}
    )

    response = await coordinator.update_skill_enabled(
        skill_id="sample-skill",
        enabled=False,
        expected_revision=expected_revision,
    )

    assert response["skill"]["enabled"] is False
    assert settings.background_agent.skills == {"sample-skill": False}
    assert hub.settings_revision == 4
    assert hub.user_settings_store.snapshots[-1]["settings_revision"] == 4
    assert hub.user_settings_store.snapshots[-1]["background_agent"]["skills"] == {
        "sample-skill": False
    }


@pytest.mark.asyncio
async def test_skill_reads_wait_for_paused_committed_update(tmp_path: Path) -> None:
    """列表和详情等待提交锁后，只能读取已经提交的新 Skill 开关。"""

    coordinator, _settings, hub, registry = _coordinator(
        tmp_path,
        initial_preferences={"sample-skill": True},
        fail_broadcast=True,
        pause_failing_broadcast=True,
    )
    update_task = asyncio.create_task(
        coordinator.update_skill_enabled(
            skill_id="sample-skill",
            enabled=False,
            expected_revision=registry.revision_for_preferences(
                {"sample-skill": True}
            ),
        )
    )
    await asyncio.wait_for(hub.broadcast_entered.wait(), timeout=1)

    # 磁盘和 revision 已经越过提交点，但 PATCH 仍持有锁完成广播。两个 GET
    # 必须等待事务退出，不能在提交过程的任意中间点读取共享对象。
    list_task = asyncio.create_task(coordinator.current_skill_library_payload())
    detail_task = asyncio.create_task(
        coordinator.current_skill_detail_payload("sample-skill")
    )
    await asyncio.sleep(0)
    assert list_task.done() is False
    assert detail_task.done() is False

    hub.allow_broadcast_to_finish.set()
    await asyncio.wait_for(update_task, timeout=1)
    list_payload, detail_payload = await asyncio.wait_for(
        asyncio.gather(list_task, detail_task),
        timeout=1,
    )

    assert list_payload["skills"][0]["enabled"] is False
    assert detail_payload["enabled"] is False


@pytest.mark.asyncio
async def test_general_settings_read_waits_for_configuration_commit(tmp_path: Path) -> None:
    """普通设置 GET 不能看到提交点前的临时字段。"""

    coordinator, settings, _hub, _registry = _coordinator(tmp_path)
    mutation_lock = coordinator._configuration_mutation_lock
    await mutation_lock.acquire()
    settings.show_speech_bubbles = False

    read_task = asyncio.create_task(coordinator.current_settings_payload())
    await asyncio.sleep(0)
    assert read_task.done() is False

    settings.show_speech_bubbles = True
    mutation_lock.release()
    payload = await read_task

    assert payload["show_speech_bubbles"] is True


@pytest.mark.asyncio
async def test_background_agent_config_save_preserves_skill_preferences(
    tmp_path: Path,
) -> None:
    """模型配置页不拥有 Skill 开关，保存时不能把已有偏好清空。"""

    coordinator, settings, _hub, _registry = _coordinator(
        tmp_path,
        initial_preferences={"sample-skill": False},
    )

    await coordinator.update_background_agent_config(
        settings=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://new.example.com"},
            model="new-model",
        ),
        credential_type="api_key",
        credential_value="new-secret",
        clear_credential=False,
    )

    assert settings.background_agent.model == "new-model"
    assert settings.background_agent.skills == {"sample-skill": False}
    background_config = await coordinator.current_background_agent_config_payload()
    assert "skills" not in background_config


def _coordinator(
    tmp_path: Path,
    *,
    initial_preferences: dict[str, bool] | None = None,
    fail_broadcast: bool = False,
    pause_failing_broadcast: bool = False,
) -> tuple[
    UserSettingsCoordinator,
    Settings,
    SkillSettingsHub,
    SkillRegistry,
]:
    """构造带真实 Registry 和最小设置事务端口的协调器。

    Args:
        tmp_path: Pytest 提供的隔离目录。
        initial_preferences: 启动时已有的 Skill 开关。
        fail_broadcast: 是否让设置事件发布失败。
        pause_failing_broadcast: 是否在广播失败前等待测试显式放行。

    Returns:
        协调器、共享设置、测试 Hub 和真实 Registry 服务。
    """

    asset_root = tmp_path / "assets"
    skill_root = asset_root / ".claude" / "skills" / "sample-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        (
            "---\n"
            "name: sample-skill\n"
            "description: 设置事务测试 Skill。\n"
            "---\n\n"
            "# Sample Skill"
        ),
        encoding="utf-8",
    )
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "sample-skill",
                "name": "示例技能",
                "summary": "用于验证设置事务。",
                "category": "测试",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(
        user_settings_path=str(tmp_path / "user-settings.json"),
        background_agent=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://old.example.com"},
            model="old-model",
            skills=initial_preferences or {},
        ),
    )
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="old-secret",
    )
    hub = SkillSettingsHub(
        settings=settings,
        secret_store=secret_store,
        fail_broadcast=fail_broadcast,
        pause_failing_broadcast=pause_failing_broadcast,
    )
    registry = SkillRegistry(asset_root=asset_root)

    coordinator = UserSettingsCoordinator(
        settings=cast(Any, settings),
        hub=cast(Any, hub),
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            tmp_path / "user-settings.json"
        ),
        skill_registry=registry,
    )
    return coordinator, settings, hub, registry
