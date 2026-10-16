from __future__ import annotations

import threading
from queue import Queue
from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.activities.notice.models import NoticeDeliveryCommand
from companion.realtime.assistant_process_bridge import (
    AssistantOutputUnavailableError,
    AssistantProcessBridge,
    _request_output_contract_payload,
)
from companion.realtime.assistant_events import (
    AssistantControlCommand,
    AssistantRequest,
    CancelCurrentGenerationCommand,
    RuntimeEvent,
)
from companion.foreground_self_reflection.contracts import (
    SelfReflectionInterruptionReplyContext,
    SelfReflectionToolCommitDecisionCommand,
)
from companion.realtime.assistant_process_handle import SwappableAssistantProcessClient
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger
from companion.realtime.assistant_request_ownership import (
    AssistantRequestOwnership,
    AssistantRequestOwnershipLedger,
)
from companion.realtime.livekit_media_session import LiveKitMediaSession
from companion.realtime.runtime_identity import (
    AssistantRequestIdentityRegistry,
)
from backend.tests.runtime_public_event_support import runtime_event_router


class CrashedAssistantRuntime:
    """模拟 bridge 检查工作信号时发现助手子进程已退出。"""

    def clear_bridge_work_available(self) -> None:
        """抛出子进程异常退出错误。"""

        raise RuntimeError("assistant process exited with code 17")

    def notify_bridge_work_available(self) -> None:
        """满足 bridge 停止端口。"""


class ReplacedDuringClearRuntime:
    """模拟旧运行时在 bridge 清理信号期间被原子替换。"""

    def __init__(self) -> None:
        self.replace_current: object | None = None

    def clear_bridge_work_available(self) -> None:
        """先发布新运行时，再抛出只归属于旧世代的关闭异常。"""

        assert callable(self.replace_current)
        self.replace_current()
        raise RuntimeError("old runtime closed")

    def notify_bridge_work_available(self) -> None:
        """满足 bridge 停止端口。"""


class CrashesAfterSubmittingRuntime:
    """模拟请求已进入 runtime 后 bridge 主循环整体崩溃。"""

    def __init__(self, *, emit_text_final: bool = False) -> None:
        """初始化清理次数和提交记录。"""

        self.clear_count = 0
        self.submitted_request_ids: list[str] = []
        self.emit_text_final = emit_text_final
        self.text_final_emitted = False

    def clear_bridge_work_available(self) -> None:
        """第一次允许提交请求，第二次模拟子进程退出。"""

        self.clear_count += 1
        if self.clear_count > 1:
            raise RuntimeError("assistant process exited")

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """记录已经越过 runtime 提交边界的请求。"""

        self.submitted_request_ids.append(request.request_id)
        return request

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """按测试场景保持未终态，或先交付一次完整文字。"""

        _ = limit
        if self.emit_text_final and not self.text_final_emitted:
            self.text_final_emitted = True
            return [
                RuntimeEvent(
                    type="assistant.text_final",
                    source="assistant_runtime",
                    payload={
                        "request_id": "text-input-1",
                        "text": "已经显示的完整回复",
                    },
                )
            ]
        return []

    def notify_bridge_work_available(self) -> None:
        """满足 bridge 唤醒端口。"""


class ReplacedWhileWaitingRuntime:
    """模拟旧运行时在正常等待唤醒后被健康新实例替换。"""

    def __init__(self) -> None:
        self.replace_current: object | None = None
        self.submitted_request_ids: list[str] = []

    def clear_bridge_work_available(self) -> None:
        """允许 Bridge 正常清理工作信号。"""

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """记录已经进入旧世代的请求。"""

        self.submitted_request_ids.append(request.request_id)
        return request

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """保持旧请求未完成。"""

        _ = limit
        return []

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """发布新运行时并像真实工作事件一样正常返回。"""

        _ = timeout
        assert callable(self.replace_current)
        self.replace_current()
        return True

    def notify_bridge_work_available(self) -> None:
        """满足 Bridge 生命周期端口。"""


class RaisesAfterReplacementRuntime(ReplacedWhileWaitingRuntime):
    """模拟旧运行时完成替换后才报告自身关闭异常。"""

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """发布新运行时及其请求后，抛出只归属于旧世代的异常。"""

        _ = timeout
        assert callable(self.replace_current)
        self.replace_current()
        raise RuntimeError("old runtime closed")


class StopsBridgeOnClearRuntime:
    """让 Bridge 在观察到健康新世代后结束测试循环。"""

    def __init__(self) -> None:
        self.stop_bridge: object | None = None
        self.submitted_request_ids: list[str] = []

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """记录已经进入健康新世代的请求。"""

        self.submitted_request_ids.append(request.request_id)
        return request

    def clear_bridge_work_available(self) -> None:
        """停止测试循环，不制造运行时失败事件。"""

        assert callable(self.stop_bridge)
        self.stop_bridge()

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """保持新运行时无待处理事件。"""

        _ = limit
        return []

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """在停止信号设置后立即返回。"""

        _ = timeout
        return False

    def notify_bridge_work_available(self) -> None:
        """满足 Bridge 生命周期端口。"""


