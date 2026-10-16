"""提供进程级单线程 deadline 调度与进度退避策略。"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


ACTIVITY_PROGRESS_INTERVALS_S = (30.0, 60.0)
TASK_RESULT_PROGRESS_INTERVALS_S = (25.0, 30.0, 60.0, 90.0)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressIntervalPolicy:
    """定义每次 Progress 定时检查之间的有界退避间隔。

    Args:
        intervals_s: 从首次到稳定上限的正数间隔序列。
    """

    intervals_s: tuple[float, ...]

    def __post_init__(self) -> None:
        """校验策略至少包含一个有效正数间隔。"""

        if not self.intervals_s or any(interval <= 0 for interval in self.intervals_s):
            raise ValueError("进度间隔必须是非空正数序列")

    def interval_for(self, completed_check_count: int) -> float:
        """返回下一次检查应等待的间隔。

        Args:
            completed_check_count: 当前活动已经完成的定时检查次数。

        Returns:
            对应退避档位；超过序列后保持最后一个间隔。
        """

        index = min(max(completed_check_count, 0), len(self.intervals_s) - 1)
        return self.intervals_s[index]


class ScheduledProgressCall(Protocol):
    """定义进度协调器持有的可取消 deadline 句柄。"""

    def cancel(self) -> None:
        """取消尚未执行的回调。"""


class ProgressScheduler(Protocol):
    """定义进度协调器所依赖的最小 deadline 调度接口。"""

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> ScheduledProgressCall:
        """在指定延迟后安排一次短回调。

        Args:
            delay_seconds: 回调执行前的等待秒数。
            callback: 到期后执行的短回调。

        Returns:
            可取消本次调度的句柄。
        """


@dataclass(order=True)
class _ScheduledEntry:
    """保存调度堆中的一次 deadline。"""

    deadline: float
    sequence: int
    callback: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class _ScheduledHandle:
    """把单条调度记录暴露为线程安全取消句柄。"""

    def __init__(self, scheduler: ActivityProgressScheduler, entry: _ScheduledEntry) -> None:
        """初始化单条调度记录的取消句柄。

        Args:
            scheduler: 拥有调度记录的共享调度器。
            entry: 当前句柄对应的调度堆记录。
        """
        self._scheduler = scheduler
        self._entry = entry

    def cancel(self) -> None:
        """取消当前记录并唤醒调度线程重算最近 deadline。"""

        self._scheduler._cancel(self._entry)


class ActivityProgressScheduler:
    """用单个条件变量线程调度全部活动的短 deadline 回调。"""

    def __init__(
        self,
        *,
        monotonic_now: Callable[[], float] = time.monotonic,
        autostart: bool = True,
    ) -> None:
        """初始化共享调度器。

        Args:
            monotonic_now: 可替换的单调时钟。
            autostart: 是否立即启动真实守护线程；单元测试可关闭。
        """

        self._monotonic_now = monotonic_now
        self._condition = threading.Condition()
        self._entries: list[_ScheduledEntry] = []
        self._next_sequence = 1
        self._stopped = False
        self._thread: threading.Thread | None = None
        if autostart:
            self._thread = threading.Thread(
                target=self._run,
                name="activity-progress-scheduler",
                daemon=True,
            )
            self._thread.start()

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> ScheduledProgressCall:
        """安排一次不会阻塞调度线程的短回调。

        Args:
            delay_seconds: 从当前单调时间开始等待的秒数。
            callback: 到期后执行的无参短回调。

        Returns:
            可由任务终态取消的调度句柄。
            
        interval,
        lambda current_record=record: self._on_reasoning_deadline(current_record),
        
        """

        if delay_seconds < 0:
            raise ValueError("delay_seconds 不能为负数")
        with self._condition:
            if self._stopped:
                raise RuntimeError("进度调度器已经停止")
            entry = _ScheduledEntry(
                deadline=self._monotonic_now() + delay_seconds,
                sequence=self._next_sequence,
                callback=callback,
            )
            self._next_sequence += 1
            # heapq 管理后，self._entries 这个列表的第 0 个元素（堆顶），
            # 永远保证是全场 deadline 最小、也就是最先到期、最迫切需要被执行的那个任务。
            heapq.heappush(self._entries, entry)
            self._condition.notify_all()
        return _ScheduledHandle(self, entry)

    def run_due(self) -> int:
        """同步执行当前已经到期的回调，供 fake clock 测试使用。

        Returns:
            本次实际执行的非取消回调数量。
        """

        callbacks = self._take_due_callbacks()
        for callback in callbacks:
            callback()
        return len(callbacks)

    def shutdown(self, timeout: float = 1.0) -> None:
        """停止调度并等待守护线程退出。

        Args:
            timeout: 最长等待线程退出的秒数。
        """

        with self._condition:
            self._stopped = True
            self._entries.clear()
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=max(timeout, 0.0))

    def _cancel(self, entry: _ScheduledEntry) -> None:
        """原子取消指定调度记录。

        Args:
            entry: 需要取消的调度堆记录。
        """

        with self._condition:
            entry.cancelled = True
            self._condition.notify_all()

    def _run(self) -> None:
        """等待最近 deadline，并在锁外执行到期回调。"""

        while True:
            callbacks: list[Callable[[], None]] = []
            with self._condition:
                while not self._stopped:
                    self._discard_cancelled_head()  # 清除取消的闹钟
                    if not self._entries:
                        self._condition.wait()  # 没有闹钟就等待
                        continue
                    wait_seconds = self._entries[0].deadline - self._monotonic_now()
                    if wait_seconds > 0:
                        self._condition.wait(timeout=wait_seconds)
                        continue
                    # 拿到所有要响起的闹中国，跑到下面的遍历去执行
                    callbacks = self._take_due_callbacks_locked()
                    break
                if self._stopped:
                    return
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    # 单个活动的异常不能终止进程级共享调度线程。
                    logger.exception("异步活动进度回调执行失败")

    def _take_due_callbacks(self) -> list[Callable[[], None]]:
        """在线程安全边界内取出当前全部到期回调。

        Returns:
            已到期且尚未取消的回调列表。
        """

        with self._condition:
            return self._take_due_callbacks_locked()

    def _take_due_callbacks_locked(self) -> list[Callable[[], None]]:
        """现在已经到点了、而且没被取消”的所有闹钟，一次性从闹钟堆里拿出来，整理成一个待执行函数列表。

        Returns:
            从调度堆移除的到期回调列表。
        """

        callbacks: list[Callable[[], None]] = []
        current_time = self._monotonic_now()
        while self._entries and self._entries[0].deadline <= current_time:
            entry = heapq.heappop(self._entries)
            if not entry.cancelled:
                callbacks.append(entry.callback)
        return callbacks

    def _discard_cancelled_head(self) -> None:
        """清除堆顶连续的取消项，避免无意义等待旧 deadline。"""

        while self._entries and self._entries[0].cancelled:
            heapq.heappop(self._entries)
