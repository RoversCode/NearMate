from __future__ import annotations

import json
import warnings

import numpy as np
import pytest

from audio_aec_poc.capture_format import normalize_capture_audio, soundcard_capture_channel_count
from audio_aec_poc.devices import SoundcardCapture
from audio_aec_sidecar.config import SidecarConfig
from audio_aec_sidecar.main import build_parser, _config_from_args


class FakeRecorder:
    """测试用 soundcard recorder。"""

    def __init__(self, *, fails: bool = False, record_warning: str | None = None, channels: int = 2) -> None:
        """保存打开阶段是否失败。"""

        self._fails = fails
        self._record_warning = record_warning
        self.channels = channels

    def __enter__(self) -> "FakeRecorder":
        """模拟 recorder 打开。"""

        if self._fails:
            raise AssertionError("bad WAVEFORMAT")
        return self

    def __exit__(self, *_args: object) -> None:
        """模拟 recorder 关闭。"""

        return None

    def record(self, numframes: int) -> np.ndarray:
        """返回静音音频。"""

        if self._record_warning is not None:
            warnings.warn(self._record_warning, RuntimeWarning, stacklevel=2)
        return np.zeros((numframes, self.channels), dtype=np.float32)


class FakeDevice:
    """测试用 soundcard 设备。"""

    def __init__(
        self,
        name: str,
        device_id: str,
        *,
        fails: bool = False,
        isloopback: bool = False,
        record_warning: str | None = None,
        channels: int = 2,
    ) -> None:
        """保存设备身份、失败状态和 loopback 标记。"""

        self.name = name
        self.id = device_id
        self.isloopback = isloopback
        self._fails = fails
        self.record_warning = record_warning
        self.channels = channels

    def recorder(self, *, samplerate: int, channels: int) -> FakeRecorder:
        """返回测试 recorder。"""

        return FakeRecorder(fails=self._fails, record_warning=self.record_warning, channels=channels)


class FakeChannelSensitiveDevice(FakeDevice):
    """模拟 Windows 上打开单声道成功、但只返回垃圾采样的真实多声道设备。"""

    def __init__(self, name: str, device_id: str, *, source_audio: np.ndarray, isloopback: bool = False) -> None:
        """保存原生声道波形，便于验证采集参数与最终混音结果。"""

        super().__init__(name, device_id, channels=source_audio.shape[1], isloopback=isloopback)
        self.source_audio = source_audio
        self.opened_channels: list[int] = []

    def recorder(self, *, samplerate: int, channels: int) -> FakeRecorder:
        """单声道请求不会抛错，只有完整原生声道请求能保留正常波形。"""

        assert samplerate == 48000
        self.opened_channels.append(channels)
        source_audio = self.source_audio
        recorder = FakeRecorder(channels=channels)

        def record(numframes: int) -> np.ndarray:
            if channels == 1:
                # 模拟底层错误：设备可以打开，返回的数据却和真实说话波形毫无关系。
                return np.full((numframes, 1), 0.8, dtype=np.float32)
            return source_audio[:numframes, :channels]

        recorder.record = record
        return recorder


class FakeSoundcard:
    """测试用 soundcard 模块替身。"""

    def __init__(self) -> None:
        """构造默认失败、备用成功的设备集合。"""

        self.default_mic = FakeDevice("麦克风 (K66)", "mic-default", fails=True)
        self.virtual_mic = FakeDevice("Voicemeeter Out A3 (VB-Audio Voicemeeter VAIO)", "mic-virtual")
        self.usb_mic = FakeDevice("麦克风 (USB Camera)", "mic-usb")
        self.default_speaker_device = FakeDevice("扬声器 (Realtek USB Audio)", "speaker-default")
        self.loopback = FakeDevice("扬声器 (Realtek USB Audio)", "speaker-loopback", isloopback=True)

    def default_microphone(self) -> FakeDevice:
        """返回默认麦克风。"""

        return self.default_mic

    def default_speaker(self) -> FakeDevice:
        """返回默认扬声器。"""

        return self.default_speaker_device

    def all_microphones(self, *, include_loopback: bool) -> list[FakeDevice]:
        """返回全部麦克风。"""

        return [self.default_mic, self.virtual_mic, self.usb_mic, self.loopback]

    def get_microphone(self, name: str, *, include_loopback: bool) -> FakeDevice:
        """按名称返回 loopback 或普通麦克风。"""

        for device in [self.default_mic, self.virtual_mic, self.usb_mic, self.loopback]:
            if device.name == name:
                return device
        raise RuntimeError(f"device not found: {name}")


