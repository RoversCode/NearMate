"""验证普通设置与 Skill PATCH 共用同一个原子提交边界。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.background_agent.config_commit_lock import (
    BackgroundAgentConfigCommitLock,
)
from companion.background_agent.settings import BackgroundAgentSettings
from companion.background_agent.skill_registry import (
    SkillRegistry,
    SkillRevisionConflictError,
)
from companion.core.api.routers.settings import (
    update_audio_device_settings,
    update_settings,
)
from companion.core.api.schemas.settings import (
    AudioDeviceSettingsRequest,
    SettingsRequest,
)
from companion.core.application.errors import StateConflict
from companion.core.application.general_settings import GeneralSettingsUpdate
from companion.core.application.user_settings_reconfiguration import (
    UserSettingsCoordinator,
)
from companion.core.settings import Settings
from companion.core.user_settings_store import MAX_SAFE_SETTINGS_REVISION
from companion.realtime.shell_projector import ShellProjector


class RecordingSettingsStore:
    """保存测试期间写入的独立设置快照。

    Attributes:
        snapshots: 按保存顺序记录的深拷贝设置载荷。
    """

    def __init__(self, *, fail_next_save: bool = False) -> None:
        """初始化空的设置快照列表。

        Args:
            fail_next_save: 是否让第一次保存模拟持久化失败。

        Returns:
            无返回值。
        """

        self.snapshots: list[dict[str, object]] = []
        self.fail_next_save = fail_next_save

    def save(self, snapshot: dict[str, object]) -> None:
        """记录一份不会被后续内存修改污染的设置快照。

        Args:
            snapshot: coordinator 准备写入用户设置文件的完整载荷。

        Returns:
            无返回值。
        """

        if self.fail_next_save:
            self.fail_next_save = False
            raise OSError("settings persistence failed")
        self.snapshots.append(deepcopy(snapshot))


class RecordingAudioDevicePublisher:
    """记录音频设备变更请求。

    Attributes:
        events: 按调用顺序保存的独立事件副本。
    """

    def __init__(self) -> None:
        """初始化空事件列表。

        Returns:
            无返回值。
        """

        self.events: list[dict[str, object]] = []

    async def publish(self, event: dict[str, object]) -> None:
        """记录一条音频设备事件。

        Args:
            event: Router 准备广播的音频设备变更请求。

        Returns:
            无返回值。
        """

        self.events.append(deepcopy(event))


class TransactionHub:
    """提供设置事务测试需要的最小 Hub 端口。

    Args:
        settings: 测试共享的真实 Settings 对象。
        fail_broadcast: 是否让下一次设置广播在记录事件后失败。
        fail_persistence: 是否让下一次设置保存失败。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        fail_broadcast: bool = False,
        fail_persistence: bool = False,
    ) -> None:
        """保存共享设置和初始投影。

        Args:
            settings: 测试共享的真实 Settings 对象。
            fail_broadcast: 是否让下一次设置广播失败。
            fail_persistence: 是否让下一次设置保存失败。

        Returns:
            无返回值。
        """

        self.settings = settings
        self.settings_revision = 7
        self.user_settings_store = RecordingSettingsStore(
            fail_next_save=fail_persistence
        )
        self.projector = ShellProjector(
            show_speech_bubbles=settings.show_speech_bubbles
        )
        self.fail_broadcast = fail_broadcast
        self.settings_events: list[dict[str, object]] = []
        self.audio_device_publisher = RecordingAudioDevicePublisher()

    def current_settings_payload(
        self,
        *,
        settings_revision: int | None = None,
    ) -> dict[str, object]:
        """构造包含并发测试关键字段的完整快照。

        Args:
            settings_revision: 本次提交准备写入的新版本。

        Returns:
            设置版本、关键普通字段、音频设备字段和后台工作设置。
        """

        return {
            "settings_revision": (
                self.settings_revision
                if settings_revision is None
                else settings_revision
            ),
            "show_speech_bubbles": self.settings.show_speech_bubbles,
            "speaker_verification_enabled": (
                self.settings.speaker_verification_enabled
            ),
            "audio_device_mode": self.settings.audio_device_mode,
            "audio_input_device_id": self.settings.audio_input_device_id,
            "audio_input_device_name": self.settings.audio_input_device_name,
            "audio_output_reference_device_id": (
                self.settings.audio_output_reference_device_id
            ),
            "audio_output_reference_device_name": (
                self.settings.audio_output_reference_device_name
            ),
            "background_agent": self.settings.background_agent.model_dump(mode="json"),
        }

    def publisher(
        self,
        domain: str,
    ) -> "TransactionHub | RecordingAudioDevicePublisher":
        """返回当前对象充当设置事件发布器。

        Args:
            domain: coordinator 请求的公共事件领域。

        Returns:
            实现 publish 的当前测试 Hub。
        """

        if domain == "settings":
            return self
        assert domain == "audio_devices"
        return self.audio_device_publisher

    async def publish(self, event: dict[str, object]) -> None:
        """接收设置广播，或按测试要求模拟失败。

        Args:
            event: 准备广播的 settings.changed 事件。

        Returns:
            无返回值。

        Raises:
            RuntimeError: 测试要求广播失败时抛出。
        """

        assert event["type"] == "settings.changed"
        self.settings_events.append(deepcopy(event))
        if self.fail_broadcast:
            self.fail_broadcast = False
            raise RuntimeError("settings broadcast failed")


