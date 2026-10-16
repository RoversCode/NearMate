from __future__ import annotations

from enum import Enum
import os
from typing import Any


class CaptureProfile(str, Enum):
    """描述当前 Windows 会话需要使用的音频采集策略。

    Returns:
        LOCAL_LOW_LATENCY 表示本地控制台低延迟路径，RDP_BUFFERED 表示远程桌面抗抖动路径。
    """

    LOCAL_LOW_LATENCY = "local_low_latency"
    RDP_BUFFERED = "rdp_buffered"


def detect_capture_profile(
    *,
    win32ts_module: Any | None = None,
    process_id: int | None = None,
) -> CaptureProfile:
    """根据当前进程所在的 Windows 会话选择采集策略。

    Args:
        win32ts_module: 可选的 win32ts 模块替身，主要供单元测试注入。
        process_id: 可选的目标进程编号；不传时使用当前侧车进程编号。

    Returns:
        只有协议 2 返回 RDP 缓冲策略；其它协议或查询失败保留本地低延迟策略。
    """

    try:
        if win32ts_module is None:
            import win32ts as loaded_win32ts

            win32ts_module = loaded_win32ts

        current_process_id = os.getpid() if process_id is None else process_id
        session_id = win32ts_module.ProcessIdToSessionId(current_process_id)
        protocol_type = win32ts_module.WTSQuerySessionInformation(
            None,
            session_id,
            win32ts_module.WTSClientProtocolType,
        )
    except Exception:
        # MediaCapture 直通路径只针对已确认的 RDP 协议 2，查询失败不能让其它会话被迫改变本地 AEC。
        return CaptureProfile.LOCAL_LOW_LATENCY

    if int(protocol_type) == 2:
        return CaptureProfile.RDP_BUFFERED
    return CaptureProfile.LOCAL_LOW_LATENCY
