from __future__ import annotations

from dataclasses import dataclass
import json
import sys
from types import TracebackType
from typing import Any
import warnings

import numpy as np
import soundcard as sc

from audio_aec_poc.capture_format import normalize_capture_audio, soundcard_capture_channel_count


_VIRTUAL_MICROPHONE_NAME_MARKERS = (
    "voicemeeter",
    "vb-audio",
    "virtual",
    "cable",
)
_AUTO_SOUNDDEVICE_MODULE = object()
_SOUNDDEVICE_PROBE_MIN_FRAMES = 32
_SOUNDDEVICE_PAIR_DUPLICATION_THRESHOLD = 0.98
_SOUNDDEVICE_SIGNAL_FLOOR = 1e-5
_SOUNDDEVICE_DIGITAL_SILENCE_FLOOR = 1e-8
_SOUNDCARD_DATA_DISCONTINUITY_WARNING = "data discontinuity in recording"


@dataclass(frozen=True)
class DeviceInfo:
    """记录本次 PoC 使用的默认输入与输出设备名称。

    功能：把麦克风名称和扬声器名称组合成不可变值对象，便于日志、界面或后续诊断代码展示实际设备。
    核心入参：microphone_name 是默认麦克风名称，speaker_name 是默认扬声器名称。
    预期输出：创建后字段不可变，调用方可安全传递而不担心被中途改写。
    边界异常：本数据类不主动校验设备是否仍在线，设备存在性由 SoundcardCapture 初始化阶段确认。
    """

    # 默认麦克风的可读名称，用于标识近端人声采集来源。
    microphone_name: str
    # 默认扬声器的可读名称，用于标识 loopback 参考音频来源。
    speaker_name: str


