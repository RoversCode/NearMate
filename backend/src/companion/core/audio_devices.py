from __future__ import annotations

import base64
from collections.abc import Mapping
from io import BytesIO
from typing import Any, Protocol
import wave

import numpy as np

from audio_aec_poc.rdp_capture import RdpBufferedCapture
from audio_aec_poc.session_transport import CaptureProfile, detect_capture_profile
from companion.core.settings import Settings


ProbeStatus = str
_AUTO_SOUNDDEVICE_MODULE = object()
_MICROPHONE_SAMPLE_RATE = 48000
_MICROPHONE_CHANNELS = 1
_QUICK_PROBE_FRAMES = _MICROPHONE_SAMPLE_RATE // 100
_TEST_RECORDING_SECONDS = 3
_TEST_RECORDING_FRAMES = _MICROPHONE_SAMPLE_RATE * _TEST_RECORDING_SECONDS


class SoundcardAudioDeviceBackend(Protocol):
    """音频设备探测后端协议。

    Args:
        无。

    Returns:
        由具体实现提供 soundcard 兼容的设备枚举和默认设备读取能力。
    """

    def default_microphone(self) -> Any:
        """返回系统默认麦克风。"""

    def all_microphones(self, *, include_loopback: bool) -> list[Any]:
        """返回系统麦克风列表。"""

    def all_speaker_loopbacks(self) -> list[Any]:
        """返回可作为扬声器参考的 loopback 设备。"""


class SoundcardAudioDeviceBackendImpl:
    """基于 soundcard 的真实音频设备后端。

    Args:
        无。

    Returns:
        提供 Windows 本地 soundcard 设备枚举能力。
    """

    def __init__(self) -> None:
        """延迟导入 soundcard。

        Args:
            无。

        Returns:
            初始化后的真实后端实例。
        """

        import soundcard as sc

        self._soundcard = sc

    def default_microphone(self) -> Any:
        """返回系统默认麦克风。"""

        return self._soundcard.default_microphone()

    def all_microphones(self, *, include_loopback: bool) -> list[Any]:
        """返回系统麦克风列表。"""

        return list(self._soundcard.all_microphones(include_loopback=include_loopback))

    def all_speaker_loopbacks(self) -> list[Any]:
        """返回可作为扬声器参考的 loopback 设备。"""

        return [
            device
            for device in self._soundcard.all_microphones(include_loopback=True)
            if bool(getattr(device, "isloopback", False))
        ]


class AudioDeviceProbeService:
    """音频设备枚举与短录音探测服务。

    Args:
        backend: 可注入的设备枚举后端，生产环境默认使用 soundcard。

    Returns:
        可被 FastAPI 路由复用的设备探测服务。
    """

    def __init__(
        self,
        backend: SoundcardAudioDeviceBackend | None = None,
        sounddevice_module: Any = _AUTO_SOUNDDEVICE_MODULE,
    ) -> None:
        """初始化设备探测服务。"""

        self._backend = backend or SoundcardAudioDeviceBackendImpl()
        self._sounddevice = _load_sounddevice_module() if sounddevice_module is _AUTO_SOUNDDEVICE_MODULE else sounddevice_module

    def inspect_devices(
        self,
        *,
        selection: Mapping[str, object],
        probe_microphones: bool = True,
    ) -> dict[str, object]:
        """枚举设备并返回当前选择。

        Args:
            selection: 已冻结的音频设备选择，用于标注用户保存的选择。
            probe_microphones: 是否对麦克风执行短录音探测。

        Returns:
            包含麦克风、扬声器参考和当前选择的 JSON 兼容字典。
        """

        default_microphone_id = _device_id_or_none(self._safe_default_microphone())
        microphones = [
            self._device_payload(
                device,
                is_default=_device_id_or_none(device) == default_microphone_id,
                probe=probe_microphones,
            )
            for device in self._real_microphones()
        ]
        speaker_loopbacks = [
            {
                **_base_device_payload(device),
                "is_default": False,
                "status": "available",
                "error_type": None,
                "error_message": None,
            }
            for device in self._safe_speaker_loopbacks()
        ]

        return {
            "microphones": microphones,
            "speaker_loopbacks": speaker_loopbacks,
            "selected": dict(selection),
        }

    def test_microphone(
        self,
        *,
        microphone_id: str | None = None,
        microphone_name: str | None = None,
    ) -> dict[str, object]:
        """测试指定或默认麦克风。

        Args:
            microphone_id: 用户选择的麦克风设备标识。
            microphone_name: 用户选择的麦克风名称。

        Returns:
            包含可用状态和简短音量结果的 JSON 兼容字典。
        """

        device = self._resolve_microphone(microphone_id=microphone_id, microphone_name=microphone_name)
        if device is None:
            return _failed_test_payload(
                "unavailable",
                error_type="device_not_found",
                error_message="找不到指定麦克风设备",
            )

        return _test_microphone_recording(device, sounddevice_module=self._sounddevice)

    def _real_microphones(self) -> list[Any]:
        """返回非 loopback 麦克风。"""

        return [
            device
            for device in self._backend.all_microphones(include_loopback=True)
            if not bool(getattr(device, "isloopback", False))
        ]

    def _safe_default_microphone(self) -> Any | None:
        """安全读取系统默认麦克风。"""

        try:
            return self._backend.default_microphone()
        except Exception:
            return None

    def _safe_speaker_loopbacks(self) -> list[Any]:
        """安全读取扬声器参考设备。"""

        try:
            return self._backend.all_speaker_loopbacks()
        except Exception:
            return []

    def _resolve_microphone(self, *, microphone_id: str | None, microphone_name: str | None) -> Any | None:
        """按 id、名称或系统默认解析麦克风。"""

        if microphone_id:
            for device in self._real_microphones():
                if _device_id_or_none(device) == microphone_id:
                    return device
            return None

        if microphone_name:
            for device in self._real_microphones():
                if getattr(device, "name", None) == microphone_name:
                    return device
            return None

        return self._safe_default_microphone()

    def _device_payload(self, device: Any, *, is_default: bool, probe: bool) -> dict[str, object]:
        """构造单个麦克风设备载荷。"""

        payload = {
            **_base_device_payload(device),
            "is_default": is_default,
        }
        if not probe:
            payload.update({"status": "unknown", "error_type": None, "error_message": None})
            return payload
        payload.update(_probe_microphone(device, include_levels=False, sounddevice_module=self._sounddevice))
        return payload


