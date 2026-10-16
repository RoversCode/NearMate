from __future__ import annotations

import socket

import pytest

from companion.core.app import LOCAL_DEV_ORIGINS
from companion.core.dev_ports import ensure_port_available, load_dev_ports


def test_shared_dev_ports_and_cors_origins() -> None:
    """共享端口契约同时驱动 Backend 默认端点和两个开发前端来源。"""

    ports = load_dev_ports()
    assert ports["backend"].port == 8765
    assert "http://127.0.0.1:5173" in LOCAL_DEV_ORIGINS
    assert "http://127.0.0.1:5174" in LOCAL_DEV_ORIGINS
    assert "http://127.0.0.1:5175" not in LOCAL_DEV_ORIGINS


def test_ensure_port_available_rejects_existing_listener() -> None:
    """服务启动前的 bind 预检应拒绝已经被监听的端口。"""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with pytest.raises(OSError):
            ensure_port_available("127.0.0.1", port)
    finally:
        listener.close()
