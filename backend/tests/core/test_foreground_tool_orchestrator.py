from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from companion.foreground_tools.orchestration import (
    BackgroundActivityPrepared,
    DeferredActionPrepared,
    ForegroundToolCallOrchestrator,
    InlineToolCompleted,
    ToolFailed,
    ToolRejected,
    ToolReplyRequirement,
)
from companion.foreground_tools.inline_execution import InlineExecutionContext
from companion.llm.foreground_models import ForegroundToolCall


def test_task_result_channel_accepts_only_first_call() -> None:
    """同一轮多个 TaskResult 调用只能真正执行第一个。"""

    orchestrator = ForegroundToolCallOrchestrator()
    executed: list[str] = []
    try:
        execution = orchestrator.prepare(
            tool_calls=[
                _call("task_result-a", "task_result"),
                _call("task_result-b", "task_result"),
            ],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda name: name == "task_result",
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda call: _record_task_result(call, executed),
            execute_inline_call=lambda _call, _context: {},
        ).collect()
    finally:
        orchestrator.shutdown()

    assert executed == ["task_result-a"]
    assert [call.state for call in execution.calls] == ["accepted", "ignored_duplicate"]
    assert isinstance(execution.calls[1].outcome, ToolRejected)
    assert execution.calls[1].outcome.reason == "duplicate_tool_call"
    assert "instruction" not in execution.calls[1].result_payload()
    assert "safe_message" not in execution.calls[1].result_payload()
    assert execution.calls[1].reply_requirement == ToolReplyRequirement.SILENT
    assert execution.calls[0].reply_requirement == ToolReplyRequirement.ACKNOWLEDGE


@pytest.mark.parametrize(
    "result",
    [
        {"state": "error", "safe_message": "即时查询没有完成。"},
        {"ok": False, "text": "即时查询没有返回结果。"},
        {},
    ],
)
def test_inline_unknown_or_missing_state_requires_failure_explanation(
    result: dict[str, object],
) -> None:
    """Inline 只有显式 completed 才能作为成功结果展示。"""

    orchestrator = ForegroundToolCallOrchestrator()
    try:
        execution = orchestrator.prepare(
            tool_calls=[_call("inline-a", "inline_tool")],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=lambda _call, _context: result,
        ).collect()
    finally:
        orchestrator.shutdown()

    assert execution.calls[0].reply_requirement == ToolReplyRequirement.EXPLAIN


def test_cognitive_channel_receives_all_calls_once() -> None:
    """同一轮多个认知意图必须一次性交给批次入口。"""

    orchestrator = ForegroundToolCallOrchestrator()
    batches: list[tuple[str, ...]] = []

    def execute_batch(calls: tuple[ForegroundToolCall, ...]) -> tuple[dict[str, object], ...]:
        """记录认知调用批次并返回同任务结果。

        Args:
            calls: 本轮全部认知调用。

        Returns:
            与调用数量一致的工具结果。
        """

        batches.append(tuple(call.tool_call_id for call in calls))
        return tuple(
            {"state": "accepted", "activity_id": "cognitive-a"}
            for _call_item in calls
        )

    try:
        execution = orchestrator.prepare(
            tool_calls=[
                _call("inspect-a", "lookup_current_fact"),
                _call("lookup-a", "lookup_current_fact"),
            ],
            is_cognitive_tool=lambda name: name in {
                "lookup_current_fact",
                "lookup_current_fact",
            },
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=execute_batch,
            execute_task_result_call=lambda _call: {},
            execute_inline_call=lambda _call, _context: {},
        ).collect()
    finally:
        orchestrator.shutdown()

    assert batches == [("inspect-a", "lookup-a")]
    assert all(isinstance(call.outcome, BackgroundActivityPrepared) for call in execution.calls)
    assert [call.activity_id for call in execution.calls] == [
        "cognitive-a",
        "cognitive-a",
    ]


def test_inline_channel_runs_concurrently_and_restores_original_order() -> None:
    """Inline 工具应同时运行，但结果仍按模型调用顺序返回。"""

    orchestrator = ForegroundToolCallOrchestrator(max_inline_workers=2)
    barrier = threading.Barrier(2, timeout=1.0)

    def execute_inline(
        call: ForegroundToolCall,
        _context: object,
    ) -> dict[str, object]:
        """等待两个工具都启动后返回各自结果。

        Args:
            call: 当前 Inline 工具调用。

        Returns:
            包含原始调用编号的结果。
        """

        barrier.wait()
        return {"state": "completed", "call_id": call.tool_call_id}

    try:
        execution = orchestrator.prepare(
            tool_calls=[
                _call("weather-a", "weather"),
                _call("air-a", "air_quality"),
            ],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=execute_inline,
        ).collect()
    finally:
        orchestrator.shutdown()

    assert all(isinstance(call.outcome, InlineToolCompleted) for call in execution.calls)
    assert [call.result_payload()["call_id"] for call in execution.calls] == [
        "weather-a",
        "air-a",
    ]


