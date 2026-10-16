from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Protocol, Sequence
import uuid

import numpy as np
import soxr

from audio_aec_poc.rdp_clock_recovery import RdpClockRecoveryController
from audio_aec_poc.rdp_time_stretch import RdpWsolaTimeStretch
from audio_aec_poc.rdp_wav_stream import IncrementalWavReader, RdpMicrophoneFrameBuffer, WavAudioFormat


_ENDPOINT_GUID_PATTERN = re.compile(
    r"\{?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\}?",
    re.IGNORECASE,
)
_MEDIA_CAPTURE_POLL_INTERVAL_SECONDS = 0.01


@dataclass(frozen=True)
class RdpAudioCaptureDevice:
    """保存 WinRT 音频采集设备的稳定 ID 和展示名称。

    Args:
        device_id: WinRT DeviceInformation 的设备 ID。
        name: Windows 展示的设备名称。

    Returns:
        一个不可变的设备描述。
    """

    device_id: str
    name: str


@dataclass(frozen=True)
class RdpMediaCaptureDiagnostics:
    """保存 MediaCapture worker 的可观测状态。

    Args:
        output_sample_count: 已送入固定帧缓冲的目标采样率样本数。
        wav_sample_rate: 当前 WAV 头报告的原始采样率。
        session_started: MediaCapture 是否已经开始录音。
        input_sample_count: 已经从 WAV 读出的原始单声道样本数。
        estimated_input_rate: 按墙钟估算的 RDP 实际输入速率。
        wsola_speed: 当前 WSOLA 速度。
        buffered_output_samples: 最近一次诊断时 ring buffer 中的样本数。
        clock_recovery_active: 当前是否正在修正持续时钟偏差。
        failure_reason: 首次失败原因，没有失败时为空。
        buffer_underflow: ring buffer 是否曾经发生下溢。
        buffer_overflow: ring buffer 是否曾经发生上溢。
        terminal_partial_sample_count: 录音结束时不足一个 10ms 帧的尾部样本数。
        shutdown_timed_out: worker 是否超过关闭等待预算仍未退出。

    Returns:
        一个不可变的 worker 诊断快照。
    """

    output_sample_count: int
    wav_sample_rate: int | None
    session_started: bool
    input_sample_count: int = 0
    estimated_input_rate: float | None = None
    wsola_speed: float = 1.0
    buffered_output_samples: int = 0
    clock_recovery_active: bool = False
    failure_reason: str | None = None
    buffer_underflow: bool = False
    buffer_overflow: bool = False
    terminal_partial_sample_count: int = 0
    shutdown_timed_out: bool = False


class RdpFixedResampler:
    """在一个采集轮次内持续执行固定比例的 SoXR 采样率转换。

    RDP 的到达时钟不能通过修改 SoXR 比例来补偿，否则会让语音音高跟着网络速度变化。
    本类只使用 WAV 头和发布配置确定的固定比例；到达速率补偿由 WSOLA 完成。

    Args:
        input_rate: WAV 头声明的输入采样率。
        output_rate: sidecar 发布采样率。
        channels: 输入声道数。

    Returns:
        一个可连续处理不同大小输入块的固定重采样器。
    """

    def __init__(self, *, input_rate: int, output_rate: int, channels: int) -> None:
        """创建不带可变比例能力的 SoXR 流。"""

        if input_rate <= 0 or output_rate <= 0 or channels <= 0:
            raise ValueError("RDP fixed resampler audio format must be positive.")
        self._stream = soxr.ResampleStream(
            input_rate,
            output_rate,
            channels,
            dtype="float32",
            quality="HQ",
        )
        self._channels = channels
        self._finished = False

    def process(self, samples: np.ndarray) -> np.ndarray:
        """把一块输入转换到固定目标采样率。"""

        if self._finished:
            raise RuntimeError("RDP fixed resampler cannot process samples after finish().")
        normalized = np.asarray(samples, dtype=np.float32)
        if normalized.ndim != 2 or normalized.shape[1] != self._channels:
            raise ValueError("RDP fixed resampler samples have invalid channels.")
        if not np.all(np.isfinite(normalized)):
            raise ValueError("RDP fixed resampler samples must be finite.")
        if normalized.shape[0] == 0:
            return np.empty((0, self._channels), dtype=np.float32)
        output = self._stream.resample_chunk(np.ascontiguousarray(normalized), last=False)
        return np.asarray(output, dtype=np.float32).reshape(-1, self._channels)

    def finish(self) -> np.ndarray:
        """刷新 SoXR 尾部并结束固定重采样流。"""

        if self._finished:
            raise RuntimeError("RDP fixed resampler finish() can only be called once.")
        self._finished = True
        output = self._stream.resample_chunk(
            np.empty((0, self._channels), dtype=np.float32),
            last=True,
        )
        return np.asarray(output, dtype=np.float32).reshape(-1, self._channels)


