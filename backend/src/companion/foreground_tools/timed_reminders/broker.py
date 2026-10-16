"""把子进程定时提醒命令交给主进程并同步等待权威回执。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, RLock
from typing import Literal
from uuid import uuid4

from companion.llm.foreground_models import ForegroundEvent
from companion.realtime.assistant_events import TimedReminderCommandResultCommand


TimedReminderCommandOperation = Literal["create", "update", "cancel", "dismiss"]
_POLL_INTERVAL_SECONDS = 0.05


@dataclass(slots=True)
class _PendingTimedReminderCommand:
    """保存一笔正在等待主进程回执的提醒命令。

    Args:
        command_id: 本次写命令的唯一编号。
        operation: 创建、修改或取消动作。
        completed: 主进程回执到达时唤醒等待线程的事件。
        result: 主进程返回的 canonical 提醒事实。
    """

    command_id: str
    operation: TimedReminderCommandOperation
    completed: Event = field(default_factory=Event)
    result: dict[str, object] | None = None


class TimedReminderCommandBroker:
    """同步等待主进程完成一笔定时提醒写命令。

    Args:
        event_notifier: 新事件入队后唤醒助手子进程主循环的回调。
        timeout_seconds: 一笔命令等待主进程回执的最长秒数。
        command_id_factory: 生成命令编号的函数。
        monotonic_now: 计算等待截止时间的单调时钟。
    """

    def __init__(
        self,
        *,
        event_notifier: Callable[[], None] | None = None,
        timeout_seconds: float = 5.0,
        command_id_factory: Callable[[], str] = lambda: uuid4().hex,
        monotonic_now: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("定时提醒命令回执等待时间必须大于零")
        self._event_notifier = event_notifier
        self._timeout_seconds = timeout_seconds
        self._command_id_factory = command_id_factory
        self._monotonic_now = monotonic_now
        self._pending: dict[str, _PendingTimedReminderCommand] = {}
        self._events: Queue[ForegroundEvent] = Queue()
        self._lock = RLock()

    def create_command_id(self) -> str:
        """创建一笔可用于 prepare 的稳定命令编号。

        Returns:
            非空命令编号；工厂返回非法值时返回空字符串。
        """

        command_id = self._command_id_factory()
        return command_id.strip() if isinstance(command_id, str) else ""

    def submit_and_wait(
        self,
        *,
        command_id: str,
        operation: TimedReminderCommandOperation,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        """提交一笔提醒命令，并等待主进程返回最终权威事实。

        Args:
            command_id: prepare 阶段已经冻结的命令编号。
            operation: 创建、修改或取消动作。
            arguments: 已在子进程完成结构校验的命令参数。

        Returns:
            主进程完成的 canonical 结果，或不会虚构成功的安全失败事实。
        """

        normalized_command_id = command_id.strip()
        if not normalized_command_id or operation not in {"create", "update", "cancel", "dismiss"}:
            return _rejected_result(
                reason="invalid_timed_reminder_command",
                safe_message="这次提醒操作无效，没有改变提醒。",
            )
        pending = _PendingTimedReminderCommand(
            command_id=normalized_command_id,
            operation=operation,
        )
        with self._lock:
            if normalized_command_id in self._pending:
                return _rejected_result(
                    reason="duplicate_timed_reminder_command",
                    safe_message="这次提醒操作没有成功提交。",
                )
            self._pending[normalized_command_id] = pending

        self._events.put(
            ForegroundEvent(
                type="llm.foreground.timed_reminder_command_requested",
                payload={
                    "command_id": normalized_command_id,
                    "operation": operation,
                    "arguments": dict(arguments),
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
                    current = self._pending.pop(normalized_command_id, None)
                    if current is pending and pending.result is not None:
                        return dict(pending.result)
                return _rejected_result(
                    reason="timed_reminder_command_receipt_timeout",
                    safe_message="这次提醒操作没有及时确认，请查看当前提醒。",
                )
            pending.completed.wait(
                timeout=min(_POLL_INTERVAL_SECONDS, remaining_seconds)
            )
            with self._lock:
                current = self._pending.get(normalized_command_id)
                if current is not pending or pending.result is None:
                    continue
                result = dict(pending.result)
                self._pending.pop(normalized_command_id, None)
                return result

    def apply_result(self, command: TimedReminderCommandResultCommand) -> bool:
        """应用主进程返回的一笔提醒命令回执。

        Args:
            command: 同时携带命令编号、动作和 canonical 结果的控制命令。

        Returns:
            回执仍对应当前等待项并成功唤醒时返回真。
        """

        with self._lock:
            pending = self._pending.get(command.command_id)
            if (
                pending is None
                or pending.operation != command.operation
                or pending.result is not None
            ):
                return False
            pending.result = dict(command.result)
            pending.completed.set()
            return True

    def drain_events(self, limit: int = 100) -> list[ForegroundEvent]:
        """非阻塞排出等待主进程处理的提醒命令事件。

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


def _rejected_result(*, reason: str, safe_message: str) -> dict[str, object]:
    """构造未获得主进程成功确认时的稳定事实。

    Args:
        reason: 供程序判断的稳定失败原因。
        safe_message: Foreground 可以直接转述的安全说明。

    Returns:
        不会声称提醒已经改变的结构化失败事实。
    """

    return {
        "state": "rejected",
        "reason": reason,
        "safe_message": safe_message,
    }