def test_inline_timeouts_share_their_original_launch_deadline() -> None:
    """并发 Inline 的超时预算必须从各自提交时开始，不能按收集顺序累加。"""

    orchestrator = ForegroundToolCallOrchestrator(max_inline_workers=2)
    release = threading.Event()

    def execute_inline(
        _call: ForegroundToolCall,
        _context: object,
    ) -> dict[str, object]:
        """阻塞到测试完成，确保两个调用都会触发编排层超时。"""

        release.wait(timeout=1.0)
        return {"state": "completed"}

    started_at = time.monotonic()
    try:
        execution = orchestrator.prepare(
            tool_calls=[
                _call("weather-a", "weather"),
                _call("air-a", "air_quality"),
            ],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=execute_inline,
            inline_timeout_resolver=lambda _name: 0.15,
        ).collect()
    finally:
        elapsed = time.monotonic() - started_at
        release.set()
        orchestrator.shutdown()

    assert elapsed < 0.24
    assert all(isinstance(call.outcome, ToolFailed) for call in execution.calls)
    assert [call.outcome.reason for call in execution.calls] == [
        "inline_tool_timeout",
        "inline_tool_timeout",
    ]
    assert [call.outcome.safe_message for call in execution.calls] == [
        "这项即时查询超时，请稍后再试。",
        "这项即时查询超时，请稍后再试。",
    ]


def test_inline_result_completed_before_deadline_survives_late_collection() -> None:
    """工具按时完成后，即使因原始顺序较晚收集，也不能被误判为超时。"""

    orchestrator = ForegroundToolCallOrchestrator(max_inline_workers=2)
    release_slow_call = threading.Event()

    def execute_inline(
        call: ForegroundToolCall,
        _context: object,
    ) -> dict[str, object]:
        """让第一个调用超时，第二个调用立即按时完成。"""

        if call.tool_call_id == "weather-a":
            release_slow_call.wait(timeout=1.0)
        return {"state": "completed", "call_id": call.tool_call_id}

    try:
        execution = orchestrator.prepare(
            tool_calls=[
                _call("weather-a", "weather"),
                _call("air-a", "air_quality"),
            ],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=execute_inline,
            inline_timeout_resolver=lambda _name: 0.1,
        ).collect()
    finally:
        release_slow_call.set()
        orchestrator.shutdown()

    assert isinstance(execution.calls[0].outcome, ToolFailed)
    assert execution.calls[0].outcome.reason == "inline_tool_timeout"
    assert execution.calls[0].outcome.safe_message == "这项即时查询超时，请稍后再试。"
    assert execution.calls[1].result_payload() == {
        "state": "completed",
        "call_id": "air-a",
    }


def test_three_channels_restore_order_and_isolate_inline_failure() -> None:
    """三通道混合时应保持协议顺序，并让单个 Inline 失败独立收口。"""

    orchestrator = ForegroundToolCallOrchestrator(max_inline_workers=2)

    def execute_inline(
        call: ForegroundToolCall,
        _context: object,
    ) -> dict[str, object]:
        """让第一个同步工具失败，第二个正常完成。"""

        if call.tool_call_id == "weather-a":
            raise RuntimeError("weather unavailable")
        return {"state": "completed", "call_id": call.tool_call_id}

    try:
        execution = orchestrator.prepare(
            tool_calls=[
                _call("weather-a", "weather"),
                _call("lookup-a", "lookup_current_fact"),
                _call("task_result-a", "task_result"),
                _call("air-a", "air_quality"),
            ],
            is_cognitive_tool=lambda name: name == "lookup_current_fact",
            is_task_result_tool=lambda name: name == "task_result",
            execute_cognitive_calls=lambda _calls: ({
                "state": "accepted",
                "activity_id": "cognitive-a",
            },),
            execute_task_result_call=lambda _call: {
                "state": "accepted",
                "activity_id": "task_result-a",
            },
            execute_inline_call=execute_inline,
        ).collect()
    finally:
        orchestrator.shutdown()

    assert [call.call.tool_call_id for call in execution.calls] == [
        "weather-a",
        "lookup-a",
        "task_result-a",
        "air-a",
    ]
    assert [call.state for call in execution.calls] == [
        "failed",
        "accepted",
        "accepted",
        "completed",
    ]
    assert [call.reply_requirement for call in execution.calls] == [
        ToolReplyRequirement.EXPLAIN,
        ToolReplyRequirement.ACKNOWLEDGE,
        ToolReplyRequirement.ACKNOWLEDGE,
        ToolReplyRequirement.PRESENT_RESULT,
    ]
    assert isinstance(execution.calls[0].outcome, ToolFailed)
    assert "weather unavailable" not in str(execution.calls[0].result_payload())
    assert execution.calls[0].outcome.safe_message == "这项即时查询失败。"


