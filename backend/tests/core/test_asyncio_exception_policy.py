from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from companion.core.asyncio_exception_policy import (
    install_backend_asyncio_exception_policy,
)


class _ProactorBasePipeTransport:
    """提供和 asyncio Proactor 回调同名的测试句柄。"""

    def _call_connection_lost(self, _exception: object) -> None:
        """模拟 Windows transport 的连接回收回调。"""


def _winerror_10054() -> ConnectionResetError:
    """构造 Windows 远端强制断开错误。"""

    exception = ConnectionResetError("远程主机强迫关闭了一个现有的连接")
    exception.winerror = 10054  # type: ignore[attr-defined]
    return exception


def test_policy_suppresses_only_proactor_peer_reset() -> None:
    """精确的 Proactor 10054 回收噪声应被吞掉。"""

    loop = asyncio.new_event_loop()
    try:
        restore = install_backend_asyncio_exception_policy(loop)
        handler = loop.get_exception_handler()
        assert handler is not None
        default_handler = Mock()
        loop.default_exception_handler = default_handler  # type: ignore[method-assign]

        callback = _ProactorBasePipeTransport()._call_connection_lost
        handler(
            loop,
            {
                "exception": _winerror_10054(),
                "handle": SimpleNamespace(_callback=callback),
            },
        )

        default_handler.assert_not_called()
        restore()
    finally:
        loop.close()


def test_policy_delegates_unrelated_exception() -> None:
    """其它异常必须继续交给事件循环默认处理器。"""

    loop = asyncio.new_event_loop()
    try:
        restore = install_backend_asyncio_exception_policy(loop)
        handler = loop.get_exception_handler()
        assert handler is not None
        default_handler = Mock()
        loop.default_exception_handler = default_handler  # type: ignore[method-assign]

        handler(loop, {"exception": RuntimeError("真实故障")})

        default_handler.assert_called_once()
        restore()
    finally:
        loop.close()
