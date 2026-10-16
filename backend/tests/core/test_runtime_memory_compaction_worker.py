from __future__ import annotations

from concurrent.futures import Executor, Future
from datetime import date, datetime
from typing import Any, Callable

from companion.llm.request_intent import LlmInvocationIntent
from companion.runtime_memory.compaction import RuntimeMemoryCompactionWorker
from companion.runtime_memory.models import (
    RuntimeMemoryCompactionPlan,
    RuntimeMemoryMessage,
)


class ImmediateExecutor(Executor):
    """在当前线程执行压缩任务，避免测试依赖调度时序。"""

    def submit(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        """立即执行函数并返回已完成的 Future。"""

        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exception:
            future.set_exception(exception)
        return future


class RecordingSummaryClient:
    """记录摘要 Prompt 并返回固定结果。"""

    def __init__(self, result: str = "压缩后的记忆", *, should_fail: bool = False) -> None:
        """保存测试结果和失败开关。"""

        self.result = result
        self.should_fail = should_fail
        self.prompts: list[str] = []
        self.intents: list[LlmInvocationIntent] = []
        self.audit_categories: list[tuple[str, str]] = []

    def complete_text(
        self,
        *,
        messages: list[dict[str, str]],
        intent: LlmInvocationIntent,
        audit_source: str,
        audit_operation: str,
    ) -> str:
        """记录统一文本请求，并按配置返回或抛错。

        Args:
            messages: Summary Worker 组装的完整消息。
            intent: 本次摘要需要的语义调用意图。
            audit_source: 本次摘要的业务来源。
            audit_operation: 本次摘要的调用阶段。

        Returns:
            测试预设的摘要正文。

        Raises:
            RuntimeError: 测试要求模拟 Summary 不可用。
        """

        self.prompts.append(messages[-1]["content"])
        self.intents.append(intent)
        self.audit_categories.append((audit_source, audit_operation))
        if self.should_fail:
            raise RuntimeError("summary unavailable")
        return self.result


class RejectingExecutor(Executor):
    """模拟已经关闭、无法再接收任务的摘要执行器。"""

    def submit(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        """拒绝执行并模拟线程池关闭竞态。"""

        del fn, args, kwargs
        raise RuntimeError("executor already shutdown")


def test_worker_returns_validated_plan_identity_and_real_dates() -> None:
    """Summary Worker 只回传结果，并保留主进程消息的真实日期边界。"""

    client = RecordingSummaryClient()
    worker = RuntimeMemoryCompactionWorker(
        summary_client=client,
        executor=ImmediateExecutor(),
    )
    plan = _plan()

    assert worker.submit(plan) is True
    event = worker.drain_events()[0]

    assert event.type == "llm.foreground.runtime_memory_compaction_completed"
    assert event.payload == {
        "memory_epoch": 7,
        "plan_id": "plan-a",
        "source_message_id": 2,
        "summary": "压缩后的记忆",
    }
    assert "2026年8月3日" in client.prompts[0]
    assert "2026年8月4日" in client.prompts[0]
    assert "固定结构" not in client.prompts[0]
    assert "必须保留 activity_id" not in client.prompts[0]
    assert client.intents == [LlmInvocationIntent.text(thinking_level="high")]
    assert client.audit_categories == [("runtime_memory", "compaction")]


def test_worker_reports_failure_without_inventing_summary() -> None:
    """摘要模型异常时只回传失败身份，由主进程保留原历史和任务详情。"""

    worker = RuntimeMemoryCompactionWorker(
        summary_client=RecordingSummaryClient(should_fail=True),
        executor=ImmediateExecutor(),
    )

    assert worker.submit(_plan()) is True
    event = worker.drain_events()[0]

    assert event.type == "llm.foreground.runtime_memory_compaction_failed"
    assert event.payload == {
        "memory_epoch": 7,
        "plan_id": "plan-a",
        "source_message_id": 2,
    }


def test_worker_submission_failure_emits_plan_identity_for_release() -> None:
    """执行器拒绝任务时也必须回传失败身份，不能让主进程计划悬空。"""

    worker = RuntimeMemoryCompactionWorker(
        summary_client=RecordingSummaryClient(),
        executor=RejectingExecutor(),
    )

    assert worker.submit(_plan()) is False
    event = worker.drain_events()[0]
    assert event.type == "llm.foreground.runtime_memory_compaction_failed"
    assert event.payload["plan_id"] == "plan-a"


def _plan() -> RuntimeMemoryCompactionPlan:
    """构造跨两天的压缩计划。"""

    return RuntimeMemoryCompactionPlan(
        memory_epoch=7,
        plan_id="plan-a",
        source_message_id=2,
        owner_runtime_epoch=3,
        covered_from=date(2026, 8, 3),
        covered_to=date(2026, 8, 4),
        previous_summary="",
        messages_to_summarize=(
            RuntimeMemoryMessage(
                message_id=1,
                role="user",
                content="昨晚的问题",
                token_count=4,
                created_at=datetime.fromisoformat("2026-08-03T23:59:00+08:00"),
            ),
            RuntimeMemoryMessage(
                message_id=2,
                role="assistant",
                content="今天的回答",
                token_count=4,
                created_at=datetime.fromisoformat("2026-08-04T00:01:00+08:00"),
            ),
        ),
    )