class FakeSounddeviceStream:
    """测试用 sounddevice 输入流。"""

    def __init__(self, *, device: int) -> None:
        """保存被打开的 sounddevice 设备索引。"""

        self.device = device
        self.closed = False

    def start(self) -> None:
        """模拟启动输入流。"""

        return None

    def stop(self) -> None:
        """模拟停止输入流。"""

        return None

    def close(self) -> None:
        """模拟关闭输入流。"""

        self.closed = True

    def read(self, frames: int) -> tuple[np.ndarray, bool]:
        """返回非静音测试音频。"""

        return np.full((frames, 1), 0.25, dtype=np.float32), False


class FakeSounddevice:
    """测试用 sounddevice 模块替身。"""

    def query_devices(self) -> list[dict[str, object]]:
        """返回可匹配 K66 的输入设备。"""

        return [
            {
                "name": "麦克风 (K66)",
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            }
        ]

    def InputStream(self, *, samplerate: int, channels: int, dtype: str, device: int) -> FakeSounddeviceStream:
        """创建测试输入流。"""

        assert samplerate == 48000
        assert channels == 1
        assert dtype == "float32"
        return FakeSounddeviceStream(device=device)


class FakeDuplicatingSounddeviceStream:
    """测试用 sounddevice 输入流，可模拟成对重复的异常输入。"""

    def __init__(self, *, device: int) -> None:
        """保存被打开的设备索引。"""

        self.device = device

    def start(self) -> None:
        """模拟启动输入流。"""

        return None

    def stop(self) -> None:
        """模拟停止输入流。"""

        return None

    def close(self) -> None:
        """模拟关闭输入流。"""

        return None

    def read(self, frames: int) -> tuple[np.ndarray, bool]:
        """按设备索引返回异常或正常的测试音频。"""

        if self.device == 0:
            # WASAPI 异常路径会把真实采样点复制成相邻两点，听感上等价于时间被拉长。
            source = np.arange((frames + 1) // 2, dtype=np.float32) / 10
            return np.repeat(source, 2)[:frames].reshape(-1, 1), False

        return (np.arange(frames, dtype=np.float32) / 10).reshape(-1, 1), False


class FakeDuplicatingSounddevice:
    """测试用 sounddevice 模块，包含一个异常 WASAPI 候选和一个正常 MME 候选。"""

    def query_devices(self) -> list[dict[str, object]]:
        """返回两个同名 K66 输入设备。"""

        return [
            {
                "name": "麦克风 (K66)",
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
                "hostapi": 0,
            },
            {
                "name": "麦克风 (K66)",
                "max_input_channels": 1,
                "default_samplerate": 44100.0,
                "hostapi": 1,
            },
        ]

    def InputStream(self, *, samplerate: int, channels: int, dtype: str, device: int) -> FakeDuplicatingSounddeviceStream:
        """创建可区分设备索引的测试输入流。"""

        assert samplerate == 48000
        assert channels == 1
        assert dtype == "float32"
        return FakeDuplicatingSounddeviceStream(device=device)


def test_sidecar_cli_accepts_audio_device_arguments() -> None:
    """sidecar CLI 应接受桌面主进程传入的设备选择参数。"""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--livekit-url",
            "wss://livekit.example",
            "--token",
            "token",
            "--room-name",
            "room",
            "--identity",
            "desktop-aec",
            "--parent-pid",
            "123",
            "--audio-device-mode",
            "manual",
            "--microphone-id",
            "mic-usb",
            "--microphone-name",
            "麦克风 (USB Camera)",
            "--speaker-loopback-id",
            "speaker-loopback",
            "--speaker-loopback-name",
            "扬声器 (Realtek USB Audio)",
        ]
    )

    config = _config_from_args(args)

    assert config.audio_device_mode == "manual"
    assert config.microphone_id == "mic-usb"
    assert config.microphone_name == "麦克风 (USB Camera)"
    assert config.speaker_loopback_id == "speaker-loopback"
    assert config.speaker_loopback_name == "扬声器 (Realtek USB Audio)"


