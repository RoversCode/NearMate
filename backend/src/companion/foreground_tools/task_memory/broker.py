"""让助手子进程同步等待主进程返回任务详情。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, RLock
from uuid import uuid4

from companion.llm.foreground_models import ForegroundEvent
from companion.realtime.assistant_events import TaskMemoryLookupResultCommand
from companion.runtime_memory.models import TaskMemoryLookupPurpose


_POLL_INTERVAL_SECONDS = 0.05


@dataclass(slots=True)
class _PendingTaskMemoryLookup:
    """保存一笔等待主进程读取的精确任务召回。"""

    lookup_id: str
    activity_id: str
    purpose: TaskMemoryLookupPurpose
    completed: Event = field(default_factory=Event)
    result: dict[str, object] | None = None


class TaskMemoryLookupBroker:
    """通过现有助手控制通道完成一次五秒内的任务详情 RPC。"""

    def __init__(
        self,
        *,
        event_notifier: Callable[[], None] | None = None,
        timeout_seconds: float = 5.0,
        lookup_id_factory: Callable[[], str] = lambda: uuid4().hex,
        monotonic_now: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化空的等待表和内部事件队列。

        Args:
            event_notifier: 新请求事件产生后唤醒子进程主循环的回调。
            timeout_seconds: 等待主进程权威回执的最长秒数。
            lookup_id_factory: 生成召回编号的函数。
            monotonic_now: 单调时间来源。
        """

        if timeout_seconds <= 0:
            raise ValueError("任务记忆召回等待时间必须大于零")
        self._event_notifier = event_notifier
        self._timeout_seconds = timeout_seconds
        self._lookup_id_factory = lookup_id_factory
        self._monotonic_now = monotonic_now
        self._pending: dict[str, _PendingTaskMemoryLookup] = {}
        self._events: Queue[ForegroundEvent] = Queue()
        self._lock = RLock()

    def lookup_and_wait(
        self,
        *,
        activity_id: str,
        purpose: TaskMemoryLookupPurpose,
        cancellation_event: Event | None,
    ) -> dict[str, object]:
        """请求主进程读取任务详情并同步等待结果。

        Args:
            activity_id: 需要精确召回的任务编号。
            purpose: 本次读取用于前台回答还是派生任务执行。
            cancellation_event: 当前 generation 的取消信号。

        Returns:
            completed、not_available 或 lookup_timeout 结果。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            return _invalid_activity_result()
        if purpose not in {"foreground_recall", "task_derivation"}:
            return _invalid_purpose_result(normalized_activity_id)
        lookup_id = self._lookup_id_factory().strip()
        if not lookup_id:
            return _lookup_timeout_result(normalized_activity_id)
        pending = _PendingTaskMemoryLookup(
            lookup_id=lookup_id,
            activity_id=normalized_activity_id,
            purpose=purpose,
        )
        with self._lock:
            self._pending[lookup_id] = pending
        self._events.put(
            ForegroundEvent(
                type="llm.foreground.task_memory_lookup_requested",
                payload={
                    "lookup_id": lookup_id,
                    "activity_id": normalized_activity_id,
                    "purpose": purpose,
                },
            )
        )
        if self._event_notifier is not None:
            self._event_notifier()

        deadline = self._monotonic_now() + self._timeout_seconds
        while True:
            remaining = deadline - self._monotonic_now()
            if remaining <= 0 or (
                cancellation_event is not None and cancellation_event.is_set()
            ):
                with self._lock:
                    self._pending.pop(lookup_id, None)
                return _lookup_timeout_result(normalized_activity_id)
            pending.completed.wait(timeout=min(_POLL_INTERVAL_SECONDS, remaining))
            with self._lock:
                current = self._pending.get(lookup_id)
                if current is not pending or pending.result is None:
                    continue
                result = dict(pending.result)
                self._pending.pop(lookup_id, None)
                return result

    def apply_result(self, command: TaskMemoryLookupResultCommand) -> bool:
        """应用主进程返回的精确任务详情。

        Args:
            command: 带 lookup_id 的控制命令。

        Returns:
            命令仍对应当前等待项并成功唤醒时返回真。
        """

        with self._lock:
            pending = self._pending.get(command.lookup_id)
            if (
                pending is None
                or pending.activity_id != command.activity_id
                or pending.purpose != command.purpose
            ):
                return False
            pending.result = dict(command.result)
            pending.completed.set()
            return True

    def drain_events(self, limit: int = 100) -> list[ForegroundEvent]:
        """非阻塞排出等待主进程处理的召回事件。

        Args:
            limit: 本次最多读取的事件数。

        Returns:
            保持产生顺序的内部事件。
        """

        events: list[ForegroundEvent] = []
        for _ in range(max(0, limit)):
            try:
                events.append(self._events.get_nowait())
            except Empty:
                break
        return events


def _invalid_activity_result() -> dict[str, object]:
    """返回缺少明确任务编号时的工具结果。"""

    return {
        "state": "not_available",
        "reason": "invalid_activity_id",
        "safe_message": "需要先从聊天里的任务记忆确定具体任务编号。",
    }


def _lookup_timeout_result(activity_id: str) -> dict[str, object]:
    """返回五秒内没有收到主进程回执的安全结果。"""

    return {
        "state": "failed",
        "activity_id": activity_id,
        "reason": "lookup_timeout",
        "safe_message": "这次没有及时读到任务详情，请稍后再试。",
    }


def _invalid_purpose_result(activity_id: str) -> dict[str, object]:
    """返回内部召回用途不合法时的稳定失败事实。

    Args:
        activity_id: 已通过基础校验的任务编号。

    Returns:
        不会向主进程发送请求的失败载荷。
    """

    return {
        "state": "failed",
        "activity_id": activity_id,
        "reason": "invalid_lookup_purpose",
        "safe_message": "任务详情当前无法读取。",
    }