class _SounddeviceInputRecorder:
    """基于 sounddevice 的麦克风输入 recorder 适配器。

    Args:
        sounddevice_module: sounddevice 模块或测试替身。
        device: soundcard 枚举到的麦克风设备，用名称映射到 sounddevice 输入设备。
        sample_rate: 采样率。
        channels: 声道数。

    Returns:
        提供 record 和上下文协议的 recorder 兼容对象。
    """

    def __init__(self, *, sounddevice_module: Any, device: Any, sample_rate: int, channels: int) -> None:
        """保存 sounddevice 输入流配置。"""

        self._sounddevice = sounddevice_module
        self._device = device
        self._sample_rate = sample_rate
        self._channels = channels
        self._stream: Any | None = None

    def __enter__(self) -> "_SounddeviceInputRecorder":
        """打开 sounddevice 输入流。"""

        device_indices = _resolve_sounddevice_input_device_indices(
            self._sounddevice,
            device_name=str(getattr(self._device, "name", "")),
            sample_rate=self._sample_rate,
        )
        if not device_indices:
            raise RuntimeError("Matching sounddevice input device was not found.")

        deferred_index: int | None = None
        deferred_reason: str | None = None
        open_exception: BaseException | None = None

        for device_index in device_indices:
            try:
                stream = self._open_stream(device_index)
            except BaseException as exception:
                # 某些 PortAudio host API 可枚举但不支持阻塞流，继续尝试同名的其它 host API。
                open_exception = exception
                _emit_sounddevice_candidate_diagnostic(
                    self._device,
                    device_index=device_index,
                    reason="open_failed",
                    exception=exception,
                )
                continue

            try:
                rejection_reason = _probe_sounddevice_stream_rejection_reason(
                    stream,
                    sample_rate=self._sample_rate,
                )
            except Exception as exception:
                # 能 start 不代表能 read；探测失败也要释放设备，再尝试同一麦克风的其他后端。
                _close_sounddevice_stream(stream)
                open_exception = exception
                _emit_sounddevice_candidate_diagnostic(
                    self._device,
                    device_index=device_index,
                    reason="probe_failed",
                    exception=exception,
                )
                continue
            if rejection_reason is None:
                self._stream = stream
                return self

            # 成对重复或全零流会制造“设备已打开但听起来不对/没声音”的假阳性，先跳过并寻找其它同名候选。
            _close_sounddevice_stream(stream)
            _emit_sounddevice_candidate_diagnostic(
                self._device,
                device_index=device_index,
                reason=rejection_reason,
            )
            if deferred_index is None:
                deferred_index = device_index
                deferred_reason = rejection_reason

        if deferred_index is not None:
            # 如果所有同名候选都不健康，仍回退到第一个可打开流，让上层保持原有可启动行为并留下清晰诊断。
            self._stream = self._open_stream(deferred_index)
            _emit_sounddevice_candidate_diagnostic(
                self._device,
                device_index=deferred_index,
                reason=f"selected_with_warning:{deferred_reason}",
            )
            return self

        if open_exception is not None:
            raise RuntimeError("Matching sounddevice input devices could not be opened.") from open_exception

        raise RuntimeError("Matching sounddevice input device was not usable.")

    def _open_stream(self, device_index: int) -> Any:
        """打开指定 sounddevice 输入流并启动读取。

        Args:
            device_index: sounddevice 输入设备索引。

        Returns:
            已经 start 的输入流对象。
        """

        stream = self._sounddevice.InputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="float32",
            device=device_index,
        )
        stream.start()
        return stream

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭 sounddevice 输入流。"""

        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        finally:
            self._stream = None

    def record(self, numframes: int) -> np.ndarray:
        """读取指定帧数的麦克风音频。"""

        if self._stream is None:
            raise RuntimeError("Sounddevice recorder must be opened before reading.")

        frame, _overflowed = self._stream.read(numframes)
        return np.asarray(frame, dtype=np.float32)


class SoundcardCapture:
    """基于 soundcard 打开默认麦克风与默认扬声器 loopback 的采集上下文。

    Args:
        sample_rate: 底层采集采样率。
        channels: 业务输出声道数；Windows 底层使用设备原生声道采集后再混音。
        frame_samples: 每次读取的每声道样本数。
        soundcard_module: soundcard 模块或测试替身。
        sounddevice_module: 真单声道麦克风或 soundcard 不兼容时使用的同设备替代后端。
        audio_device_mode: 系统默认或手动设备模式。
        microphone_id: 用户选择的麦克风设备标识。
        microphone_name: 用户选择的麦克风设备名称。
        speaker_loopback_id: 用户选择的扬声器 loopback 标识。
        speaker_loopback_name: 用户选择的扬声器 loopback 名称。

    Returns:
        管理两路录音器生命周期，并输出二维 float32 业务音频的采集上下文。
    """

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        frame_samples: int,
        *,
        soundcard_module: Any = sc,
        sounddevice_module: Any = _AUTO_SOUNDDEVICE_MODULE,
        audio_device_mode: str = "system_default",
        microphone_id: str | None = None,
        microphone_name: str | None = None,
        speaker_loopback_id: str | None = None,
        speaker_loopback_name: str | None = None,
    ) -> None:
        """保存采集参数并解析当前系统默认音频设备。

        功能：记录固定采集配置，获取默认麦克风和默认扬声器，并为扬声器准备 loopback 输入设备。
        核心入参：sample_rate 控制 recorder 采样率，channels 控制业务返回数组列数，frame_samples 控制每次读取长度。
        预期输出：生成一个尚未打开 recorder、但已绑定默认设备信息的采集器实例。
        边界异常：系统没有默认麦克风或默认扬声器时抛出 RuntimeError，避免进入上下文后才暴露配置缺失。
        """
        # 采样率、声道数和帧长共同定义采集契约，实例生命周期内保持不变。
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_samples = frame_samples
        self._soundcard = soundcard_module
        self._sounddevice = _load_sounddevice_module() if sounddevice_module is _AUTO_SOUNDDEVICE_MODULE else sounddevice_module
        self._audio_device_mode = audio_device_mode
        self._microphone_id = microphone_id
        self._microphone_name = microphone_name
        self._speaker_loopback_id = speaker_loopback_id
        self._speaker_loopback_name = speaker_loopback_name

        # 麦克风负责采集近端人声，手动模式严格使用用户选择，系统默认模式允许后续打开失败时 fallback。
        self._microphone = self._select_initial_microphone()
        # 默认扬声器代表系统当前播放终端，其 loopback 流作为 AEC 远端参考。
        self._speaker = self._soundcard.default_speaker()

        # 没有默认麦克风时无法获得近端输入，提前失败能让调用方给出明确设备提示。
        if self._microphone is None:
            raise RuntimeError("No default microphone is available.")

        # 没有默认扬声器时无法建立回声参考，AEC PoC 的核心验证条件不成立。
        if self._speaker is None:
            raise RuntimeError("No default speaker is available.")

        # soundcard 在 Windows 上通过同名 loopback microphone 暴露扬声器回放流。
        self._speaker_loopback = self._select_speaker_loopback()

        # recorder 只在上下文打开期间存在，None 表示当前禁止读取。
        self._microphone_recorder: Any | None = None
        # 扬声器参考 recorder 同样受上下文管理，避免设备句柄泄漏。
        self._speaker_recorder: Any | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """返回当前绑定的默认设备名称。

        功能：把初始化阶段解析到的默认麦克风和默认扬声器名称封装为 DeviceInfo。
        核心入参：无显式入参，读取实例内保存的 soundcard 设备对象。
        预期输出：返回包含 microphone_name 和 speaker_name 的不可变数据对象。
        边界异常：初始化已确保设备对象存在，因此本属性不会因缺少默认设备抛出业务异常。
        """
        # 设备名称来自初始化时的默认设备快照，用于让一次采集运行具备可追溯性。
        return DeviceInfo(
            microphone_name=self._microphone.name,
            speaker_name=self._speaker.name,
        )

    def __enter__(self) -> "SoundcardCapture":
        """打开麦克风 recorder 和扬声器 loopback recorder。

        Args:
            无额外参数，使用构造时保存的设备与采集配置。

        Returns:
            当前采集器，供 with 块内读取固定长度的业务音频帧。

        Raises:
            ValueError: 扬声器 loopback 只有 Windows soundcard 无法可靠采集的单声道。
            RuntimeError: 设备不可用或无法打开时由底层采集后端抛出。
        """
        # 先确认近端麦克风；手动模式只允许为同一设备更换后端，系统默认模式才可选择备用设备。
        self._microphone_recorder = self._open_microphone_recorder_with_fallback()

        try:
            reference_channels = soundcard_capture_channel_count(self._speaker_loopback, self._channels)
            # 参考流也不能请求 Windows 单声道，原生多声道在读取出口统一混音。
            self._speaker_recorder = self._speaker_loopback.recorder(
                samplerate=self._sample_rate,
                channels=reference_channels,
            )
            self._speaker_recorder.__enter__()
        except ValueError as exception:
            self.__exit__(None, None, None)
            # sounddevice 的普通 InputStream 不提供 WASAPI loopback；不能转到另一个麦克风伪装回声参考。
            raise ValueError("扬声器 loopback 采集格式不兼容；若输出设备是单声道，请在 Windows 中改为双声道。") from exception
        except BaseException:
            # 任一路设备打开失败都会让采集器不可用，因此立即释放已打开的资源并保留原始异常。
            self.__exit__(None, None, None)
            raise

        return self

    def _select_initial_microphone(self) -> Any:
        """按模式选择初始麦克风设备。

        功能：手动模式按用户保存的 id/name 查找，系统默认模式读取系统默认麦克风。
        核心入参：无显式入参，读取实例中的设备模式和选择字段。
        预期输出：返回 soundcard 麦克风设备。
        边界异常：找不到设备时输出结构化诊断并抛出 RuntimeError。
        """

        if self._audio_device_mode == "manual":
            device = self._find_microphone(device_id=self._microphone_id, device_name=self._microphone_name)
            if device is None:
                _emit_device_diagnostic(
                    {
                        "event": "aec-sidecar-device-error",
                        "stage": "microphone_select",
                        "error_type": "device_not_found",
                        "requested_device": {
                            "id": self._microphone_id,
                            "name": self._microphone_name,
                        },
                        "suggestion": "Open Console audio settings and save a working microphone.",
                    }
                )
                raise RuntimeError("Selected microphone device is not available.")
            return device

        return self._soundcard.default_microphone()

    def _select_speaker_loopback(self) -> Any:
        """按用户选择或默认扬声器名称选择 loopback 参考设备。"""

        selected_loopback = self._find_speaker_loopback(
            device_id=self._speaker_loopback_id,
            device_name=self._speaker_loopback_name,
        )
        if selected_loopback is not None:
            return selected_loopback
        return self._soundcard.get_microphone(self._speaker.name, include_loopback=True)

    def _open_microphone_recorder_with_fallback(self) -> Any:
        """打开麦克风 recorder，并在系统默认模式下自动 fallback。"""

        try:
            return self._open_microphone_recorder(self._microphone)
        except BaseException as exception:
            if self._audio_device_mode != "system_default":
                _emit_device_open_error(self._microphone, exception)
                raise

            fallback_device = self._select_fallback_microphone(excluded_device=self._microphone)
            if fallback_device is None:
                _emit_device_open_error(self._microphone, exception)
                raise

            failed_device = self._microphone
            self._microphone = fallback_device
            _emit_device_diagnostic(
                {
                    "event": "aec-sidecar-device-fallback",
                    "stage": "microphone_open",
                    "failed_device": _device_payload(failed_device),
                    "selected_device": _device_payload(fallback_device),
                    "error_type": type(exception).__name__,
                    "error_message": str(exception),
                    "suggestion": "Open Console audio settings and save a working microphone.",
                }
            )
            return self._open_microphone_recorder(fallback_device)

    def _open_microphone_recorder(self, device: Any) -> Any:
        """采集设备原生声道；Windows 真单声道或格式不兼容时改用同设备 sounddevice。"""

        try:
            recorder = device.recorder(
                samplerate=self._sample_rate,
                channels=soundcard_capture_channel_count(device, self._channels),
            )
            recorder.__enter__()
            return recorder
        except BaseException as exception:
            if self._sounddevice is None:
                raise

            recorder = _SounddeviceInputRecorder(
                sounddevice_module=self._sounddevice,
                device=device,
                sample_rate=self._sample_rate,
                channels=self._channels,
            )
            try:
                recorder.__enter__()
            except BaseException:
                raise exception

            _emit_device_diagnostic(
                {
                    "event": "aec-sidecar-device-backend-fallback",
                    "stage": "microphone_open",
                    "device": _device_payload(device),
                    "failed_backend": "soundcard",
                    "selected_backend": "sounddevice",
                    "error_type": type(exception).__name__,
                    "error_message": str(exception),
                    "suggestion": "Soundcard could not open this microphone; sounddevice captured it instead.",
                }
            )
            return recorder

    def _select_fallback_microphone(self, *, excluded_device: Any) -> Any | None:
        """选择第一个可打开的备用真实麦克风。"""

        excluded_id = _device_id(excluded_device)
        candidates = [
            device
            for device in self._all_real_microphones()
            if _device_id(device) != excluded_id
        ]
        candidates.sort(key=_microphone_fallback_priority)

        for device in candidates:
            if _device_id(device) == excluded_id:
                continue
            if _can_open_microphone(device, self._sample_rate, self._channels, self._sounddevice):
                return device
        return None

    def _find_microphone(self, *, device_id: str | None, device_name: str | None) -> Any | None:
        """按 id 或名称查找真实麦克风。"""

        for device in self._all_real_microphones():
            if device_id is not None and _device_id(device) == device_id:
                return device
            if device_name is not None and getattr(device, "name", None) == device_name:
                return device
        return None

    def _find_speaker_loopback(self, *, device_id: str | None, device_name: str | None) -> Any | None:
        """按 id 或名称查找扬声器 loopback。"""

        if device_id is None and device_name is None:
            return None
        for device in self._soundcard.all_microphones(include_loopback=True):
            if not bool(getattr(device, "isloopback", False)):
                continue
            if device_id is not None and _device_id(device) == device_id:
                return device
            if device_name is not None and getattr(device, "name", None) == device_name:
                return device
        return None

    def _all_real_microphones(self) -> list[Any]:
        """返回非 loopback 麦克风设备。"""

        return [
            device
            for device in self._soundcard.all_microphones(include_loopback=True)
            if not bool(getattr(device, "isloopback", False))
        ]

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭两路 recorder，并保留 with 块内抛出的原始异常。

        功能：退出上下文时释放麦克风和 loopback 录音资源，并清空 recorder 引用。
        核心入参：exc_type、exc_value、traceback 是 Python 上下文协议传入的异常信息。
        预期输出：设备句柄被关闭，方法返回 None 以避免吞掉调用方异常。
        边界异常：关闭 recorder 时若 soundcard 抛错，会按上下文协议继续向外传播。
        """
        try:
            # 先关闭麦克风 recorder，确保近端输入设备尽快释放给系统或其他程序。
            if self._microphone_recorder is not None:
                self._microphone_recorder.__exit__(exc_type, exc_value, traceback)

            # 再关闭扬声器参考 recorder，保持两路资源都在退出阶段得到释放。
            if self._speaker_recorder is not None:
                self._speaker_recorder.__exit__(exc_type, exc_value, traceback)
        finally:
            # 无论关闭过程是否抛错，都清空引用，避免调用方误以为上下文仍处于打开状态。
            self._microphone_recorder = None
            self._speaker_recorder = None

        return None

    def read_microphone(self) -> np.ndarray:
        """读取一帧默认麦克风音频。

        Args:
            无额外参数，读取长度来自构造时保存的 frame_samples。

        Returns:
            形状为“实际样本帧 × 业务声道”的 float32 数组，单声道输出混合所有原生声道。

        Raises:
            RuntimeError: 未进入采集上下文或底层读取失败。
            ValueError: 采集音频形状或采样值不符合业务契约。
        """
        # 未打开上下文时拒绝读取，避免调用方在生命周期错误下得到误导性的空数据。
        if self._microphone_recorder is None:
            raise RuntimeError("read_microphone() must be called inside an open context.")

        # soundcard 按样本数读取当前 recorder 数据，返回值可能是一维或二维浮点数组。
        audio = self._microphone_recorder.record(numframes=self._frame_samples)
        return normalize_capture_audio(audio, self._channels)

    def read_speaker_reference(self) -> np.ndarray:
        """读取一帧默认扬声器 loopback 参考音频。

        Args:
            无额外参数，读取长度来自构造时保存的 frame_samples。

        Returns:
            与麦克风业务格式一致的二维 float32 参考帧，单声道输出混合所有原生声道。

        Raises:
            RuntimeError: 未进入采集上下文或底层读取失败。
            ValueError: 采集音频形状或采样值不符合业务契约。
        """
        # 未打开上下文时拒绝读取，保证参考流生命周期和麦克风流保持一致。
        if self._speaker_recorder is None:
            raise RuntimeError("read_speaker_reference() must be called inside an open context.")

        # 部分 Windows loopback 驱动会对每个有效 capture buffer 标记 DATA_DISCONTINUITY。
        # 这会让 soundcard 持续向 stderr 打假阳性 warning，但实际参考音频仍可正常读取。
        audio = _record_loopback_reference_without_discontinuity_warning(
            self._speaker_recorder,
            numframes=self._frame_samples,
        )
        return normalize_capture_audio(audio, self._channels)

    def read_frame_pair(self) -> tuple[np.ndarray, np.ndarray]:
        """按现有本地低延迟顺序读取一组 AEC 输入帧。

        Returns:
            依次返回扬声器参考帧和麦克风帧，底层设备、参数和读取顺序均保持不变。
        """

        # 本地路径不需要额外缓冲；这里只把原有两次相邻读取组合成统一的原子调用契约。
        reference_frame = self.read_speaker_reference()
        microphone_frame = self.read_microphone()
        return reference_frame, microphone_frame


