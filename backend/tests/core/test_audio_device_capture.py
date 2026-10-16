"""验证设备测试使用真实监听的采集路径，且回放保留人声波形。"""

import base64
import asyncio
from io import BytesIO
import threading
import wave

import numpy as np
import pytest

from audio_aec_poc.session_transport import CaptureProfile
from companion.core import audio_devices


class Microphone:
    """模拟打开成功但单声道返回噪声的 Windows 麦克风。"""

    id = "selected-microphone"
    name = "远程音频"
    channels = 2

    def recorder(self, *, samplerate, channels, blocksize):
        """用两声道保留原波形，用单声道模拟驱动损坏。"""
        self.requested_channels = channels
        self.sample_rate = samplerate
        self.closed = False
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def record(self, numframes):
        if self.requested_channels == 1:
            return np.random.default_rng(42).uniform(-1, 1, (numframes, 1)).astype(np.float32)
        voice = np.sin(np.arange(numframes) * 2 * np.pi * 440 / self.sample_rate) * 0.2
        return np.column_stack((voice, voice)).astype(np.float32)


def decode_test_audio(payload):
    """解码 Console 实际回放的 WAV，供波形断言。"""
    with wave.open(BytesIO(base64.b64decode(payload["audio_wav_b64"])), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getframerate() == 48000
        return np.frombuffer(recording.readframes(recording.getnframes()), dtype="<i2") / 32767


def test_local_device_test_preserves_voice_instead_of_wasapi_mono_noise(monkeypatch):
    """真实人声必须穿过测试回放；录音器成功打开并不能证明采样有效。"""
    monkeypatch.setattr(audio_devices, "detect_capture_profile", lambda: CaptureProfile.LOCAL_LOW_LATENCY, raising=False)
    microphone = Microphone()
    payload = audio_devices._test_microphone_recording(microphone)
    actual = decode_test_audio(payload)
    expected = np.sin(np.arange(144000) * 2 * np.pi * 440 / 48000) * 0.2
    np.testing.assert_allclose(actual, expected, atol=1 / 32767)
    assert microphone.requested_channels == 2
    assert microphone.closed


@pytest.mark.parametrize("probe", [False, True])
def test_rdp_device_probe_and_test_use_the_selected_mediacapture_device(monkeypatch, probe):
    """RDP 测试与正式监听共用采集器，不能降级成不可靠的 soundcard。"""
    monkeypatch.setattr(audio_devices, "detect_capture_profile", lambda: CaptureProfile.RDP_BUFFERED, raising=False)
    calls = []

    class Capture:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.frame_samples = kwargs["frame_samples"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            calls.append("closed")

        def read_frame_pair(self):
            frame = np.full((self.frame_samples, 1), 0.125, dtype=np.float32)
            return np.zeros_like(frame), frame

    monkeypatch.setattr(audio_devices, "RdpBufferedCapture", Capture, raising=False)
    if probe:
        payload = audio_devices._probe_microphone(Microphone(), include_levels=True)
        assert payload["peak_level"] == 0.125
    else:
        payload = audio_devices._test_microphone_recording(Microphone())
        np.testing.assert_allclose(decode_test_audio(payload), 0.125, atol=1 / 32767)
        assert payload["duration_ms"] == 3000
    assert calls[0]["audio_device_mode"] == "manual"
    assert calls[0]["microphone_id"] == Microphone.id
    assert calls[-1] == "closed"


def test_rdp_capture_failure_cannot_fall_back_to_soundcard_noise(monkeypatch):
    """远程重定向失效时报告失败，不能用能打开的噪声流冒充可用。"""
    monkeypatch.setattr(audio_devices, "detect_capture_profile", lambda: CaptureProfile.RDP_BUFFERED, raising=False)

    class FailedCapture:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("RDP microphone unavailable")

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(audio_devices, "RdpBufferedCapture", FailedCapture, raising=False)
    payload = audio_devices._test_microphone_recording(Microphone())
    assert payload["status"] == "unavailable"
    assert payload["audio_wav_b64"] is None
    assert payload["error_message"] == "RDP microphone unavailable"


@pytest.mark.asyncio
async def test_recording_does_not_block_realtime_media_event_loop(monkeypatch):
    """三秒测试等待录音时，实时媒体所在的事件循环仍能继续处理音频。"""
    from companion.core.api.routers import settings
    from companion.core.api.schemas import AudioDeviceTestRequest

    recording_entered = threading.Event()
    release_recording = threading.Event()

    class ProbeService:
        def test_microphone(self, **_kwargs):
            recording_entered.set()
            assert release_recording.wait(2)
            return {"status": "available"}

    monkeypatch.setattr(settings, "AudioDeviceProbeService", ProbeService)
    task = asyncio.create_task(settings.test_audio_device(AudioDeviceTestRequest()))
    try:
        await asyncio.wait_for(asyncio.to_thread(recording_entered.wait, 1), timeout=1.5)
        # 录音线程尚未释放时本协程仍被调度，证明控制接口没有阻塞媒体事件循环。
        assert not task.done()
    finally:
        release_recording.set()
    assert await task == {"status": "available"}


@pytest.mark.parametrize("first_candidate", ["open_failed", "read_failed", "pair_duplicated_samples"])
def test_mono_console_capture_uses_the_same_healthy_host_api_as_sidecar(monkeypatch, first_candidate):
    """同设备的首个后端失败或损坏时，设备测试仍应选到实际监听使用的正常后端。"""
    monkeypatch.setattr(audio_devices, "detect_capture_profile", lambda: CaptureProfile.LOCAL_LOW_LATENCY)

    class MonoMicrophone(Microphone):
        channels = 1

    microphone = MonoMicrophone()
    opened = []
    closed = []

    def waveform(frames, device):
        if device == 0:
            return np.repeat(np.sin(np.arange((frames + 1) // 2) * 0.1) * 0.2, 2)[:frames, None].astype(np.float32)
        return (np.sin(np.arange(frames) * 0.1) * 0.2)[:, None].astype(np.float32)

    class Stream:
        def __init__(self, device):
            self.device = device

        def start(self):
            pass

        def read(self, frames):
            if self.device == 0 and first_candidate == "read_failed":
                raise RuntimeError("host API cannot read input")
            return waveform(frames, self.device), False

        def stop(self):
            pass

        def close(self):
            closed.append(self.device)

    class Sounddevice:
        def query_devices(self):
            return [{"name": microphone.name, "max_input_channels": 1, "default_samplerate": 48000}] * 2

        def InputStream(self, *, device, **_kwargs):
            opened.append(device)
            if device == 0 and first_candidate == "open_failed":
                raise RuntimeError("host API cannot open input")
            return Stream(device)

        def rec(self, frames, *, device, **_kwargs):
            # 保留旧 Console API 以证明回归在真实旧分支上会失败。
            opened.append(device)
            if device == 0 and first_candidate == "open_failed":
                raise RuntimeError("host API cannot open input")
            return waveform(frames, device)

        def wait(self):
            pass

    payload = audio_devices._test_microphone_recording(microphone, sounddevice_module=Sounddevice())
    assert payload["status"] == "available"
    np.testing.assert_allclose(decode_test_audio(payload), waveform(144000, 1)[:, 0], atol=1 / 32767)
    assert opened == [0, 1]
    assert 1 in closed
    if first_candidate != "open_failed":
        assert 0 in closed