class RecordingRuntimeOwner:
    """记录说话人确认 provider 每次收到的候选设置。"""

    def __init__(self) -> None:
        """初始化空的 provider 重建记录。

        Returns:
            无返回值。
        """

        self.speaker_verification_states: list[bool] = []


@pytest.mark.asyncio
async def test_exhausted_settings_revision_rejects_before_any_side_effect(
    tmp_path: Path,
) -> None:
    """版本轴耗尽必须在准备、内存修改、落盘和广播之前失败。"""

    coordinator, settings, hub, _registry, _runtime_owner = _coordinator(tmp_path)
    hub.settings_revision = MAX_SAFE_SETTINGS_REVISION
    prepare_calls = 0

    def prepare_update(_current_settings: Settings) -> dict[str, object]:
        """记录准备动作是否被错误执行。

        Args:
            _current_settings: 事务门禁内的最新设置。

        Returns:
            一个本应修改气泡开关的字段 patch。
        """

        nonlocal prepare_calls
        prepare_calls += 1
        return {"show_speech_bubbles": False}

    with pytest.raises(StateConflict) as conflict:
        await coordinator.commit_user_settings(
            prepare_update=prepare_update,
            expected_revision=MAX_SAFE_SETTINGS_REVISION - 1,
        )

    assert conflict.value.detail == {
        "code": "settings_revision_exhausted",
        "message": "设置版本已达到安全上限，请修复本地设置后重试",
        "actual_revision": MAX_SAFE_SETTINGS_REVISION,
    }
    assert prepare_calls == 0
    assert settings.show_speech_bubbles is True
    assert hub.settings_revision == MAX_SAFE_SETTINGS_REVISION
    assert hub.user_settings_store.snapshots == []
    assert hub.settings_events == []