def audio_device_selection_payload(settings: Settings) -> dict[str, object]:
    """把 Settings 中的音频设备选择转换为 session/API 载荷。

    Args:
        settings: 当前运行时设置。

    Returns:
        JSON 兼容的音频设备选择字典。
    """

    return {
        "mode": settings.audio_device_mode,
        "microphone_id": settings.audio_input_device_id,
        "microphone_name": settings.audio_input_device_name,
        "speaker_loopback_id": settings.audio_output_reference_device_id,
        "speaker_loopback_name": settings.audio_output_reference_device_name,
    }


def audio_device_selection_payload_from_snapshot(
    settings_snapshot: Mapping[str, object],
) -> dict[str, object]:
    """从某次已提交的设置快照构造音频设备选择。

    Args:
        settings_snapshot: 统一设置事务返回的冻结提交载荷。

    Returns:
        与该次提交严格对应的音频设备选择字典。
    """

    return {
        "mode": settings_snapshot["audio_device_mode"],
        "microphone_id": settings_snapshot["audio_input_device_id"],
        "microphone_name": settings_snapshot["audio_input_device_name"],
        "speaker_loopback_id": settings_snapshot[
            "audio_output_reference_device_id"
        ],
        "speaker_loopback_name": settings_snapshot[
            "audio_output_reference_device_name"
        ],
    }


def _base_device_payload(device: Any) -> dict[str, object]:
    """构造设备基础载荷。"""

    return {
        "id": _device_id_or_none(device),
        "name": str(getattr(device, "name", "")),
    }


def _device_id_or_none(device: Any | None) -> str | None:
    """读取 soundcard 设备标识。"""

    if device is None:
        return None
    value = getattr(device, "id", None)
    if value is None:
        value = getattr(device, "_id", None)
    return str(value) if value is not None else None


def _probe_microphone(device: Any, *, include_levels: bool, sounddevice_module: Any | None = None) -> dict[str, object]:
    """按实际监听的采集路径探测麦克风。"""

    try:
        audio = _record_microphone(device, frame_count=_QUICK_PROBE_FRAMES, sounddevice_module=sounddevice_module)
    except AssertionError as exception:
        return _failed_probe_payload("format_incompatible", exception, include_levels=include_levels)
    except PermissionError as exception:
        return _failed_probe_payload("permission_or_busy", exception, include_levels=include_levels)
    except Exception as exception:
        return _failed_probe_payload("unavailable", exception, include_levels=include_levels)

    return _successful_probe_payload(audio, include_levels=include_levels)


