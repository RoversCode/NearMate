"""验证后台智能体 JSON 与 SQLite 配置按同一请求提交或回滚。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from companion.background_agent.config_commit_lock import (
    BackgroundAgentConfigCommitLock,
)
from companion.background_agent.config_provider import BackgroundAgentConfigProvider
from companion.background_agent.settings import BackgroundAgentSettings
from companion.core.application.user_settings_reconfiguration import UserSettingsCoordinator
from companion.core.settings import Settings
from companion.core.user_settings_store import (
    MAX_SAFE_SETTINGS_REVISION,
    SETTINGS_REVISION_FIELD,
    UserSettingsStore,
)
from companion.records.bootstrap_secret_store import BootstrapSecretStore


def test_legacy_background_agent_settings_are_rewritten_once(tmp_path: Path) -> None:
    """启动读取旧配置后立即原子回写当前字段，清除退役数据。"""

    user_settings_path = tmp_path / "user-settings.json"
    user_settings_path.write_text(
        json.dumps(
            {
                "background_agent": {
                    "environment": {
                        "ANTHROPIC_BASE_URL": "https://legacy.example.com",
                        "  anthropic_model  ": "legacy-model",
                        "claude_code_effort_level": "high",
                        " Claude_Code_Subagent_Model ": "unused-model",
                    },
                    "max_turns": 0,
                    "max_budget_usd": 9.5,
                    "permission_mode": "acceptEdits",
                }
            }
        ),
        encoding="utf-8",
    )
    store = UserSettingsStore(user_settings_path)

    snapshot = store.load(Settings(user_settings_path=str(user_settings_path)))

    assert snapshot is not None
    migrated_settings = snapshot["background_agent"]
    assert isinstance(migrated_settings, BackgroundAgentSettings)
    assert migrated_settings.model == "legacy-model"
    assert migrated_settings.effort_level == "high"
    persisted = json.loads(user_settings_path.read_text(encoding="utf-8"))
    persisted_background_agent = persisted["background_agent"]
    assert persisted_background_agent["model"] == "legacy-model"
    assert persisted_background_agent["effort_level"] == "high"
    assert persisted_background_agent["max_turns"] == 0
    assert "max_budget_usd" not in persisted_background_agent
    assert persisted_background_agent["environment"] == {
        "ANTHROPIC_BASE_URL": "https://legacy.example.com"
    }


@pytest.mark.parametrize(
    ("legacy_preferences", "expected_cua_enabled"),
    [
        ({"browser-use": True, "computer-use": True}, True),
        ({"browser-use": True, "computer-use": False}, False),
        ({"browser-use": False, "computer-use": True}, False),
        ({"browser-use": False, "computer-use": False}, False),
    ],
)
def test_legacy_control_skill_preferences_migrate_to_official_cua(
    tmp_path: Path,
    legacy_preferences: dict[str, bool],
    expected_cua_enabled: bool,
) -> None:
    """旧两项控制开关必须保守折叠成唯一官方 CUA Skill 偏好。

    Args:
        tmp_path: pytest 临时目录。
        legacy_preferences: 旧 Browser/Computer Skill 开关。
        expected_cua_enabled: 迁移后官方 CUA Skill 是否开启。

    Returns:
        无返回值；断言失败时由 pytest 报告。
    """

    user_settings_path = tmp_path / "user-settings.json"
    user_settings_path.write_text(
        json.dumps(
            {
                "background_agent": {
                    "environment": {
                        "ANTHROPIC_BASE_URL": "https://example.com"
                    },
                    "model": "test-model",
                    "max_turns": 30,
                    "permission_mode": "acceptEdits",
                    "skills": {
                        **legacy_preferences,
                        "document-read": True,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    snapshot = UserSettingsStore(user_settings_path).load(
        Settings(user_settings_path=str(user_settings_path))
    )

    assert snapshot is not None
    migrated_settings = snapshot["background_agent"]
    assert isinstance(migrated_settings, BackgroundAgentSettings)
    assert migrated_settings.skills == {
        "cua-driver": expected_cua_enabled,
        "document-read": True,
    }
    persisted = json.loads(user_settings_path.read_text(encoding="utf-8"))
    assert persisted["background_agent"]["skills"] == {
        "cua-driver": expected_cua_enabled,
        "document-read": True,
    }


def test_invalid_legacy_field_preserves_file_and_recovers_valid_settings(
    tmp_path: Path,
) -> None:
    """单个非法旧字段不能清空有效端点、模型或改写用户文件。"""

    user_settings_path = tmp_path / "user-settings.json"
    original_text = json.dumps(
        {
            "show_speech_bubbles": False,
            "background_agent": {
                "environment": {
                    "ANTHROPIC_BASE_URL": "https://legacy.example.com",
                    "anthropic_model": "legacy-model",
                    "claude_code_effort_level": "unsupported",
                },
                "max_turns": 30,
                "permission_mode": "acceptEdits",
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    user_settings_path.write_text(original_text, encoding="utf-8")
    store = UserSettingsStore(user_settings_path)

    snapshot = store.load(Settings(user_settings_path=str(user_settings_path)))

    assert snapshot is not None
    recovered = snapshot["background_agent"]
    assert isinstance(recovered, BackgroundAgentSettings)
    assert recovered.environment == {
        "ANTHROPIC_BASE_URL": "https://legacy.example.com"
    }
    assert recovered.model == "legacy-model"
    assert recovered.effort_level is None
    persisted_payload = json.loads(user_settings_path.read_text(encoding="utf-8"))
    assert persisted_payload[SETTINGS_REVISION_FIELD] == 0
    assert persisted_payload["show_speech_bubbles"] is False
    assert persisted_payload["background_agent"] == {
        "environment": {
            "ANTHROPIC_BASE_URL": "https://legacy.example.com",
            "anthropic_model": "legacy-model",
            "claude_code_effort_level": "unsupported",
        },
        "max_turns": 30,
        "permission_mode": "acceptEdits",
    }


def test_invalid_environment_field_recovers_configured_default(tmp_path: Path) -> None:
    """本地 environment 类型损坏时只回退该字段，不能清空启动默认端点。"""

    user_settings_path = tmp_path / "user-settings.json"
    user_settings_path.write_text(
        json.dumps(
            {
                "background_agent": {
                    "environment": ["invalid"],
                    "model": "saved-model",
                    "effort_level": "high",
                    "max_turns": 30,
                    "permission_mode": "acceptEdits",
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        user_settings_path=str(user_settings_path),
        background_agent=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://default.example.com"},
            model="default-model",
        ),
    )

    snapshot = UserSettingsStore(user_settings_path).load(settings)

    assert snapshot is not None
    recovered = snapshot["background_agent"]
    assert isinstance(recovered, BackgroundAgentSettings)
    assert recovered.environment == {
        "ANTHROPIC_BASE_URL": "https://default.example.com"
    }
    assert recovered.model == "saved-model"
    assert recovered.effort_level == "high"


def test_invalid_legacy_environment_preserves_original_file(tmp_path: Path) -> None:
    """旧预算触发迁移时，非法 environment 不能覆盖业务配置，但可以修复版本元数据。"""

    user_settings_path = tmp_path / "user-settings.json"
    original_payload = {
        "show_speech_bubbles": False,
        "future_setting": {"kept": True},
        "background_agent": {
            "environment": ["invalid"],
            "model": "saved-model",
            "effort_level": "high",
            "max_turns": 30,
            "max_budget_usd": 1.0,
            "permission_mode": "acceptEdits",
        },
    }
    user_settings_path.write_text(
        json.dumps(original_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    settings = Settings(
        user_settings_path=str(user_settings_path),
        background_agent=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://default.example.com"},
            model="default-model",
        ),
    )

    snapshot = UserSettingsStore(user_settings_path).load(settings)

    assert snapshot is not None
    recovered = snapshot["background_agent"]
    assert isinstance(recovered, BackgroundAgentSettings)
    assert recovered.environment == {
        "ANTHROPIC_BASE_URL": "https://default.example.com"
    }
    assert recovered.model == "saved-model"
    assert recovered.effort_level == "high"
    persisted_payload = json.loads(user_settings_path.read_text(encoding="utf-8"))
    assert persisted_payload[SETTINGS_REVISION_FIELD] == 0
    assert persisted_payload["show_speech_bubbles"] == original_payload["show_speech_bubbles"]
    assert persisted_payload["future_setting"] == original_payload["future_setting"]
    assert persisted_payload["background_agent"] == original_payload["background_agent"]


def test_explicit_fields_win_over_legacy_environment_values(tmp_path: Path) -> None:
    """当前字段存在时，旧环境变量只能被清理，不能反向覆盖。"""

    user_settings_path = tmp_path / "user-settings.json"
    user_settings_path.write_text(
        json.dumps(
            {
                "background_agent": {
                    "environment": {
                        "ANTHROPIC_BASE_URL": "https://endpoint.example.com",
                        "anthropic_model": "legacy-model",
                        "claude_code_effort_level": "low",
                    },
                    "model": "current-model",
                    "effort_level": "high",
                    "max_turns": 30,
                    "permission_mode": "acceptEdits",
                }
            }
        ),
        encoding="utf-8",
    )

    snapshot = UserSettingsStore(user_settings_path).load(
        Settings(user_settings_path=str(user_settings_path))
    )

    assert snapshot is not None
    migrated = snapshot["background_agent"]
    assert isinstance(migrated, BackgroundAgentSettings)
    assert migrated.model == "current-model"
    assert migrated.effort_level == "high"


def test_user_settings_revision_survives_backend_restart(tmp_path: Path) -> None:
    """持久化 revision 必须在新进程读取同一设置文件时继续使用。"""

    user_settings_path = tmp_path / "user-settings.json"
    original_settings = Settings(user_settings_path=str(user_settings_path))
    snapshot = original_settings.model_dump(mode="json")
    snapshot[SETTINGS_REVISION_FIELD] = 17

    UserSettingsStore(user_settings_path).save(snapshot)
    restored_snapshot = UserSettingsStore(user_settings_path).load(
        Settings(user_settings_path=str(user_settings_path))
    )

    assert restored_snapshot is not None
    assert restored_snapshot[SETTINGS_REVISION_FIELD] == 17
    persisted_payload = json.loads(user_settings_path.read_text(encoding="utf-8"))
    assert persisted_payload[SETTINGS_REVISION_FIELD] == 17


@pytest.mark.parametrize(
    "invalid_revision",
    [None, -1, True, 1.5, MAX_SAFE_SETTINGS_REVISION + 1],
)
def test_invalid_settings_revision_recovers_to_zero(
    caplog: pytest.LogCaptureFixture,
    invalid_revision: object,
    tmp_path: Path,
) -> None:
    """本地版本缺失或损坏时，启动应重建零版本轴并留下诊断。"""

    user_settings_path = tmp_path / "user-settings.json"
    user_settings_path.write_text(
        json.dumps({SETTINGS_REVISION_FIELD: invalid_revision}),
        encoding="utf-8",
    )

    snapshot = UserSettingsStore(user_settings_path).load(
        Settings(user_settings_path=str(user_settings_path))
    )

    assert snapshot is not None
    assert snapshot[SETTINGS_REVISION_FIELD] == 0
    assert "settings_revision 非法" in caplog.text
    persisted_payload = json.loads(user_settings_path.read_text(encoding="utf-8"))
    assert persisted_payload[SETTINGS_REVISION_FIELD] == 0

    caplog.clear()
    second_snapshot = UserSettingsStore(user_settings_path).load(
        Settings(user_settings_path=str(user_settings_path))
    )

    assert second_snapshot is not None
    assert second_snapshot[SETTINGS_REVISION_FIELD] == 0
    assert "settings_revision 非法" not in caplog.text


def test_maximum_safe_settings_revision_round_trips(tmp_path: Path) -> None:
    """JavaScript 安全整数上限本身仍是合法、可恢复的持久化版本。"""

    user_settings_path = tmp_path / "user-settings.json"
    settings = Settings(user_settings_path=str(user_settings_path))
    snapshot = settings.model_dump(mode="json")
    snapshot[SETTINGS_REVISION_FIELD] = MAX_SAFE_SETTINGS_REVISION

    store = UserSettingsStore(user_settings_path)
    store.save(snapshot)
    restored_snapshot = store.load(settings)

    assert restored_snapshot is not None
    assert restored_snapshot[SETTINGS_REVISION_FIELD] == MAX_SAFE_SETTINGS_REVISION


def test_user_settings_store_rejects_revision_above_safe_integer_bound(
    tmp_path: Path,
) -> None:
    """仓储不能把浏览器无法无损表示的版本写入本地设置。"""

    user_settings_path = tmp_path / "user-settings.json"
    snapshot = Settings(
        user_settings_path=str(user_settings_path)
    ).model_dump(mode="json")
    snapshot[SETTINGS_REVISION_FIELD] = MAX_SAFE_SETTINGS_REVISION + 1

    with pytest.raises(ValueError, match="合法 settings_revision"):
        UserSettingsStore(user_settings_path).save(snapshot)

    assert not user_settings_path.exists()


@pytest.mark.asyncio
async def test_cancelled_config_lock_waiter_never_acquires_lock_later(
    tmp_path: Path,
) -> None:
    """等待文件锁的请求被取消后，不能在后台偷偷取得并永久占住锁。"""

    settings_path = tmp_path / "user-settings.json"
    owner_lock = BackgroundAgentConfigCommitLock(settings_path)
    waiting_lock = BackgroundAgentConfigCommitLock(settings_path)
    owner_lease = owner_lock.acquire()
    cancelled_waiter_entered = asyncio.Event()

    async def wait_for_lock() -> None:
        """等待同一路径锁，并记录是否真的进入临界区。

        Returns:
            无返回值。
        """

        async with waiting_lock.hold_async():
            cancelled_waiter_entered.set()

    try:
        waiter = asyncio.create_task(wait_for_lock())
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        owner_lease.release()

    assert cancelled_waiter_entered.is_set() is False
    with waiting_lock.hold():
        assert waiting_lock.lock_path.exists()


@pytest.mark.asyncio
async def test_background_agent_broadcast_failure_keeps_durable_commit(
    tmp_path: Path,
) -> None:
    """事件可能已经送达时，广播失败不能回退后台配置提交。"""

    settings = Settings(
        user_settings_path=str(tmp_path / "user-settings.json"),
        background_agent=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://old.example.com"}
        ),
    )
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="old-secret",
    )
    saved_snapshots: list[dict[str, object]] = []

    class SettingsStore:
        """记录原子设置文件本应收到的快照。"""

        def save(self, snapshot: dict[str, object]) -> None:
            saved_snapshots.append(snapshot)

    class Hub:
        """提供配置更新路径使用的最小控制面端口。"""

        settings_revision = 7
        bootstrap_secret_store = secret_store
        user_settings_store = SettingsStore()

        def current_settings_payload(self, *, settings_revision: int | None = None) -> dict[str, object]:
            """按当前内存设置生成可写入 JSON 的安全快照。"""

            return {
                "settings_revision": settings_revision or self.settings_revision,
                "background_agent": settings.background_agent.model_dump(mode="json"),
            }

        def publisher(self, domain: str) -> "Hub":
            """返回测试使用的设置事件发布端口。

            Args:
                domain: 生产代码申请的公共事件领域。

            Returns:
                当前 Hub；它同时实现测试需要的 publish 方法。
            """

            assert domain == "settings"
            return self

        async def publish(self, _event: dict[str, object]) -> None:
            """模拟新 JSON 已写入后发布失败。

            Args:
                _event: 本次准备公开的设置变化事件。
            """

            raise RuntimeError("broadcast failed")

    coordinator = UserSettingsCoordinator(
        settings=cast(Any, settings),
        hub=cast(Any, Hub()),
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            tmp_path / "user-settings.json"
        ),
    )

    result = await coordinator.update_background_agent_config(
        settings=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://new.example.com"},
            permission_mode="bypassPermissions",
        ),
        credential_type="auth_token",
        credential_value="new-secret",
        clear_credential=False,
    )

    assert settings.background_agent.environment == {
        "ANTHROPIC_BASE_URL": "https://new.example.com"
    }
    assert result["permission_mode"] == "bypassPermissions"
    assert result["settings"]["settings_revision"] == 8
    assert coordinator.hub.settings_revision == 8
    assert secret_store.background_agent_credentials() == {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": "new-secret",
    }
    assert saved_snapshots[-1] == {
        "settings_revision": 8,
        "background_agent": settings.background_agent.model_dump(mode="json"),
    }


@pytest.mark.asyncio
async def test_background_agent_config_serializes_concurrent_commit_and_rollback(
    tmp_path: Path,
) -> None:
    """失败请求的旧快照不能覆盖同时提交成功的后台智能体配置。"""

    settings = Settings(
        user_settings_path=str(tmp_path / "user-settings.json"),
        background_agent=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://old.example.com"}
        ),
    )
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="old-secret",
    )
    first_broadcast_entered = asyncio.Event()
    release_first_broadcast = asyncio.Event()
    saved_snapshots: list[dict[str, object]] = []

    class SettingsStore:
        """记录并发事务最终写入的安全设置快照。"""

        def save(self, snapshot: dict[str, object]) -> None:
            saved_snapshots.append(snapshot)

    class Hub:
        """让第一次广播暂停并失败，第二次广播正常完成。"""

        settings_revision = 7
        bootstrap_secret_store = secret_store
        user_settings_store = SettingsStore()
        broadcast_calls = 0

        def current_settings_payload(self, *, settings_revision: int | None = None) -> dict[str, object]:
            """按当前内存状态生成完整安全设置快照。"""

            return {
                "settings_revision": (
                    settings_revision
                    if settings_revision is not None
                    else self.settings_revision
                ),
                "background_agent": settings.background_agent.model_dump(mode="json"),
            }

        def publisher(self, domain: str) -> "Hub":
            """返回测试使用的设置事件发布端口。

            Args:
                domain: 生产代码申请的公共事件领域。

            Returns:
                当前 Hub；它同时实现测试需要的 publish 方法。
            """

            assert domain == "settings"
            return self

        async def publish(self, _event: dict[str, object]) -> None:
            """第一次发布等待并失败，后续发布立即成功。

            Args:
                _event: 本次准备公开的设置变化事件。
            """

            self.broadcast_calls += 1
            if self.broadcast_calls != 1:
                return
            first_broadcast_entered.set()
            await release_first_broadcast.wait()
            raise RuntimeError("first broadcast failed")

    hub = Hub()
    coordinator = UserSettingsCoordinator(
        settings=cast(Any, settings),
        hub=cast(Any, hub),
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            tmp_path / "user-settings.json"
        ),
    )

    first_update = asyncio.create_task(
        coordinator.update_background_agent_config(
            settings=BackgroundAgentSettings(
                environment={"ANTHROPIC_BASE_URL": "https://first.example.com"},
                permission_mode="bypassPermissions",
            ),
            credential_type="auth_token",
            credential_value="first-secret",
            clear_credential=False,
        )
    )
    await first_broadcast_entered.wait()
    second_update = asyncio.create_task(
        coordinator.update_background_agent_config(
            settings=BackgroundAgentSettings(
                environment={"ANTHROPIC_BASE_URL": "https://second.example.com"}
            ),
            credential_type="api_key",
            credential_value="second-secret",
            clear_credential=False,
        )
    )
    await asyncio.sleep(0)
    calls_while_first_pending = hub.broadcast_calls
    release_first_broadcast.set()
    first_result, second_result = await asyncio.gather(
        first_update,
        second_update,
        return_exceptions=True,
    )

    assert calls_while_first_pending == 1
    assert isinstance(first_result, dict)
    assert isinstance(second_result, dict)
    assert settings.background_agent.environment == {
        "ANTHROPIC_BASE_URL": "https://second.example.com"
    }
    assert hub.settings_revision == 9
    assert secret_store.background_agent_credentials() == {
        "ANTHROPIC_API_KEY": "second-secret",
        "ANTHROPIC_AUTH_TOKEN": None,
    }
    assert saved_snapshots[-1]["background_agent"] == settings.background_agent.model_dump(
        mode="json"
    )


@pytest.mark.asyncio
async def test_background_agent_commit_serializes_with_regular_settings_patch(
    tmp_path: Path,
) -> None:
    """后台配置广播期间，普通字段 patch 必须等待且保留两项提交。"""

    settings = Settings(
        user_settings_path=str(tmp_path / "user-settings.json"),
        background_agent=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://old.example.com"}
        ),
        show_speech_bubbles=True,
    )
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="old-secret",
    )
    background_broadcast_entered = asyncio.Event()
    release_background_broadcast = asyncio.Event()
    saved_snapshots: list[dict[str, object]] = []

    class SettingsStore:
        """记录两个设置请求写入磁盘的完整快照。"""

        def save(self, snapshot: dict[str, object]) -> None:
            saved_snapshots.append(snapshot)

    class Hub:
        """让后台配置广播暂停并失败，普通设置广播随后成功。"""

        settings_revision = 7
        bootstrap_secret_store = secret_store
        user_settings_store = SettingsStore()
        broadcast_calls = 0

        def current_settings_payload(
            self,
            *,
            settings_revision: int | None = None,
        ) -> dict[str, object]:
            """按当前内存状态生成包含普通设置的安全快照。"""

            return {
                "settings_revision": (
                    settings_revision
                    if settings_revision is not None
                    else self.settings_revision
                ),
                "background_agent": settings.background_agent.model_dump(mode="json"),
                "show_speech_bubbles": settings.show_speech_bubbles,
            }

        def publisher(self, domain: str) -> "Hub":
            """返回测试使用的设置事件发布端口。

            Args:
                domain: 生产代码申请的公共事件领域。

            Returns:
                当前 Hub；它同时实现测试需要的 publish 方法。
            """

            assert domain == "settings"
            return self

        async def publish(self, _event: dict[str, object]) -> None:
            """第一次发布等待后失败，第二次发布代表普通设置提交。

            Args:
                _event: 本次准备公开的设置变化事件。
            """

            self.broadcast_calls += 1
            if self.broadcast_calls != 1:
                return
            background_broadcast_entered.set()
            await release_background_broadcast.wait()
            raise RuntimeError("background broadcast failed")

    hub = Hub()
    coordinator = UserSettingsCoordinator(
        settings=cast(Any, settings),
        hub=cast(Any, hub),
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            tmp_path / "user-settings.json"
        ),
    )

    background_update = asyncio.create_task(
        coordinator.update_background_agent_config(
            settings=BackgroundAgentSettings(
                environment={"ANTHROPIC_BASE_URL": "https://new.example.com"},
                permission_mode="bypassPermissions",
            ),
            credential_type="auth_token",
            credential_value="new-secret",
            clear_credential=False,
        )
    )
    await background_broadcast_entered.wait()

    def prepare_regular_update(_current_settings: Settings) -> dict[str, object]:
        """构造与后台配置互不重叠的普通字段 patch。

        Args:
            _current_settings: 拿到统一锁后的最新设置；静态测试值无需读取它。

        Returns:
            关闭语音气泡的字段 patch。
        """

        return {"show_speech_bubbles": False}

    regular_persist = asyncio.create_task(
        coordinator.commit_user_settings(prepare_update=prepare_regular_update)
    )
    await asyncio.sleep(0)

    assert regular_persist.done() is False
    assert hub.broadcast_calls == 1

    release_background_broadcast.set()
    background_result, regular_result = await asyncio.gather(
        background_update,
        regular_persist,
        return_exceptions=True,
    )

    assert isinstance(background_result, dict)
    assert isinstance(regular_result, dict)
    assert settings.background_agent.environment == {
        "ANTHROPIC_BASE_URL": "https://new.example.com"
    }
    assert settings.show_speech_bubbles is False
    assert hub.settings_revision == 9
    assert secret_store.background_agent_credentials() == {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": "new-secret",
    }
    assert saved_snapshots[-1] == {
        "settings_revision": 9,
        "background_agent": settings.background_agent.model_dump(mode="json"),
        "show_speech_bubbles": False,
    }


@pytest.mark.asyncio
async def test_new_task_waits_until_background_agent_config_commit_finishes(
    tmp_path: Path,
) -> None:
    """新任务只能在配置提交锁释放后冻结已提交的新配置。"""

    user_settings_path = tmp_path / "user-settings.json"
    bootstrap_db_path = tmp_path / "bootstrap.sqlite3"
    old_settings = BackgroundAgentSettings(
        environment={"ANTHROPIC_BASE_URL": "https://old.example.com"},
        model="claude-sonnet-4-5",
    )
    settings = Settings(
        user_settings_path=str(user_settings_path),
        background_agent=old_settings.model_copy(deep=True),
    )
    user_settings_path.write_text(
        json.dumps(
            {
                "settings_revision": 7,
                "background_agent": old_settings.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    secret_store = BootstrapSecretStore(bootstrap_db_path)
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="old-secret",
    )
    background_broadcast_entered = asyncio.Event()
    release_background_broadcast = asyncio.Event()

    class SettingsStore:
        """把控制面快照写入 Provider 实际读取的 JSON 文件。"""

        def save(self, snapshot: dict[str, object]) -> None:
            user_settings_path.write_text(json.dumps(snapshot), encoding="utf-8")

    class Hub:
        """在新设置已经落盘后暂停广播并模拟事务失败。"""

        settings_revision = 7
        bootstrap_secret_store = secret_store
        user_settings_store = SettingsStore()

        def current_settings_payload(
            self,
            *,
            settings_revision: int | None = None,
        ) -> dict[str, object]:
            """按当前内存配置生成 Provider 可读取的快照。"""

            return {
                "settings_revision": (
                    settings_revision
                    if settings_revision is not None
                    else self.settings_revision
                ),
                "background_agent": settings.background_agent.model_dump(mode="json"),
            }

        def publisher(self, domain: str) -> "Hub":
            """返回测试使用的设置事件发布端口。

            Args:
                domain: 生产代码申请的公共事件领域。

            Returns:
                当前 Hub；它同时实现测试需要的 publish 方法。
            """

            assert domain == "settings"
            return self

        async def publish(self, _event: dict[str, object]) -> None:
            """让 Provider 有机会与未提交事务并发读取。

            Args:
                _event: 本次准备公开的设置变化事件。
            """

            background_broadcast_entered.set()
            await release_background_broadcast.wait()
            raise RuntimeError("background broadcast failed")

    coordinator = UserSettingsCoordinator(
        settings=cast(Any, settings),
        hub=cast(Any, Hub()),
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            user_settings_path
        ),
    )
    reader_secret_store = BootstrapSecretStore(bootstrap_db_path)
    reader_secret_store.initialize()
    provider = BackgroundAgentConfigProvider(
        user_settings_path=user_settings_path,
        secret_store=reader_secret_store,
        config_dir=tmp_path / "runtime",
    )

    background_update = asyncio.create_task(
        coordinator.update_background_agent_config(
            settings=BackgroundAgentSettings(
                environment={"ANTHROPIC_BASE_URL": "https://new.example.com"},
                model="claude-opus-4-1",
                permission_mode="bypassPermissions",
            ),
            credential_type="auth_token",
            credential_value="new-secret",
            clear_credential=False,
        )
    )
    await background_broadcast_entered.wait()
    provider_read = asyncio.create_task(asyncio.to_thread(provider.snapshot))
    await asyncio.sleep(0.05)
    provider_finished_before_transaction = provider_read.done()

    release_background_broadcast.set()
    update_result, task_config = await asyncio.gather(
        background_update,
        provider_read,
        return_exceptions=True,
    )

    assert provider_finished_before_transaction is False
    assert isinstance(update_result, dict)
    assert not isinstance(task_config, BaseException)
    assert task_config.permission_mode == "bypassPermissions"
    assert task_config.model_config.environment == {
        "ANTHROPIC_BASE_URL": "https://new.example.com",
        "ANTHROPIC_AUTH_TOKEN": "new-secret",
    }
