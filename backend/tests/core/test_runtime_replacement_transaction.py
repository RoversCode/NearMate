from __future__ import annotations

import asyncio
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from companion.background_agent.interaction_registry import BackgroundAgentInteractionRegistry
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.application.runtime_replacement import RuntimeReplacementTransaction
from companion.core.application import runtime_reconfiguration as runtime_reconfiguration_module
from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator
from companion.core.settings import Settings
from companion.realtime.assistant_process_handle import AssistantProcessReplacement


class RecordingSelfReflectionRuntime:
    """记录旧 Assistant Runtime 自我反思状态被终止的顺序。

    Args:
        events: 共享的生命周期事件列表。
    """

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def terminate_runtime_epoch(self, runtime_epoch: int, new_runtime_epoch: int) -> None:
        """记录指定运行时世代的自我反思终止动作。

        Args:
            runtime_epoch: 已被热替换的 Assistant Runtime 世代。
            new_runtime_epoch: 新 Assistant Runtime 世代。
        """

        self.events.append(f"self_reflection:terminate:{runtime_epoch}:{new_runtime_epoch}")


@pytest.mark.asyncio
async def test_runtime_configuration_waits_for_shared_mutation_gate(
    tmp_path: Path,
) -> None:
    """运行时替换必须等待用户设置共用门禁，不能并发修改 Settings。"""

    configuration_mutation_lock = asyncio.Lock()
    await configuration_mutation_lock.acquire()

    async def no_media_session_to_restart() -> bool:
        """表示门禁测试中没有活跃媒体会话。

        Returns:
            始终返回假。
        """

        return False

    coordinator = RuntimeReconfigurationCoordinator(
        settings=Settings(user_settings_path=str(tmp_path / "user-settings.json")),
        hub=cast(Any, SimpleNamespace()),
        realtime_input_runtime_owner=cast(Any, object()),
        assistant_process_supervisor=cast(Any, object()),
        activity_service=cast(Any, object()),
        background_agent_interactions=BackgroundAgentInteractionRegistry(),
        interrupt_runtime_activities=lambda _runtime_epoch: [],
        release_runtime_memory_compaction=lambda _runtime_epoch: False,
        terminate_self_reflection_runtime=cast(
            Any,
            RecordingSelfReflectionRuntime([]).terminate_runtime_epoch,
        ),
        restart_livekit_media_session=no_media_session_to_restart,
        configuration_mutation_lock=configuration_mutation_lock,
    )

    pending_update = asyncio.create_task(
        coordinator.initialize_bootstrap(
            qweather_api_key=None,
            qweather_api_host=None,
        )
    )
    await asyncio.sleep(0)

    assert pending_update.done() is False
    pending_update.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_update
    configuration_mutation_lock.release()


@pytest.mark.asyncio
async def test_bootstrap_status_read_keeps_runtime_blocked_during_configuration_loading(
    tmp_path: Path,
) -> None:
    """配置加载期间立即返回阻断状态，不能让状态读取本身等待模型完成。

    Args:
        tmp_path: 保存测试配置的临时目录。
    Returns:
        无；运行时门禁决定能否放行，配置锁只约束业务快照。
    """

    configuration_mutation_lock = asyncio.Lock()
    bootstrap = SimpleNamespace(runtime_blocked=True)
    bootstrap.status_payload = lambda: {"is_initialized": not bootstrap.runtime_blocked}
    hub = SimpleNamespace(
        bootstrap=bootstrap,
    )
    coordinator = RuntimeReconfigurationCoordinator(
        settings=Settings(user_settings_path=str(tmp_path / "user-settings.json")),
        hub=cast(Any, hub),
        realtime_input_runtime_owner=cast(Any, object()),
        assistant_process_supervisor=cast(Any, object()),
        activity_service=cast(Any, object()),
        background_agent_interactions=BackgroundAgentInteractionRegistry(),
        interrupt_runtime_activities=lambda _runtime_epoch: [],
        release_runtime_memory_compaction=lambda _runtime_epoch: False,
        terminate_self_reflection_runtime=cast(
            Any,
            RecordingSelfReflectionRuntime([]).terminate_runtime_epoch,
        ),
        restart_livekit_media_session=lambda: False,
        configuration_mutation_lock=configuration_mutation_lock,
    )

    async with configuration_mutation_lock:
        payload = await asyncio.wait_for(coordinator.current_bootstrap_status_payload(), timeout=1)
        assert payload["is_initialized"] is False
    bootstrap.runtime_blocked = False
    assert (await coordinator.current_bootstrap_status_payload())["is_initialized"] is True


