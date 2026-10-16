"""把子进程的 Foreground 取消请求交给主进程权威 Activity 服务。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, RLock
from uuid import uuid4

from companion.llm.foreground_models import ForegroundEvent
from companion.realtime.assistant_events import ActivityCancellationResultCommand


_POLL_INTERVAL_SECONDS = 0.05


@dataclass(slots=True)
class _PendingActivityCancellation:
    """保存一笔正在等待主进程回执的 Activity 取消请求。

    Args:
        cancellation_id: 本次取消请求的唯一编号。
        activity_id: 用户明确要求取消的 Activity 编号。
        completed: 主进程回执到达时唤醒等待者的事件。
        result: 主进程返回的结构化取消事实。
    """

    cancellation_id: str
    activity_id: str
    completed: Event = field(default_factory=Event)
    result: dict[str, object] | None = None


class ActivityCancellationBroker:
    """同步等待主进程确认取消请求是否已被权威运行时接收。

    Args:
        event_notifier: 新请求事件入队后唤醒助手子进程主循环的回调。
        timeout_seconds: 一笔取消请求等待主进程回执的最长秒数。
        cancellation_id_factory: 生成取消请求编号的函数。
        monotonic_now: 计算等待截止时间的单调时钟。
    """

    def __init__(
        self,
        *,
        event_notifier: Callable[[], None] | None = None,
        timeout_seconds: float = 5.0,
        cancellation_id_factory: Callable[[], str] = lambda: uuid4().hex,
        monotonic_now: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化空的取消等待表和请求事件队列。

        Args:
            event_notifier: 新请求事件入队后唤醒助手子进程主循环的回调。
            timeout_seconds: 一笔取消请求等待主进程回执的最长秒数。
            cancellation_id_factory: 生成取消请求编号的函数。
            monotonic_now: 计算等待截止时间的单调时钟。
        """

        if timeout_seconds <= 0:
            raise ValueError("Activity 取消回执等待时间必须大于零")
        self._event_notifier = event_notifier
        self._timeout_seconds = timeout_seconds
        self._cancellation_id_factory = cancellation_id_factory
        self._monotonic_now = monotonic_now
        self._pending: dict[str, _PendingActivityCancellation] = {}
        self._events: Queue[ForegroundEvent] = Queue()
        self._lock = RLock()

    def create_cancellation_id(self) -> str:
        """创建一笔可用于 prepare 的稳定取消请求编号。

        Returns:
            非空请求编号；工厂返回非法值时返回空字符串。
        """

        cancellation_id = self._cancellation_id_factory()
        return cancellation_id.strip() if isinstance(cancellation_id, str) else ""

    def submit_and_wait(
        self,
        *,
        cancellation_id: str,
        activity_id: str,
    ) -> dict[str, object]:
        """提交一笔取消请求，并等待主进程返回权威接收事实。

        这个入口只允许在工具完成事件越过提交边界后调用，因此不会读取 Foreground
        generation 的取消信号。用户打断后续回复不能撤回已经正式提交的取消意图。

        Args:
            cancellation_id: prepare 阶段已经冻结的取消请求编号。
            activity_id: 本次精确取消的 Activity 编号。

        Returns:
            取消请求已接收、被拒绝或回执超时的结构化安全事实。
        """

        normalized_cancellation_id = cancellation_id.strip()
        normalized_activity_id = activity_id.strip()
        if not normalized_cancellation_id or not normalized_activity_id:
            return _rejected_result(
                normalized_activity_id,
                reason="invalid_cancellation_request",
                safe_message="这次取消请求无效，任务没有被取消。",
            )
        pending = _PendingActivityCancellation(
            cancellation_id=normalized_cancellation_id,
            activity_id=normalized_activity_id,
        )
        with self._lock:
            if normalized_cancellation_id in self._pending:
                return _rejected_result(
                    normalized_activity_id,
                    reason="duplicate_cancellation_request",
                    safe_message="这次取消请求没有成功提交。",
                )
            self._pending[normalized_cancellation_id] = pending

        self._events.put(
            ForegroundEvent(
                type="llm.foreground.activity_cancellation_requested",
                payload={
                    "cancellation_id": normalized_cancellation_id,
                    "activity_id": normalized_activity_id,
                },
            )
        )
        if self._event_notifier is not None:
            self._event_notifier()

        deadline = self._monotonic_now() + self._timeout_seconds
        while True:
            remaining_seconds = deadline - self._monotonic_now()
            if remaining_seconds <= 0:
                with self._lock:
                    current = self._pending.pop(normalized_cancellation_id, None)
                    if current is pending and pending.result is not None:
                        return dict(pending.result)
                return _rejected_result(
                    normalized_activity_id,
                    reason="cancellation_receipt_timeout",
                    safe_message=(
                        "这次取消请求没有及时确认，请查看任务当前状态。"
                    ),
                )
            pending.completed.wait(
                timeout=min(_POLL_INTERVAL_SECONDS, remaining_seconds)
            )
            with self._lock:
                current = self._pending.get(normalized_cancellation_id)
                if current is not pending or pending.result is None:
                    continue
                result = dict(pending.result)
                self._pending.pop(normalized_cancellation_id, None)
                return result

    def apply_result(self, command: ActivityCancellationResultCommand) -> bool:
        """应用主进程返回的一笔 Activity 取消回执。

        Args:
            command: 同时携带取消请求编号和 Activity 编号的控制命令。

        Returns:
            回执仍对应当前等待项并成功唤醒时返回真。
        """

        with self._lock:
            pending = self._pending.get(command.cancellation_id)
            if (
                pending is None
                or pending.activity_id != command.activity_id.strip()
                or pending.result is not None
            ):
                return False
            pending.result = {
                "state": command.state,
                "activity_id": command.activity_id,
                "reason": command.reason,
                "safe_message": command.safe_message,
            }
            pending.completed.set()
            return True

    def drain_events(self, limit: int = 100) -> list[ForegroundEvent]:
        """非阻塞排出等待主进程处理的取消请求事件。

        Args:
            limit: 本次最多读取的事件数。

        Returns:
            保持产生顺序的内部 Foreground 事件。
        """

        events: list[ForegroundEvent] = []
        for _ in range(max(0, limit)):
            try:
                events.append(self._events.get_nowait())
            except Empty:
                break
        return events


def _rejected_result(
    activity_id: str,
    *,
    reason: str,
    safe_message: str,
) -> dict[str, object]:
    """构造取消请求没有得到权威接收时的稳定事实。

    Args:
        activity_id: 本次尝试取消的 Activity 编号。
        reason: 供程序判断的稳定拒绝原因。
        safe_message: Foreground 可以直接转述的安全说明。

    Returns:
        不会声称任务已经取消的结构化拒绝事实。
    """

    return {
        "state": "rejected",
        "activity_id": activity_id,
        "reason": reason,
        "safe_message": safe_message,
    }
