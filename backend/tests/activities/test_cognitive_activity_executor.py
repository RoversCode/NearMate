from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest

from companion.activities.foreground_snapshot import ForegroundActivitySnapshot
from companion.foreground_tools.cognitive.contracts import (
    CognitiveExecutionPlan,
    CognitiveGoal,
)
from companion.foreground_tools.cognitive.activity_executor import CognitiveActivityExecutor
from companion.foreground_tools.cognitive.result_resolver import CognitiveResultResolver
from companion.llm.foreground_models import LlmTextStreamChunk
from companion.llm.stream_lifecycle import iter_cancellable_stream
from companion.materials.foreground_images import VisualEvidence


class ImmediateExecutor:
    """在提交调用内同步执行认知任务。"""

    def submit(self, function: Any, *args: object) -> None:
        """立即调用给定任务函数。"""

        function(*args)


class HoldingExecutor:
    """捕获认知任务但不自动执行。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[object, ...]]] = []

    def submit(self, function: Any, *args: object) -> None:
        """保存待执行函数及参数。"""

        self.calls.append((function, args))


class RejectingExecutor:
    """模拟本地线程池已经关闭，无法接管任何新任务。"""

    def submit(self, _function: Any, *_args: object) -> None:
        """稳定拒绝提交，让测试区分本地启动失败和模型执行失败。"""

        raise RuntimeError("线程池已经关闭")


class BlockingBeforeSubmitExecutor:
    """在任务真正交给后台线程前暂停，稳定复现 starting 与 shutdown 竞态。"""

    def __init__(self) -> None:
        """初始化 submit 进入与继续执行两个同步信号。"""

        self.entered = threading.Event()
        self.release = threading.Event()

    def submit(self, _function: Any, *_args: object) -> None:
        """阻塞到测试允许返回，不实际执行认知任务。

        Args:
            _function: 本测试不执行的任务函数。
            *_args: 本测试不使用的任务参数。
        """

        self.entered.set()
        if not self.release.wait(timeout=1.0):
            raise RuntimeError("测试没有释放 Cognitive submit")


@dataclass
class FakeTimer:
    """记录测试 deadline 的回调和取消状态。"""

    callback: Any
    cancelled: bool = False

    def cancel(self) -> None:
        """把当前 deadline 标记为已取消。"""

        self.cancelled = True


class FakeScheduler:
    """保存全部 deadline，供测试按确定顺序触发。"""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.timers: list[FakeTimer] = []

    def schedule(self, delay: float, callback: Any) -> FakeTimer:
        """创建一个不会自动执行的测试 deadline。"""

        timer = FakeTimer(callback=callback)
        self.delays.append(delay)
        self.timers.append(timer)
        return timer


class FailingScheduler:
    """模拟首次登记进度定时器就崩溃的调度器。"""

    def schedule(self, _delay: float, _callback: Any) -> FakeTimer:
        """稳定抛出异常，复现 starting 预留的失败收口路径。"""

        raise RuntimeError("无法登记进度定时器")


class ScriptedCognitiveExecutor:
    """把 Knowledge 模型流交给测试脚本精确控制。"""

    def __init__(self, stream_factory: Any) -> None:
        self.stream_factory = stream_factory

    def stream(self, **_: object) -> Any:
        """返回测试定义的统一认知任务流。"""

        return self.stream_factory()


class InvalidResultRepairer:
    """让 Activity 单元测试在协议错误时稳定形成二次校验失败。"""

    def repair(self, **_: object) -> str:
        """返回仍不符合二分支合同的正文。"""

        return "invalid-repaired-result"


class RecordingResultRepairer:
    """返回测试指定结果并记录 Plus 原文。"""

    def __init__(self, response: str) -> None:
        """保存修复结果。

        Args:
            response: 唯一一次修复调用应返回的正文。
        """

        self.response = response
        self.raw_outputs: list[str] = []

    def repair(self, **kwargs: object) -> str:
        """记录原文并返回测试结果。"""

        self.raw_outputs.append(str(kwargs["raw_output"]))
        return self.response


def _activity_executor(**kwargs: Any) -> CognitiveActivityExecutor:
    """创建注入确定性修复器的 Activity 测试执行器。

    Args:
        **kwargs: 除结果解析管线以外的执行器构造参数。

    Returns:
        使用真实二分支解析管线的 Cognitive Activity Executor。
    """

    result_resolver = kwargs.pop(
        "result_resolver",
        CognitiveResultResolver(repairer=InvalidResultRepairer()),
    )
    return CognitiveActivityExecutor(result_resolver=result_resolver, **kwargs)


def _cognitive_json(
    *,
    result_text: str | None = "正式结果",
    summary: str | None = "认知任务取得了正式结果。",
    failure_reason: str | None = None,
) -> str:
    """构造 Activity 测试使用的合法 Plus 标记正文。

    Args:
        result_text: 成功时可交付的完整结果。
        summary: 成功时给 Foreground 使用的摘要。
        failure_reason: 失败时可安全进入恢复链路的自然语言原因。

    Returns:
        严格二选一的单个标记协议文本。
    """

    if failure_reason is not None:
        if result_text is not None or summary is not None:
            raise ValueError("失败结果不能携带成功字段")
        return f"<failure_tag>\n{failure_reason}"
    else:
        if result_text is None or summary is None:
            raise ValueError("成功结果必须同时提供 result 和 summary")
        return f"{result_text}\n<summary_tag>\n{summary}"


def _reserve_correction(
    activity_executor: CognitiveActivityExecutor,
    **kwargs: Any,
) -> tuple[bool, tuple[dict[str, object], ...]]:
    """调用新的结构化纠正预留接口，并返回便于既有场景断言的字段。"""

    reservation = activity_executor.reserve_correction(**kwargs)
    return reservation.accepted, reservation.events


class BlockingClosableStream:
    """模拟只有主动 close 才会退出的底层 SDK 流。"""

    def __init__(self) -> None:
        """初始化阻塞和关闭信号。"""

        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self) -> BlockingClosableStream:
        """返回自身作为流迭代器。"""

        return self

    def __next__(self) -> LlmTextStreamChunk:
        """阻塞到 close 被调用，然后结束流。"""

        self.started.set()
        if not self.closed.wait(timeout=2.0):
            raise RuntimeError("测试流没有被主动关闭")
        raise StopIteration

    def close(self) -> None:
        """解除底层流阻塞。"""

        self.closed.set()


class CancellableBlockingExecutor:
    """把 activity_executor 的取消 token 绑定到底层可关闭流。"""

    def __init__(self, stream: BlockingClosableStream) -> None:
        """保存被测底层流。"""

        self.stream_instance = stream

    def stream(self, **kwargs: object) -> Any:
        """返回受取消 token 管理的阻塞流。"""

        return iter_cancellable_stream(
            self.stream_instance,
            kwargs["cancellation_event"],
        )


class UncooperativeCognitiveExecutor:
    """模拟短时间内不响应取消信号的第三方执行器。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def stream(self, **_: object) -> Any:
        """忽略 cancellation token，直到测试主动释放。"""

        self.started.set()
        self.release.wait(timeout=0.3)
        return iter(
            (
                LlmTextStreamChunk(
                    kind="text_delta",
                    text=_cognitive_json(result_text="迟到结果"),
                ),
            )
        )