def _device_id(device: Any) -> str | None:
    """读取 soundcard 设备标识。"""

    value = getattr(device, "id", None)
    if value is None:
        value = getattr(device, "_id", None)
    return str(value) if value is not None else None


def _record_loopback_reference_without_discontinuity_warning(recorder: Any, *, numframes: int) -> Any:
    """读取扬声器 loopback，并吞掉 soundcard 在 loopback 上的断续假阳性。

    Args:
        recorder: 已打开的 soundcard loopback recorder。
        numframes: 本次需要读取的样本帧数。

    Returns:
        recorder 返回的原始音频数组。
    """

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=f"^{_SOUNDCARD_DATA_DISCONTINUITY_WARNING}$",
            category=RuntimeWarning,
        )
        return recorder.record(numframes=numframes)


def _device_payload(device: Any) -> dict[str, str | None]:
    """构造设备诊断载荷。"""

    return {
        "id": _device_id(device),
        "name": str(getattr(device, "name", "")),
    }


def _can_open_microphone(device: Any, sample_rate: int, channels: int, sounddevice_module: Any | None = None) -> bool:
    """检查麦克风是否能按 sidecar 格式打开。"""

    try:
        recorder = device.recorder(samplerate=sample_rate, channels=soundcard_capture_channel_count(device, channels))
        recorder.__enter__()
        recorder.__exit__(None, None, None)
        return True
    except BaseException:
        if sounddevice_module is None:
            return False

    try:
        recorder = _SounddeviceInputRecorder(
            sounddevice_module=sounddevice_module,
            device=device,
            sample_rate=sample_rate,
            channels=channels,
        )
        recorder.__enter__()
        recorder.__exit__(None, None, None)
        return True
    except BaseException:
        return False


