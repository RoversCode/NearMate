"""在助手子进程执行主进程授权的运行期记忆摘要。"""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor
from queue import Empty, Queue
from threading import RLock
from typing import Protocol

from companion.llm.foreground_models import ForegroundEvent
from companion.llm.request_intent import LlmInvocationIntent
from companion.runtime_memory.models import (
    RuntimeMemoryCompactionPlan,
)
from companion.runtime_memory.summary_prompt import build_runtime_memory_summary_prompt


class RuntimeMemorySummaryClientProtocol(Protocol):
    """定义运行期记忆摘要需要的最小模型接口。"""

    def complete_text(
        self,
        *,
        messages: list[dict[str, str]],
        intent: LlmInvocationIntent,
        audit_source: str,
        audit_operation: str,
    ) -> str:
        """根据完整消息、语义意图和审查分类返回文本。"""


class RuntimeMemoryCompactionWorker:
    """执行摘要模型调用，但不直接修改任何记忆状态。"""

    def __init__(
        self,
        *,
        summary_client: RuntimeMemorySummaryClientProtocol,
        event_notifier: object | None = None,
        executor: Executor | None = None,
    ) -> None:
        """初始化单线程摘要 Worker。

        Args:
            summary_client: 当前 Summary 角色客户端。
            event_notifier: 新结果产生后唤醒助手进程主循环的可调用对象。
            executor: 测试可替换的异步执行器。
        """

        self._summary_client = summary_client
        self._event_notifier = event_notifier
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="runtime-memory-compaction",
        )
        self._events: Queue[ForegroundEvent] = Queue()
        self._running_plan_ids: set[str] = set()
        self._lock = RLock()

    def submit(self, plan: RuntimeMemoryCompactionPlan) -> bool:
        """提交一笔主进程已经冻结的摘要计划。

        Args:
            plan: 带严格身份和消息前缀的不可变计划。

        Returns:
            首次接收该计划时返回真；重复或非法计划返回假。
        """

        if not plan.plan_id.strip() or not plan.messages_to_summarize:
            return False
        with self._lock:
            if plan.plan_id in self._running_plan_ids:
                return False
            self._running_plan_ids.add(plan.plan_id)
        try:
            self._executor.submit(self._run, plan)
        except Exception:
            with self._lock:
                self._running_plan_ids.discard(plan.plan_id)
            self._events.put(ForegroundEvent(
                type="llm.foreground.runtime_memory_compaction_failed",
                payload={
                    "memory_epoch": plan.memory_epoch,
                    "plan_id": plan.plan_id,
                    "source_message_id": plan.source_message_id,
                },
            ))
            notifier = self._event_notifier
            if callable(notifier):
                notifier()
            return False
        return True

    def drain_events(self, limit: int = 100) -> list[ForegroundEvent]:
        """按顺序排出已经完成的摘要结果事件。

        Args:
            limit: 本次最多读取的事件数。

        Returns:
            只包含内部压缩成功或失败事件的列表。
        """

        events: list[ForegroundEvent] = []
        for _ in range(max(0, limit)):
            try:
                events.append(self._events.get_nowait())
            except Empty:
                break
        return events

    def shutdown(self, timeout: float = 1.0) -> None:
        """停止接收新摘要任务并取消尚未启动的任务。

        Args:
            timeout: 为统一关闭接口保留的时限参数。
        """

        del timeout
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, plan: RuntimeMemoryCompactionPlan) -> None:
        """执行一次摘要并回传带完整计划身份的结果。

        Args:
            plan: 当前唯一摘要计划。
        """

        try:
            prompt = build_runtime_memory_summary_prompt(
                plan.messages_to_summarize,
                existing_summary=plan.previous_summary,
                covered_from=plan.covered_from,
                covered_to=plan.covered_to,
            )
            summary = self._summary_client.complete_text(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是会话上下文压缩器，只输出可继续当前对话的"
                            "简洁中文摘要。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                intent=LlmInvocationIntent.text(thinking_level="high"),
                audit_source="runtime_memory",
                audit_operation="compaction",
            ).strip()
            if not summary:
                raise ValueError("Summary 模型返回空摘要")
            event_type = "llm.foreground.runtime_memory_compaction_completed"
            payload: dict[str, object] = {
                "memory_epoch": plan.memory_epoch,
                "plan_id": plan.plan_id,
                "source_message_id": plan.source_message_id,
                "summary": summary,
            }
        except Exception:
            event_type = "llm.foreground.runtime_memory_compaction_failed"
            payload = {
                "memory_epoch": plan.memory_epoch,
                "plan_id": plan.plan_id,
                "source_message_id": plan.source_message_id,
            }
        finally:
            with self._lock:
                self._running_plan_ids.discard(plan.plan_id)
        self._events.put(ForegroundEvent(type=event_type, payload=payload))
        notifier = self._event_notifier
        if callable(notifier):
            notifier()
