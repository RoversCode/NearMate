"""验证 Self-Reflection 工具提交 Broker 的等待、重试和迟到回执边界。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

from companion.foreground_self_reflection.contracts import (
    SelfReflectionToolCommitDecisionCommand,
)
from companion.foreground_self_reflection.tool_commit_broker import (
    SelfReflectionToolCommitBroker,
)
from companion.realtime.assistant_events import RuntimeEvent


def _canonical_event() -> RuntimeEvent:
    """构造等待主进程登记的后台工具 canonical 事实。

    Returns:
        身份完整且可安全跨进程传递的工具完成事件。
    """

    return RuntimeEvent(
        type="llm.foreground.tool_call_completed",
        source="assistant_runtime",
        runtime_epoch=7,
        produced_at=10.0,
        payload={
            "self_reflection_id": "self-reflection-1",
            "recovery_of_activity_id": "cognitive-1",
            "tool_call_id": "tool-call-1",
            "name": "start_background_work",
            "activity_id": "background-1",
            "state": "accepted",
            "request_id": "foreground-request-1",
            "protocol_transaction_id": "self-reflection-self-reflection-1",
            "interaction_session_id": "interaction-1",
            "result": {
                "state": "accepted",
                "activity_id": "background-1",
            },
        },
    )


def _decision_command(*, approved: bool, reason: str) -> SelfReflectionToolCommitDecisionCommand:
    """构造与测试等待项完全匹配的主进程回执。

    Args:
        approved: 主进程是否已经完成授权和必要的 canonical 登记。
        reason: 本次批准或拒绝的稳定原因。

    Returns:
        可以交给 Broker 应用的控制命令。
    """

    return SelfReflectionToolCommitDecisionCommand(
        commit_request_id="commit-request-1",
        self_reflection_id="self-reflection-1",
        source_activity_id="cognitive-1",
        runtime_epoch=7,
        tool_call_id="tool-call-1",
        tool_name="start_background_work",
        approved=approved,
        reason=reason,
        issued_at=11.0,
    )


def test_cancelled_inline_wait_rejects_late_approval() -> None:
    """Inline 等待已被热替换取消后，迟到批准不能重新启动旧工具。"""

    emitted: list[RuntimeEvent] = []
    request_emitted = Event()
    cancellation = Event()

    def emit_event(event: RuntimeEvent) -> None:
        """保存请求事件并唤醒测试线程。"""

        emitted.append(event)
        request_emitted.set()

    broker = SelfReflectionToolCommitBroker(
        emit_event=emit_event,
        response_timeout_seconds=1.0,
        retry_interval_seconds=0.02,
        commit_request_id_factory=lambda: "commit-request-1",
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            broker.request_and_wait,
            self_reflection_id="self-reflection-1",
            source_activity_id="cognitive-1",
            runtime_epoch=7,
            tool_call_id="tool-call-1",
            tool_name="start_background_work",
            execution_channel="inline",
            canonical_event=None,
            deadline=monotonic() + 1.0,
            cancellation_event=cancellation,
        )
        assert request_emitted.wait(timeout=1.0)
        cancellation.set()
        decision = future.result(timeout=1.0)

    assert decision.approved is False
    assert decision.reason == "tool_commit_cancelled"
    assert emitted
    assert broker.apply_decision(
        _decision_command(approved=True, reason="tool_commit_approved")
    ) is False


def test_retry_reuses_identity_and_duplicate_approval_is_ignored() -> None:
    """主进程暂时不回执时重复同一请求，批准只能唤醒一次。"""

    emitted: list[RuntimeEvent] = []
    second_request_emitted = Event()

    def emit_event(event: RuntimeEvent) -> None:
        """保存每次重试，并在第二次请求出现时唤醒测试。"""

        emitted.append(event)
        if len(emitted) >= 2:
            second_request_emitted.set()

    broker = SelfReflectionToolCommitBroker(
        emit_event=emit_event,
        response_timeout_seconds=1.0,
        retry_interval_seconds=0.02,
        commit_request_id_factory=lambda: "commit-request-1",
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            broker.request_and_wait,
            self_reflection_id="self-reflection-1",
            source_activity_id="cognitive-1",
            runtime_epoch=7,
            tool_call_id="tool-call-1",
            tool_name="start_background_work",
            execution_channel="background",
            canonical_event=_canonical_event(),
            deadline=monotonic() + 1.0,
            cancellation_event=Event(),
        )
        assert second_request_emitted.wait(timeout=1.0)
        command = _decision_command(
            approved=True,
            reason="tool_commit_approved",
        )
        assert broker.apply_decision(command) is True
        assert broker.apply_decision(command) is False
        decision = future.result(timeout=1.0)

    assert decision.approved is True
    assert decision.reason == "tool_commit_approved"
    assert {
        event.payload["commit_request_id"]
        for event in emitted
    } == {"commit-request-1"}
    assert {
        event.payload["canonical_event_payload"]["activity_id"]
        for event in emitted
    } == {"background-1"}


def test_forwarded_background_wait_is_rejected_by_runtime_shutdown() -> None:
    """跨进程转发后的等待由主进程裁决，但 Runtime 关闭必须让 worker 有界退出。"""

    locally_queued: list[RuntimeEvent] = []
    request_queued = Event()
    cancellation = Event()

    def emit_event(event: RuntimeEvent) -> None:
        """模拟先进入子进程本地 FIFO 的提交请求。"""

        locally_queued.append(event)
        request_queued.set()

    broker = SelfReflectionToolCommitBroker(
        emit_event=emit_event,
        response_timeout_seconds=1.0,
        retry_interval_seconds=0.02,
        commit_request_id_factory=lambda: "commit-request-1",
    )
    executor = ThreadPoolExecutor(max_workers=1)
    future = None
    forwarded: list[RuntimeEvent] = []
    try:
        future = executor.submit(
            broker.request_and_wait,
            self_reflection_id="self-reflection-1",
            source_activity_id="cognitive-1",
            runtime_epoch=7,
            tool_call_id="tool-call-1",
            tool_name="start_background_work",
            execution_channel="background",
            canonical_event=_canonical_event(),
            deadline=monotonic() + 1.0,
            cancellation_event=cancellation,
        )
        assert request_queued.wait(timeout=1.0)
        assert broker.forward_request_to_main(
            locally_queued[0],
            send_event=forwarded.append,
        ) is True
        cancellation.set()
        assert future.done() is False

        broker.shutdown(reason="runtime_shutdown")
        decision = future.result(timeout=1.0)
    finally:
        if future is not None and not future.done():
            broker.apply_decision(
                _decision_command(approved=False, reason="test_cleanup")
            )
        executor.shutdown(wait=True, cancel_futures=True)

    assert forwarded == [locally_queued[0]]
    assert decision.approved is False
    assert decision.reason == "runtime_shutdown"
    assert broker.apply_decision(
        _decision_command(approved=True, reason="tool_commit_approved")
    ) is False


def test_local_queue_notification_failure_never_transfers_commit_ownership() -> None:
    """本地入队后的唤醒失败不能伪装成主进程已经接管。"""

    locally_queued: list[RuntimeEvent] = []

    def enqueue_then_fail(event: RuntimeEvent) -> None:
        """模拟事件留在本地 FIFO，但主循环没有被成功唤醒。"""

        locally_queued.append(event)
        raise RuntimeError("local_runtime_event_notifier_failed")

    broker = SelfReflectionToolCommitBroker(
        emit_event=enqueue_then_fail,
        response_timeout_seconds=0.05,
        retry_interval_seconds=0.01,
        commit_request_id_factory=lambda: "commit-request-1",
    )

    decision = broker.request_and_wait(
        self_reflection_id="self-reflection-1",
        source_activity_id="cognitive-1",
        runtime_epoch=7,
        tool_call_id="tool-call-1",
        tool_name="start_background_work",
        execution_channel="background",
        canonical_event=_canonical_event(),
        deadline=monotonic() + 1.0,
        cancellation_event=Event(),
    )

    assert decision.approved is False
    assert decision.reason == "tool_commit_authorization_timeout"
    assert 1 <= len(locally_queued) <= 10
    forwarded: list[RuntimeEvent] = []
    assert all(
        broker.forward_request_to_main(event, send_event=forwarded.append) is False
        for event in locally_queued
    )
    assert forwarded == []