@pytest.mark.asyncio
async def test_exhausted_revision_rejects_before_skill_dependency_precheck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Skill PATCH 在版本耗尽时不能先执行外部依赖命令。"""

    coordinator, settings, hub, registry, _runtime_owner = _coordinator(tmp_path)
    hub.settings_revision = MAX_SAFE_SETTINGS_REVISION
    registry_update_calls = 0

    def record_unexpected_registry_update(**_kwargs: object) -> None:
        """记录本不应该开始的 Registry 依赖预检。

        Args:
            **_kwargs: 协调器准备传给 Registry 的开关参数。

        Returns:
            无返回值。
        """

        nonlocal registry_update_calls
        registry_update_calls += 1

    monkeypatch.setattr(registry, "update_enabled", record_unexpected_registry_update)

    with pytest.raises(StateConflict) as conflict:
        await coordinator.update_skill_enabled(
            skill_id="sample-skill",
            enabled=False,
            expected_revision=registry.revision_for_preferences(
                settings.background_agent.skills
            ),
        )

    assert conflict.value.detail["code"] == "settings_revision_exhausted"
    assert registry_update_calls == 0
    assert settings.background_agent.skills == {"sample-skill": True}
    assert hub.user_settings_store.snapshots == []
    assert hub.settings_events == []

    def rebuild_speaker_verification_provider(self, settings: Settings) -> None:
        """记录本次 provider 应采用的开关。

        Args:
            settings: 已冻结说话人开关和白名单的候选设置。

        Returns:
            无返回值。
        """

        self.speaker_verification_states.append(
            settings.speaker_verification_enabled
        )


@pytest.mark.asyncio
async def test_stale_general_settings_cannot_leak_into_concurrent_skill_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """普通设置版本过期后，Skill PATCH 只能持久化已提交字段。"""

    coordinator, settings, hub, registry, _runtime_owner = _coordinator(tmp_path)
    dependency_check_entered = Event()
    allow_dependency_check_to_finish = Event()

    def wait_for_dependency(_name: str) -> None:
        """暂停 Skill 依赖预检，制造两个设置入口的确定性交错。

        Args:
            _name: 依赖清单声明的命令名。

        Returns:
            始终返回空，模拟依赖未安装。
        """

        dependency_check_entered.set()
        if not allow_dependency_check_to_finish.wait(timeout=2):
            raise AssertionError("Skill 依赖预检未按测试计划放行")
        return None

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        wait_for_dependency,
    )
    skill_update = asyncio.create_task(
        coordinator.update_skill_enabled(
            skill_id="sample-skill",
            enabled=False,
            expected_revision=registry.revision_for_preferences(
                {"sample-skill": True}
            ),
        )
    )
    assert await asyncio.to_thread(dependency_check_entered.wait, 2)

    route_container = SimpleNamespace(
        hub=hub,
        user_settings=coordinator,
    )
    with pytest.raises(StateConflict) as conflict:
        await update_settings(
            _settings_request(
                expected_revision=6,
                show_speech_bubbles=False,
            ),
            cast(Any, route_container),
        )
    assert conflict.value.detail == {
        "code": "settings_revision_conflict",
        "expected_revision": 6,
        "actual_revision": 7,
    }
    assert settings.show_speech_bubbles is True

    allow_dependency_check_to_finish.set()
    await asyncio.wait_for(skill_update, timeout=2)

    assert hub.user_settings_store.snapshots[-1]["show_speech_bubbles"] is True
    assert hub.user_settings_store.snapshots[-1]["background_agent"]["skills"] == {
        "sample-skill": False
    }


@pytest.mark.asyncio
async def test_general_settings_commit_does_not_wait_for_skill_dependency_precheck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Skill 锁外预检期间，普通设置仍可完成自己的原子提交。"""

    coordinator, settings, hub, registry, _runtime_owner = _coordinator(tmp_path)
    dependency_check_entered = Event()
    allow_dependency_check_to_finish = Event()

    def wait_for_dependency(_name: str) -> None:
        """暂停 Skill 的真实依赖探测。

        Args:
            _name: 依赖清单声明的命令名。

        Returns:
            始终返回空，模拟依赖未安装。
        """

        dependency_check_entered.set()
        if not allow_dependency_check_to_finish.wait(timeout=2):
            raise AssertionError("Skill 依赖预检未按测试计划放行")
        return None

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        wait_for_dependency,
    )
    skill_update = asyncio.create_task(
        coordinator.update_skill_enabled(
            skill_id="sample-skill",
            enabled=False,
            expected_revision=registry.revision_for_preferences(
                {"sample-skill": True}
            ),
        )
    )
    assert await asyncio.to_thread(dependency_check_entered.wait, 2)

    await asyncio.wait_for(
        coordinator.update_general_settings(
            update=_general_settings_update(show_speech_bubbles=False),
            expected_revision=7,
        ),
        timeout=1,
    )
    assert settings.show_speech_bubbles is False
    assert skill_update.done() is False

    allow_dependency_check_to_finish.set()
    await asyncio.wait_for(skill_update, timeout=2)

    assert hub.user_settings_store.snapshots[-1]["show_speech_bubbles"] is False
    assert settings.background_agent.skills == {"sample-skill": False}