class RecordingRealtimeInputRuntimeOwner:
    """记录实时输入候选的构造、发布与关闭。"""

    def __init__(
        self,
        events: list[str],
        candidate: object | None,
        old_runtime: object | None,
    ) -> None:
        self.events = events
        self.candidate = candidate
        self.runtime = old_runtime

    def build_candidate(self) -> object | None:
        """返回测试指定的输入候选。"""

        self.events.append("input:build")
        return self.candidate

    def publish_candidate(self, candidate: object | None) -> object:
        """记录输入候选的所有权转移并返回新旧资源。"""

        old_runtime = self.runtime
        self.runtime = candidate
        self.events.append(f"input:publish:{id(candidate)}")
        return SimpleNamespace(
            old_runtime=old_runtime,
            current_runtime=candidate,
        )

    def shutdown_runtime(self, runtime: object | None) -> None:
        """记录尚未发布的输入候选被回收。"""

        if runtime is not None:
            self.events.append(f"input:shutdown:{id(runtime)}")

    def restore_replacement(self, replacement: object) -> None:
        """把输入 Owner 恢复为交接前的运行时。"""

        old_runtime = getattr(replacement, "old_runtime")
        self.runtime = old_runtime
        self.events.append(f"input:restore:{id(old_runtime)}")

    async def broadcast_current_runtime_identity(self) -> None:
        """记录事务越过提交点后才公开输入 Runtime 身份。"""

        self.events.append(f"input:identity:{id(self.runtime)}")


class RecordingAssistantProcessSupervisor:
    """记录助手候选的构造、启动、发布与关闭。"""

    def __init__(
        self,
        events: list[str],
        *,
        candidate: object | None,
        old_process: object | None,
    ) -> None:
        self.events = events
        self.candidate = candidate
        self.old_process = old_process
        self.current_target: object | None = None
        self.pending_replacement: AssistantProcessReplacement | None = None

    def build_candidate(self) -> object | None:
        """返回测试指定的助手候选。"""

        self.events.append("assistant:build")
        return self.candidate

    def start_candidate(self, candidate: object) -> None:
        """记录候选助手子进程已经就绪。"""

        self.events.append(f"assistant:start:{id(candidate)}")

    def publish_candidate(self, candidate: object) -> AssistantProcessReplacement:
        """记录候选发布并返回新旧世代。"""

        self.events.append(f"assistant:publish:{id(candidate)}")
        self.current_target = candidate
        replacement = AssistantProcessReplacement(
            old_runtime=cast(Any, self.old_process),
            old_epoch=1 if self.old_process is not None else None,
            current_epoch=2 if self.old_process is not None else 1,
        )
        self.pending_replacement = replacement
        return replacement

    def is_current_target(self, candidate: object | None) -> bool:
        """与生产语义一致：已经完成发布且仍现役的候选不能再被当作未发布资源回收。"""

        return candidate is not None and candidate is self.current_target

    def take_published_replacement(
        self,
        candidate: object,
    ) -> AssistantProcessReplacement | None:
        """返回并清空现役候选的交接事实，模拟生产 Supervisor 的恢复观察口。"""

        if not self.is_current_target(candidate):
            return None
        replacement = self.pending_replacement
        self.pending_replacement = None
        return replacement

    def shutdown_process(
        self,
        process: object | None,
        *,
        runtime_epoch: int | None = None,
    ) -> None:
        """记录候选或旧助手进程被关闭。

        Args:
            process: 等待关闭的候选或旧助手进程。
            runtime_epoch: 已发布旧进程的运行时世代；未发布候选为空。
        """

        if process is not None:
            epoch_suffix = f":{runtime_epoch}" if runtime_epoch is not None else ""
            self.events.append(f"assistant:shutdown:{id(process)}{epoch_suffix}")


