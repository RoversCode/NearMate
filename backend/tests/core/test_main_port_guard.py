from __future__ import annotations

import pytest

from companion.core import main as backend_main


def test_backend_main_exits_before_build_when_control_port_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """控制端口预检失败时不应构建应用或进入 Uvicorn。"""

    class FakeSettings:
        control_host = "127.0.0.1"
        control_port = 8765

    monkeypatch.setattr(backend_main, "configure_native_runtime_environment", lambda: None)
    monkeypatch.setattr('companion.observability.logging_config.configure_backend_logging', lambda process_role: None)
    monkeypatch.setattr('companion.observability.logging_config.shutdown_backend_logging', lambda: None)
    monkeypatch.setattr('companion.core.settings.Settings', FakeSettings)
    monkeypatch.setattr('companion.core.app.build_app', lambda: pytest.fail("端口预检失败时不应构建应用"))

    def fail_port_guard(host: str, port: int) -> None:
        error = OSError("地址已被占用")
        error.errno = 10048
        raise error

    monkeypatch.setattr('companion.core.dev_ports.ensure_port_available', fail_port_guard)
    monkeypatch.setattr('uvicorn.run', lambda *args, **kwargs: pytest.fail("端口预检失败时不应启动 Uvicorn"))

    with pytest.raises(SystemExit) as raised:
        backend_main.run()

    assert raised.value.code == 1
