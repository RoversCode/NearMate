from __future__ import annotations

import queue
import threading
from collections import deque
from collections.abc import Callable
from typing import Generic, TypeVar


T = TypeVar("T")


class NotifyQueue(queue.Queue, Generic[T]):
    """带通知能力的线程队列。

    Args:
        maxsize: 队列最大容量，0 表示不限制容量。

    Returns:
        NotifyQueue: 兼容标准 Queue 的通知队列实例。
    """

    def __init__(self, maxsize: int = 0) -> None:
        """初始化队列和数据可用事件。"""

        super().__init__(maxsize=maxsize)
        # 数据可用事件用于唤醒等待消费者，避免空队列轮询。
        self._data_available = threading.Event()

    def put(self, item: T, block: bool = True, timeout: float | None = None) -> None:
        """放入一条数据并通知等待消费者。

        Args:
            item: 待入队数据。
            block: 队列满时是否阻塞。
            timeout: 阻塞等待的最长秒数。

        Returns:
            None: 数据成功入队后无返回值。
        """

        super().put(item, block=block, timeout=timeout)
        self._data_available.set()

    def put_nowait(self, item: T) -> None:
        """以非阻塞方式放入一条数据。

        Args:
            item: 待入队数据。

        Returns:
            None: 数据成功入队后无返回值。
        """

        self.put(item, block=False)

    def get(self, block: bool = True, timeout: float | None = None) -> T:
        """读取一条数据并在队列耗尽时清理通知状态。

        Args:
            block: 队列为空时是否阻塞。
            timeout: 阻塞等待的最长秒数。

        Returns:
            T: 队列中取出的数据。
        """

        item = super().get(block=block, timeout=timeout)
        self._clear_event_if_empty()
        return item

    def get_nowait(self) -> T:
        """以非阻塞方式读取一条数据。

        Returns:
            T: 队列中取出的数据。
        """

        return self.get(block=False)

    def wait_and_get(self, timeout: float | None = None) -> T | None:
        """等待数据可用并读取一条数据。

        Args:
            timeout: 等待数据的最长秒数，None 表示一直等待。

        Returns:
            T | None: 成功取到数据时返回数据，超时或竞态取空时返回 None。
        """

        if not self._data_available.wait(timeout=timeout):
            return None

        try:
            return self.get_nowait()
        except queue.Empty:
            # 其它消费者可能已经抢先取走最后一条数据，这里保持等待接口的宽容语义。
            self._clear_event_if_empty()
            return None

    def wait_for_data(self, timeout: float | None = None) -> bool:
        """等待队列出现数据但不消费数据。

        Args:
            timeout: 等待数据的最长秒数，None 表示一直等待。

        Returns:
            bool: 等待期间观察到队列非空时返回 True，否则返回 False。
        """

        if not self._data_available.wait(timeout=timeout):
            return False
        if self.empty():
            self._data_available.clear()
            return False
        return True

    def clear(self) -> None:
        """清空当前队列并重置通知状态。

        Returns:
            None: 清空操作完成后无返回值。
        """

        with self.mutex:
            self.queue.clear()
            self.unfinished_tasks = 0
            self.all_tasks_done.notify_all()
            self.not_full.notify_all()
        self._data_available.clear()

    def remove_matching(self, predicate: Callable[[T], bool]) -> list[T]:
        """移除所有满足条件的待消费数据。

        Args:
            predicate: 判断队列项是否需要被移除的函数。

        Returns:
            被移除的队列项，顺序与原队列一致。
        """

        removed: list[T] = []
        with self.mutex:
            kept: deque[T] = deque()
            for item in self.queue:
                if predicate(item):
                    removed.append(item)
                    continue
                kept.append(item)
            if not removed:
                return []

            # remove_matching 只处理尚未被消费者取出的待处理项，因此同步扣减未完成任务计数。
            self.queue.clear()
            self.queue.extend(kept)
            self.unfinished_tasks = max(0, self.unfinished_tasks - len(removed))
            self.all_tasks_done.notify_all()
            self.not_full.notify_all()

        if kept:
            self._data_available.set()
        else:
            self._data_available.clear()
        return removed

    def notify(self) -> None:
        """按当前队列状态刷新通知事件。

        Returns:
            None: 只更新通知事件，不移动队列数据。
        """

        self._data_available.set()

    def _clear_event_if_empty(self) -> None:
        """队列为空时清理数据可用事件。"""

        if self.empty():
            self._data_available.clear()