@pytest.mark.asyncio
async def test_general_settings_persistence_failure_restores_memory_and_projection(
    tmp_path: Path,
) -> None:
    """提交点前保存失败时，普通设置和 Shell 投影恢复旧事实。"""

    coordinator, settings, hub, _registry, _runtime_owner = _coordinator(
        tmp_path,
        fail_persistence=True,
    )
    previous_projector = hub.projector
    settings_identity = id(settings)

    with pytest.raises(OSError, match="settings persistence failed"):
        await coordinator.update_general_settings(
            update=_general_settings_update(show_speech_bubbles=False),
            expected_revision=7,
        )

    assert settings.show_speech_bubbles is True
    assert hub.projector is previous_projector
    assert hub.settings_revision == 7
    assert id(settings) == settings_identity == id(coordinator.settings) == id(hub.settings)
    assert hub.user_settings_store.snapshots[-1] == {
        "settings_revision": 7,
        "show_speech_bubbles": True,
        "speaker_verification_enabled": False,
        "audio_device_mode": "system_default",
        "audio_input_device_id": None,
        "audio_input_device_name": None,
        "audio_output_reference_device_id": None,
        "audio_output_reference_device_name": None,
        "background_agent": settings.background_agent.model_dump(mode="json"),
    }


@pytest.mark.asyncio
async def test_cancelled_prepare_waits_for_worker_before_external_rollback(
    tmp_path: Path,
) -> None:
    """取消设置请求时，外部回调必须收口后才能执行补偿。"""

    coordinator, settings, hub, _registry, _runtime_owner = _coordinator(tmp_path)
    prepare_started = Event()
    release_prepare = Event()
    timeline: list[str] = []

    def prepare_update(_current_settings: Settings) -> dict[str, object]:
        """模拟会修改外部资源的阻塞准备动作。"""

        timeline.append("prepare-start")
        prepare_started.set()
        if not release_prepare.wait(timeout=2):
            raise AssertionError("准备回调未按测试计划收口")
        timeline.append("prepare-done")
        return {"user_self_intro": "late-prepared-value"}

    def rollback_external(_previous_settings: Settings) -> None:
        """记录外部副作用补偿发生的时机。"""

        timeline.append("rollback")

    pending = asyncio.create_task(
        coordinator.commit_user_settings(
            prepare_update=prepare_update,
            rollback_external=rollback_external,
        )
    )
    assert await asyncio.to_thread(prepare_started.wait, 1)

    pending.cancel()
    await asyncio.sleep(0)
    # worker 还在等待时不能先执行 rollback，否则迟到的准备结果可能覆盖补偿。
    assert timeline == ["prepare-start"]
    assert settings.user_self_intro == ""
    assert hub.settings_revision == 7

    release_prepare.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=2)

    assert timeline == ["prepare-start", "prepare-done", "rollback"]
    assert settings.user_self_intro == ""
    assert hub.settings_revision == 7
    assert hub.user_settings_store.snapshots == []


@pytest.mark.asyncio
async def test_broadcast_failure_after_commit_never_rolls_back_revision(
    tmp_path: Path,
) -> None:
    """事件可能已经送达时，广播异常不能把 Backend 倒退到旧版本。"""

    coordinator, settings, hub, _registry, _runtime_owner = _coordinator(
        tmp_path,
        fail_broadcast=True,
    )

    committed_snapshot = await coordinator.update_general_settings(
        update=_general_settings_update(show_speech_bubbles=False),
        expected_revision=7,
    )

    assert committed_snapshot["settings_revision"] == 8
    assert settings.show_speech_bubbles is False
    assert hub.settings_revision == 8
    assert hub.user_settings_store.snapshots[-1] == committed_snapshot
    assert hub.settings_events == [
        {"type": "settings.changed", "payload": committed_snapshot}
    ]


@pytest.mark.asyncio
async def test_failed_general_commit_cannot_leak_into_next_skill_patch(
    tmp_path: Path,
) -> None:
    """普通设置保存失败后，下一次 Skill PATCH 不得携带失败字段。"""

    coordinator, settings, hub, registry, _runtime_owner = _coordinator(
        tmp_path,
        fail_persistence=True,
    )
    with pytest.raises(OSError, match="settings persistence failed"):
        await coordinator.update_general_settings(
            update=_general_settings_update(show_speech_bubbles=False),
            expected_revision=7,
        )

    await coordinator.update_skill_enabled(
        skill_id="sample-skill",
        enabled=False,
        expected_revision=registry.revision_for_preferences(
            {"sample-skill": True}
        ),
    )

    assert settings.show_speech_bubbles is True
    assert hub.user_settings_store.snapshots[-1]["show_speech_bubbles"] is True
    assert hub.user_settings_store.snapshots[-1]["background_agent"]["skills"] == {
        "sample-skill": False
    }