class DelayedSubmitReturnExecutor(ThreadPoolExecutor):
    """任务已经投递后暂停 submit 返回，放大 Future 登记前的竞态窗口。"""

    def __init__(self) -> None:
        super().__init__(max_workers=1, thread_name_prefix="cognitive-task-submit-race")
        self.submitted = threading.Event()
        self.release_submit = threading.Event()

    def submit(self, function: Any, *args: object, **kwargs: object) -> Any:
        """先真实投递 Activity，再延迟把 Future 交还给 Activity Executor。"""

        future = super().submit(function, *args, **kwargs)
        self.submitted.set()
        self.release_submit.wait(timeout=0.3)
        return future


def _submit(activity_executor: CognitiveActivityExecutor) -> tuple[bool, list[dict[str, object]]]:
    """使用固定会话提交一个实时事实查询。"""

    accepted, events = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal(
                tool_call_id="call-lookup",
                kind="lookup_current_fact",
                brief="查询今天的天气",
            ),),
            reasoning="minimal",
            web_search=True,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
        ),
        source_user_text="今天天气怎么样",
    )
    if accepted:
        activity_id = str(events[0]["payload"]["activity_id"])
        activity_executor.start(activity_id)
    return accepted, events


def test_committed_cognitive_correction_failure_emits_before_start_terminal() -> None:
    """已提交但未运行的认知 B 必须明确记成启动前失败。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=HoldingExecutor(),
        id_factory=lambda _prefix: "cognitive-correction",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _reserve_correction(activity_executor,
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(
                CognitiveGoal(
                    tool_call_id="call-correction",
                    kind="analyze_deeply",
                    brief="执行正确目标",
                ),
            ),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="旧目标错了，请改做正确目标",
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.commit_reserved_correction(activity_id) is True

    assert activity_executor.fail_reserved_correction(
        activity_id,
        reason="correction_cleanup_timeout",
    ) is True

    [failed] = activity_executor.drain_events()
    assert failed["type"] == "cognitive_task.failed"
    assert failed["payload"]["correction_failure_reason"] == "correction_cleanup_timeout"
    assert failed["payload"]["execution_phase"] == "before_start"
    assert failed["payload"]["started_at"] is None


def test_cognitive_start_returns_false_when_runtime_rejects_submission() -> None:
    """Runtime 没有接管任务时，只能记录启动前失败。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=RejectingExecutor(),
        id_factory=lambda _prefix: "cognitive-rejected",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "lookup_current_fact", "查询天气"),),
            reasoning="minimal",
            web_search=True,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
        ),
        source_user_text="查询天气",
    )
    activity_id = str(events[0]["payload"]["activity_id"])

    assert accepted is True
    assert activity_executor.start(activity_id) is False
    emitted = activity_executor.drain_events()
    assert [event["type"] for event in emitted] == ["cognitive_task.failed"]
    assert emitted[0]["payload"]["execution_phase"] == "before_start"
    assert emitted[0]["payload"]["started_at"] is None
    assert emitted[0]["payload"]["suppress_notice"] is True
    assert activity_executor.snapshot("session-a").state == "failed"


def test_cognitive_correction_start_returns_false_when_runtime_rejects_submission() -> None:
    """纠正 B 没进线程池时必须以启动前失败释放预留。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=RejectingExecutor(),
        id_factory=lambda _prefix: "cognitive-correction-rejected",
        progress_scheduler=FakeScheduler(),
    )
    reservation = activity_executor.reserve_correction(
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="纠正目标",
    )
    assert reservation.accepted is True
    assert activity_executor.commit_reserved_correction(reservation.activity_id) is True

    assert activity_executor.start_reserved_correction(reservation.activity_id) is False
    emitted = activity_executor.drain_events()
    assert [event["type"] for event in emitted] == ["cognitive_task.failed"]
    assert emitted[0]["payload"]["execution_phase"] == "before_start"
    assert emitted[0]["payload"]["started_at"] is None
    assert emitted[0]["payload"]["suppress_notice"] is True
    assert activity_executor.rollback_reserved_correction(reservation.activity_id) is False


def test_cognitive_correction_with_empty_id_factory_does_not_reserve_activity() -> None:
    """纠正编号生成失败时必须拒绝，不能留下无身份 B。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=HoldingExecutor(),
        id_factory=lambda _prefix: "",
        progress_scheduler=FakeScheduler(),
    )
    reservation = activity_executor.reserve_correction(
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
        ),
        source_user_text="纠正目标",
    )

    assert reservation.accepted is False
    assert reservation.activity_id == ""
    assert reservation.events[0]["payload"]["reason"] == (
        "cognitive_activity_identity_missing"
    )
    assert activity_executor._prepared_correction_record is None