def test_soundcard_capture_falls_back_when_default_microphone_format_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """系统默认麦克风格式不兼容时，system_default 模式应跳过虚拟端并选择真实备用麦克风。"""

    capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=480,
        soundcard_module=FakeSoundcard(),
        sounddevice_module=None,
        audio_device_mode="system_default",
    )

    with capture:
        assert capture.device_info.microphone_name == "麦克风 (USB Camera)"

    stderr_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    payload = json.loads(stderr_lines[-1])
    assert payload["event"] == "aec-sidecar-device-fallback"
    assert payload["failed_device"]["name"] == "麦克风 (K66)"
    assert payload["selected_device"]["name"] == "麦克风 (USB Camera)"
    assert payload["error_type"] == "AssertionError"


@pytest.mark.parametrize("native_channels", [2, 4])
def test_soundcard_capture_preserves_voice_when_mono_wasapi_open_succeeds_with_noise(native_channels: int) -> None:
    """单声道打开成功也可能产生噪声，必须采集原生声道并混成业务单声道。"""

    positions = np.arange(8, dtype=np.float32)
    microphone_audio = np.column_stack([positions / (index + 10) for index in range(native_channels)])
    reference_audio = np.column_stack([-positions / (index + 20) for index in range(native_channels)])
    soundcard = FakeSoundcard()
    soundcard.default_mic = FakeChannelSensitiveDevice("麦克风 (K66)", "mic-default", source_audio=microphone_audio)
    soundcard.loopback = FakeChannelSensitiveDevice(
        soundcard.default_speaker_device.name,
        "speaker-loopback",
        source_audio=reference_audio,
        isloopback=True,
    )
    capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=8,
        soundcard_module=soundcard,
        sounddevice_module=None,
    )

    with capture:
        reference_frame, microphone_frame = capture.read_frame_pair()

    assert soundcard.default_mic.opened_channels == [native_channels]
    assert soundcard.loopback.opened_channels == [native_channels]
    assert microphone_frame.shape == reference_frame.shape == (8, 1)
    assert microphone_frame.dtype == reference_frame.dtype == np.float32
    np.testing.assert_array_equal(microphone_frame[:, 0], np.mean(microphone_audio, axis=1))
    np.testing.assert_array_equal(reference_frame[:, 0], np.mean(reference_audio, axis=1))


def test_soundcard_capture_uses_selected_sounddevice_for_native_mono_microphone() -> None:
    """真单声道麦克风改用同一设备的 sounddevice，不强迫驱动伪装双声道。"""

    soundcard = FakeSoundcard()
    soundcard.default_mic = FakeChannelSensitiveDevice(
        "麦克风 (K66)",
        "mic-default",
        source_audio=np.arange(8, dtype=np.float32).reshape(-1, 1),
    )
    capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=8,
        soundcard_module=soundcard,
        sounddevice_module=FakeSounddevice(),
        audio_device_mode="manual",
        microphone_id="mic-default",
    )

    with capture:
        frame = capture.read_microphone()
        assert capture.device_info.microphone_name == "麦克风 (K66)"

    assert soundcard.default_mic.opened_channels == []
    assert frame.shape == (8, 1)
    np.testing.assert_array_equal(frame[:, 0], np.full(8, 0.25, dtype=np.float32))