class CompletesAudioImmediatelyRuntime:
    """模拟同一批次完成可见文字与助手音频。"""

    def __init__(self) -> None:
        self.submitted_request_ids: list[str] = []
        self.completion_emitted = False

    def clear_bridge_work_available(self) -> None:
        """允许 Bridge 正常清理工作信号。"""

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """记录已经进入运行时的请求。"""

        self.submitted_request_ids.append(request.request_id)
        return request

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """首次排水返回文字完成与音频完成事件。"""

        _ = limit
        if self.completion_emitted:
            return []
        self.completion_emitted = True
        return [
            RuntimeEvent(
                type="assistant.text_final",
                source="assistant_runtime",
                payload={"request_id": "text-input-1", "text": "已经显示的完整回复"},
            ),
            RuntimeEvent(
                type="assistant.audio_finished",
                source="assistant_runtime",
                payload={"request_id": "text-input-1"},
            ),
        ]

    def notify_bridge_work_available(self) -> None:
        """满足 Bridge 生命周期端口。"""


class ControlledOutputLifecycleRuntime:
    """按测试指定顺序回流取消、结算和音频终态。"""

    def __init__(self) -> None:
        """初始化空事件缓冲区。"""

        self.events: list[RuntimeEvent] = []

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """接受请求并原样返回。"""

        return request

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """一次取走当前测试已经放入的事件。"""

        _ = limit
        events = list(self.events)
        self.events.clear()
        return events


class EmitsIdentityConflictRuntime:
    """第一次排水时返回改写 generation 的事件，验证 Bridge 失败收口。"""

    def __init__(self) -> None:
        self.event_emitted = False

    def clear_bridge_work_available(self) -> None:
        """允许 Bridge 清理工作信号。"""

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """返回一次携带错误 generation 的助手事件。"""

        _ = limit
        if self.event_emitted:
            return []
        self.event_emitted = True
        return [
            RuntimeEvent(
                type="assistant.text_delta",
                source="assistant_runtime",
                payload={
                    "request_id": "identity-conflict-request",
                    "generation_id": "generation-from-event",
                    "text": "不应广播",
                },
            )
        ]

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """不等待，因为测试事件已经在第一次排水返回。"""

        _ = timeout
        return False

    def notify_bridge_work_available(self) -> None:
        """满足 Bridge 唤醒端口。"""


class RestartableIdleRuntime:
    """模拟可被同一 Bridge 重复启停的空闲助手运行时。"""

    def __init__(self) -> None:
        """初始化工作信号和请求提交观测点。

        Returns:
            无。
        """

        self.work_available = threading.Event()
        self.request_submitted = threading.Event()
        self.submitted_request_ids: list[str] = []

    def clear_bridge_work_available(self) -> None:
        """清除上一轮用于唤醒 Bridge 的工作信号。

        Returns:
            无。
        """

        self.work_available.clear()

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """记录已经进入运行时、但尚未产生终态的请求。

        Args:
            request: Bridge 已经接管的助手请求。

        Returns:
            原样返回已接收的请求。
        """

        self.submitted_request_ids.append(request.request_id)
        self.request_submitted.set()
        return request

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """保持请求未完成，不向 Bridge 返回任何终态。

        Args:
            limit: 本轮允许读取的最大事件数。

        Returns:
            空事件列表。
        """

        _ = limit
        return []

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """等待请求提交、停止或其它生命周期唤醒。

        Args:
            timeout: 最长等待秒数。

        Returns:
            等待期间收到工作信号时返回真。
        """

        return self.work_available.wait(timeout=timeout)

    def notify_bridge_work_available(self) -> None:
        """唤醒正在等待的 Bridge 线程。

        Returns:
            无。
        """

        self.work_available.set()


def test_bridge_stop_marks_process_health_unavailable() -> None:
    """Bridge 停止后进程级健康快照不能继续声称输出可用。"""

    runtime = RestartableIdleRuntime()
    health = AssistantOutputHealthLedger()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, SwappableAssistantProcessClient(cast(Any, runtime))),
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=Queue(),
        audio_output_scheduler=cast(Any, SimpleNamespace(clear_pending_audio=lambda: None)),
        output_health_ledger=health,
    )

    bridge.start()
    bridge.stop()

    assert health.snapshot().available is False
    assert health.snapshot().code == "output_unavailable"