@pytest.mark.asyncio
async def test_interleaved_skill_prechecks_reject_stale_second_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """两个 Skill 预检都完成后，第二个请求必须在提交锁内重校验 revision。"""

    coordinator, settings, _hub, registry, _runtime_owner = _coordinator(tmp_path)
    original_update_enabled = registry.update_enabled
    both_prechecks_completed = Barrier(2)

    def interleave_after_precheck(**kwargs: Any) -> Any:
        """让两个线程都完成 Registry 预检后再返回协调器。

        Args:
            **kwargs: 原样传给真实 Registry 的关键字参数。

        Returns:
            真实 Registry 计算的偏好、状态和 revision。
        """

        result = original_update_enabled(**kwargs)
        both_prechecks_completed.wait(timeout=2)
        return result

    monkeypatch.setattr(registry, "update_enabled", interleave_after_precheck)
    expected_revision = registry.revision_for_preferences({"sample-skill": True})
    first_update = asyncio.create_task(
        coordinator.update_skill_enabled(
            skill_id="sample-skill",
            enabled=False,
            expected_revision=expected_revision,
        )
    )
    second_update = asyncio.create_task(
        coordinator.update_skill_enabled(
            skill_id="sample-skill",
            enabled=False,
            expected_revision=expected_revision,
        )
    )

    results = await asyncio.wait_for(
        asyncio.gather(first_update, second_update, return_exceptions=True),
        timeout=3,
    )

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(
        isinstance(result, SkillRevisionConflictError) for result in results
    ) == 1
    assert settings.background_agent.skills == {"sample-skill": False}


@pytest.mark.asyncio
async def test_audio_device_writer_does_not_mutate_settings_while_waiting_for_lock(
    tmp_path: Path,
) -> None:
    """音频设备请求等待统一锁时，共享 Settings 必须保持已提交值。"""

    coordinator, settings, hub, _registry, _runtime_owner = _coordinator(tmp_path)
    broadcast_entered = asyncio.Event()
    release_broadcast = asyncio.Event()
    original_publish = hub.publish

    async def block_first_settings_broadcast(event: dict[str, object]) -> None:
        """占住首个设置事务的提交锁，直到测试完成等待态断言。

        Args:
            event: 首个事务准备广播的设置事件。

        Returns:
            无返回值。
        """

        broadcast_entered.set()
        await release_broadcast.wait()
        await original_publish(event)

    def prepare_empty_update(_current_settings: Settings) -> dict[str, object]:
        """构造只用于占住广播阶段的空字段 patch。

        Args:
            _current_settings: 当前最新设置；本测试不修改任何字段。

        Returns:
            空字段 patch。
        """

        return {}

    hub.publish = block_first_settings_broadcast  # type: ignore[method-assign]
    lock_holder = asyncio.create_task(
        coordinator.commit_user_settings(prepare_update=prepare_empty_update)
    )
    await asyncio.wait_for(broadcast_entered.wait(), timeout=1)

    route_container = SimpleNamespace(
        hub=hub,
        user_settings=coordinator,
    )
    audio_update = asyncio.create_task(
        update_audio_device_settings(
            AudioDeviceSettingsRequest(
                mode="manual",
                microphone_id=" mic-a ",
                microphone_name=" Main mic ",
                speaker_loopback_id=" loop-a ",
                speaker_loopback_name=" Speakers ",
            ),
            cast(Any, route_container),
        )
    )
    await asyncio.sleep(0)

    assert audio_update.done() is False
    assert settings.audio_device_mode == "system_default"
    assert settings.audio_input_device_id is None

    release_broadcast.set()
    await asyncio.wait_for(asyncio.gather(lock_holder, audio_update), timeout=2)

    assert settings.audio_device_mode == "manual"
    assert settings.audio_input_device_id == "mic-a"
    audio_event = hub.audio_device_publisher.events[-1]
    assert audio_event["payload"]["selection"] == {
        "mode": "manual",
        "microphone_id": "mic-a",
        "microphone_name": "Main mic",
        "speaker_loopback_id": "loop-a",
        "speaker_loopback_name": "Speakers",
    }