def test_cognitive_worker_failure_keeps_successful_start_fact() -> None:
    """线程已经接管后发生模型异常，start 仍应表示真实启动成功。"""

    def failing_stream() -> Any:
        """模拟 worker 内部的模型调用失败。"""

        raise RuntimeError("模型接口不可用")
        yield

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(failing_stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-worker-failed",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "分析问题"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
        ),
        source_user_text="分析问题",
    )
    activity_id = str(events[0]["payload"]["activity_id"])

    assert accepted is True
    assert activity_executor.start(activity_id) is True
    emitted = activity_executor.drain_events()
    assert emitted[0]["type"] == "cognitive_task.started"
    assert emitted[-1]["type"] == "cognitive_task.failed"
    assert emitted[-1]["payload"].get("suppress_notice") is not True


@pytest.mark.parametrize("previous_state", ["running", "waiting_user", "attention"])
def test_cognitive_correction_cancel_rejection_restores_exact_previous_state(
    monkeypatch: pytest.MonkeyPatch,
    previous_state: str,
) -> None:
    """取消未被 token 接纳时，旧 Cognitive Activity 必须按原状态继续。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=HoldingExecutor(),
        id_factory=lambda _prefix: "cognitive-active",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _submit(activity_executor)
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    active_record = activity_executor._active_record
    assert active_record is not None
    active_record.state = previous_state
    monkeypatch.setattr(
        active_record.cancellation_token,
        "cancel",
        lambda _reason: False,
    )

    outcome = activity_executor.cancel_for_correction(activity_id)

    assert outcome == "cancel_not_accepted"
    assert activity_executor.snapshot("session-a").state == previous_state
    assert not any(
        event["type"] == "cognitive_task.progress_invalidated"
        for event in activity_executor.drain_events()
    )


def test_committed_cognitive_failure_port_releases_reservation_when_ledger_raises(
    monkeypatch,
) -> None:
    """Cognitive 失败账本异常时也要释放 B，且失败端口保持不抛异常。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=HoldingExecutor(),
        id_factory=lambda _prefix: "cognitive-ledger-failure",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _reserve_correction(activity_executor,
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="纠正目标",
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.commit_reserved_correction(activity_id) is True

    def reject_event(_event: dict[str, object]) -> None:
        """模拟提交后 Cognitive 事件账本突然不可写。"""

        raise RuntimeError("事件账本不可写")

    monkeypatch.setattr(activity_executor, "_append_event", reject_event)

    assert activity_executor.fail_reserved_correction(
        activity_id,
        reason="correction_cancel_failed",
    ) is True
    assert activity_executor.fail_reserved_correction(
        activity_id,
        reason="correction_cancel_failed",
    ) is False


def test_shutdown_compensates_foreground_committed_cognitive_correction() -> None:
    """Foreground 已接受但未启动的 Cognitive B 必须生成启动前失败。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=HoldingExecutor(),
        id_factory=lambda _prefix: "cognitive-shutdown-correction",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _reserve_correction(activity_executor,
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="纠正目标",
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.commit_reserved_correction(activity_id) is True

    activity_executor.shutdown()

    [failed] = activity_executor.drain_events()
    assert failed["type"] == "cognitive_task.failed"
    assert failed["payload"]["correction_failure_reason"] == "backend_shutdown"
    assert failed["payload"]["execution_phase"] == "before_start"
    assert failed["payload"]["started_at"] is None


def test_shutdown_compensates_cognitive_correction_while_start_submit_is_blocked() -> None:
    """B 的 submit 尚未返回时，shutdown 必须只补齐启动前失败。"""

    executor = BlockingBeforeSubmitExecutor()
    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=executor,
        id_factory=lambda _prefix: "cognitive-starting-shutdown",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _reserve_correction(activity_executor,
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="纠正目标",
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.commit_reserved_correction(activity_id) is True

    start_thread = threading.Thread(
        target=lambda: activity_executor.start_reserved_correction(activity_id),
        daemon=True,
    )
    start_thread.start()
    assert executor.entered.wait(timeout=1.0) is True
    try:
        activity_executor.shutdown(timeout=0.02)
    finally:
        executor.release.set()
        start_thread.join(timeout=1.0)

    assert start_thread.is_alive() is False
    [failed] = activity_executor.drain_events()
    assert failed["type"] == "cognitive_task.failed"
    assert failed["payload"]["correction_failure_reason"] == "backend_shutdown"
    assert failed["payload"]["execution_phase"] == "before_start"
    assert failed["payload"]["started_at"] is None


def test_shutdown_compensates_cognitive_correction_queued_but_not_running() -> None:
    """submit 已返回但 worker 尚未运行时，B 仍属于必须失败收口的 committed 预留。"""

    executor = HoldingExecutor()
    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=executor,
        id_factory=lambda _prefix: "cognitive-queued-shutdown",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _reserve_correction(activity_executor,
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="纠正目标",
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.commit_reserved_correction(activity_id) is True
    assert activity_executor.start_reserved_correction(activity_id) is True
    assert len(executor.calls) == 1

    activity_executor.shutdown(timeout=0.02)

    started, failed = activity_executor.drain_events()
    assert started["type"] == "cognitive_task.started"
    assert failed["type"] == "cognitive_task.failed"
    assert failed["payload"]["correction_failure_reason"] == "backend_shutdown"


def test_cognitive_start_survives_progress_scheduler_failure() -> None:
    """普通 Cognitive 已进入 running 后，进度调度故障不能阻止模型任务提交。"""

    runtime = HoldingExecutor()
    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=runtime,
        id_factory=lambda _prefix: "cognitive-schedule-failure",
        progress_scheduler=FailingScheduler(),
    )

    accepted, _events = _submit(activity_executor)

    assert accepted is True
    assert [event["type"] for event in activity_executor.drain_events()] == [
        "cognitive_task.started"
    ]
    assert len(runtime.calls) == 1
    assert activity_executor.is_activity_correctable("cognitive-schedule-failure") is True
    activity_executor.shutdown()


def test_cognitive_correction_survives_progress_scheduler_failure() -> None:
    """纠正 B 已进入 running 后，进度调度故障不能把真实任务误报为失败。"""

    runtime = HoldingExecutor()
    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=runtime,
        id_factory=lambda _prefix: "cognitive-schedule-failure",
        progress_scheduler=FailingScheduler(),
    )
    accepted, events = _reserve_correction(activity_executor,
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="纠正目标",
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.commit_reserved_correction(activity_id) is True

    assert activity_executor.start_reserved_correction(activity_id) is True

    assert [event["type"] for event in activity_executor.drain_events()] == [
        "cognitive_task.started"
    ]
    assert len(runtime.calls) == 1
    assert activity_executor.is_activity_correctable(activity_id) is True
    activity_executor.shutdown()


def test_cognitive_correction_cancelled_before_worker_still_reaches_terminal() -> None:
    """B 排队后被取消时，worker 必须写 cancelled 并释放 starting 预留。"""

    executor = HoldingExecutor()
    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=executor,
        id_factory=lambda _prefix: "cognitive-cancel-before-worker",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _reserve_correction(activity_executor,
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="纠正目标",
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.commit_reserved_correction(activity_id) is True
    assert activity_executor.start_reserved_correction(activity_id) is True
    assert activity_executor.cancel_running(activity_id) is True

    worker, worker_args = executor.calls[0]
    worker(*worker_args)

    event_types = [event["type"] for event in activity_executor.drain_events()]
    assert event_types == [
        "cognitive_task.started",
        "cognitive_task.progress_invalidated",
        "cognitive_task.cancelled",
    ]
    assert _only_activity_snapshot(activity_executor, "session-a") is None


def test_cognitive_correction_reservation_rechecks_parent_state() -> None:
    """旧 Cognitive 在预留前进入 cancelling 时，不能创建新的纠正占位。"""

    runtime = HoldingExecutor()
    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=runtime,
        id_factory=lambda _prefix: "cognitive-parent",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _submit(activity_executor)
    assert accepted is True
    parent_activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.cancel_running(parent_activity_id) is True

    correction_accepted, correction_events = _reserve_correction(activity_executor,
        based_on_activity_id=parent_activity_id,
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call-correction", "analyze_deeply", "改做新目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id=parent_activity_id,
        ),
        source_user_text="旧目标错了，改做新目标",
    )

    assert correction_accepted is False
    assert correction_events[0]["payload"]["reason"] == "correction_activity_not_found"
    activity_executor.shutdown()


def test_shutdown_compensates_cognitive_correction_after_worker_enters_stream() -> None:
    """B 的模型流已经开始但尚未终态时，shutdown 仍必须补齐 started 与 failed。"""

    stream_entered = threading.Event()
    release_stream = threading.Event()

    def blocking_stream() -> Any:
        """进入模型流后阻塞，稳定制造 worker 已接管但尚未终态的窗口。"""

        stream_entered.set()
        assert release_stream.wait(timeout=1.0)
        if False:
            yield LlmTextStreamChunk(kind="text_delta", text="不会产生的结果")

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(blocking_stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-running-shutdown",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = _reserve_correction(activity_executor,
        based_on_activity_id="background-a",
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("call", "analyze_deeply", "正确目标"),),
            reasoning="high",
            web_search=False,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
            based_on_activity_id="background-a",
        ),
        source_user_text="纠正目标",
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert activity_executor.commit_reserved_correction(activity_id) is True

    start_thread = threading.Thread(
        target=lambda: activity_executor.start_reserved_correction(activity_id),
        daemon=True,
    )
    start_thread.start()
    assert stream_entered.wait(timeout=1.0) is True
    try:
        activity_executor.shutdown(timeout=0.02)
    finally:
        release_stream.set()
        start_thread.join(timeout=1.0)

    assert start_thread.is_alive() is False
    started, failed = activity_executor.drain_events()
    assert started["type"] == "cognitive_task.started"
    assert failed["type"] == "cognitive_task.failed"
    assert failed["payload"]["correction_failure_reason"] == "backend_shutdown"


def test_cognitive_activity_executor_uses_backoff_and_contentless_progress_intents() -> None:
    """认知任务应完整冻结每段 reasoning，且 progress 事件不携带正文。"""

    scheduler = FakeScheduler()
    observed: list[tuple[str, str, int]] = []

    def stream() -> Any:
        yield LlmTextStreamChunk(kind="reasoning_delta", text="第一段进展")
        scheduler.timers[0].callback()
        snapshot = _only_activity_snapshot(activity_executor, "session-a")
        assert snapshot is not None
        observed.append((snapshot.progress_reasoning_text, snapshot.progress_sequence))
        activity_executor.acknowledge_progress_notice("cognitive-a", 1)
        yield LlmTextStreamChunk(kind="reasoning_delta", text="第二段进展")
        scheduler.timers[1].callback()
        snapshot = _only_activity_snapshot(activity_executor, "session-a")
        assert snapshot is not None
        observed.append((snapshot.progress_reasoning_text, snapshot.progress_sequence))
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=_cognitive_json(result_text="正式天气结果"),
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=scheduler,
    )

    accepted, _ = _submit(activity_executor)
    events = activity_executor.drain_events()

    assert accepted is True
    assert scheduler.delays == [30.0, 60.0, 60.0]
    assert observed == [("第一段进展", 1), ("第二段进展", 2)]
    progress_events = [event for event in events if event["type"] == "cognitive_task.progress"]
    assert [event["payload"]["notice_seq"] for event in progress_events] == [1, 2]
    assert all(
        set(event["payload"])
        == {"activity_id", "interaction_session_id", "notice_seq", "updated_at"}
        for event in progress_events
    )
    assert events[-1]["type"] == "cognitive_task.completed"
    assert events[-1]["payload"]["result_text"] == "正式天气结果"
    assert scheduler.timers[-1].cancelled is True


def test_cognitive_activity_executor_preserves_result_markdown() -> None:
    """合法成功对象中的 Markdown 正文必须原样交付。"""

    def stream() -> Any:
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=_cognitive_json(
                result_text="**Kimi（月之暗面）** 发布了新模型。",
            ),
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)

    snapshot = activity_executor.snapshot("session-a")
    events = activity_executor.drain_events()
    assert snapshot.result_text == "**Kimi（月之暗面）** 发布了新模型。"
    assert events[-1]["type"] == "cognitive_task.completed"
    assert events[-1]["payload"]["result_text"] == snapshot.result_text


def test_cognitive_protocol_failure_discards_raw_output_and_uses_safe_fact() -> None:
    """非法正文必须失败关闭，且事件、快照和任务记忆都不能保留原文。"""

    secret_raw_output = "```json\n{\"provider_response\":\"secret-raw-body\"}\n```"

    def stream() -> Any:
        yield LlmTextStreamChunk(kind="text_delta", text=secret_raw_output)

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)

    snapshot = activity_executor.snapshot("session-a")
    terminal_event = activity_executor.drain_events()[-1]
    payload = terminal_event["payload"]
    assert terminal_event["type"] == "cognitive_task.failed"
    assert snapshot.result_text == ""
    assert snapshot.error_text == "cognitive_result_protocol_failed"
    assert payload["failure_reason"] == "认知结果没有通过结构化协议校验。"
    assert payload["summary"] == "Cognitive 因结果协议校验失败而未完成任务。"
    assert payload["result_text"] == ""
    assert payload["failure"]["code"] == "cognitive_result_protocol_failed"
    assert "secret-raw-body" not in repr(terminal_event)
    assert "secret-raw-body" not in repr(snapshot)


def test_repaired_success_completes_activity_without_exposing_plus_body() -> None:
    """Flash 修复成功后只提交二分支结果，不保留 Plus 错误正文。"""

    plus_raw_output = '{"result":"修复后的正式结果","summary":缺少引号}'
    repairer = RecordingResultRepairer(
        '{"result":"修复后的正式结果","summary":"修复后的摘要"}'
    )

    def stream() -> Any:
        yield LlmTextStreamChunk(kind="text_delta", text=plus_raw_output)

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        result_resolver=CognitiveResultResolver(repairer=repairer),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)

    terminal_event = activity_executor.drain_events()[-1]
    assert repairer.raw_outputs == [plus_raw_output]
    assert terminal_event["type"] == "cognitive_task.completed"
    assert terminal_event["payload"]["result_text"] == "修复后的正式结果"
    assert terminal_event["payload"]["summary"] == "修复后的摘要"
    assert plus_raw_output not in repr(terminal_event)


def test_repaired_business_failure_enters_unified_failure_path() -> None:
    """Flash 修出的合法业务失败使用 cognitive_task_failed 收口。"""

    repairer = RecordingResultRepairer(
        '{"failure_reason":"必要来源无法读取。"}'
    )

    def stream() -> Any:
        yield LlmTextStreamChunk(kind="text_delta", text="来源读取失败")

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        result_resolver=CognitiveResultResolver(repairer=repairer),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)

    terminal_event = activity_executor.drain_events()[-1]
    assert terminal_event["type"] == "cognitive_task.failed"
    assert terminal_event["payload"]["failure_reason"] == "必要来源无法读取。"
    assert terminal_event["payload"]["failure"]["code"] == "cognitive_task_failed"


def test_missing_user_input_is_a_business_failure_for_self_reflection() -> None:
    """缺少必要用户信息时由二分支失败进入统一恢复链路。"""

    def stream() -> Any:
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=_cognitive_json(
                result_text=None,
                summary=None,
                failure_reason="还缺少用户指定的目标文件。",
            ),
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)

    terminal_event = activity_executor.drain_events()[-1]
    payload = terminal_event["payload"]
    assert terminal_event["type"] == "cognitive_task.failed"
    assert payload["state"] == "failed"
    assert payload["failure_reason"] == "还缺少用户指定的目标文件。"
    assert payload["result_text"] == ""
    assert payload["failure"]["code"] == "cognitive_task_failed"


@pytest.mark.parametrize(
    "failure_reason",
    [
        "用户目标需要修改本地文件，当前认知能力不能执行。",
        "现有证据不足以形成可靠结论。",
    ],
)
def test_unfinished_cognitive_result_projects_recoverable_failure(
    failure_reason: str,
) -> None:
    """任意合法业务失败都投影为统一可恢复的 Activity 失败。

    Args:
        failure_reason: 可安全进入 Self-Reflection 的自然语言事实。
    """

    def stream() -> Any:
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=_cognitive_json(
                result_text=None,
                summary=None,
                failure_reason=failure_reason,
            ),
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)

    terminal_event = activity_executor.drain_events()[-1]
    payload = terminal_event["payload"]
    assert terminal_event["type"] == "cognitive_task.failed"
    assert payload["failure_reason"] == failure_reason
    assert payload["result_text"] == ""
    assert payload["summary"] == ""
    assert payload["failure"]["code"] == "cognitive_task_failed"


def test_cognitive_activity_executor_waits_until_next_check_after_empty_deadline() -> None:
    """空检查后新 reasoning 只积累，并等待下一次 60 秒检查。"""

    scheduler = FakeScheduler()
    observed_sequences: list[int] = []

    def stream() -> Any:
        scheduler.timers[0].callback()
        yield LlmTextStreamChunk(kind="reasoning_delta", text="稍后到达的进展")
        snapshot = _only_activity_snapshot(activity_executor, "session-a")
        assert snapshot is not None
        observed_sequences.append(snapshot.progress_sequence)
        scheduler.timers[1].callback()
        snapshot = _only_activity_snapshot(activity_executor, "session-a")
        assert snapshot is not None
        observed_sequences.append(snapshot.progress_sequence)
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=_cognitive_json(result_text="正式结果"),
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=scheduler,
    )

    _submit(activity_executor)

    assert observed_sequences == [0, 1]
    assert scheduler.delays == [30.0, 60.0, 60.0]
    assert [
        event["type"] for event in activity_executor.drain_events()
    ] == [
        "cognitive_task.started",
        "cognitive_task.stage_changed",
        "cognitive_task.progress",
        "cognitive_task.stage_changed",
        "cognitive_task.progress_invalidated",
        "cognitive_task.stage_changed",
        "cognitive_task.completed",
    ]


def test_cognitive_activity_executor_keeps_newer_segment_after_acknowledgement() -> None:
    """旧播报确认只能消费对应前缀，随后冻结的进度必须继续保留。"""

    scheduler = FakeScheduler()
    observed: list[tuple[str, int]] = []

    def stream() -> Any:
        yield LlmTextStreamChunk(kind="reasoning_delta", text="第一段")
        scheduler.timers[0].callback()
        yield LlmTextStreamChunk(kind="reasoning_delta", text="第二段")
        scheduler.timers[1].callback()
        snapshot = _only_activity_snapshot(activity_executor, "session-a")
        assert snapshot is not None
        observed.append((snapshot.progress_reasoning_text, snapshot.progress_sequence))
        assert activity_executor.acknowledge_progress_notice("cognitive-a", 1) is True
        snapshot = _only_activity_snapshot(activity_executor, "session-a")
        assert snapshot is not None
        observed.append((snapshot.progress_reasoning_text, snapshot.progress_sequence))
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=_cognitive_json(result_text="正式结果"),
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=scheduler,
    )

    _submit(activity_executor)

    assert observed == [("第一段第二段", 2), ("第二段", 2)]


def test_cognitive_activity_executor_content_start_invalidates_progress() -> None:
    """首个正文片段应废止弱进度，此后不得再安排任何认知提醒。"""

    scheduler = FakeScheduler()
    observed: list[tuple[str, str, int]] = []

    def stream() -> Any:
        raw_result = _cognitive_json(result_text="正式答案补充")
        split_index = len(raw_result) // 2
        yield LlmTextStreamChunk(kind="reasoning_delta", text="旧进度")
        scheduler.timers[0].callback()
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=raw_result[:split_index],
        )
        snapshot = _only_activity_snapshot(activity_executor, "session-a")
        assert snapshot is not None
        observed.append(
            (
                snapshot.phase,
                snapshot.progress_reasoning_text,
                snapshot.progress_sequence,
            )
        )
        yield LlmTextStreamChunk(kind="reasoning_delta", text="正文后的思考不应公开")
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=raw_result[split_index:],
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=scheduler,
    )

    _submit(activity_executor)
    events = activity_executor.drain_events()

    assert scheduler.delays == [30.0, 60.0]
    assert scheduler.timers[1].cancelled is True
    assert observed == [("finalizing", "", 1)]
    assert [event["type"] for event in events] == [
        "cognitive_task.started",
        "cognitive_task.stage_changed",
        "cognitive_task.progress",
        "cognitive_task.stage_changed",
        "cognitive_task.progress_invalidated",
        "cognitive_task.stage_changed",
        "cognitive_task.completed",
    ]
    assert events[-1]["payload"]["result_text"] == "正式答案补充"


def test_cognitive_activity_executor_failure_hides_exception_details() -> None:
    """执行异常应产生脱敏终态，并立即清理所有临时进度。"""

    scheduler = FakeScheduler()

    def stream() -> Any:
        yield LlmTextStreamChunk(kind="reasoning_delta", text="已开始")
        raise RuntimeError("C:/secret/token.txt")

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=scheduler,
    )

    _submit(activity_executor)
    events = activity_executor.drain_events()

    assert events[-1]["type"] == "cognitive_task.failed"
    failure = events[-1]["payload"]["failure"]
    assert failure["code"] == "unclassified_error"
    assert failure["stage_code"] == "processing"
    assert "error_type=RuntimeError" in failure["technical_detail"]
    assert "C:/secret" not in str(events[-1])
    assert "error_text" not in events[-1]["payload"]
    assert _only_activity_snapshot(activity_executor, "session-a") is None
    assert scheduler.timers[0].cancelled is True


def test_cognitive_activity_executor_events_keep_plan_metadata() -> None:
    """生命周期事件应保留目标与能力档位，但不得复制图片或来源正文。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )
    plan = CognitiveExecutionPlan(
        goals=(
            CognitiveGoal("inspect-a", "lookup_current_fact", "读取人物"),
            CognitiveGoal(
                "lookup-a",
                "lookup_current_fact",
                "核查身份",
                response_goal="根据核实结果解释人物是否适合当前角色。",
            ),
        ),
        reasoning="minimal",
        web_search=True,
        web_extract=True,
        images=(VisualEvidence("desktop", "桌面截图", "data:image/png;base64,secret-image"),),
        source_urls=("https://example.com/private",),
        progress_policy="reasoning_driven",
    )

    accepted, prepared_events = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=plan,
        source_user_text="这个人是谁",
    )
    activity_executor.start(str(prepared_events[0]["payload"]["activity_id"]))
    events = activity_executor.drain_events()

    assert accepted is True
    assert events[0]["payload"]["goals"] == [
        {"kind": "lookup_current_fact", "brief": "读取人物"},
        {"kind": "lookup_current_fact", "brief": "核查身份"},
    ]
    assert events[0]["payload"]["reasoning"] == "minimal"
    assert "encoded_images" not in events[0]["payload"]
    assert "source_urls" not in events[0]["payload"]
    assert "secret-image" not in str(events[:-1])
    assert "example.com/private" not in str(events[:-1])
    assert events[-1]["payload"]["task_input_snapshot"]["encoded_image_count"] == 1
    assert "secret-image" not in str(events[-1])
    assert events[-1]["payload"]["task_input_snapshot"]["source_urls"] == [
        "https://example.com/private"
    ]
    assert events[-1]["payload"]["task_input_snapshot"]["response_goals"] == [
        "根据核实结果解释人物是否适合当前角色。"
    ]


