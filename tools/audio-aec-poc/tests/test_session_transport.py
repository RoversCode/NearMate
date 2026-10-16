from __future__ import annotations

import pytest

from audio_aec_poc.session_transport import CaptureProfile, detect_capture_profile


class FakeWin32Ts:
    """为会话传输判断提供可控的 Windows 终端服务返回值。

    Args:
        protocol_type: 当前会话协议编号。
        query_error: 查询协议时需要抛出的异常；不传表示查询成功。

    Returns:
        提供与 win32ts 相同的最小调用接口。
    """

    WTSClientProtocolType = 16

    def __init__(self, protocol_type: int, query_error: BaseException | None = None) -> None:
        """保存测试会话协议和可选查询异常。"""

        self.protocol_type = protocol_type
        self.query_error = query_error
        self.process_ids: list[int] = []

    def ProcessIdToSessionId(self, process_id: int) -> int:
        """记录进程编号并返回固定会话编号。"""

        self.process_ids.append(process_id)
        return 7

    def WTSQuerySessionInformation(self, server: object, session_id: int, info_class: int) -> int:
        """返回协议编号，或模拟 WTS 查询失败。

        Args:
            server: WTS 服务句柄，当前测试只接受本机句柄。
            session_id: ProcessIdToSessionId 返回的会话编号。
            info_class: 需要查询的 WTS 信息类型。

        Returns:
            当前测试配置的协议编号。
        """

        assert server is None
        assert session_id == 7
        assert info_class == self.WTSClientProtocolType
        if self.query_error is not None:
            raise self.query_error
        return self.protocol_type


def test_rdp_session_uses_buffered_capture_profile() -> None:
    """WTS 协议 2 必须确定性选择 RDP 缓冲采集。"""

    win32ts = FakeWin32Ts(protocol_type=2)

    profile = detect_capture_profile(win32ts_module=win32ts, process_id=1234)

    assert profile is CaptureProfile.RDP_BUFFERED
    assert win32ts.process_ids == [1234]


@pytest.mark.parametrize("protocol_type", [0, 1, 3, 99])
def test_non_rdp_session_keeps_local_low_latency_profile(protocol_type: int) -> None:
    """只有 WTS 协议 2 可以启用 RDP 采集，其它协议不得改变原有路径。"""

    profile = detect_capture_profile(
        win32ts_module=FakeWin32Ts(protocol_type=protocol_type),
        process_id=1234,
    )

    assert profile is CaptureProfile.LOCAL_LOW_LATENCY


def test_wts_query_failure_preserves_local_low_latency_profile() -> None:
    """WTS 查询失败时不能猜测为 RDP，避免非协议 2 会话被迫依赖 FFmpeg。"""

    profile = detect_capture_profile(
        win32ts_module=FakeWin32Ts(protocol_type=0, query_error=OSError("WTS unavailable")),
        process_id=1234,
    )

    assert profile is CaptureProfile.LOCAL_LOW_LATENCY
