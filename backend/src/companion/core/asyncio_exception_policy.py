from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


def _is_windows_peer_reset(context: dict[str, Any]) -> bool:
    """判断是否是 Windows Proactor 回收已断开连接时产生的固定噪声。

    Args:
        context: asyncio 事件循环传入的异常上下文。

    Returns:
        仅当异常是 WinError 10054 且来自 Proactor 的连接回收回调时返回 True。
    """

    exception = context.get("exception")
    if not isinstance(exception, ConnectionResetError):
        return False
    if getattr(exception, "winerror", None) != 10054:
        return False

    handle = context.get("handle")
    callback = getattr(handle, "_callback", None)
    callback_name = getattr(callback, "__qualname__", "")
    return callback_name == "_ProactorBasePipeTransport._call_connection_lost"


def install_backend_asyncio_exception_policy(
    loop: asyncio.AbstractEventLoop | None = None,
) -> Callable[[], None]:
    """安装 Backend 事件循环的窄异常策略。

    Windows 客户端主动断开 WebSocket 后，Proactor 会在 transport 清理回调中
    抛出 WinError 10054。它不代表 Backend 请求失败，也不应污染终端；其它
    未知异常仍交给原有处理器，避免把真实故障隐藏掉。

    Args:
        loop: 要配置的事件循环；为空时使用当前正在运行的循环。

    Returns:
        调用后可恢复原异常处理器的无参函数。
    """

    target_loop = loop or asyncio.get_running_loop()
    previous_handler = target_loop.get_exception_handler()

    def handle_exception(
        current_loop: asyncio.AbstractEventLoop,
        context: dict[str, Any],
    ) -> None:
        """处理单个事件循环异常上下文。"""

        if _is_windows_peer_reset(context):
            return
        if previous_handler is not None:
            previous_handler(current_loop, context)
            return
        current_loop.default_exception_handler(context)

    target_loop.set_exception_handler(handle_exception)

    def restore() -> None:
        """恢复安装策略之前的异常处理器。"""

        target_loop.set_exception_handler(previous_handler)

    return restore