class RecordingActivityService:
    """记录旧世代 Activity 的中断时机。"""

    def __init__(self, events: list[str], *, failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure

    async def interrupt_active(self, runtime_epoch: int, failure_code: str) -> bool:
        """记录中断错误码并模拟权威快照完成更新。"""

        self.events.append(f"activity:interrupt:{runtime_epoch}:{failure_code}")
        if self.failure is not None:
            raise self.failure
        return True


def test_assistant_process_candidate_can_be_built_before_first_bootstrap_commit() -> None:
    """首次初始化必须能在持久化 Bootstrap 之前准备助手子进程候选。"""

    candidate = object()
    supervisor = object.__new__(AssistantProcessSupervisor)
    supervisor._shutdown_lock = RLock()
    supervisor._pending_shutdown_clients = {}
    supervisor.output_enabled = True
    supervisor.owns_process = True
    supervisor.hub = SimpleNamespace(
        bootstrap=SimpleNamespace(
            status_payload=lambda: {"is_initialized": False},
        )
    )
    supervisor.build_owned_process_client = lambda: cast(Any, candidate)

    assert supervisor.build_candidate() is candidate


@pytest.mark.asyncio
async def test_first_bootstrap_commit_failure_restores_settings_and_persisted_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """首次运行时提交失败时，内存设置与启动密钥必须恢复到请求前快照。"""

    old_persisted = {
        "llm_config_json": "old-llm",
        "qweather_api_key": "old-weather",
        "qweather_api_host": "old-host",
    }

    class RecordingSecretStore:
        """记录启动密钥快照、写入和恢复。"""

        def __init__(self) -> None:
            self.values = dict(old_persisted)

        def snapshot_service_secrets(self) -> dict[str, str | None]:
            return dict(self.values)

        def restore_service_secrets(
            self,
            snapshot: dict[str, str | None],
            *,
            keys: tuple[str, ...] = ("llm_config_json", "qweather_api_key", "qweather_api_host"),
        ) -> None:
            for key in keys:
                self.values[key] = snapshot[key]

    settings = SimpleNamespace(
        llm=object(),
        qweather_api_key="old-weather",
        qweather_api_host="old-host",
    )
    store = RecordingSecretStore()

    class RecordingBootstrap:
        """模拟首次 Bootstrap 持久化和 Settings 同步。"""

        secret_store = store

        def normalize_secrets(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                qweather_api_key=None,
                qweather_api_host=None,
            )

        def initialize(self, **_kwargs: object) -> dict[str, object]:
            store.values["qweather_api_key"] = "new-weather"
            settings.qweather_api_key = "new-weather"
            return {"is_initialized": True}

    class FailingReplacement:
        """模拟候选启动阶段失败的替换事务。"""

        state = "prepared"

        async def commit(self) -> object:
            self.state = "rolled_back"
            raise RuntimeError("assistant process failed to start")

        def rollback(self) -> bool:
            self.state = "rolled_back"
            return True

    monkeypatch.setattr(
        runtime_reconfiguration_module,
        "validate_llm_settings_ready",
        lambda _settings: SimpleNamespace(is_ready=True),
    )
    async def no_media_session_to_restart() -> bool:
        return False

    coordinator = RuntimeReconfigurationCoordinator(
        settings=cast(Any, settings),
        hub=cast(Any, SimpleNamespace(bootstrap=RecordingBootstrap())),
        realtime_input_runtime_owner=cast(Any, object()),
        assistant_process_supervisor=cast(Any, object()),
        activity_service=cast(Any, object()),
        background_agent_interactions=BackgroundAgentInteractionRegistry(),
        interrupt_runtime_activities=lambda _runtime_epoch: [],
        release_runtime_memory_compaction=lambda _runtime_epoch: False,
        terminate_self_reflection_runtime=cast(
            Any,
            RecordingSelfReflectionRuntime([]).terminate_runtime_epoch,
        ),
        restart_livekit_media_session=no_media_session_to_restart,
    )
    coordinator._prepare_runtime_replacement = AsyncMock(return_value=cast(
        Any,
        FailingReplacement(),
    ))

    with pytest.raises(RuntimeError, match="assistant process failed to start"):
        await coordinator.initialize_bootstrap(
            qweather_api_key=None,
            qweather_api_host=None,
        )

    assert settings.qweather_api_key == "old-weather"
    assert settings.qweather_api_host == "old-host"
    assert store.values == old_persisted


@pytest.mark.asyncio
async def test_first_bootstrap_cancellation_after_commit_keeps_applied_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """提交点后的请求取消可以传播，但不得恢复已生效的配置。"""

    old_persisted = {
        "llm_config_json": "old-llm",
        "qweather_api_key": None,
        "qweather_api_host": None,
    }

    class RecordingSecretStore:
        """记录提交点前后的启动密钥状态。"""

        def __init__(self) -> None:
            self.values = dict(old_persisted)

        def snapshot_service_secrets(self) -> dict[str, str | None]:
            return dict(self.values)

        def restore_service_secrets(
            self,
            snapshot: dict[str, str | None],
            *,
            keys: tuple[str, ...] = ("llm_config_json", "qweather_api_key", "qweather_api_host"),
        ) -> None:
            for key in keys:
                self.values[key] = snapshot[key]

    settings = SimpleNamespace(
        llm=object(),
        qweather_api_key="",
        qweather_api_host="",
    )
    store = RecordingSecretStore()

    class RecordingBootstrap:
        """模拟首次 Bootstrap 已经越过持久化提交点。"""

        secret_store = store

        def normalize_secrets(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                qweather_api_key=None,
                qweather_api_host=None,
            )

        def initialize(self, **_kwargs: object) -> dict[str, object]:
            store.values["qweather_api_key"] = "new-weather"
            settings.qweather_api_key = "new-weather"
            return {"is_initialized": True}

    class CancelledAfterCommitReplacement:
        """模拟提交点后的调用方取消。"""

        state = "prepared"

        async def commit(self) -> object:
            self.state = "committed"
            raise asyncio.CancelledError

        def rollback(self) -> bool:
            return False

    monkeypatch.setattr(
        runtime_reconfiguration_module,
        "validate_llm_settings_ready",
        lambda _settings: SimpleNamespace(is_ready=True),
    )

    async def no_media_session_to_restart() -> bool:
        return False

    coordinator = RuntimeReconfigurationCoordinator(
        settings=cast(Any, settings),
        hub=cast(Any, SimpleNamespace(bootstrap=RecordingBootstrap())),
        realtime_input_runtime_owner=cast(Any, object()),
        assistant_process_supervisor=cast(Any, object()),
        activity_service=cast(Any, object()),
        background_agent_interactions=BackgroundAgentInteractionRegistry(),
        interrupt_runtime_activities=lambda _runtime_epoch: [],
        release_runtime_memory_compaction=lambda _runtime_epoch: False,
        terminate_self_reflection_runtime=cast(
            Any,
            RecordingSelfReflectionRuntime([]).terminate_runtime_epoch,
        ),
        restart_livekit_media_session=no_media_session_to_restart,
    )
    coordinator._prepare_runtime_replacement = AsyncMock(return_value=cast(
        Any,
        CancelledAfterCommitReplacement(),
    ))

    with pytest.raises(asyncio.CancelledError):
        await coordinator.initialize_bootstrap(
            qweather_api_key=None,
            qweather_api_host=None,
        )

    assert settings.qweather_api_key == "new-weather"
    assert store.values["qweather_api_key"] == "new-weather"


@pytest.mark.asyncio
async def test_initialize_bootstrap_starts_timed_reminders_once_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次初始化越过提交点后，必须补上 lifecycle 启动点早已错过的提醒调度。"""

    class RecordingBootstrap:
        """模拟首次初始化直接成功并就绪。"""

        secret_store = SimpleNamespace(
            snapshot_service_secrets=lambda: {},
            restore_service_secrets=lambda _snapshot, **_kwargs: None,
        )

        def normalize_secrets(self, **_kwargs: object) -> object:
            return SimpleNamespace(qweather_api_key=None, qweather_api_host=None)

        def initialize(self, **_kwargs: object) -> dict[str, object]:
            return {"is_initialized": True}

    started: list[str] = []

    monkeypatch.setattr(
        runtime_reconfiguration_module,
        "validate_llm_settings_ready",
        lambda _settings: SimpleNamespace(is_ready=True),
    )

    async def no_media_session_to_restart() -> bool:
        return False

    coordinator = RuntimeReconfigurationCoordinator(
        settings=cast(
            Any,
            SimpleNamespace(llm=object(), qweather_api_key="", qweather_api_host=""),
        ),
        hub=cast(Any, SimpleNamespace(bootstrap=RecordingBootstrap())),
        realtime_input_runtime_owner=cast(Any, object()),
        assistant_process_supervisor=cast(Any, object()),
        activity_service=cast(Any, object()),
        background_agent_interactions=BackgroundAgentInteractionRegistry(),
        interrupt_runtime_activities=lambda _runtime_epoch: [],
        release_runtime_memory_compaction=lambda _runtime_epoch: False,
        terminate_self_reflection_runtime=cast(
            Any,
            RecordingSelfReflectionRuntime([]).terminate_runtime_epoch,
        ),
        restart_livekit_media_session=no_media_session_to_restart,
        resume_runtime_consumers=lambda: started.extend(("delivery", "scheduler")),
    )
    # 本用例不涉及运行时替换；语音门禁与候选构造由其它测试覆盖。
    coordinator._prepare_runtime_replacement = AsyncMock(return_value=None)

    await coordinator.initialize_bootstrap(
        qweather_api_key=None,
        qweather_api_host=None,
    )

    assert started == ["delivery", "scheduler"]


def _prepared_transaction(
    *,
    events: list[str],
    input_candidate: object | None,
    assistant_candidate: object | None,
    old_process: object | None,
    old_input_runtime: object | None = None,
    activity_failure: Exception | None = None,
    media_restart_failure: Exception | None = None,
) -> RuntimeReplacementTransaction:
    """构造带完整补偿记录器的运行时替换事务。"""

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        events.append(f"material:{event['type']}")

    input_owner = RecordingRealtimeInputRuntimeOwner(
        events,
        input_candidate,
        old_input_runtime,
    )

    async def restart_livekit_media_session() -> bool:
        events.append(f"media:restart:{id(input_owner.runtime)}")
        if input_owner.runtime is input_candidate and media_restart_failure is not None:
            raise media_restart_failure
        return True

    return RuntimeReplacementTransaction.prepare(
        realtime_input_runtime_owner=cast(
            Any,
            input_owner,
        ),
        assistant_process_supervisor=cast(
            Any,
            RecordingAssistantProcessSupervisor(
                events,
                candidate=assistant_candidate,
                old_process=old_process,
            ),
        ),
        activity_service=cast(
            Any,
            RecordingActivityService(events, failure=activity_failure),
        ),
        interrupt_runtime_activities=lambda runtime_epoch: (
            events.append(f"notice:interrupt:{runtime_epoch}")
            or [{"type": "materials.assets_released", "payload": {}}]
        ),
        interrupt_background_agent_interactions=lambda runtime_epoch: (
            events.append(f"interaction:interrupt:{runtime_epoch}") or []
        ),
        release_runtime_memory_compaction=lambda runtime_epoch: (
            events.append(f"memory:release:{runtime_epoch}") or True
        ),
        terminate_self_reflection_runtime=cast(
            Any,
            RecordingSelfReflectionRuntime(events).terminate_runtime_epoch,
        ),
        broadcast_ephemeral=broadcast_ephemeral,
        restart_livekit_media_session=restart_livekit_media_session,
    )


@pytest.mark.asyncio
async def test_commit_transfers_candidates_then_retires_previous_epoch() -> None:
    """提交必须先发布输入与媒体链，再发布助手世代并回收旧资源。"""

    events: list[str] = []
    input_candidate = object()
    old_input_runtime = object()
    assistant_candidate = object()
    old_process = object()
    transaction = _prepared_transaction(
        events=events,
        input_candidate=input_candidate,
        assistant_candidate=assistant_candidate,
        old_process=old_process,
        old_input_runtime=old_input_runtime,
    )

    result = await transaction.commit()

    assert events == [
        "input:build",
        "assistant:build",
        f"assistant:start:{id(assistant_candidate)}",
        f"input:publish:{id(input_candidate)}",
        f"media:restart:{id(input_candidate)}",
        f"assistant:publish:{id(assistant_candidate)}",
        f"input:identity:{id(input_candidate)}",
        f"input:shutdown:{id(old_input_runtime)}",
        "self_reflection:terminate:1:2",
        "memory:release:1",
        "activity:interrupt:1:runtime_reconfigured",
        "notice:interrupt:1",
        "interaction:interrupt:1",
        f"assistant:shutdown:{id(old_process)}:1",
        "material:materials.assets_released",
    ]
    assert result.realtime_input_replaced is True
    assert result.assistant_process_replaced is True
    assert result.previous_runtime_epoch == 1
    assert result.current_runtime_epoch == 2
    assert transaction.state == "committed"


@pytest.mark.asyncio
async def test_media_restart_failure_rolls_back_before_assistant_publish() -> None:
    """媒体链无法绑定新输入 Runtime 时必须恢复旧 Owner，且不得发布助手候选。"""

    events: list[str] = []
    old_input_runtime = object()
    input_candidate = object()
    assistant_candidate = object()
    transaction = _prepared_transaction(
        events=events,
        input_candidate=input_candidate,
        assistant_candidate=assistant_candidate,
        old_process=object(),
        old_input_runtime=old_input_runtime,
        media_restart_failure=RuntimeError("media restart failed"),
    )

    with pytest.raises(RuntimeError, match="media restart failed"):
        await transaction.commit()

    assert events == [
        "input:build",
        "assistant:build",
        f"assistant:start:{id(assistant_candidate)}",
        f"input:publish:{id(input_candidate)}",
        f"media:restart:{id(input_candidate)}",
        f"input:restore:{id(old_input_runtime)}",
        f"media:restart:{id(old_input_runtime)}",
        f"assistant:shutdown:{id(assistant_candidate)}",
        f"input:shutdown:{id(input_candidate)}",
    ]
    assert transaction.state == "rolled_back"


@pytest.mark.asyncio
async def test_post_commit_retirement_failure_does_not_reject_applied_configuration() -> None:
    """新资源发布后旧 Activity 清理失败只能记录，不能把已提交配置报告为失败。"""

    events: list[str] = []
    input_candidate = object()
    assistant_candidate = object()
    old_process = object()
    transaction = _prepared_transaction(
        events=events,
        input_candidate=input_candidate,
        assistant_candidate=assistant_candidate,
        old_process=old_process,
        old_input_runtime=object(),
        activity_failure=RuntimeError("activity cleanup failed"),
    )

    result = await transaction.commit()

    assert result.realtime_input_replaced is True
    assert result.assistant_process_replaced is True
    assert f"assistant:publish:{id(assistant_candidate)}" in events
    assert "notice:interrupt:1" in events
    assert f"assistant:shutdown:{id(old_process)}:1" in events
    assert transaction.state == "committed"


@pytest.mark.asyncio
async def test_post_commit_input_shutdown_failure_does_not_reject_applied_configuration() -> None:
    """输入 Runtime 已发布后，旧实例关闭失败只能记录诊断。"""

    events: list[str] = []
    transaction = _prepared_transaction(
        events=events,
        input_candidate=object(),
        assistant_candidate=None,
        old_process=None,
        old_input_runtime=object(),
    )

    def fail_shutdown(_runtime: object | None) -> None:
        raise RuntimeError("input cleanup failed")

    transaction.realtime_input_runtime_owner.shutdown_runtime = fail_shutdown

    result = await transaction.commit()

    assert result.realtime_input_replaced is True
    assert transaction.state == "committed"


@pytest.mark.asyncio
async def test_post_commit_process_shutdown_failure_does_not_reject_applied_configuration() -> None:
    """助手世代已发布后，旧进程关闭失败只能记录诊断。"""

    events: list[str] = []
    transaction = _prepared_transaction(
        events=events,
        input_candidate=None,
        assistant_candidate=object(),
        old_process=object(),
    )

    def fail_shutdown(_process: object | None) -> None:
        raise RuntimeError("assistant cleanup failed")

    transaction.assistant_process_supervisor.shutdown_process = fail_shutdown

    result = await transaction.commit()

    assert result.assistant_process_replaced is True
    assert transaction.state == "committed"


@pytest.mark.asyncio
async def test_first_publish_has_no_spurious_interruption() -> None:
    """首次创建助手子进程时没有旧世代，不得生成中断事实。"""

    events: list[str] = []
    assistant_candidate = object()
    transaction = _prepared_transaction(
        events=events,
        input_candidate=None,
        assistant_candidate=assistant_candidate,
        old_process=None,
    )

    result = await transaction.commit()

    assert events == [
        "input:build",
        "assistant:build",
        f"assistant:start:{id(assistant_candidate)}",
        f"assistant:publish:{id(assistant_candidate)}",
    ]
    assert result.previous_runtime_epoch is None
    assert result.current_runtime_epoch == 1


def test_rollback_closes_both_unpublished_candidates_once() -> None:
    """持久化失败时事务必须定向关闭仍由自己持有的全部候选。"""

    events: list[str] = []
    input_candidate = SimpleNamespace()
    assistant_candidate = SimpleNamespace()
    transaction = _prepared_transaction(
        events=events,
        input_candidate=input_candidate,
        assistant_candidate=assistant_candidate,
        old_process=None,
    )

    assert transaction.rollback() is True
    assert transaction.rollback() is False

    assert events == [
        "input:build",
        "assistant:build",
        f"assistant:shutdown:{id(assistant_candidate)}",
        f"input:shutdown:{id(input_candidate)}",
    ]
    assert transaction.state == "rolled_back"


@pytest.mark.asyncio
async def test_post_swap_wrapper_failure_returns_committed_success() -> None:
    """发布包装器在句柄交换后抛错时，事务仍提交并回收旧进程。"""

    events: list[str] = []
    assistant_candidate = object()
    old_process = object()
    transaction = _prepared_transaction(
        events=events,
        input_candidate=None,
        assistant_candidate=assistant_candidate,
        old_process=old_process,
    )

    def failing_wrapper(
        candidate: object,
        _mark_committed: object,
    ) -> AssistantProcessReplacement:
        # 真实发布已经完成（稳定句柄指向候选进程），后续记账步骤才失败。
        replacement = transaction.assistant_process_supervisor.publish_candidate(
            cast(Any, candidate)
        )
        raise RuntimeError("post-swap bookkeeping failed") from None

    transaction.publish_assistant_candidate = failing_wrapper

    result = await transaction.commit()

    assert f"assistant:publish:{id(assistant_candidate)}" in events
    assert f"assistant:shutdown:{id(assistant_candidate)}" not in events
    assert f"assistant:shutdown:{id(old_process)}:1" in events
    assert transaction.state == "committed"
    assert result.previous_runtime_epoch == 1
    assert result.current_runtime_epoch == 2