@pytest.mark.parametrize("audio_device_mode", ["manual", "system_default"])
def test_soundcard_capture_preserves_selected_or_fallback_microphone_waveform(audio_device_mode: str) -> None:
    """手动设备和系统备用设备都应按自己的原生声道采集，不能悄悄改回默认麦克风。"""

    positions = np.arange(8, dtype=np.float32)
    waveform = np.column_stack([positions / 10, positions / 20, positions / 30, positions / 40])
    soundcard = FakeSoundcard()
    soundcard.usb_mic = FakeChannelSensitiveDevice("麦克风 (USB Camera)", "mic-usb", source_audio=waveform)
    capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=8,
        soundcard_module=soundcard,
        sounddevice_module=None,
        audio_device_mode=audio_device_mode,
        microphone_id="mic-usb" if audio_device_mode == "manual" else None,
    )

    with capture:
        assert capture.device_info.microphone_name == "麦克风 (USB Camera)"
        frame = capture.read_microphone()

    assert soundcard.usb_mic.opened_channels == ([4] if audio_device_mode == "manual" else [4, 4])
    np.testing.assert_array_equal(frame[:, 0], np.mean(waveform, axis=1))


def test_soundcard_capture_rejects_native_mono_loopback_and_closes_microphone() -> None:
    """单声道 loopback 没有可靠的 sounddevice 替代，应报格式问题并释放已开麦克风。"""

    soundcard = FakeSoundcard()
    soundcard.default_mic._fails = False
    soundcard.loopback.channels = 1
    closed_microphones: list[bool] = []
    microphone_recorder = FakeRecorder(channels=2)
    microphone_recorder.__exit__ = lambda *_args: closed_microphones.append(True)
    soundcard.default_mic.recorder = lambda **_kwargs: microphone_recorder
    capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=8,
        soundcard_module=soundcard,
        sounddevice_module=None,
    )

    with pytest.raises(ValueError, match="单声道"):
        with capture:
            pass

    assert closed_microphones == [True]
    with pytest.raises(RuntimeError, match="open context"):
        capture.read_microphone()


def test_capture_audio_preserves_multichannel_output_without_remixing() -> None:
    """业务明确请求多声道时保留各列，避免为修单声道问题改写已有多声道行为。"""

    waveform = np.array([[0.2, -0.4, 0.1], [0.6, 0.8, -0.5]], dtype=np.float64)

    frame = normalize_capture_audio(waveform, channels=2)

    assert frame.shape == (2, 2)
    assert frame.dtype == np.float32
    np.testing.assert_array_equal(frame, waveform[:, :2].astype(np.float32))


@pytest.mark.parametrize(
    "audio",
    [
        np.array([], dtype=np.float32),
        np.empty((4, 0), dtype=np.float32),
        np.zeros((1, 2, 3), dtype=np.float32),
        np.array([0.1, np.nan], dtype=np.float32),
        np.array([0.1, np.inf], dtype=np.float32),
    ],
)
def test_capture_audio_rejects_invalid_sample_arrays(audio: np.ndarray) -> None:
    """空帧、异常形状和非有限采样不能继续进入 AEC 或测试回放。"""

    with pytest.raises(ValueError):
        normalize_capture_audio(audio, channels=1)


def test_capture_audio_rejects_missing_output_channels() -> None:
    """不补虚构静音声道来掩盖设备实际交付格式与业务契约的差异。"""

    with pytest.raises(ValueError, match="少于"):
        normalize_capture_audio(np.ones((4, 1), dtype=np.float32), channels=2)