def test_bridge_startup_timeout_marks_instance_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动线程未确认 running 时，start 必须失败且永久关闭提交入口。"""

    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, CrashedAssistantRuntime()),
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=Queue(),
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(
                enqueue_audio_event=lambda _event: None,
                clear_pending_audio=lambda: None,
            ),
        ),
    )
    monkeypatch.setattr(
        "companion.realtime.assistant_process_bridge.threading.Thread.start",
        lambda _thread: None,
    )
    monkeypatch.setattr(bridge._started_event, "wait", lambda timeout: False)

    with pytest.raises(AssistantOutputUnavailableError, match="启动超时"):
        bridge.start()

    assert bridge.health_state == "failed"
    assert isinstance(bridge.last_error, TimeoutError)
    assert bridge.submit_request(_assistant_request("late-request")) is False


def test_bridge_start_rejects_a_thread_that_is_still_stopping() -> None:
    """旧线程尚未退出时不得把存活误判为新一轮 Bridge 已经可用。"""

    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, CrashedAssistantRuntime()),
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=Queue(),
        audio_output_scheduler=cast(Any, object()),
    )
    bridge._health_state = "stopping"
    bridge._thread = cast(Any, SimpleNamespace(is_alive=lambda: True))

    with pytest.raises(AssistantOutputUnavailableError, match="仍在停止"):
        bridge.start()


def test_bridge_start_does_not_restart_a_dead_running_instance() -> None:
    """running 状态下线程消失属于致命故障，原实例不得静默重启。"""

    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, CrashedAssistantRuntime()),
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=Queue(),
        audio_output_scheduler=cast(Any, object()),
    )
    bridge._health_state = "running"
    bridge._thread = cast(Any, SimpleNamespace(is_alive=lambda: False))

    with pytest.raises(AssistantOutputUnavailableError, match="线程已退出"):
        bridge.start()

    assert bridge.health_state == "failed"
    assert isinstance(bridge.last_error, RuntimeError)


def test_bridge_restart_preserves_requests_waiting_for_both_terminal_facts() -> None:
    """room 重连重启 Bridge 时，不得清空尚未完成双终态的请求身份。

    Returns:
        无。
    """

    runtime = RestartableIdleRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, runtime))
    bridge = AssistantProcessBridge(
        assistant_runtime=stable_runtime,
        assistant_request_queue=Queue[AssistantRequest](),
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=Queue[RuntimeEvent](),
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(clear_pending_audio=lambda: None),
        ),
    )

    bridge.start()
    try:
        assert bridge.submit_request(_assistant_request("request-across-reconnect")) is True
        assert runtime.request_submitted.wait(timeout=1.0) is True

        bridge.stop()
        assert bridge.is_running is False
        assert any(
            record.request.request_id == "request-across-reconnect"
            for record in bridge.request_ownership_ledger.snapshot()
        )
    finally:
        bridge.stop()

        bridge.start()
        assert any(
            record.request.request_id == "request-across-reconnect"
            for record in bridge.request_ownership_ledger.snapshot()
        )


def test_new_bridge_adopts_queued_request_from_old_media_session() -> None:
    """媒体会话对象重建后，新 Bridge 必须接管旧会话尚未提交的请求。"""

    runtime = RestartableIdleRuntime()
    ledger = AssistantRequestOwnershipLedger()
    old_event_queue: Queue[RuntimeEvent] = Queue()
    old_bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, SwappableAssistantProcessClient(cast(Any, runtime))),
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=old_event_queue,
        audio_output_scheduler=cast(Any, SimpleNamespace(clear_pending_audio=lambda: None)),
        request_ownership_ledger=ledger,
    )
    request = _assistant_request("queued-across-media-rebuild")
    ledger.claim(request, bridge_id=old_bridge.bridge_id)
    old_bridge.assistant_request_queue.put(request)

    old_bridge.handoff(timeout=0.0)

    new_bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, SwappableAssistantProcessClient(cast(Any, runtime))),
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=Queue(),
        audio_output_scheduler=cast(Any, SimpleNamespace(clear_pending_audio=lambda: None)),
        request_ownership_ledger=ledger,
    )
    new_bridge.start()
    try:
        assert runtime.request_submitted.wait(timeout=1.0) is True
        assert runtime.submitted_request_ids == ["queued-across-media-rebuild"]
    finally:
        new_bridge.stop()


def test_bridge_handoff_accepts_exact_old_generation_terminals_within_window() -> None:
    """旧世代在确认窗口内送达双终态时，交接不得重复补偿请求。"""

    ledger = AssistantRequestOwnershipLedger()
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, RestartableIdleRuntime()),
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=event_queue,
        audio_output_scheduler=cast(Any, SimpleNamespace(clear_pending_audio=lambda: None)),
        request_ownership_ledger=ledger,
    )
    request = _assistant_request("request-within-handoff-window")
    ledger.claim(request, bridge_id=bridge.bridge_id)
    ledger.mark_submitted(request.request_id, bridge_id=bridge.bridge_id)
    ledger.bind_runtime_epoch(request.request_id, 7, bridge_id=bridge.bridge_id)
    bridge._thread = cast(Any, _StoppedThread())
    bridge._health_state = "running"

    ledger.observe_event(
        RuntimeEvent(
            type="assistant.generation_interrupted",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={"request_id": request.request_id, "generation_id": "generation-7"},
        )
    )
    ledger.observe_event(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={"request_id": request.request_id, "generation_id": "generation-7"},
        )
    )

    bridge.handoff(timeout=0.1)

    assert [event.type for event in _drain_events(event_queue)] == [
        "assistant.output_status_changed"
    ]


def test_bridge_handoff_times_out_and_compensates_submitted_old_generation() -> None:
    """确认窗口超时后必须补发中断和 output_unavailable 结算。"""

    ledger = AssistantRequestOwnershipLedger()
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, RestartableIdleRuntime()),
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=event_queue,
        audio_output_scheduler=cast(Any, SimpleNamespace(clear_pending_audio=lambda: None)),
        request_ownership_ledger=ledger,
    )
    request = _assistant_request("request-timeout-handoff-window")
    ledger.claim(request, bridge_id=bridge.bridge_id)
    ledger.mark_submitted(request.request_id, bridge_id=bridge.bridge_id)
    ledger.bind_runtime_epoch(request.request_id, 8, bridge_id=bridge.bridge_id)
    bridge._thread = cast(Any, _StoppedThread())
    bridge._health_state = "running"

    bridge.handoff(timeout=0.0)

    request_events = [
        event
        for event in _drain_events(event_queue)
        if event.type != "assistant.output_status_changed"
    ]
    first, second = request_events
    assert first.type == "assistant.generation_interrupted"
    assert second.type == "assistant.output_settled"
    assert second.payload["outcome"] == "output_unavailable"


class _StoppedThread:
    """让交接测试只关注账本结果，不依赖真实后台线程调度。"""

    def join(self, timeout: float | None = None) -> None:
        """模拟线程已经完成退出。"""

        _ = timeout

    def is_alive(self) -> bool:
        """返回线程已经停止。"""

        return False


def _drain_events(event_queue: Queue[RuntimeEvent]) -> list[RuntimeEvent]:
    """读取测试队列中当前已经产生的全部事件。"""

    events: list[RuntimeEvent] = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    return events


def _assistant_request(
    request_id: str,
    *,
    turn_material_context_id: str | None = None,
) -> AssistantRequest:
    """创建 bridge 生命周期测试使用的最小助手请求。"""

    return AssistantRequest(
        request_id=request_id,
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(Any, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="请回答",
        source="text_input",
        source_turn_id=None,
        requested_at=1.0,
        turn_material_context_id=turn_material_context_id,
    )


def test_followup_interruption_covers_queued_submitted_and_playing_requests() -> None:
    """回执覆盖已有输出，旧取消转发时不能再次清空随后新请求的共享音轨。"""

    cleared: list[object] = []
    commands: list[object] = []
    runtime = SimpleNamespace(
        notify_bridge_work_available=lambda: None,
        submit_control_command=commands.append,
    )
    audio = SimpleNamespace(
        clear_pending_audio=lambda **kwargs: (cleared.append(kwargs), ("playing",))[1],
        clear_pending_audio_for_request=cleared.append,
    )
    bridge = AssistantProcessBridge(
        assistant_runtime=runtime,
        assistant_request_queue=Queue(),
        assistant_control_queue=Queue(),
        event_queue=Queue(),
        audio_output_scheduler=audio,
    )
    bridge._health_state = "running"
    bridge._thread = SimpleNamespace(is_alive=lambda: True)
    for request_id in ("queued", "submitted"):
        bridge.request_ownership_ledger.claim(_assistant_request(request_id), bridge_id=bridge.bridge_id)
    bridge.request_ownership_ledger.mark_submitted("submitted", bridge_id=bridge.bridge_id)

    interrupted = bridge.interrupt_current_output(issued_at=10.0, output_identity=("reserved", None))
    assert set(interrupted) == {"queued", "submitted", "playing", "reserved"}
    assert cleared[0] == {"interrupt_before": 10.0}
    bridge.request_ownership_ledger.claim(_assistant_request("new-request"), bridge_id=bridge.bridge_id)
    cleared.clear()
    bridge.bridge_assistant_control_commands_once()
    assert set(cleared) == set(interrupted)
    assert len(commands) == 4
    assert all(isinstance(command, CancelCurrentGenerationCommand) for command in commands)
    assert {command.request_id for command in commands} == set(interrupted)
    assert all(command.reason == "followup_toggle" for command in commands)


def test_bridge_compensation_preserves_complete_self_reflection_identity() -> None:
    """热替换补偿必须让旧辅助请求仍能结算到原 Episode。"""

    context = SelfReflectionInterruptionReplyContext(
        request_id="self-reflection-reply-a",
        self_reflection_id="self-reflection-a",
        episode_id="episode-a",
        runtime_epoch=7,
        trigger_input_identity="input-a",
        instruction="只确认已经听到。",
        fallback_text="我听到了。",
    )
    request = AssistantRequest(
        request_id=context.request_id,
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(Any, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="",
        source="self_reflection_interruption_reply",
        source_turn_id=None,
        requested_at=1.0,
        history_policy="unattended_episode",
        self_reflection_context=context,
    )

    payload = _request_output_contract_payload(
        AssistantRequestOwnership(
            request=request,
            owner_bridge_id="bridge-a",
            phase="submitted",
            runtime_epoch=7,
            generation_id="generation-a",
        )
    )

    assert payload["self_reflection_id"] == "self-reflection-a"
    assert payload["episode_id"] == "episode-a"
    assert payload["runtime_epoch"] == 7
    assert payload["trigger_input_identity"] == "input-a"


def _self_reflection_commit_decision(
    runtime_epoch: int,
) -> SelfReflectionToolCommitDecisionCommand:
    """创建只用于 Bridge 世代门禁测试的工具提交批准命令。

    Args:
        runtime_epoch: 回执明确绑定的 Assistant Runtime 世代。

    Returns:
        身份完整的批准命令。
    """

    return SelfReflectionToolCommitDecisionCommand(
        commit_request_id="commit-request-a",
        self_reflection_id="self-reflection-a",
        source_activity_id="cognitive-a",
        runtime_epoch=runtime_epoch,
        tool_call_id="tool-call-a",
        tool_name="start_background_work",
        approved=True,
        reason="tool_commit_approved",
        issued_at=1.0,
    )


def test_bridge_absorbs_late_tool_commit_approval_after_runtime_replacement() -> None:
    """旧世代迟到批准不能被转发给新子进程并启动已经取消的工具。"""

    old_runtime = SimpleNamespace(submit_control_command=lambda _command: None)
    submitted_to_new: list[AssistantControlCommand] = []
    new_runtime = SimpleNamespace(
        submit_control_command=submitted_to_new.append,
    )
    stable_runtime = SwappableAssistantProcessClient(cast(Any, old_runtime))
    control_queue: Queue[AssistantControlCommand] = Queue()
    control_queue.put(_self_reflection_commit_decision(runtime_epoch=1))
    stable_runtime.replace(cast(Any, new_runtime))
    bridge = AssistantProcessBridge(
        assistant_runtime=stable_runtime,
        assistant_request_queue=Queue[AssistantRequest](),
        assistant_control_queue=control_queue,
        event_queue=Queue[RuntimeEvent](),
        audio_output_scheduler=cast(Any, SimpleNamespace()),
    )

    assert bridge.bridge_assistant_control_commands_once() is True
    assert submitted_to_new == []


def test_bridge_failure_emits_internal_activity_lifecycle_event() -> None:
    """Bridge 异常应进入主进程事件队列，而不是只留在日志中。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(
            Any,
            SwappableAssistantProcessClient(cast(Any, CrashedAssistantRuntime())),
        ),
        assistant_request_queue=Queue[AssistantRequest](),
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(Any, object()),
    )

    bridge._run_thread()

    lifecycle_event = event_queue.get_nowait()
    assert lifecycle_event.type == "assistant.runtime_unavailable"
    assert lifecycle_event.source == "assistant_process"
    assert lifecycle_event.payload == {"reason": "bridge_failed"}
    assert lifecycle_event.runtime_epoch == 1