def test_cognitive_correction_events_project_composite_category_and_limitations() -> None:
    """单 Goal 多能力纠正也要投影综合类别，并审计证据限制。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-correction",
        progress_scheduler=FakeScheduler(),
    )
    plan = CognitiveExecutionPlan(
        goals=(
            CognitiveGoal(
                "correction-a",
                "lookup_current_fact",
                "核查 Edge 版本并分析兼容性",
            ),
        ),
        reasoning="high",
        web_search=True,
        web_extract=False,
        images=(),
        source_urls=(),
        progress_policy="reasoning_driven",
        based_on_activity_id="cognitive-a",
        cognitive_tool_types=("lookup_current_fact", "analyze_deeply"),
        evidence_limitations=("父任务中有 1 项图片证据已经失效。",),
    )

    accepted, prepared_events = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=plan,
        source_user_text="不是 Chrome，是 Edge",
    )
    activity_executor.start(str(prepared_events[0]["payload"]["activity_id"]))
    events = activity_executor.drain_events()

    assert accepted is True
    assert all(
        event["payload"].get("category") == "综合认知任务"
        for event in events
        if event["type"]
        in {
            "cognitive_task.started",
            "cognitive_task.completed",
            "cognitive_task.failed",
            "cognitive_task.cancelled",
        }
    )
    assert events[-1]["payload"]["task_input_snapshot"]["evidence_limitations"] == [
        "父任务中有 1 项图片证据已经失效。"
    ]


def test_reasoning_off_task_emits_only_started_and_terminal() -> None:
    """不需要 thinking 的异步任务不得创建任何弱进度事件。"""

    scheduler = FakeScheduler()

    def stream() -> Any:
        yield LlmTextStreamChunk(kind="reasoning_delta", text="供应商意外返回的思考")
        yield LlmTextStreamChunk(
            kind="text_delta",
            text=_cognitive_json(result_text="屏幕显示登录失败。"),
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "inspect-a",
        progress_scheduler=scheduler,
    )
    accepted, prepared = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("inspect-a", "lookup_current_fact", "读取报错"),),
            reasoning="off",
            web_search=False,
            web_extract=False,
            images=(VisualEvidence("desktop", "桌面截图", "data:image/png;base64,screen"),),
            source_urls=(),
            progress_policy="disabled",
        ),
        source_user_text="看看屏幕",
    )
    assert accepted is True
    activity_executor.start(str(prepared[0]["payload"]["activity_id"]))

    events = activity_executor.drain_events()
    assert scheduler.delays == []
    assert [event["type"] for event in events] == [
        "cognitive_task.started",
        "cognitive_task.stage_changed",
        "cognitive_task.stage_changed",
        "cognitive_task.stage_changed",
        "cognitive_task.completed",
    ]
    assert [
        event["payload"]["stage"]
        for event in events
        if event["type"] == "cognitive_task.stage_changed"
    ] == ["processing", "generating_result", "delivering_result"]


def test_reasoning_only_stream_fails_instead_of_exposing_internal_thought() -> None:
    """没有正式 content 的流必须失败，不能把 reasoning 当作用户结果。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(
            lambda: iter((LlmTextStreamChunk(kind="reasoning_delta", text="内部思考"),))
        ),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)

    terminal = activity_executor.drain_events()[-1]
    assert terminal["type"] == "cognitive_task.failed"
    assert terminal["payload"]["result_text"] == ""
    assert terminal["payload"]["failure"]["code"] == (
        "cognitive_result_protocol_failed"
    )
    assert "内部思考" not in str(terminal["payload"])


