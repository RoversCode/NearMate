"""按绝对时刻激活提醒，并唤醒独立串行交付协调器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from threading import RLock

from companion.observability.logging_config import get_backend_logger
from companion.timed_reminders.service import TimedReminderService


logger = get_backend_logger(__name__)


class TimedReminderScheduler:
    """只判断时间和推进 ``scheduled -> ringing``，不构造助手请求。

    Args:
        service: 提供下一时刻和到点激活的提醒服务。
        notify_ready: 告知 Delivery Coordinator 重新读取可响任务的同步端口。
        now: 调度等待使用的带时区当前时间函数。
    """

    def __init__(
        self,
        *,
        service: TimedReminderService,
        notify_ready: Callable[[], None],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._service = service
        self._notify_ready = notify_ready
        self._now = now
        self._wake_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = RLock()
        self._stopping = False

    def start(self) -> None:
        """在当前事件循环启动唯一调度任务。"""

        with self._lifecycle_lock:
            if self._task is not None and not self._task.done():
                return
            self._loop = asyncio.get_running_loop()
            self._wake_event = asyncio.Event()
            self._stopping = False
            self._task = asyncio.create_task(
                self._run(),
                name="timed-reminder-scheduler",
            )
        self.wake()

    def wake(self) -> None:
        """从任意线程通知调度器重新计算最近时刻。"""

        with self._lifecycle_lock:
            loop = self._loop
            wake_event = self._wake_event
        if loop is None or wake_event is None or loop.is_closed():
            return

        def set_event() -> None:
            """在事件所属循环内置位。"""

            wake_event.set()

        try:
            if asyncio.get_running_loop() is loop:
                set_event()
            else:
                loop.call_soon_threadsafe(set_event)
        except RuntimeError:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(set_event)

    async def shutdown(self) -> None:
        """停止调度循环并等待其退出。"""

        self._stopping = True
        self.wake()
        with self._lifecycle_lock:
            task = self._task
            self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        with self._lifecycle_lock:
            self._loop = None
            self._wake_event = None

    async def _run(self) -> None:
        """持续激活到点任务，并等待下一时刻或外部状态变化。"""

        while not self._stopping:
            wake_event = self._wake_event
            if wake_event is None:
                return
            # 先消费旧电平，再读取权威状态。状态检查期间到达的新 wake 会保持置位，
            # 后续 wait 会立即返回，不会落入 timeout 与 clear 之间的丢通知窗口。
            wake_event.clear()
            try:
                checked_at = self._now()
                activated = await self._service.activate_due()
                if activated or self._service.ready_to_ring():
                    self._notify_ready()
                await self._wait_until_next_change(checked_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 仓储或广播故障不应杀死当前运行期唯一的调度任务。
                logger.exception("激活定时提醒失败")
                await self._wait_for_wake(timeout=1.0)

    async def _wait_until_next_change(self, checked_at: datetime) -> None:
        """等待下一次时间变化，不让已到期的待播项遮挡后续提醒。

        Args:
            checked_at: 本轮激活和待播检查开始前的时间。
        """

        next_wake_at = self._service.next_wake_at(checked_at)
        if next_wake_at is None:
            await self._wait_for_wake(timeout=None)
            return
        now = self._now()
        delay = (next_wake_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
        if delay <= 0:
            # 检查期间刚越过的时刻尚未被完整处理，立即进入下一轮，不能等待外部通知。
            return
        await self._wait_for_wake(timeout=delay)

    async def _wait_for_wake(self, *, timeout: float | None) -> None:
        """等待新电平或 deadline，电平只由下一轮状态检查消费。"""

        wake_event = self._wake_event
        if wake_event is None:
            return
        if timeout is None:
            await wake_event.wait()
        else:
            with suppress(TimeoutError):
                await asyncio.wait_for(wake_event.wait(), timeout=timeout)