def test_failed_bridge_rejects_new_requests_without_leaving_ghost_queue_items() -> None:
    """Bridge 致命失败后必须先关闭提交入口，再补偿旧请求。"""

    request_queue: Queue[AssistantRequest] = Queue()
    request_queue.put(
        _assistant_request(
            "text-input-before-failure",
            turn_material_context_id="context-before-failure",
        )
    )
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(
            Any,
            SwappableAssistantProcessClient(cast(Any, CrashedAssistantRuntime())),
        ),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(Any, object()),
    )

    bridge._run_thread()

    request_failure = event_queue.get_nowait()
    assert bridge.health_state == "failed"
    assert request_failure.type == "assistant.generation_interrupted"
    assert request_failure.payload["reason"] == "output_unavailable"
    assert bridge.submit_request(_assistant_request("text-input-after-failure")) is False
    assert request_queue.empty()
    with pytest.raises(RuntimeError, match="失败的 Bridge 实例不能重新启动"):
        bridge.start()


def test_duplicate_request_claim_fails_closed_without_overwriting_queued_request() -> None:
    """重复请求身份不得入队或覆盖原请求，并且必须关闭输出和补偿屏障。

    Returns:
        None: 通过 Bridge 状态、队列和补偿事件验证永久 claim 语义。
    """

    request_queue: Queue[AssistantRequest] = Queue()
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(
            Any,
            SwappableAssistantProcessClient(cast(Any, CrashedAssistantRuntime())),
        ),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(Any, object()),
        request_identity_registry=AssistantRequestIdentityRegistry(),
    )
    bridge._health_state = "running"
    bridge._thread = cast(Any, SimpleNamespace(is_alive=lambda: True))

    original_request = _assistant_request("duplicate-request")
    duplicate_request = _assistant_request("duplicate-request")

    assert bridge.submit_request(original_request) is True
    assert bridge.submit_request(duplicate_request) is False

    assert bridge.health_state == "failed"
    assert request_queue.empty()
    emitted_events = []
    while not event_queue.empty():
        emitted_events.append(event_queue.get_nowait())
    assert [event.type for event in emitted_events] == [
        "assistant.generation_interrupted",
        "assistant.output_settled",
        "assistant.output_status_changed",
    ]
    assert emitted_events[0].payload["reason"] == "request_identity_conflict"
    assert emitted_events[-1].payload == {
        "available": False,
        "code": "request_identity_conflict",
        "message": "助手输出身份发生冲突，已暂停继续发送。",
    }


