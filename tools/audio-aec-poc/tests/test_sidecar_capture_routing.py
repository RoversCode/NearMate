from __future__ import annotations

from audio_aec_poc.devices import SoundcardCapture
from audio_aec_poc.rdp_capture import RdpBufferedCapture
from audio_aec_poc.session_transport import CaptureProfile
from audio_aec_sidecar.pipeline import capture_factory_for_profile


def test_local_profile_keeps_existing_soundcard_capture() -> None:
    """本地控制台会话必须继续使用原有低延迟 SoundcardCapture。"""

    assert capture_factory_for_profile(CaptureProfile.LOCAL_LOW_LATENCY) is SoundcardCapture


def test_rdp_profile_uses_buffered_capture() -> None:
    """RDP 会话必须确定性选择能吸收传输抖动的缓冲采集器。"""

    assert capture_factory_for_profile(CaptureProfile.RDP_BUFFERED) is RdpBufferedCapture