class _MediaCaptureSession(Protocol):
    """描述真实和测试 MediaCapture 会话的最小生命周期。"""

    def start(self) -> Any:
        """开始把音频写入 WAV。"""

    def stop(self) -> Any:
        """停止录音并刷新 WAV。"""

    def close(self) -> Any:
        """释放 MediaCapture 句柄。"""


def list_winrt_audio_capture_devices() -> list[RdpAudioCaptureDevice]:
    """枚举当前 Windows 会话中的 WinRT 音频采集设备。

    Returns:
        当前可见的音频采集设备列表。

    Raises:
        RuntimeError: 当前 Python 环境没有 WinRT 依赖或 Windows 枚举失败。
    """

    async def enumerate_devices() -> list[RdpAudioCaptureDevice]:
        """在当前 worker 事件循环中执行 WinRT 异步枚举。"""

        try:
            from winrt.windows.devices.enumeration import DeviceClass, DeviceInformation
        except ImportError as exception:
            raise RuntimeError("Windows Runtime audio capture dependencies are not installed.") from exception

        try:
            # Python/WinRT 3.2.1 将带 DeviceClass 的 overload 生成为独立方法名；必须只取
            # AUDIO_CAPTURE，避免同名扬声器或 MIDI 设备参与后续名称 fallback。
            devices = await DeviceInformation.find_all_async_device_class(DeviceClass.AUDIO_CAPTURE)
        except BaseException as exception:
            raise RuntimeError("Windows Runtime audio capture device enumeration failed.") from exception
        return [
            RdpAudioCaptureDevice(device_id=str(device.id), name=str(device.name))
            for device in devices
            if "#MMDEVAPI#" in str(device.id).upper() and _extract_endpoint_guid(str(device.id)) is not None
        ]

    results: list[list[RdpAudioCaptureDevice]] = []
    failures: list[BaseException] = []

    def run_enumeration() -> None:
        """在没有 sidecar 事件循环的专用线程中完成 WinRT 枚举。"""

        try:
            results.append(asyncio.run(enumerate_devices()))
        except BaseException as exception:
            failures.append(exception)

    enumeration_thread = threading.Thread(
        target=run_enumeration,
        name="aec-rdp-winrt-device-enumeration",
    )
    enumeration_thread.start()
    enumeration_thread.join()
    if failures:
        raise failures[0]
    if len(results) != 1:
        raise RuntimeError("Windows Runtime audio capture device enumeration returned no result.")
    return results[0]