def test_shutdown_closes_active_stream_and_suppresses_late_terminal() -> None:
    """shutdown 必须主动关流，并让已启动任务失去追加迟到终态的资格。"""

    stream = BlockingClosableStream()
    activity_executor = _activity_executor(
        task_executor=CancellableBlockingExecutor(stream),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )
    accepted, events = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("inspect-a", "lookup_current_fact", "看屏幕"),),
            reasoning="off",
            web_search=False,
            web_extract=False,
            images=(VisualEvidence("desktop", "桌面截图", "data:image/png;base64,screen"),),
            source_urls=(),
            progress_policy="disabled",
        ),
        source_user_text="看看屏幕",
    )
    assert accepted is True
    activity_executor.start(str(events[0]["payload"]["activity_id"]))
    assert stream.started.wait(timeout=1.0) is True
    assert [event["type"] for event in activity_executor.drain_events()] == [
        "cognitive_task.started",
        "cognitive_task.stage_changed",
    ]

    activity_executor.shutdown(timeout=1.0)

    assert stream.closed.is_set() is True
    assert activity_executor.drain_events() == []
    assert not any(
        thread.name.startswith("cognitive-task") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_shutdown_timeout_does_not_wait_for_uncooperative_cognitive_executor() -> None:
    """第三方执行器暂不响应取消时，Activity Executor 也必须在总预算内返回。"""

    task_executor = UncooperativeCognitiveExecutor()
    activity_executor = _activity_executor(
        task_executor=task_executor,
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )
    _submit(activity_executor)
    assert task_executor.started.wait(timeout=1.0) is True
    activity_executor.drain_events()

    started_at = time.monotonic()
    activity_executor.shutdown(timeout=0.02)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.1
    assert activity_executor.drain_events() == []
    task_executor.release.set()


def test_shutdown_does_not_miss_future_while_submit_is_returning() -> None:
    """submit 已投递但尚未返回 Future 时，关闭流程也不能错误地无限 join。"""

    task_executor = UncooperativeCognitiveExecutor()
    executor = DelayedSubmitReturnExecutor()
    activity_executor = _activity_executor(
        task_executor=task_executor,
        executor=executor,
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )
    activity_executor._owns_executor = True  # noqa: SLF001
    accepted, events = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("inspect-a", "lookup_current_fact", "看屏幕"),),
            reasoning="off",
            web_search=False,
            web_extract=False,
            images=(VisualEvidence("desktop", "桌面截图", "data:image/png;base64,screen"),),
            source_urls=(),
            progress_policy="disabled",
        ),
        source_user_text="看看屏幕",
    )
    assert accepted is True
    start_thread = threading.Thread(
        target=lambda: activity_executor.start(str(events[0]["payload"]["activity_id"])),
        daemon=True,
    )
    start_thread.start()
    assert executor.submitted.wait(timeout=1.0) is True
    assert task_executor.started.wait(timeout=1.0) is True

    started_at = time.monotonic()
    try:
        activity_executor.shutdown(timeout=0.02)
        elapsed = time.monotonic() - started_at
    finally:
        task_executor.release.set()
        executor.release_submit.set()
        start_thread.join(timeout=1.0)

    assert elapsed < 0.1
    assert start_thread.is_alive() is False
    assert activity_executor.drain_events() == []