def test_soundcard_channel_selection_preserves_non_windows_mono(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 的兼容规则不应迫使其他平台把正常单声道设备改成多声道。"""

    monkeypatch.setattr("audio_aec_poc.capture_format.sys.platform", "linux")

    assert soundcard_capture_channel_count(FakeDevice("Mic", "mic", channels=1), output_channels=1) == 1


def test_soundcard_capture_uses_sounddevice_for_same_microphone_when_soundcard_format_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """soundcard 无法打开默认麦克风时，应优先用 sounddevice 打开同一个物理设备。"""

    capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=480,
        soundcard_module=FakeSoundcard(),
        sounddevice_module=FakeSounddevice(),
        audio_device_mode="system_default",
    )

    with capture:
        assert capture.device_info.microphone_name == "麦克风 (K66)"
        frame = capture.read_microphone()

    assert frame.shape == (480, 1)
    assert float(np.max(np.abs(frame))) == 0.25
    stderr_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    payload = json.loads(stderr_lines[-1])
    assert payload["event"] == "aec-sidecar-device-backend-fallback"
    assert payload["device"]["name"] == "麦克风 (K66)"
    assert payload["failed_backend"] == "soundcard"
    assert payload["selected_backend"] == "sounddevice"


def test_soundcard_capture_skips_pair_duplicating_sounddevice_stream() -> None:
    """sounddevice 同名候选中若存在成对重复输入，应选择正常流式设备。"""

    capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=8,
        soundcard_module=FakeSoundcard(),
        sounddevice_module=FakeDuplicatingSounddevice(),
        audio_device_mode="system_default",
    )

    with capture:
        frame = capture.read_microphone()

    assert frame[:, 0].tolist() == pytest.approx([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])


def test_soundcard_capture_suppresses_loopback_discontinuity_warning_only() -> None:
    """扬声器 loopback 的 soundcard 断续假阳性不应泄漏到 stderr，但麦克风断续仍应保留。"""

    loopback_soundcard = FakeSoundcard()
    loopback_soundcard.default_mic._fails = False
    loopback_soundcard.loopback.record_warning = "data discontinuity in recording"
    loopback_capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=480,
        soundcard_module=loopback_soundcard,
        sounddevice_module=None,
        audio_device_mode="system_default",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with loopback_capture:
            loopback_capture.read_speaker_reference()

    assert not [warning for warning in caught if "data discontinuity in recording" in str(warning.message)]

    microphone_soundcard = FakeSoundcard()
    microphone_soundcard.default_mic._fails = False
    microphone_soundcard.default_mic.record_warning = "data discontinuity in recording"
    microphone_capture = SoundcardCapture(
        sample_rate=48000,
        channels=1,
        frame_samples=480,
        soundcard_module=microphone_soundcard,
        sounddevice_module=None,
        audio_device_mode="system_default",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with microphone_capture:
            microphone_capture.read_microphone()

    assert [warning for warning in caught if "data discontinuity in recording" in str(warning.message)]


def test_soundcard_capture_manual_missing_microphone_fails_with_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """手动选择的麦克风不存在时，应输出结构化设备错误并拒绝启动。"""

    with pytest.raises(RuntimeError, match="microphone"):
        capture = SoundcardCapture(
            sample_rate=48000,
            channels=1,
            frame_samples=480,
            soundcard_module=FakeSoundcard(),
            sounddevice_module=None,
            audio_device_mode="manual",
            microphone_id="missing-mic",
        )
        with capture:
            pass

    stderr_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    payload = json.loads(stderr_lines[-1])
    assert payload["event"] == "aec-sidecar-device-error"
    assert payload["stage"] == "microphone_select"
    assert payload["error_type"] == "device_not_found"


def test_sidecar_config_rejects_invalid_audio_device_mode() -> None:
    """音频设备模式只允许系统默认和手动选择。"""

    config = SidecarConfig(
        livekit_url="wss://livekit.example",
        token="token",
        room_name="room",
        identity="desktop-aec",
        parent_pid=123,
        audio_device_mode="invalid",
    )

    with pytest.raises(ValueError, match="audio_device_mode"):
        config.validate()
