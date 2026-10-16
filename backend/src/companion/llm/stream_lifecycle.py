from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from threading import Lock
from typing import Protocol, TypeVar

from companion.observability.logging_config import get_backend_logger


T = TypeVar("T")
logger = get_backend_logger(__name__)


class CancellationSignal(Protocol):
    """定义 LLM 流生命周期需要的最小取消信号。"""

    def is_set(self) -> bool:
        """判断是否已经取消。"""


def resolve_cancellation_reason(
    cancellation_signal: object | None,
    *,
    fallback: str,
) -> str:
    """读取取消信号携带的业务原因。

    Args:
        cancellation_signal: CancellationToken、Event 或空值。
        fallback: 信号没有声明原因时使用的稳定文案。

    Returns:
        已去除首尾空白的业务取消原因。
    """

    reason = getattr(cancellation_signal, "reason", "")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return fallback


def iter_cancellable_stream(
    stream: Iterable[T],
    cancellation_signal: CancellationSignal | None,
) -> Iterator[T]:
    """迭代 SDK 流，并在取消或结束时主动关闭底层连接。

    Args:
        stream: Chat Completions SDK 返回的流对象。
        cancellation_signal: 可选 generation 或后台任务取消信号。

    Yields:
        底层流按原顺序产生的事件。
    """

    close_stream = getattr(stream, "close", None)
    close_lock = Lock()
    stream_closed = False

    def close_once() -> None:
        """保证取消回调与 finally 竞争时底层流只关闭一次。"""

        nonlocal stream_closed
        if not callable(close_stream):
            return
        with close_lock:
            if stream_closed:
                return
            try:
                close_stream()
            except Exception:
                # 取消回调失败后保留重试资格，finally 会再次尝试，但关闭异常不污染回复主链路。
                logger.warning("关闭底层 LLM 流失败", exc_info=True)
                return
            stream_closed = True

    unregister: Callable[[], None] = lambda: None
    register = getattr(cancellation_signal, "register", None)
    if callable(register) and callable(close_stream):
        unregister = register(close_once)

    stream_iterator = iter(stream)
    try:
        while True:
            if cancellation_signal is not None and cancellation_signal.is_set():
                return

            try:
                item = next(stream_iterator)
            except StopIteration:
                return
            except Exception:
                # 主动关闭与阻塞读取存在竞态；已取消时，源流异常只是取消收尾的一部分。
                if cancellation_signal is not None and cancellation_signal.is_set():
                    return
                raise

            if cancellation_signal is not None and cancellation_signal.is_set():
                return
            yield item
    finally:
        unregister()
        close_once()