def test_event_notifier_failure_does_not_leak_running_cognitive_task() -> None:
    """本地事件已登记后，旁路唤醒异常不得留下未执行的 running 占位。"""

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(
            lambda: iter(
                (
                    LlmTextStreamChunk(
                        kind="text_delta",
                        text=_cognitive_json(result_text="正式结果"),
                    ),
                )
            )
        ),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
        event_notifier=lambda: (_ for _ in ()).throw(RuntimeError("notify failed")),
    )

    accepted, _ = _submit(activity_executor)

    assert accepted is True
    assert [event["type"] for event in activity_executor.drain_events()] == [
        "cognitive_task.started",
        "cognitive_task.stage_changed",
        "cognitive_task.stage_changed",
        "cognitive_task.progress_invalidated",
        "cognitive_task.stage_changed",
        "cognitive_task.completed",
    ]


def test_cognitive_activity_executor_enforces_global_singleton_across_sessions() -> None:
    """认知任务在整个 activity_executor 内全局唯一，不因会话切换绕过约束。"""

    executor = HoldingExecutor()
    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(lambda: iter(())),
        executor=executor,
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )
    accepted_a, prepared = activity_executor.prepare(
        interaction_session_id="session-a",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("lookup-a", "lookup_current_fact", "查询天气"),),
            reasoning="minimal",
            web_search=True,
            web_extract=False,
            images=(),
            source_urls=(),
            progress_policy="reasoning_driven",
        ),
        source_user_text="今天天气怎么样",
    )
    assert activity_executor.start(str(prepared[0]["payload"]["activity_id"])) is True

    accepted_b, rejected = activity_executor.prepare(
        interaction_session_id="session-b",
        plan=CognitiveExecutionPlan(
            goals=(CognitiveGoal("inspect-b", "lookup_current_fact", "查看屏幕"),),
            reasoning="off",
            web_search=False,
            web_extract=False,
            images=(VisualEvidence("desktop", "桌面截图", "data:image/png;base64,screen"),),
            source_urls=(),
            progress_policy="disabled",
        ),
        source_user_text="屏幕上是什么",
    )

    assert accepted_a is True
    assert accepted_b is False
    assert rejected[0]["payload"]["activity_id"] == "cognitive-a"
    assert rejected[0]["payload"]["reason"] == "cognitive_task_running"
    assert len(executor.calls) == 1