def _test_microphone_recording(device: Any, *, sounddevice_module: Any | None = None) -> dict[str, object]:
    """录制三秒麦克风测试音频。

    Args:
        device: 待测试的麦克风设备。
        sounddevice_module: 可选 sounddevice 模块，用于 soundcard 失败后的兼容路径。

    Returns:
        包含状态、音量统计、音量分级和 WAV 回放数据的 JSON 兼容字典。
    """

    try:
        audio = _record_microphone(device, frame_count=_TEST_RECORDING_FRAMES, sounddevice_module=sounddevice_module)
    except AssertionError as exception:
        return _failed_test_payload("format_incompatible", exception=exception)
    except PermissionError as exception:
        return _failed_test_payload("permission_or_busy", exception=exception)
    except Exception as exception:
        return _failed_test_payload("unavailable", exception=exception)

    return _successful_test_payload(audio)


def _record_microphone(device: Any, *, frame_count: int, sounddevice_module: Any | None) -> np.ndarray:
    """按 Windows 会话类型采集用户选中的麦克风，不用噪声流掩盖 RDP 失败。

    Args:
        device: 已解析的真实麦克风设备。
        frame_count: 需要保留的单声道样本数。
        sounddevice_module: 本地 soundcard 不兼容时使用的备用采集后端。

    Returns:
        与正式监听相同来源的单声道浮点音频。
    """

    if detect_capture_profile() == CaptureProfile.RDP_BUFFERED:
        # RDP 的 WASAPI 数据会欠帧或损坏；设备测试必须复用正式监听的 MediaCapture 和缓冲恢复。
        with RdpBufferedCapture(
            sample_rate=_MICROPHONE_SAMPLE_RATE,
            channels=_MICROPHONE_CHANNELS,
            frame_samples=_QUICK_PROBE_FRAMES,
            audio_device_mode="manual",
            microphone_id=_device_id_or_none(device),
            microphone_name=str(getattr(device, "name", "")) or None,
        ) as capture:
            frames = [capture.read_frame_pair()[1] for _ in range((frame_count + _QUICK_PROBE_FRAMES - 1) // _QUICK_PROBE_FRAMES)]
        return np.concatenate(frames, axis=0)[:frame_count]

    try:
        return _record_with_soundcard(device, frame_count=frame_count)
    except PermissionError:
        # 权限拒绝不能被备用后端掩盖，否则用户看到的设备状态与 Windows 实际权限不一致。
        raise
    except Exception:
        audio = _record_with_sounddevice(device, sounddevice_module=sounddevice_module, frame_count=frame_count)
        if audio is None:
            raise
        return audio


def _record_with_soundcard(device: Any, *, frame_count: int) -> Any:
    """使用 soundcard 录制固定帧数的麦克风音频。

    Args:
        device: soundcard 麦克风设备。
        frame_count: 需要读取的每声道样本数。

    Returns:
        使用设备原生声道采集后合成的单声道音频数组。
    """

    from audio_aec_poc.capture_format import normalize_capture_audio, soundcard_capture_channel_count

    # Windows WASAPI 单声道请求可能成功但返回垃圾数据，先完整读取原生声道再合成人声。
    with device.recorder(
        samplerate=_MICROPHONE_SAMPLE_RATE,
        channels=soundcard_capture_channel_count(device, _MICROPHONE_CHANNELS),
        blocksize=_QUICK_PROBE_FRAMES,
    ) as recorder:
        return normalize_capture_audio(recorder.record(numframes=frame_count), _MICROPHONE_CHANNELS)


def _record_with_sounddevice(device: Any, *, sounddevice_module: Any | None, frame_count: int) -> Any | None:
    """使用 sounddevice 录制固定帧数的麦克风音频。

    Args:
        device: soundcard 枚举出的麦克风设备。
        sounddevice_module: sounddevice 模块或测试替身。
        frame_count: 需要读取的每声道样本数。

    Returns:
        录音数组；无法匹配或打开失败时返回 None。
    """

    if sounddevice_module is None:
        return None

    from audio_aec_poc.capture_format import normalize_capture_audio
    from audio_aec_poc.devices import _SounddeviceInputRecorder

    try:
        # 复用正式监听的同设备候选遍历和波形探测，不能把首个 host API 的损坏音频当作可用结果。
        with _SounddeviceInputRecorder(
            sounddevice_module=sounddevice_module,
            device=device,
            sample_rate=_MICROPHONE_SAMPLE_RATE,
            channels=_MICROPHONE_CHANNELS,
        ) as recorder:
            return normalize_capture_audio(recorder.record(frame_count), _MICROPHONE_CHANNELS)
    except Exception:
        return None


def _successful_probe_payload(audio: Any, *, include_levels: bool) -> dict[str, object]:
    """构造探测成功载荷。"""

    payload: dict[str, object] = {
        "status": "available",
        "error_type": None,
        "error_message": None,
    }
    if include_levels:
        frame = np.asarray(audio, dtype=np.float32)
        payload["peak_level"] = float(np.max(np.abs(frame))) if frame.size else 0.0
        payload["rms_level"] = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
    return payload


def _successful_test_payload(audio: Any) -> dict[str, object]:
    """构造三秒麦克风测试成功载荷。

    Args:
        audio: 录制得到的浮点音频数组。

    Returns:
        包含音量统计、分级和 WAV 回放数据的 JSON 兼容字典。
    """

    frame = _normalize_float_audio(audio)
    peak_level = float(np.max(np.abs(frame))) if frame.size else 0.0
    rms_level = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
    return {
        "status": "available",
        "peak_level": peak_level,
        "rms_level": rms_level,
        "duration_ms": _duration_ms(frame),
        "sample_rate": _MICROPHONE_SAMPLE_RATE,
        "level_status": _classify_level(peak_level=peak_level, rms_level=rms_level),
        "audio_wav_b64": _encode_wav_b64(frame),
        "error_type": None,
        "error_message": None,
    }


def _failed_test_payload(
    status: ProbeStatus,
    exception: Exception | None = None,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
) -> dict[str, object]:
    """构造三秒麦克风测试失败载荷。

    Args:
        status: 设备测试状态。
        exception: 可选原始异常。
        error_type: 无异常对象时的稳定错误类型。
        error_message: 无异常对象时的用户可读错误。

    Returns:
        包含失败状态和空录音字段的 JSON 兼容字典。
    """

    resolved_error_type = error_type if error_type is not None else (type(exception).__name__ if exception else None)
    resolved_error_message = error_message if error_message is not None else (str(exception) if exception else None)
    return {
        "status": status,
        "peak_level": 0.0,
        "rms_level": 0.0,
        "duration_ms": 0,
        "sample_rate": _MICROPHONE_SAMPLE_RATE,
        "level_status": "silent",
        "audio_wav_b64": None,
        "error_type": resolved_error_type,
        "error_message": resolved_error_message,
    }


def _normalize_float_audio(audio: Any) -> np.ndarray:
    """把录音结果规整为单声道 float32 数组。

    Args:
        audio: soundcard 或 sounddevice 返回的音频数据。

    Returns:
        一维 float32 单声道数组。
    """

    frame = np.asarray(audio, dtype=np.float32)
    if frame.ndim == 0:
        return frame.reshape(1)
    if frame.ndim == 1:
        return frame
    return frame[:, 0]


def _duration_ms(frame: np.ndarray) -> int:
    """按采样点数量计算测试录音时长毫秒数。"""

    return int(round(frame.size / _MICROPHONE_SAMPLE_RATE * 1000))


def _classify_level(*, peak_level: float, rms_level: float) -> str:
    """把音量统计映射为用户可理解的测试结果。"""

    if peak_level >= 0.98:
        return "clipping"
    if peak_level < 0.005 or rms_level < 0.001:
        return "silent"
    if peak_level < 0.05 or rms_level < 0.015:
        return "low"
    return "good"


def _encode_wav_b64(frame: np.ndarray) -> str:
    """把单声道 float32 音频编码为 base64 WAV。

    Args:
        frame: 单声道 float32 音频数组。

    Returns:
        base64 编码的 PCM16 WAV 字符串。
    """

    clipped = np.clip(frame, -1.0, 1.0)
    pcm16 = np.round(clipped * 32767.0).astype("<i2")
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(_MICROPHONE_CHANNELS)
        wav_file.setsampwidth(2)
        wav_file.setframerate(_MICROPHONE_SAMPLE_RATE)
        wav_file.writeframes(pcm16.tobytes())
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _load_sounddevice_module() -> Any | None:
    """延迟加载 sounddevice 模块。"""

    try:
        import sounddevice as sd

        return sd
    except Exception:
        return None


def _failed_probe_payload(status: ProbeStatus, exception: Exception, *, include_levels: bool) -> dict[str, object]:
    """构造探测失败载荷。"""

    payload: dict[str, object] = {
        "status": status,
        "error_type": type(exception).__name__,
        "error_message": str(exception),
    }
    if include_levels:
        payload["peak_level"] = 0.0
        payload["rms_level"] = 0.0
    return payload
