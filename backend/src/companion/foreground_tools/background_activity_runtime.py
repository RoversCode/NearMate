"""为后台活动提供共享执行资源、事件账本和关闭屏障。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor, wait
from typing import Any

from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


class BackgroundActivityRuntime:
    """统一管理后台活动的执行器、任务账本、事件队列和关闭屏障。

    领域执行器只负责自己的活动语义；本类负责线程任务与事件的通用生命周期。
    关闭开始后不再接受新任务或新事件，避免迟到输出进入已经失效的活动边界。
    """

    def __init__(
        self,
        *,
        executor: Executor | None = None,
        max_workers: int = 2,
        thread_name_prefix: str,
        event_notifier: Callable[[], None] | None = None,
    ) -> None:
        """初始化后台活动共用的运行资源。

        Args:
            executor: 可选的外部执行器；未提供时由当前运行时创建并负责关闭。
            max_workers: 自建线程池的最大工作线程数。
            thread_name_prefix: 自建线程池的线程名称前缀。
            event_notifier: 新事件已入账后用于唤醒外层排水循环的旁路回调。
        """

        if max_workers <= 0:
            raise ValueError("max_workers 必须大于零")
        normalized_prefix = thread_name_prefix.strip()
        if not normalized_prefix:
            raise ValueError("thread_name_prefix 不能为空")

        self._executor = executor or ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=normalized_prefix,
        )
        self._owns_executor = executor is None
        self._event_notifier = event_notifier
        self._lock = threading.Lock()
        # 事件先进入受锁保护的账本，再由领域所有者按预算排水。
        self._events: list[dict[str, object]] = []
        # 只登记已成功提交的标准 Future，供关闭时取消和等待。
        self._futures: set[Future[Any]] = set()
        # submit 已越过关闭检查、但 Future 尚未登记的次数，用来封住关闭竞态。
        self._submissions_in_progress = 0
        self._closed = False

    def submit(self, function: Callable[..., object], *args: object) -> None:
        """提交一项后台工作，并封住 Future 登记前的关闭竞态。

        ``submit`` 不能在持锁状态下调用底层执行器，因为测试执行器或自定义执行器
        可能同步运行工作函数；计数器保证关闭操作知道是否仍有提交正在完成登记。

        Args:
            function: 需要在执行器中运行的工作函数。
            *args: 原样传给工作函数的位置参数。

        Raises:
            RuntimeError: Runtime 已关闭或底层执行器拒绝任务时抛出。
        """

        with self._lock:
            if self._closed:
                raise RuntimeError("后台 Activity Runtime 已关闭")
            self._submissions_in_progress += 1

        submitted_future: Future[Any] | None = None
        try:
            future = self._executor.submit(function, *args)
            # 部分同步测试执行器不返回标准 Future，此时没有可取消的后台句柄。
            if isinstance(future, Future):
                submitted_future = future
        finally:
            self._complete_submission(submitted_future)

    def append_event(self, event: dict[str, object]) -> bool:
        """追加一条事件，并在成功入账后尝试唤醒外层排水循环。

        Args:
            event: 已完成来源标准化的内部 Activity 事件。

        Returns:
            事件成功进入账本时返回真；Runtime 关闭后返回假。
        """

        with self._lock:
            if self._closed:
                return False
            self._events.append(event)

        if self._event_notifier is None:
            return True
        try:
            self._event_notifier()
        except Exception:
            # 事件已进入权威账本；唤醒只是加速消费，失败不能反向撤销领域事实。
            logger.warning(
                "后台 Activity 事件唤醒失败 event_type=%s",
                event.get("type"),
                exc_info=True,
            )
        return True

    def drain_events(self, limit: int = 100) -> list[dict[str, object]]:
        """按产生顺序取出预算内的后台活动事件。

        Args:
            limit: 本次最多取出的事件数量。

        Returns:
            当前预算内的事件；非正预算返回空列表。
        """

        if limit <= 0:
            return []
        with self._lock:
            events = self._events[:limit]
            del self._events[:limit]
        return events

    def clear_events(self) -> None:
        """清空尚未排水的事件，供领域所有者关闭时丢弃无效输出。"""

        with self._lock:
            self._events.clear()

    def shutdown(
        self,
        timeout: float = 1.0,
        *,
        preserve_pending_events: bool = False,
    ) -> None:
        """关闭提交入口、取消已知任务，并等待自建执行器退出。

        关闭先在线程安全边界内摘除事件和任务账本，再在锁外取消任务与关闭执行器。
        这样取消回调即使同步触发，也无法重新写入已关闭的运行时。

        Args:
            timeout: 等待已登记 Future 的最长秒数。
            preserve_pending_events: 关闭前已经写入的补偿终态是否保留给外层排水。
        """

        deadline = time.monotonic() + max(timeout, 0.0)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = tuple(self._futures)
            has_pending_submission = self._submissions_in_progress > 0
            if not preserve_pending_events:
                self._events.clear()
            self._futures.clear()

        for future in futures:
            future.cancel()
        unfinished_futures: set[Future[Any]] = set(futures)
        if futures:
            _finished, unfinished_futures = wait(
                futures,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        if self._owns_executor and isinstance(self._executor, ThreadPoolExecutor):
            # Python 无法强制杀死运行中的线程。超过优雅等待预算后，进程级边界负责兜底。
            self._executor.shutdown(
                wait=not unfinished_futures and not has_pending_submission,
                cancel_futures=True,
            )

    def _complete_submission(self, future: Future[Any] | None) -> None:
        """关闭一次提交窗口，并登记成功返回的任务句柄。

        Args:
            future: 底层执行器返回的标准 Future；同步测试执行器可返回空。
        """

        with self._lock:
            self._submissions_in_progress -= 1
            if self._submissions_in_progress < 0:
                raise RuntimeError("后台 Activity submit 计数不能小于零")
            if future is None:
                return
            if self._closed:
                # 关闭抢先发生时不能把新任务留在无人管理的账本外。
                should_cancel = True
            else:
                self._futures.add(future)
                should_cancel = False
        if should_cancel:
            future.cancel()
            return
        future.add_done_callback(self._discard_future)

    def _discard_future(self, future: Future[Any]) -> None:
        """从运行账本移除已结束的任务句柄。

        Args:
            future: 已经进入完成态的后台工作。
        """

        with self._lock:
            self._futures.discard(future)
