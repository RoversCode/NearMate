from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock


CancellationCallback = Callable[[], None]


class CancellationToken:
    """提供可等待、可查询且能主动关闭底层资源的取消信号。"""

    def __init__(self) -> None:
        """初始化尚未取消的 token。"""

        self._event = Event()
        self._lock = Lock()
        self._callbacks: dict[int, CancellationCallback] = {}
        self._next_callback_id = 1
        self._reason = ""

    @property
    def reason(self) -> str:
        """返回首次设置的取消原因。"""

        with self._lock:
            return self._reason

    def is_set(self) -> bool:
        """判断 token 是否已经取消。"""

        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """等待取消发生。

        Args:
            timeout: 最长等待秒数；为空时一直等待。

        Returns:
            等待期间发生取消时返回真。
        """

        return self._event.wait(timeout)

    def set(self, reason: str = "cancelled") -> bool:
        """设置取消状态并调用当前登记的资源关闭回调。

        Args:
            reason: 首次取消的业务原因。

        Returns:
            本次调用首次完成状态切换时返回真。
        """

        normalized_reason = reason.strip() or "cancelled"
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = normalized_reason
            self._event.set()
            callbacks = tuple(self._callbacks.values())
            self._callbacks.clear()

        # 关闭网络流可能触发 SDK 内部锁，不能在 token 自身锁内调用。
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # 取消必须继续传播；单个资源的关闭异常由资源拥有者在 finally 中再次兜底。
                continue
        return True

    def cancel(self, reason: str) -> bool:
        """使用显式业务原因设置取消状态。

        Args:
            reason: generation、deadline 或 shutdown 等取消原因。

        Returns:
            本次调用首次完成状态切换时返回真。
        """

        return self.set(reason)

    def register(self, callback: CancellationCallback) -> Callable[[], None]:
        """登记取消时需要执行的资源关闭回调。

        Args:
            callback: 取消发生时执行的无参函数。

        Returns:
            可幂等撤销本次登记的函数。
        """

        with self._lock:
            if self._event.is_set():
                callback_id = None
            else:
                callback_id = self._next_callback_id
                self._next_callback_id += 1
                self._callbacks[callback_id] = callback

        if callback_id is None:
            callback()
            return lambda: None

        def unregister() -> None:
            """撤销尚未触发的回调登记。"""

            with self._lock:
                self._callbacks.pop(callback_id, None)

        return unregister


class CombinedCancellationSignal:
    """把两个独立取消域合并成 LLM 流需要的只读信号。

    Args:
        primary: 主输出域取消信号。
        secondary: 用来停止模型决策、但不应直接关闭可见输出的信号。
    """

    def __init__(
        self,
        primary: CancellationToken,
        secondary: CancellationToken,
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    @property
    def reason(self) -> str:
        """返回最先触发的取消原因。"""

        return self._primary.reason or self._secondary.reason

    def is_set(self) -> bool:
        """判断任一取消域是否已经触发。"""

        return self._primary.is_set() or self._secondary.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """等待任一取消域触发，兼容需要等待接口的模型客户端。"""

        # 不能先等待 primary 再检查 secondary，否则 deadline 只取消模型流时，
        # provider 可能要等完整 timeout 才发现辅助取消，导致 Recovery fallback 卡住。
        if self.is_set():
            return True
        wake_event = Event()
        unregister = self.register(wake_event.set)
        try:
            return wake_event.wait(timeout)
        finally:
            unregister()

    def register(self, callback: CancellationCallback) -> Callable[[], None]:
        """在任一取消域触发时执行一次底层流关闭回调。"""

        callback_lock = Lock()
        called = False

        def call_once() -> None:
            nonlocal called
            with callback_lock:
                if called:
                    return
                called = True
            callback()

        unregister_primary = self._primary.register(call_once)
        unregister_secondary = self._secondary.register(call_once)

        def unregister() -> None:
            unregister_primary()
            unregister_secondary()

        return unregister
