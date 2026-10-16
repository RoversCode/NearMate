"""验证媒体 smoke 的网络边界和原生子进程失败后的收口。"""

from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from installer import verify_livekit_runtime as smoke


def test_smoke_matches_local_server_network_boundary() -> None:
    """信令只监听本机，媒体允许系统网卡候选，不能强制不兼容的环回候选。"""
    configuration = smoke.create_server_configuration("one-shot-key", "one-shot-secret")
    assert configuration["bind_addresses"] == ["127.0.0.1"]
    assert configuration["rtc"]["use_external_ip"] is False
    assert configuration["rtc"]["enable_loopback_candidate"] is True
    assert "node_ip" not in configuration["rtc"]
    assert "ips" not in configuration["rtc"]
    assert configuration["port"] != configuration["rtc"]["tcp_port"]
    assert configuration["rtc"]["udp_port"] > 0


@pytest.mark.parametrize("native_failure", ["crash", "timeout"])
def test_native_rtc_failure_still_stops_owned_media_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_failure: str,
) -> None:
    """RTC 原生崩溃或超时不得遗留服务进程，后续构建必须从干净状态重试。"""
    server = Mock()
    server.poll.return_value = None
    server.wait.return_value = 0
    monkeypatch.setattr(smoke.os, "environ", {"PATH": "fixture", "LIVEKIT_API_SECRET": "host-secret"})
    launch = Mock(return_value=server)
    monkeypatch.setattr(smoke.subprocess, "Popen", launch)
    if native_failure == "crash":
        run = Mock(return_value=subprocess.CompletedProcess([], -1073741819, "", ""))
        expected_error = RuntimeError
    else:
        run = Mock(side_effect=subprocess.TimeoutExpired("native-probe", 65))
        expected_error = subprocess.TimeoutExpired
    monkeypatch.setattr(smoke.subprocess, "run", run)

    with pytest.raises(expected_error):
        smoke.verify_livekit_runtime(tmp_path / "livekit-server.exe", tmp_path / "server.log")

    server.terminate.assert_called_once_with()
    server.wait.assert_called_once_with(timeout=5)
    assert "LIVEKIT_API_SECRET" not in launch.call_args.kwargs["env"]
    assert "LIVEKIT_CONFIG" in launch.call_args.kwargs["env"]
    assert launch.call_args.args[0] == [str((tmp_path / "livekit-server.exe").resolve())]