def test_deferred_answer_and_inline_tool_can_run_in_same_turn() -> None:
    """回答后台问题时仍可同时执行本轮其它即时工具。"""

    orchestrator = ForegroundToolCallOrchestrator()
    prepared_answers: list[str] = []
    try:
        execution = orchestrator.prepare(
            tool_calls=[
                _call("answer-a", "respond_to_work_interaction"),
                _call("weather-a", "weather"),
            ],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=lambda call, _context: {
                "state": "completed",
                "call_id": call.tool_call_id,
            },
            is_deferred_action_tool=lambda name: name == "respond_to_work_interaction",
            execute_deferred_action_call=lambda call: prepared_answers.append(
                call.tool_call_id
            )
            or {
                "state": "accepted",
                "request_id": "question-a",
                "activity_id": "activity-a",
            },
        ).collect()
    finally:
        orchestrator.shutdown()

    assert prepared_answers == ["answer-a"]
    assert [call.channel for call in execution.calls] == ["deferred", "inline"]
    assert isinstance(execution.calls[0].outcome, DeferredActionPrepared)
    assert execution.calls[0].result_payload()["request_id"] == "question-a"
    assert execution.calls[1].result_payload()["call_id"] == "weather-a"
    assert execution.calls[0].reply_requirement == ToolReplyRequirement.ACKNOWLEDGE
    assert execution.calls[1].reply_requirement == ToolReplyRequirement.PRESENT_RESULT


def test_all_cognitive_results_are_background_reservations() -> None:
    """纯 inspect 的 accepted 与 merged 结果都必须属于后台事务。"""

    orchestrator = ForegroundToolCallOrchestrator()
    try:
        execution = orchestrator.prepare(
            tool_calls=[
                _call("inspect-a", "lookup_current_fact"),
                _call("inspect-b", "lookup_current_fact"),
            ],
            is_cognitive_tool=lambda name: name == "lookup_current_fact",
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (
                {
                    "state": "accepted",
                    "activity_id": "cognitive-a",
                },
                {
                    "state": "merged",
                    "activity_id": "cognitive-a",
                },
            ),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=lambda _call, _context: {},
        ).collect()
    finally:
        orchestrator.shutdown()

    assert all(
        call.reply_requirement == ToolReplyRequirement.ACKNOWLEDGE
        for call in execution.calls
    )


def test_shutdown_does_not_wait_forever_for_running_inline_tool() -> None:
    """运行中 Inline 卡住时 shutdown 仍应立即让后续清理回调继续。"""

    orchestrator = ForegroundToolCallOrchestrator(max_inline_workers=1)
    release = threading.Event()
    started = threading.Event()

    def block() -> None:
        """占住测试线程池，直到断言完成后主动释放。"""

        started.set()
        release.wait(timeout=1.0)

    orchestrator._inline_executor.submit(block)  # noqa: SLF001
    assert started.wait(timeout=0.2) is True

    started_at = time.monotonic()
    orchestrator.shutdown(timeout=0.01)
    elapsed = time.monotonic() - started_at
    release.set()

    assert elapsed < 0.1


def test_cancel_inline_sets_local_context_for_running_worker() -> None:
    """Future 已经运行时，取消准备对象也必须让工具看见本地取消信号。"""

    orchestrator = ForegroundToolCallOrchestrator(max_inline_workers=1)
    started = threading.Event()
    release = threading.Event()

    def execute_inline(
        _call: ForegroundToolCall,
        context: InlineExecutionContext,
    ) -> dict[str, object]:
        """保持 Future 处于运行态，直到主线程验证共享执行门禁。"""

        started.set()
        release.wait(timeout=1.0)
        return {"state": "completed"}

    try:
        preparation = orchestrator.prepare(
            tool_calls=[_call("weather-a", "weather")],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=execute_inline,
        )
        assert started.wait(timeout=0.5) is True

        preparation.cancel_inline()

        execution_context = preparation.inline_execution_contexts[0]
        assert execution_context.cancellation_requested() is True
    finally:
        release.set()
        orchestrator.shutdown()