def _load_sounddevice_module() -> Any | None:
    """延迟加载 sounddevice 模块。"""

    try:
        import sounddevice as sd

        return sd
    except Exception:
        return None


def _resolve_sounddevice_input_device_indices(
    sounddevice_module: Any,
    *,
    device_name: str,
    sample_rate: int,
) -> list[int]:
    """按设备名称解析 sounddevice 输入设备候选索引。

    Args:
        sounddevice_module: sounddevice 模块或测试替身。
        device_name: soundcard 侧枚举出的设备名。
        sample_rate: 侧车期望采样率。

    Returns:
        按优先级排序后的 sounddevice 输入设备索引列表。
    """

    candidates: list[tuple[tuple[int, int, int], int]] = []
    for index, device in enumerate(sounddevice_module.query_devices()):
        name = str(device.get("name", ""))
        if int(device.get("max_input_channels", 0)) <= 0:
            continue
        if not _sounddevice_names_match(name, device_name):
            continue

        samplerate = float(device.get("default_samplerate", 0.0) or 0.0)
        exact_name_priority = 0 if name == device_name else 1
        samplerate_priority = 0 if abs(samplerate - sample_rate) < 1 else 1
        candidates.append(((exact_name_priority, samplerate_priority, index), index))

    candidates.sort(key=lambda item: item[0])
    return [index for _priority, index in candidates]