def select_rdp_audio_capture_device(
    devices: Sequence[RdpAudioCaptureDevice],
    *,
    audio_device_mode: str,
    microphone_id: str | None,
    microphone_name: str | None,
    default_microphone_id: str | None,
    default_microphone_name: str | None,
) -> RdpAudioCaptureDevice:
    """把 soundcard 选择映射到同一个 Windows endpoint 的 WinRT 设备。

    Args:
        devices: WinRT 枚举到的音频采集设备。
        audio_device_mode: `manual` 表示严格使用用户选择，否则使用默认设备。
        microphone_id: 用户保存的 soundcard 设备 ID。
        microphone_name: 用户保存的麦克风名称，仅用于调用方诊断。
        default_microphone_id: 当前 soundcard 默认麦克风 ID。
        default_microphone_name: 当前 soundcard 默认麦克风名称，仅用于调用方诊断。

    Returns:
        与目标 soundcard endpoint 对应的唯一 WinRT 设备。

    Raises:
        RuntimeError: 设备缺失、endpoint GUID 有歧义或无法安全建立映射。
    """

    if not devices:
        raise RuntimeError("No Windows Runtime audio capture devices are available.")

    # 系统默认模式必须跟随当前 Windows 默认端点，不能让历史手动选择覆盖用户刚切换的设备。
    if audio_device_mode == "manual":
        requested_id = microphone_id
    else:
        requested_id = default_microphone_id

    # 同名设备在 RDP 中可能分别代表普通麦克风和其它音频端，缺少 endpoint ID 时禁止猜测。
    if not requested_id:
        raise RuntimeError("A stable RDP microphone endpoint ID is required for Windows Runtime mapping.")
    endpoint_guid = _extract_endpoint_guid(requested_id)
    if endpoint_guid is None:
        raise RuntimeError("The selected RDP microphone ID does not contain an endpoint GUID.")

    matches = [device for device in devices if endpoint_guid in _extract_endpoint_guids(device.device_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError("The selected RDP microphone endpoint is ambiguous in Windows Runtime.")
    raise RuntimeError("The selected RDP microphone endpoint is not available in Windows Runtime.")


class _WinRTMediaCaptureSession:
    """把 WinRT MediaCapture 的异步接口适配成 worker 使用的会话对象。"""

    def __init__(self, *, wav_path: Path, device_id: str) -> None:
        """保存输出文件和 WinRT 设备 ID。"""

        self._wav_path = wav_path
        self._device_id = device_id
        self._capture: Any | None = None
        self._storage_file: Any | None = None

    async def start(self) -> None:
        """初始化指定输入设备并开始持续写 WAV。"""

        try:
            from winrt.windows.media.capture import (
                MediaCapture,
                MediaCaptureInitializationSettings,
                StreamingCaptureMode,
            )
            from winrt.windows.media.mediaproperties import AudioEncodingQuality, MediaEncodingProfile
            from winrt.windows.storage import CreationCollisionOption, StorageFolder
        except ImportError as exception:
            raise RuntimeError("Windows Runtime MediaCapture dependencies are not installed.") from exception

        folder = await StorageFolder.get_folder_from_path_async(str(self._wav_path.parent.resolve()))
        self._storage_file = await folder.create_file_async(
            self._wav_path.name,
            CreationCollisionOption.REPLACE_EXISTING,
        )
        settings = MediaCaptureInitializationSettings()
        settings.streaming_capture_mode = StreamingCaptureMode.AUDIO
        settings.audio_device_id = self._device_id
        capture = MediaCapture()
        self._capture = capture
        await capture.initialize_with_settings_async(settings)
        profile = MediaEncodingProfile.create_wav(AudioEncodingQuality.HIGH)
        await capture.start_record_to_storage_file_async(profile, self._storage_file)

    async def stop(self) -> None:
        """停止录音并等待 MediaCapture 完成 WAV 尾部刷新。"""

        capture = self._capture
        if capture is None:
            return
        await capture.stop_record_async()

    def close(self) -> None:
        """释放 WinRT MediaCapture 对象。"""

        capture = self._capture
        self._capture = None
        if capture is not None:
            capture.close()


class RdpMediaCaptureMicrophone:
    """在后台用 Windows MediaCapture 录音，并顺序填充 RDP 麦克风帧缓冲。

    Args:
        device_id: 已经通过 endpoint GUID 映射出的 WinRT 设备 ID。
        frame_buffer: 接收目标格式样本的固定 ring buffer。
        sample_rate: sidecar 需要的目标采样率。
        channels: sidecar 需要的目标声道数。
        frame_samples: sidecar 的固定 10ms 帧长。
        temp_directory: MediaCapture 临时 WAV 文件目录。
        session_factory: 可注入的 MediaCapture 会话工厂。
        clock_controller_factory: 可注入的 RDP 时钟控制器工厂。
        fixed_resampler_factory: 可注入的固定比例 SoXR 工厂。
        time_stretch_factory: 可注入的 WSOLA 工厂。
        poll_interval_seconds: 增量 WAV 轮询间隔。

    Returns:
        提供 start、stop、join 和健康诊断的后台 worker。
    """

    def __init__(
        self,
        *,
        device_id: str,
        frame_buffer: RdpMicrophoneFrameBuffer,
        sample_rate: int,
        channels: int,
        frame_samples: int,
        temp_directory: Path | None = None,
        session_factory: Callable[..., _MediaCaptureSession] = _WinRTMediaCaptureSession,
        clock_controller_factory: Callable[..., Any] = RdpClockRecoveryController,
        fixed_resampler_factory: Callable[..., Any] = RdpFixedResampler,
        time_stretch_factory: Callable[..., Any] = RdpWsolaTimeStretch,
        poll_interval_seconds: float = _MEDIA_CAPTURE_POLL_INTERVAL_SECONDS,
    ) -> None:
        """校验 worker 参数并创建尚未启动的线程状态。"""

        if not device_id.strip():
            raise ValueError("RDP MediaCapture device_id must not be blank.")
        if sample_rate <= 0 or channels <= 0 or frame_samples <= 0:
            raise ValueError("RDP MediaCapture audio format must be positive.")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than 0.")
        self._device_id = device_id
        self._frame_buffer = frame_buffer
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_samples = frame_samples
        self._temp_directory = Path(temp_directory) if temp_directory is not None else Path(tempfile.gettempdir())
        self._session_factory = session_factory
        self._clock_controller_factory = clock_controller_factory
        self._fixed_resampler_factory = fixed_resampler_factory
        self._time_stretch_factory = time_stretch_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._session: _MediaCaptureSession | None = None
        self._session_started = False
        self._input_sample_count = 0
        self._output_sample_count = 0
        self._wav_sample_rate: int | None = None
        self._estimated_input_rate: float | None = None
        self._wsola_speed = 1.0
        self._buffered_output_samples = 0
        self._clock_recovery_active = False
        self._failure_reason: str | None = None
        self._shutdown_timed_out = False
        self._diagnostic_lock = threading.Lock()

    @property
    def is_alive(self) -> bool:
        """返回后台 worker 是否仍在运行。"""

        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def diagnostics(self) -> RdpMediaCaptureDiagnostics:
        """返回当前 worker 的诊断快照。"""

        with self._diagnostic_lock:
            return RdpMediaCaptureDiagnostics(
                output_sample_count=self._output_sample_count,
                wav_sample_rate=self._wav_sample_rate,
                session_started=self._session_started,
                input_sample_count=self._input_sample_count,
                estimated_input_rate=self._estimated_input_rate,
                wsola_speed=self._wsola_speed,
                buffered_output_samples=self._buffered_output_samples,
                clock_recovery_active=self._clock_recovery_active,
                failure_reason=self._failure_reason,
                buffer_underflow=self._frame_buffer.has_underflow,
                buffer_overflow=self._frame_buffer.has_overflow,
                terminal_partial_sample_count=self._frame_buffer.terminal_partial_sample_count,
                shutdown_timed_out=self._shutdown_timed_out,
            )

    def start(self) -> None:
        """启动唯一的 MediaCapture worker 线程。"""

        if self._thread is not None:
            raise RuntimeError("RDP MediaCapture microphone cannot be started twice.")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="aec-rdp-mediacapture-microphone",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """设置停止标志，worker 随后停止录音并刷新 WAV。"""

        self._stop_event.set()

    def join(self, timeout_seconds: float = 3.0) -> bool:
        """等待 worker 完成关闭。

        Args:
            timeout_seconds: 允许 worker 退出的最长时间。

        Returns:
            线程已经退出时返回 True，否则返回 False。
        """

        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_seconds)
        if not thread.is_alive():
            return True
        exception = TimeoutError("RDP MediaCapture worker did not stop within the shutdown budget.")
        with self._diagnostic_lock:
            self._shutdown_timed_out = True
            if self._failure_reason is None:
                self._failure_reason = f"{type(exception).__name__}: {exception}"
        # 让仍在等待帧的消费者立即知道本轮已经失败；worker 引用故意保留，避免误报已释放。
        self._frame_buffer.mark_failed(exception)
        return False

    def ensure_healthy(self) -> None:
        """确认 worker 仍在工作，否则向采集管线报告失败。"""

        self._frame_buffer.raise_if_failed()
        if not self._stop_event.is_set() and self._thread is not None and not self._thread.is_alive():
            exception = RuntimeError("RDP MediaCapture worker stopped unexpectedly.")
            with self._diagnostic_lock:
                if self._failure_reason is None:
                    self._failure_reason = f"{type(exception).__name__}: {exception}"
            self._frame_buffer.mark_failed(exception)
            self._frame_buffer.raise_if_failed()

    def _run(self) -> None:
        """在线程中运行独立事件循环，并把所有异常交给帧缓冲。"""

        try:
            asyncio.run(self._run_async())
        except BaseException as exception:
            with self._diagnostic_lock:
                if self._failure_reason is None:
                    self._failure_reason = f"{type(exception).__name__}: {exception}"
            self._frame_buffer.mark_failed(exception)

    async def _run_async(self) -> None:
        """启动录音、固定重采样、WSOLA 恢复时钟并完成最终排空。"""

        self._temp_directory.mkdir(parents=True, exist_ok=True)
        wav_path = self._temp_directory / f"nearmate-rdp-{uuid.uuid4().hex}.wav"
        session: _MediaCaptureSession | None = None
        reader = IncrementalWavReader(wav_path)
        fixed_resampler: Any | None = None
        clock_controller: Any | None = None
        time_stretch: Any | None = None
        primary_exception: BaseException | None = None
        cleanup_exception: BaseException | None = None
        stop_attempted = False
        try:
            session = self._session_factory(wav_path=wav_path, device_id=self._device_id)
            self._session = session
            await _await_if_needed(session.start())
            with self._diagnostic_lock:
                self._session_started = True

            while not self._stop_event.is_set():
                fixed_resampler, clock_controller, time_stretch = self._consume_available_audio(
                    reader,
                    fixed_resampler=fixed_resampler,
                    clock_controller=clock_controller,
                    time_stretch=time_stretch,
                )
                await asyncio.sleep(self._poll_interval_seconds)

            stop_attempted = True
            await _await_if_needed(session.stop())
            # stop_record_async 完成后文件已经定稿，但尾包可能远大于单次读取上限；必须读到本轮无新增数据。
            while True:
                previous_bytes = reader.data_bytes_read
                fixed_resampler, clock_controller, time_stretch = self._consume_available_audio(
                    reader,
                    fixed_resampler=fixed_resampler,
                    clock_controller=clock_controller,
                    time_stretch=time_stretch,
                    final=True,
                )
                if reader.data_bytes_read == previous_bytes:
                    break
                await asyncio.sleep(0)
            if reader.audio_format is None:
                raise RuntimeError("RDP MediaCapture did not produce a readable WAV header.")
            if fixed_resampler is not None and time_stretch is not None:
                fixed_tail = fixed_resampler.finish()
                if fixed_tail.shape[0] > 0:
                    self._append_output(time_stretch.process(fixed_tail))
                stretch_tail = time_stretch.finish()
                if stretch_tail.shape[0] > 0:
                    self._append_output(stretch_tail)
        except BaseException as exception:
            primary_exception = exception
            raise
        finally:
            # 即使采集主流程异常，也要把固定帧缓冲的未满尾帧显式记账后再交给诊断读取。
            self._frame_buffer.finalize()
            if session is not None:
                # 主流程任何位置失败都要先停止录音，再释放 WinRT 句柄；原始业务异常仍保持最高优先级。
                if not stop_attempted:
                    try:
                        stop_attempted = True
                        await _await_if_needed(session.stop())
                    except BaseException as exception:
                        cleanup_exception = exception
                try:
                    await _await_if_needed(session.close())
                except BaseException as exception:
                    if cleanup_exception is None:
                        cleanup_exception = exception
            try:
                wav_path.unlink(missing_ok=True)
            except OSError as exception:
                if cleanup_exception is None:
                    cleanup_exception = exception
            self._session = None
            # 主异常会在 _run() 外层登记；只有主流程正常时，清理异常才是本轮真实根因。
            if primary_exception is None and cleanup_exception is not None:
                with self._diagnostic_lock:
                    if self._failure_reason is None:
                        self._failure_reason = f"{type(cleanup_exception).__name__}: {cleanup_exception}"
                self._frame_buffer.mark_failed(cleanup_exception)

    def _consume_available_audio(
        self,
        reader: IncrementalWavReader,
        *,
        fixed_resampler: Any | None,
        clock_controller: Any | None,
        time_stretch: Any | None,
        final: bool = False,
    ) -> tuple[Any | None, Any | None, Any | None]:
        """读取新增 WAV，固定转换采样率后用 WSOLA 恢复发布时钟。"""

        samples = reader.read_available(final=final)
        if samples is None:
            return fixed_resampler, clock_controller, time_stretch
        wav_format = reader.audio_format
        if wav_format is None:
            raise RuntimeError("RDP MediaCapture WAV audio format is unavailable.")
        with self._diagnostic_lock:
            self._wav_sample_rate = wav_format.sample_rate
        mono_samples = _mix_to_mono(samples, wav_format)
        with self._diagnostic_lock:
            self._input_sample_count += mono_samples.shape[0]
        if fixed_resampler is None or clock_controller is None or time_stretch is None:
            fixed_resampler = self._fixed_resampler_factory(
                input_rate=wav_format.sample_rate,
                output_rate=self._sample_rate,
                channels=1,
            )
            clock_controller = self._clock_controller_factory(
                nominal_input_rate=wav_format.sample_rate,
                output_rate=self._sample_rate,
            )
            time_stretch = self._time_stretch_factory(
                sample_rate=self._sample_rate,
                channels=1,
            )
        observed_at = time.monotonic()
        output_after_resampling = fixed_resampler.process(mono_samples)
        speed = clock_controller.observe(
            sample_count=mono_samples.shape[0],
            observed_at=observed_at,
            buffered_output_samples=self._frame_buffer.buffered_sample_count,
            target_buffer_samples=self._frame_buffer.target_sample_count,
        )
        time_stretch.set_speed(speed)
        output_samples = time_stretch.process(output_after_resampling)
        self._append_output(output_samples)
        controller_diagnostics = getattr(clock_controller, "diagnostics", None)
        stretch_diagnostics = getattr(time_stretch, "diagnostics", None)
        with self._diagnostic_lock:
            self._buffered_output_samples = self._frame_buffer.buffered_sample_count
        if controller_diagnostics is not None or stretch_diagnostics is not None:
            with self._diagnostic_lock:
                if controller_diagnostics is not None:
                    self._estimated_input_rate = controller_diagnostics.estimated_input_rate
                    self._clock_recovery_active = controller_diagnostics.clock_recovery_active
                    self._wsola_speed = controller_diagnostics.wsola_speed
                if stretch_diagnostics is not None:
                    self._wsola_speed = stretch_diagnostics.speed
        return fixed_resampler, clock_controller, time_stretch

    def _append_output(self, samples: np.ndarray) -> None:
        """把目标格式样本写入 ring buffer，并累计诊断数量。"""

        if samples.shape[0] == 0:
            return
        self._frame_buffer.append(np.asarray(samples, dtype=np.float32))
        with self._diagnostic_lock:
            self._output_sample_count += int(samples.shape[0])


def _extract_endpoint_guid(device_id: str) -> str | None:
    """提取 soundcard 设备 ID 中的 endpoint GUID。"""

    matches = _ENDPOINT_GUID_PATTERN.findall(device_id)
    return matches[-1].upper() if matches else None


def _extract_endpoint_guids(device_id: str) -> set[str]:
    """返回 WinRT 设备 ID 中出现的全部 GUID，用于跨 API 建立 endpoint 映射。"""

    return {match.upper() for match in _ENDPOINT_GUID_PATTERN.findall(device_id)}


def _mix_to_mono(samples: np.ndarray, wav_format: WavAudioFormat) -> np.ndarray:
    """把 WAV 多声道按平均值合成为单声道。"""

    if samples.shape[1] != wav_format.channels:
        raise ValueError("RDP MediaCapture WAV channel count changed while recording.")
    if wav_format.channels == 1:
        return np.ascontiguousarray(samples[:, :1], dtype=np.float32)
    return np.mean(samples, axis=1, dtype=np.float32, keepdims=True)


async def _await_if_needed(value: Any) -> Any:
    """兼容真实异步会话和同步测试替身。"""

    if inspect.isawaitable(value):
        return await value
    return value