@pytest.mark.asyncio
async def test_audio_route_returns_success_after_device_broadcast_failure(
    tmp_path: Path,
) -> None:
    """音频事件投递失败时，路由仍返回提交快照而不制造假保存失败。"""

    coordinator, settings, hub, _registry, _runtime_owner = _coordinator(tmp_path)

    async def fail_audio_publish(_event: dict[str, object]) -> None:
        """模拟 Desktop 音频设备事件发布失败。"""

        raise RuntimeError("audio device broadcast failed")

    hub.audio_device_publisher.publish = fail_audio_publish  # type: ignore[method-assign]
    route_container = SimpleNamespace(hub=hub, user_settings=coordinator)

    response = await update_audio_device_settings(
        AudioDeviceSettingsRequest(
            mode="manual",
            microphone_id="mic-b",
            microphone_name="Backup mic",
            speaker_loopback_id="loop-b",
            speaker_loopback_name="Backup speakers",
        ),
        cast(Any, route_container),
    )

    assert response["accepted"] is True
    assert response["settings"]["settings_revision"] == 8
    assert settings.audio_device_mode == "manual"
    assert settings.audio_input_device_id == "mic-b"
    assert hub.settings_revision == 8


@pytest.mark.asyncio
async def test_after_commit_action_keeps_revision_order_before_next_writer(
    tmp_path: Path,
) -> None:
    """提交后动作完成前，下一项设置不能越过门禁并发布更新。"""

    coordinator, settings, _hub, _registry, _runtime_owner = _coordinator(
        tmp_path
    )
    first_action_started = asyncio.Event()
    release_first_action = asyncio.Event()

    def prepare_first_update(_current_settings: Settings) -> dict[str, object]:
        """构造第一项用户信息设置。

        Args:
            _current_settings: 门禁内的最新设置；本测试使用固定候选值。

        Returns:
            第一项设置拥有的字段 patch。
        """

        return {"user_self_intro": "first"}

    async def publish_first_action(_snapshot: dict[str, object]) -> None:
        """暂停第一项提交后动作，为第二个请求制造并发窗口。

        Args:
            _snapshot: 第一项已经持久化的完整设置快照。

        Returns:
            无返回值。
        """

        first_action_started.set()
        await release_first_action.wait()

    def prepare_second_update(_current_settings: Settings) -> dict[str, object]:
        """构造第二项用户信息设置。

        Args:
            _current_settings: 门禁内的最新设置；本测试使用固定候选值。

        Returns:
            第二项设置拥有的字段 patch。
        """

        return {"user_self_intro": "second"}

    first_update = asyncio.create_task(
        coordinator.commit_user_settings(
            prepare_update=prepare_first_update,
            after_commit=publish_first_action,
        )
    )
    await asyncio.wait_for(first_action_started.wait(), timeout=1)
    second_update = asyncio.create_task(
        coordinator.commit_user_settings(prepare_update=prepare_second_update)
    )
    await asyncio.sleep(0)

    assert second_update.done() is False
    assert settings.user_self_intro == "first"

    release_first_action.set()
    await asyncio.wait_for(asyncio.gather(first_update, second_update), timeout=2)

    assert settings.user_self_intro == "second"


@pytest.mark.asyncio
async def test_after_commit_failure_returns_durable_snapshot(
    tmp_path: Path,
) -> None:
    """提交后的设备通知失败时，调用方仍拿到已经落盘的新版本。"""

    coordinator, settings, hub, _registry, _runtime_owner = _coordinator(tmp_path)

    def prepare_update(_current_settings: Settings) -> dict[str, object]:
        """构造一项简单的设置变更。"""

        return {"user_self_intro": "committed-before-notify-failure"}

    async def fail_after_commit(_snapshot: dict[str, object]) -> None:
        """模拟音频设备事件投递失败。"""

        raise RuntimeError("device notification failed")

    snapshot = await coordinator.commit_user_settings(
        prepare_update=prepare_update,
        after_commit=fail_after_commit,
    )

    assert snapshot["settings_revision"] == 8
    assert settings.user_self_intro == "committed-before-notify-failure"
    assert hub.settings_revision == 8
    assert hub.user_settings_store.snapshots[-1] == snapshot