def _probe_sounddevice_stream_rejection_reason(stream: Any, *, sample_rate: int) -> str | None:
    """探测 sounddevice 流是否存在明显不可用特征。

    Args:
        stream: 已启动的 sounddevice 输入流。
        sample_rate: 当前输入流采样率。

    Returns:
        返回 None 表示探测通过；否则返回稳定的拒绝原因字符串。
    """

    probe_frames = max(sample_rate // 100, _SOUNDDEVICE_PROBE_MIN_FRAMES)
    frame, _overflowed = stream.read(probe_frames)
    audio = np.asarray(frame, dtype=np.float32)
    if audio.ndim == 0:
        return "empty_probe"

    flattened = audio.reshape(-1)
    if flattened.size < _SOUNDDEVICE_PROBE_MIN_FRAMES:
        return "empty_probe"

    peak = float(np.max(np.abs(flattened)))
    if peak <= _SOUNDDEVICE_DIGITAL_SILENCE_FLOOR:
        return "digital_silence"

    even = flattened[0::2]
    odd = flattened[1::2]
    pair_count = min(even.size, odd.size)
    if pair_count == 0:
        return None

    # Windows WASAPI 的部分 USB 麦克风会在阻塞 read() 中把相邻采样点成对复制，导致人声听起来低沉失真。
    equal_pairs = np.isclose(even[:pair_count], odd[:pair_count], rtol=0.0, atol=0.0)
    equal_ratio = float(np.mean(equal_pairs))
    if peak > _SOUNDDEVICE_SIGNAL_FLOOR and equal_ratio >= _SOUNDDEVICE_PAIR_DUPLICATION_THRESHOLD:
        return "pair_duplicated_samples"

    return None


def _close_sounddevice_stream(stream: Any) -> None:
    """关闭 sounddevice 输入流。

    Args:
        stream: 待关闭的 sounddevice 输入流。

    Returns:
        无返回值。
    """

    try:
        stream.stop()
    finally:
        stream.close()


def _emit_sounddevice_candidate_diagnostic(
    device: Any,
    *,
    device_index: int,
    reason: str,
    exception: BaseException | None = None,
) -> None:
    """输出 sounddevice 候选设备诊断。

    Args:
        device: soundcard 侧对应的麦克风设备。
        device_index: sounddevice 侧候选索引。
        reason: 跳过或警告原因。
        exception: 打开候选失败时的原始异常。

    Returns:
        无返回值。
    """

    payload: dict[str, Any] = {
        "event": "aec-sidecar-sounddevice-candidate",
        "stage": "microphone_open",
        "backend": "sounddevice",
        "device": _device_payload(device),
        "sounddevice_index": device_index,
        "reason": reason,
        "suggestion": "Trying another matching host API, or open Console audio settings and save a working microphone.",
    }
    if exception is not None:
        payload["error_type"] = type(exception).__name__
        payload["error_message"] = str(exception)
    _emit_device_diagnostic(payload)


def _sounddevice_names_match(sounddevice_name: str, soundcard_name: str) -> bool:
    """判断 sounddevice 名称和 soundcard 名称是否指向同一输入设备。"""

    if sounddevice_name == soundcard_name:
        return True
    return bool(sounddevice_name and soundcard_name) and (
        sounddevice_name.startswith(soundcard_name) or soundcard_name.startswith(sounddevice_name)
    )


def _microphone_fallback_priority(device: Any) -> int:
    """计算自动 fallback 的麦克风优先级。"""

    # Voicemeeter、VB-Cable 一类虚拟端常能打开但没有真实近端人声，自动救援时放到物理麦克风之后。
    return 1 if _is_likely_virtual_microphone(device) else 0


def _is_likely_virtual_microphone(device: Any) -> bool:
    """判断设备名称是否像虚拟音频端。"""

    name = str(getattr(device, "name", "")).lower()
    return any(marker in name for marker in _VIRTUAL_MICROPHONE_NAME_MARKERS)


def _emit_device_open_error(device: Any, exception: BaseException) -> None:
    """输出结构化设备打开错误。"""

    _emit_device_diagnostic(
        {
            "event": "aec-sidecar-device-error",
            "stage": "microphone_open",
            "device": _device_payload(device),
            "error_type": type(exception).__name__,
            "error_message": str(exception),
            "suggestion": "Open Console audio settings and save a working microphone.",
        }
    )


def _emit_device_diagnostic(payload: dict[str, Any]) -> None:
    """向 stderr 输出单行 JSON 设备诊断。"""

    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