def test_cancel_running_waits_for_cognitive_executor_exit() -> None:
    """认知取消必须先展示 cancelling，底层流退出后才提交 cancelled。"""

    task_executor = UncooperativeCognitiveExecutor()
    activity_executor = _activity_executor(
        task_executor=task_executor,
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )
    _submit(activity_executor)
    assert task_executor.started.wait(timeout=1.0) is True
    assert activity_executor.drain_events()[0]["type"] == "cognitive_task.started"

    assert activity_executor.cancel_running("cognitive-a") is True
    assert activity_executor.cancel_running("cognitive-a") is False
    snapshot = _only_activity_snapshot(activity_executor, "session-a")
    assert snapshot is not None
    assert snapshot.state == "cancelling"
    assert [event["type"] for event in activity_executor.drain_events()] == [
        "cognitive_task.progress_invalidated"
    ]

    task_executor.release.set()
    deadline = time.monotonic() + 1.0
    terminal_events: list[dict[str, object]] = []
    while time.monotonic() < deadline and not terminal_events:
        terminal_events = activity_executor.drain_events()
        if not terminal_events:
            time.sleep(0.01)

    assert [event["type"] for event in terminal_events] == [
        "cognitive_task.cancelled"
    ]
    assert activity_executor.snapshot("session-a").state == "cancelled"
    assert _only_activity_snapshot(activity_executor, "session-a") is None
    activity_executor.shutdown()