def _coordinator(
    tmp_path: Path,
    *,
    fail_broadcast: bool = False,
    fail_persistence: bool = False,
) -> tuple[
    UserSettingsCoordinator,
    Settings,
    TransactionHub,
    SkillRegistry,
    RecordingRuntimeOwner,
]:
    """构造带真实 Settings、Registry 和事务端口的 coordinator。

    Args:
        tmp_path: Pytest 提供的隔离目录。
        fail_broadcast: 是否让下一次普通设置广播失败。
        fail_persistence: 是否让下一次普通设置保存失败。

    Returns:
        coordinator、共享设置、Hub、Registry 和运行时所有者。
    """

    _write_skill(tmp_path)
    settings = Settings(
        user_settings_path=str(tmp_path / "user-settings.json"),
        show_speech_bubbles=True,
        speaker_verification_enabled=False,
        background_agent=BackgroundAgentSettings(
            environment={"ANTHROPIC_BASE_URL": "https://example.com"},
            model="test-model",
            skills={"sample-skill": True},
        ),
    )
    hub = TransactionHub(
        settings=settings,
        fail_broadcast=fail_broadcast,
        fail_persistence=fail_persistence,
    )
    registry = SkillRegistry(asset_root=tmp_path)
    runtime_owner = RecordingRuntimeOwner()

    coordinator = UserSettingsCoordinator(
        settings=settings,
        hub=cast(Any, hub),
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            tmp_path / "user-settings.json"
        ),
        skill_registry=registry,
    )
    return coordinator, settings, hub, registry, runtime_owner


def _general_settings_update(
    *,
    show_speech_bubbles: bool,
) -> GeneralSettingsUpdate:
    """构造事务测试使用的完整普通设置命令。

    Args:
        show_speech_bubbles: 候选气泡开关。

    Returns:
        其余字段使用稳定测试值的普通设置命令。
    """

    return GeneralSettingsUpdate(
        show_speech_bubbles=show_speech_bubbles,
        followup_toggle_shortcut_enabled=True,
        followup_toggle_shortcut="CommandOrControl+Alt+Space",
        avatar_visibility_shortcut_enabled=True,
        avatar_visibility_shortcut="CommandOrControl+Q",
        selection_capture_shortcut_enabled=True,
        selection_capture_shortcut="CommandOrControl+Alt+C",
        selected_live2d_model_id="Haru",
        user_self_intro="",
        user_extra_notes="",
    )


def _settings_request(
    *,
    expected_revision: int,
    show_speech_bubbles: bool,
) -> SettingsRequest:
    """构造普通设置 Router 测试使用的窄请求。

    Args:
        expected_revision: 客户端最近读取到的设置版本。
        show_speech_bubbles: 候选气泡开关。

    Returns:
        其余普通字段使用稳定值的严格请求模型。
    """

    return SettingsRequest(
        expected_revision=expected_revision,
        show_speech_bubbles=show_speech_bubbles,
        followup_toggle_shortcut_enabled=True,
        followup_toggle_shortcut="CommandOrControl+Alt+Space",
        avatar_visibility_shortcut_enabled=True,
        avatar_visibility_shortcut="CommandOrControl+Q",
        selection_capture_shortcut_enabled=True,
        selection_capture_shortcut="CommandOrControl+Alt+C",
        selected_live2d_model_id="Haru",
        user_self_intro="",
        user_extra_notes="",
    )


def _write_skill(asset_root: Path) -> None:
    """写入带一个外部依赖的合法测试 Skill。

    Args:
        asset_root: 产品工作区资产根目录。

    Returns:
        无返回值。
    """

    skill_root = asset_root / ".claude" / "skills" / "sample-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        (
            "---\n"
            "name: sample-skill\n"
            "description: 设置事务并发测试 Skill。\n"
            "---\n\n"
            "# 使用说明\n"
        ),
        encoding="utf-8",
    )
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "sample-skill",
                "name": "示例 Skill",
                "summary": "验证设置事务。",
                "category": "测试",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": [
                    {
                        "kind": "executable",
                        "name": "sample-tool",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
