from __future__ import annotations

from threading import Event, Thread

from companion.foreground_tools.task_memory.broker import TaskMemoryLookupBroker
from companion.realtime.assistant_events import TaskMemoryLookupResultCommand


def test_lookup_waits_for_matching_authoritative_result() -> None:
    """召回只能被编号、任务和用途都匹配的主进程回执唤醒。"""

    broker = TaskMemoryLookupBroker(
        lookup_id_factory=lambda: "lookup-a",
        timeout_seconds=1.0,
    )
    result_holder: list[dict[str, object]] = []
    worker = Thread(
        target=lambda: result_holder.append(
            broker.lookup_and_wait(
                activity_id="activity-a",
                purpose="foreground_recall",
                cancellation_event=None,
            )
        )
    )
    worker.start()
    request = broker.drain_events()[0]

    assert request.payload == {
        "lookup_id": "lookup-a",
        "activity_id": "activity-a",
        "purpose": "foreground_recall",
    }
    assert broker.apply_result(
        TaskMemoryLookupResultCommand(
            lookup_id="lookup-a",
            activity_id="activity-a",
            purpose="foreground_recall",
            result={"state": "completed", "activity_id": "activity-a"},
        )
    ) is True

    worker.join(timeout=1.0)
    assert worker.is_alive() is False
    assert result_holder == [{"state": "completed", "activity_id": "activity-a"}]


def test_cancelled_lookup_rejects_late_result() -> None:
    """generation 取消后，迟到回执不得恢复已经结束的召回。"""

    cancellation = Event()
    cancellation.set()
    broker = TaskMemoryLookupBroker(
        lookup_id_factory=lambda: "lookup-cancelled",
        timeout_seconds=1.0,
    )

    result = broker.lookup_and_wait(
        activity_id="activity-a",
        purpose="task_derivation",
        cancellation_event=cancellation,
    )

    assert result["reason"] == "lookup_timeout"
    assert broker.apply_result(
        TaskMemoryLookupResultCommand(
            lookup_id="lookup-cancelled",
            activity_id="activity-a",
            purpose="task_derivation",
            result={"state": "completed"},
        )
    ) is False


def test_lookup_times_out_without_main_process_receipt() -> None:
    """主进程没有在时限内确认时，工具必须返回稳定失败而不是永久阻塞。"""

    broker = TaskMemoryLookupBroker(
        lookup_id_factory=lambda: "lookup-timeout",
        timeout_seconds=0.01,
    )

    result = broker.lookup_and_wait(
        activity_id="activity-a",
        purpose="foreground_recall",
        cancellation_event=None,
    )

    assert result == {
        "state": "failed",
        "activity_id": "activity-a",
        "reason": "lookup_timeout",
        "safe_message": "这次没有及时读到任务详情，请稍后再试。",
    }