def test_cognitive_stream_interruption_maps_to_stable_failure_code() -> None:
    """已建立流中途断连必须归类为 provider_stream_interrupted 并记录真实阶段。"""

    import httpx

    def stream() -> Any:
        yield LlmTextStreamChunk(kind="text_delta", text="半截正文")
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)
    events = activity_executor.drain_events()

    assert events[-1]["type"] == "cognitive_task.failed"
    failure = events[-1]["payload"]["failure"]
    assert failure["code"] == "provider_stream_interrupted"
    assert failure["stage_code"] == "generating_result"
    assert failure["stage_label"] == "生成结果"
    assert "error_type=RemoteProtocolError" in failure["technical_detail"]


def test_cognitive_model_not_configured_maps_to_configuration_failure() -> None:
    """模型未配置的结构化异常必须归类为 configuration_invalid。"""

    from companion.foreground_tools.cognitive.executor import (
        CognitiveModelNotConfiguredError,
    )

    def stream() -> Any:
        raise CognitiveModelNotConfiguredError("认知任务模型尚未配置。")

    activity_executor = _activity_executor(
        task_executor=ScriptedCognitiveExecutor(stream),
        executor=ImmediateExecutor(),
        id_factory=lambda _prefix: "cognitive-a",
        progress_scheduler=FakeScheduler(),
    )

    _submit(activity_executor)
    events = activity_executor.drain_events()

    assert events[-1]["type"] == "cognitive_task.failed"
    failure = events[-1]["payload"]["failure"]
    assert failure["code"] == "configuration_invalid"
    assert "配置" in failure["summary"]


def _only_activity_snapshot(
    activity_executor: CognitiveActivityExecutor,
    interaction_session_id: str,
) -> ForegroundActivitySnapshot | None:
    """读取 Cognitive 唯一允许存在的 Foreground Activity 快照。

    Args:
        activity_executor: 保证单任务占用的 Cognitive 执行器。
        interaction_session_id: 当前交互会话编号。

    Returns:
        没有活动时返回空；存在活动时返回唯一快照。
    """

    snapshots = activity_executor.activity_snapshots(interaction_session_id)
    assert len(snapshots) <= 1
    return snapshots[0] if snapshots else None