def test_bridge_runtime_identity_conflict_is_not_reported_as_output_unavailable() -> None:
    """回流事件身份冲突必须永久关闭 Bridge 并使用专门失败码。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    runtime = EmitsIdentityConflictRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, runtime))
    identity_registry = AssistantRequestIdentityRegistry()
    request = _assistant_request("identity-conflict-request")
    identity_registry.claim(request)
    identity_registry.bind_runtime_epoch(request.request_id, 1)
    identity_registry.bind_generation(request.request_id, "generation-original")
    bridge = AssistantProcessBridge(
        assistant_runtime=stable_runtime,
        assistant_request_queue=Queue[AssistantRequest](),
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(Any, object()),
        request_identity_registry=identity_registry,
    )
    bridge._health_state = "running"
    bridge._thread = cast(Any, SimpleNamespace(is_alive=lambda: True))
    bridge.request_ownership_ledger.claim(request, bridge_id=bridge.bridge_id)
    bridge.request_ownership_ledger.mark_submitted(
        request.request_id,
        bridge_id=bridge.bridge_id,
    )
    bridge.request_ownership_ledger.bind_runtime_epoch(
        request.request_id,
        1,
        bridge_id=bridge.bridge_id,
    )

    bridge._run_thread()

    assert bridge.health_state == "failed"
    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    assert [event.type for event in events] == [
        "assistant.generation_interrupted",
        "assistant.output_settled",
        "assistant.output_status_changed",
    ]
    assert events[0].payload["reason"] == "request_identity_conflict"
    assert events[-1].payload["code"] == "request_identity_conflict"


def test_bridge_recovers_when_old_runtime_operation_fails_after_replacement() -> None:
    """旧世代关闭异常不能杀死服务新世代的唯一 bridge 线程。"""

    old_runtime = ReplacedDuringClearRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, old_runtime))
    old_runtime.replace_current = lambda: stable_runtime.replace(
        cast(Any, CrashedAssistantRuntime())
    )
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, stable_runtime),
        assistant_request_queue=Queue[AssistantRequest](),
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(Any, object()),
    )

    bridge._run_thread()

    lifecycle_event = event_queue.get_nowait()
    assert lifecycle_event.runtime_epoch == 2
    assert isinstance(bridge.last_error, BaseException)
    status_event = event_queue.get_nowait()
    assert status_event.type == "assistant.output_status_changed"
    assert status_event.payload["code"] == "output_unavailable"
    assert event_queue.empty()


def test_old_runtime_error_only_compensates_requests_owned_by_that_epoch() -> None:
    """旧世代迟到异常不得中断已经由健康新世代接管的请求。"""

    old_runtime = RaisesAfterReplacementRuntime()
    new_runtime = StopsBridgeOnClearRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, old_runtime))
    request_queue: Queue[AssistantRequest] = Queue()
    request_queue.put(_assistant_request("old-epoch-request"))
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, stable_runtime),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(clear_pending_audio=lambda: None),
        ),
    )

    def replace_and_submit_new_request() -> None:
        """构造旧操作返回前，新世代已经接管请求的并发窗口。"""

        stable_runtime.replace(cast(Any, new_runtime))
        request_queue.put(_assistant_request("new-epoch-request"))
        assert bridge.bridge_assistant_requests_once() is True

    old_runtime.replace_current = replace_and_submit_new_request
    new_runtime.stop_bridge = bridge._stop_event.set

    bridge._run_thread()

    request_failure = event_queue.get_nowait()
    assert old_runtime.submitted_request_ids == ["old-epoch-request"]
    assert new_runtime.submitted_request_ids == ["new-epoch-request"]
    assert request_failure.type == "assistant.generation_interrupted"
    assert request_failure.payload["request_id"] == "old-epoch-request"
    assert request_failure.runtime_epoch == 1
    output_settlement = event_queue.get_nowait()
    assert output_settlement.type == "assistant.output_settled"
    assert output_settlement.payload["request_id"] == "old-epoch-request"
    assert output_settlement.payload["outcome"] == "output_unavailable"
    assert event_queue.empty()


def test_bridge_compensates_old_request_after_normal_runtime_replacement() -> None:
    """健康热替换正常唤醒时也必须收口旧世代请求。"""

    old_runtime = ReplacedWhileWaitingRuntime()
    new_runtime = StopsBridgeOnClearRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, old_runtime))
    request_queue: Queue[AssistantRequest] = Queue()
    request_queue.put(
        _assistant_request(
            "text-input-1",
            turn_material_context_id="context-a",
        )
    )
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, stable_runtime),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(clear_pending_audio=lambda: None),
        ),
    )
    old_runtime.replace_current = lambda: stable_runtime.replace(
        cast(Any, new_runtime)
    )
    new_runtime.stop_bridge = bridge._stop_event.set

    bridge._run_thread()

    request_failure = event_queue.get_nowait()
    assert old_runtime.submitted_request_ids == ["text-input-1"]
    assert request_failure.type == "assistant.generation_interrupted"
    assert request_failure.payload == {
        "request_id": "text-input-1",
        "source": "text_input",
        "source_turn_id": None,
        "turn_material_context_id": "context-a",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
        "derived_from_activity_ids": [],
        "reason": "runtime_unavailable",
    }
    assert request_failure.runtime_epoch == 1
    output_settlement = event_queue.get_nowait()
    assert output_settlement.type == "assistant.output_settled"
    assert output_settlement.payload["outcome"] == "output_unavailable"
    assert event_queue.empty()


def test_bridge_failure_emits_identity_bound_failure_for_submitted_request() -> None:
    """Bridge 整体退出时必须先为已提交请求生成精确补偿事件。"""

    runtime = CrashesAfterSubmittingRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, runtime))
    request_queue: Queue[AssistantRequest] = Queue()
    request_queue.put(
        _assistant_request(
            "text-input-1",
            turn_material_context_id="context-a",
        )
    )
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, stable_runtime),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(clear_pending_audio=lambda: None),
        ),
    )

    bridge._run_thread()

    request_failure = event_queue.get_nowait()
    output_settlement = event_queue.get_nowait()
    lifecycle_failure = event_queue.get_nowait()
    assert runtime.submitted_request_ids == ["text-input-1"]
    assert request_failure.type == "assistant.generation_interrupted"
    assert request_failure.payload == {
        "request_id": "text-input-1",
        "source": "text_input",
        "source_turn_id": None,
        "turn_material_context_id": "context-a",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
        "derived_from_activity_ids": [],
        "reason": "output_unavailable",
    }
    assert request_failure.runtime_epoch == 1
    assert output_settlement.type == "assistant.output_settled"
    assert output_settlement.payload["outcome"] == "output_unavailable"
    assert lifecycle_failure.type == "assistant.runtime_unavailable"


def test_bridge_failure_after_dequeue_preserves_owned_request_identity() -> None:
    """请求出队后的音频清理异常仍须按原身份完成失败补偿。"""

    runtime = CrashesAfterSubmittingRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, runtime))
    request_queue: Queue[AssistantRequest] = Queue()
    request_queue.put(
        _assistant_request(
            "text-input-1",
            turn_material_context_id="context-a",
        )
    )
    event_queue: Queue[RuntimeEvent] = Queue()

    def fail_audio_clear() -> None:
        """模拟请求出队后清理旧音频失败。"""

        raise RuntimeError("audio scheduler failed")

    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, stable_runtime),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(clear_pending_audio=fail_audio_clear),
        ),
    )

    bridge._run_thread()

    request_failure = event_queue.get_nowait()
    output_settlement = event_queue.get_nowait()
    lifecycle_failure = event_queue.get_nowait()
    assert runtime.submitted_request_ids == []
    assert request_failure.type == "assistant.generation_interrupted"
    assert request_failure.payload == {
        "request_id": "text-input-1",
        "source": "text_input",
        "source_turn_id": None,
        "turn_material_context_id": "context-a",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
        "derived_from_activity_ids": [],
        "reason": "output_unavailable",
    }
    assert output_settlement.type == "assistant.output_settled"
    assert output_settlement.payload["outcome"] == "output_unavailable"
    assert lifecycle_failure.type == "assistant.runtime_unavailable"
    assert event_queue.get_nowait().type == "assistant.output_status_changed"
    assert event_queue.empty()


def test_bridge_failure_marks_request_whose_visible_text_already_completed() -> None:
    """Bridge 应保留文字已完成阶段，供 Router 避免追加误导性失败提示。"""

    runtime = CrashesAfterSubmittingRuntime(emit_text_final=True)
    stable_runtime = SwappableAssistantProcessClient(cast(Any, runtime))
    request_queue: Queue[AssistantRequest] = Queue()
    request_queue.put(_assistant_request("text-input-1"))
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, stable_runtime),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(clear_pending_audio=lambda: None),
        ),
    )

    bridge._run_thread()

    assert event_queue.get_nowait().type == "assistant.text_final"
    request_failure = event_queue.get_nowait()
    assert request_failure.type == "assistant.generation_interrupted"
    assert request_failure.payload["visible_response_completed"] is True
    assert event_queue.get_nowait().type == "assistant.output_settled"


def test_audio_handoff_failure_preserves_owned_request_for_compensation() -> None:
    """音频完成事件交接失败前不得释放 Bridge 请求所有权。"""

    runtime = CompletesAudioImmediatelyRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, runtime))
    request_queue: Queue[AssistantRequest] = Queue()
    request_queue.put(
        _assistant_request(
            "text-input-1",
            turn_material_context_id="context-a",
        )
    )
    event_queue: Queue[RuntimeEvent] = Queue()

    def fail_audio_handoff(_event: RuntimeEvent) -> None:
        """模拟音频调度器在接管完成事件前停止。"""

        raise RuntimeError("audio scheduler stopped")

    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, stable_runtime),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(
                clear_pending_audio=lambda: None,
                enqueue_audio_event=fail_audio_handoff,
            ),
        ),
    )

    bridge._run_thread()

    assert event_queue.get_nowait().type == "assistant.text_final"
    request_failure = event_queue.get_nowait()
    output_settlement = event_queue.get_nowait()
    lifecycle_failure = event_queue.get_nowait()
    assert runtime.submitted_request_ids == ["text-input-1"]
    assert request_failure.type == "assistant.generation_interrupted"
    assert request_failure.payload == {
        "request_id": "text-input-1",
        "source": "text_input",
        "source_turn_id": None,
        "turn_material_context_id": "context-a",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
        "derived_from_activity_ids": [],
        "reason": "output_unavailable",
        "visible_response_completed": True,
    }
    assert request_failure.runtime_epoch == 1
    assert output_settlement.type == "assistant.output_settled"
    assert output_settlement.payload["outcome"] == "output_unavailable"
    assert lifecycle_failure.type == "assistant.runtime_unavailable"
    assert event_queue.get_nowait().type == "assistant.output_status_changed"
    assert event_queue.empty()


def test_bridge_keeps_interrupted_request_until_output_settlement_arrives() -> None:
    """取消事件早于中断 final 时，Bridge 不能提前丢掉请求补偿身份。"""

    runtime = ControlledOutputLifecycleRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, runtime))
    request_queue: Queue[AssistantRequest] = Queue()
    request_queue.put(_assistant_request("text-input-1"))
    event_queue: Queue[RuntimeEvent] = Queue()
    bridge = AssistantProcessBridge(
        assistant_runtime=cast(Any, stable_runtime),
        assistant_request_queue=request_queue,
        assistant_control_queue=Queue[AssistantControlCommand](),
        event_queue=event_queue,
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(
                clear_pending_audio=lambda: None,
                clear_pending_audio_for_request=lambda _request_id: True,
            ),
        ),
    )
    assert bridge.bridge_assistant_requests_once() is True

    runtime.events = [
        RuntimeEvent(
            type="assistant.generation_interrupted",
            source="assistant_runtime",
            payload={
                "request_id": "text-input-1",
                "generation_id": "generation-a",
                "reason": "user_barge_in",
            },
        )
    ]
    assert bridge.drain_assistant_runtime_once() is True
    assert any(
        record.request.request_id == "text-input-1"
        for record in bridge.request_ownership_ledger.snapshot()
    )

    runtime.events = [
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            payload={
                "request_id": "text-input-1",
                "generation_id": "generation-a",
                "outcome": "interrupted",
            },
        )
    ]
    assert bridge.drain_assistant_runtime_once() is True
    assert all(
        record.request.request_id != "text-input-1"
        for record in bridge.request_ownership_ledger.snapshot()
    )


@pytest.mark.asyncio
async def test_runtime_unavailable_event_only_interrupts_activity_projection() -> None:
    """生命周期事件只触发 Activity 中断，不得公开原始内部事件。"""

    public_broadcasts: list[dict[str, object]] = []
    ephemeral_broadcasts: list[dict[str, object]] = []
    interruptions: list[tuple[int, str]] = []
    notice_interruptions: list[tuple[int, str]] = []

    async def broadcast(event: dict[str, object]) -> None:
        public_broadcasts.append(event)

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        ephemeral_broadcasts.append(event)

    async def interrupt(runtime_epoch: int, failure_code: str) -> None:
        interruptions.append((runtime_epoch, failure_code))

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast_ephemeral,
        realtime_input_runtime=cast(Any, object()),
        current_runtime_epoch=lambda: 1,
        activity_interruption_handler=interrupt,
        activity_notice_interruption_handler=lambda runtime_epoch, strategy: notice_interruptions.append(
            (runtime_epoch, strategy)
        ),
    )

    await event_router.dispatch(
        RuntimeEvent(
            type="assistant.runtime_unavailable",
            source="assistant_process",
            payload={"reason": "bridge_failed"},
            runtime_epoch=1,
        )
    )

    assert interruptions == [(1, "runtime_interrupted")]
    assert notice_interruptions == [(1, "notify")]
    assert public_broadcasts == []
    assert ephemeral_broadcasts == []


@pytest.mark.asyncio
async def test_runtime_unavailable_passes_notify_strategy_through_livekit_media_session() -> None:
    """生产 Agent 端口必须把意外中断策略完整传给提醒协调器。"""

    notice_interruptions: list[tuple[int, str]] = []
    agent = object.__new__(LiveKitMediaSession)
    agent.activity_notice_interruption_handler = (
        lambda runtime_epoch, strategy: notice_interruptions.append(
            (runtime_epoch, strategy)
        )
    )

    async def broadcast(_event: dict[str, object]) -> None:
        """测试不关心公共广播内容。"""

    async def interrupt(_runtime_epoch: int, _failure_code: str) -> None:
        """满足 Activity 中断端口。"""

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=cast(Any, object()),
        current_runtime_epoch=lambda: 1,
        activity_interruption_handler=interrupt,
        activity_notice_interruption_handler=agent.interrupt_activity_notices,
    )

    await event_router.dispatch(
        RuntimeEvent(
            type="assistant.runtime_unavailable",
            source="assistant_process",
            payload={"reason": "bridge_failed"},
            runtime_epoch=1,
        )
    )

    assert notice_interruptions == [(1, "notify")]


@pytest.mark.asyncio
async def test_old_runtime_failure_cannot_interrupt_current_runtime() -> None:
    """旧 bridge 已排队的崩溃事件不得中断新运行时活动。"""

    interruptions: list[int] = []

    async def interrupt(runtime_epoch: int, _error_text: str) -> None:
        interruptions.append(runtime_epoch)

    event_router = runtime_event_router(
        publish=cast(Any, _noop_async),
        publish_ephemeral=cast(Any, _noop_async),
        realtime_input_runtime=cast(Any, object()),
        current_runtime_epoch=lambda: 2,
        activity_interruption_handler=interrupt,
    )

    await event_router.dispatch(
        RuntimeEvent(
            type="assistant.runtime_unavailable",
            source="assistant_process",
            payload={"reason": "bridge_failed"},
            runtime_epoch=1,
        )
    )

    assert interruptions == []


@pytest.mark.asyncio
async def test_old_runtime_submit_failure_still_runs_identity_safe_compensation() -> None:
    """旧世代过滤不得吞掉指定请求的 Notice、材料和聊天失败闭环。"""

    public_broadcasts: list[dict[str, object]] = []
    finished_contexts: list[str] = []
    notice_commands: list[NoticeDeliveryCommand] = []
    output_finish_calls: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        public_broadcasts.append(event)

    def finish_materials(context_id: str) -> list[dict[str, object]]:
        finished_contexts.append(context_id)
        return []

    def finish_output(**kwargs: object) -> list[dict[str, object]]:
        output_finish_calls.append(kwargs)
        # 模拟当前状态已经属于新请求，因此旧身份不能产生 idle 事件。
        return []

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=cast(Any, _noop_async),
        realtime_input_runtime=cast(
            Any,
            SimpleNamespace(apply_assistant_output_finished=finish_output),
        ),
        current_runtime_epoch=lambda: 2,
        notice_delivery_handler=notice_commands.append,
        turn_material_final_handler=finish_materials,
    )
    payload = {
        "request_id": "text-input-1",
        "source": "text_input",
        "source_turn_id": None,
        "turn_material_context_id": "context-a",
        "reason": "runtime_submit_failed",
    }

    await event_router.dispatch(
        RuntimeEvent(
            type="assistant.generation_interrupted",
            source="assistant_process",
            payload=payload,
            runtime_epoch=1,
        )
    )

    assert finished_contexts == ["context-a"]
    assert notice_commands == [
        NoticeDeliveryCommand(
            request_id="text-input-1",
            outcome="delivery_cancelled",
        )
    ]
    assert output_finish_calls == [{
        "reason": "assistant_request_start_failed",
        "now": pytest.approx(output_finish_calls[0]["now"]),
        "request_id": "text-input-1",
    }]
    assert public_broadcasts == [{
        "type": "assistant.request_failed",
        "payload": {
                "request_id": "text-input-1",
                "code": "request_start_failed",
                "message": "消息已经发送，但我现在没能开始回复，请稍后再试。",
        },
    }]


@pytest.mark.asyncio
async def test_runtime_failure_after_visible_final_only_restores_request_state() -> None:
    """完整文字已经交付后只收口状态，不再追加误导性的启动失败提示。"""

    public_broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        public_broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=cast(Any, _noop_async),
        realtime_input_runtime=cast(
            Any,
            SimpleNamespace(
                apply_assistant_output_finished=lambda **_kwargs: [
                    {"type": "turn.state_changed", "payload": {"turn_state": "idle"}}
                ]
            ),
        ),
    )

    await event_router.dispatch(
        RuntimeEvent(
            type="assistant.generation_interrupted",
            source="assistant_process",
            payload={
                "request_id": "text-input-1",
                "source": "text_input",
                "source_turn_id": None,
                "turn_material_context_id": None,
                "reason": "runtime_unavailable",
                "visible_response_completed": True,
            },
            runtime_epoch=1,
        )
    )

    assert public_broadcasts == [
        {"type": "turn.state_changed", "payload": {"turn_state": "idle"}}
    ]


async def _noop_async(_event: dict[str, object]) -> None:
    """提供不产生副作用的异步广播测试端口。"""