def test_precancelled_prepare_starts_no_tool_channel() -> None:
    """prepare 入口已经取消时，三个工具通道都不得产生副作用。"""

    orchestrator = ForegroundToolCallOrchestrator()
    cancellation = threading.Event()
    cancellation.set()
    calls: list[str] = []
    try:
        preparation = orchestrator.prepare(
            tool_calls=[
                _call("weather-a", "weather"),
                _call("lookup-a", "lookup_current_fact"),
                _call("task_result-a", "task_result"),
            ],
            is_cognitive_tool=lambda name: name == "lookup_current_fact",
            is_task_result_tool=lambda name: name == "task_result",
            execute_cognitive_calls=lambda _calls: calls.append("cognitive") or (),
            execute_task_result_call=lambda _call: calls.append("task_result") or {},
            execute_inline_call=lambda _call, _context: calls.append("inline") or {},
            cancellation_event=cancellation,
        )
    finally:
        orchestrator.shutdown()

    assert preparation.cancelled_before_commit is True
    assert preparation.prepared_calls == ()
    assert preparation.inline_futures == {}
    assert calls == []
    with pytest.raises(RuntimeError, match="已经取消"):
        preparation.collect()


def test_prepare_stops_submitting_after_cancellation_is_observed() -> None:
    """一个 Inline 提交后观察到取消时，不得再启动后续工具通道。"""

    cancellation = threading.Event()
    submitted: list[str] = []
    pending_future: Future[object] = Future()

    class CancellingExecutor:
        """第一次提交后立刻触发 generation 取消。"""

        def submit(
            self,
            _function: object,
            _execute: object,
            call: ForegroundToolCall,
            _context: object,
        ) -> Future[object]:
            """记录提交并返回尚未运行的 Future。"""

            submitted.append(call.tool_call_id)
            cancellation.set()
            return pending_future

        def shutdown(self, **_: object) -> None:
            """测试执行器没有自有线程，无需关闭。"""

    orchestrator = ForegroundToolCallOrchestrator()
    orchestrator._inline_executor.shutdown(wait=False, cancel_futures=True)  # noqa: SLF001
    orchestrator._inline_executor = CancellingExecutor()  # type: ignore[assignment]  # noqa: SLF001
    side_effects: list[str] = []
    preparation = orchestrator.prepare(
        tool_calls=[
            _call("weather-a", "weather"),
            _call("air-a", "air_quality"),
            _call("lookup-a", "lookup_current_fact"),
            _call("task_result-a", "task_result"),
        ],
        is_cognitive_tool=lambda name: name == "lookup_current_fact",
        is_task_result_tool=lambda name: name == "task_result",
        execute_cognitive_calls=lambda _calls: side_effects.append("cognitive") or (),
        execute_task_result_call=lambda _call: side_effects.append("task_result") or {},
        execute_inline_call=lambda _call, _context: side_effects.append("inline") or {},
        cancellation_event=cancellation,
    )

    assert preparation.cancelled_before_commit is True
    assert submitted == ["weather-a"]
    assert pending_future.cancelled() is True
    assert side_effects == []


def test_prepare_preserves_cognitive_reservation_when_cancelled_before_task_result() -> None:
    """认知 prepare 后取消时应保留其占位结果，让上层能够准确回滚。"""

    orchestrator = ForegroundToolCallOrchestrator()
    cancellation = threading.Event()
    task_result_calls: list[str] = []

    def prepare_cognitive(
        _calls: tuple[ForegroundToolCall, ...],
    ) -> tuple[dict[str, object], ...]:
        """准备认知占位后触发取消。"""

        cancellation.set()
        return ({"state": "accepted", "activity_id": "cognitive-a"},)

    try:
        preparation = orchestrator.prepare(
            tool_calls=[
                _call("lookup-a", "lookup_current_fact"),
                _call("task_result-a", "task_result"),
            ],
            is_cognitive_tool=lambda name: name == "lookup_current_fact",
            is_task_result_tool=lambda name: name == "task_result",
            execute_cognitive_calls=prepare_cognitive,
            execute_task_result_call=lambda call: task_result_calls.append(call.tool_call_id) or {},
            execute_inline_call=lambda _call, _context: {},
            cancellation_event=cancellation,
        )
    finally:
        orchestrator.shutdown()

    assert preparation.cancelled_before_commit is True
    assert [call.activity_id for call in preparation.prepared_calls] == [
        "cognitive-a"
    ]
    assert task_result_calls == []


def _call(tool_call_id: str, name: str) -> ForegroundToolCall:
    """构造测试工具调用。

    Args:
        tool_call_id: 工具调用编号。
        name: 工具名称。

    Returns:
        无参数工具调用。
    """

    return ForegroundToolCall(tool_call_id=tool_call_id, name=name, arguments={})


def _record_task_result(
    call: ForegroundToolCall,
    executed: list[str],
) -> dict[str, object]:
    """记录真实执行的 TaskResult 调用。

    Args:
        call: 当前 TaskResult 调用。
        executed: 调用编号记录列表。

    Returns:
        后台任务接受结果。
    """

    executed.append(call.tool_call_id)
    return {"state": "accepted", "activity_id": "task_result-task-a"}
