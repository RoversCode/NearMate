from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import threading
import time
from types import TracebackType
from typing import Any

import numpy as np
import soundcard as sc

from audio_aec_poc.devices import DeviceInfo, _device_id, _emit_device_diagnostic
from audio_aec_poc.rdp_mediacapture import (
    RdpAudioCaptureDevice,
    RdpMediaCaptureDiagnostics,
    RdpMediaCaptureMicrophone,
    list_winrt_audio_capture_devices,
    select_rdp_audio_capture_device,
)
from audio_aec_poc.rdp_wav_stream import RdpMicrophoneFrameBuffer


_RDP_TARGET_BUFFER_MS = 2000
_RDP_MAX_BUFFER_MS = 5000
_RDP_STARTUP_TIMEOUT_SECONDS = 8.0
_RDP_FRAME_WAIT_TIMEOUT_SECONDS = 0.5


def _advance_frame_deadline(
    *,
    previous_deadline: float,
    observed_after_read: float,
    frame_period_seconds: float,
) -> float:
    """推进 RDP 发布节拍，并吸收 Windows 等待造成的小幅固定超时。

    Args:
        previous_deadline: 当前帧原本对应的单调墙钟时刻。
        observed_after_read: 当前帧读取完成后的实际单调墙钟时刻。
        frame_period_seconds: 每个发布帧代表的音频时长。

    Returns:
        下一帧应等待到的单调墙钟时刻。
    """

    next_deadline = previous_deadline + frame_period_seconds
    # 小于一帧的调度迟到由下一次较短等待自动吸收；真正阻塞超过一帧才重建节拍，避免密集追帧。
    if observed_after_read - next_deadline > frame_period_seconds:
        return observed_after_read
    return next_deadline


class RdpBufferedCapture:
    """通过 Windows MediaCapture 连续采集 RDP 重定向麦克风。

    RDP 场景不做 AEC，也不打开扬声器 loopback。返回值仍保持 sidecar 的双路读取契约：
    第一项是固定的零参考帧，第二项是经过有界预缓冲和连续切帧的原始麦克风。

    Args:
        sample_rate: RDP `clean-mic` 需要的发布采样率，通常为 16000。
        channels: sidecar 需要的声道数，当前只支持单声道。
        frame_samples: 每个 10ms 帧的样本数。
        soundcard_module: 用于读取当前默认麦克风 endpoint ID 的模块或测试替身。
        audio_device_mode: `system_default` 或 `manual`。
        microphone_id: 用户保存的 soundcard 麦克风 ID。
        microphone_name: 用户保存的麦克风名称。
        target_buffer_ms: 首次 ready 前积累的音频时长。
        max_buffer_ms: 固定 ring buffer 的最大容量。
        startup_timeout_seconds: 等待 MediaCapture 产生预缓冲的最长时间。
        winrt_device_lister: WinRT 音频设备枚举函数。
        microphone_worker_factory: MediaCapture worker 工厂。

    Returns:
        进入上下文后提供固定 10ms 双路帧对的采集器。
    """

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        frame_samples: int,
        *,
        soundcard_module: Any = sc,
        audio_device_mode: str = "system_default",
        microphone_id: str | None = None,
        microphone_name: str | None = None,
        target_buffer_ms: int = _RDP_TARGET_BUFFER_MS,
        max_buffer_ms: int = _RDP_MAX_BUFFER_MS,
        startup_timeout_seconds: float = _RDP_STARTUP_TIMEOUT_SECONDS,
        winrt_device_lister: Callable[[], list[RdpAudioCaptureDevice]] = list_winrt_audio_capture_devices,
        microphone_worker_factory: Callable[..., Any] = RdpMediaCaptureMicrophone,
    ) -> None:
        """保存音频格式、选择参数和固定容量缓冲。"""

        if sample_rate <= 0 or channels <= 0 or frame_samples <= 0:
            raise ValueError("RDP capture audio format must be positive.")
        if channels != 1:
            raise ValueError("RDP capture currently supports one channel only.")
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be greater than 0.")
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_samples = frame_samples
        self._soundcard = soundcard_module
        self._audio_device_mode = audio_device_mode
        self._microphone_id = microphone_id
        self._microphone_name = microphone_name
        self._winrt_device_lister = winrt_device_lister
        self._microphone_worker_factory = microphone_worker_factory
        self._startup_timeout_seconds = startup_timeout_seconds
        self._buffer = RdpMicrophoneFrameBuffer(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
            target_buffer_ms=target_buffer_ms,
            max_buffer_ms=max_buffer_ms,
            underflow_timeout_seconds=_RDP_FRAME_WAIT_TIMEOUT_SECONDS,
        )
        self._default_microphone = self._resolve_default_microphone()
        self._selected_device: RdpAudioCaptureDevice | None = None
        self._microphone_worker_instance: Any | None = None
        self._last_microphone_diagnostics = RdpMediaCaptureDiagnostics(0, None, False)
        self._next_frame_deadline: float | None = None
        self._has_started = False
        self._stopping = threading.Event()

    @property
    def device_info(self) -> DeviceInfo:
        """返回本次 RDP 采集的设备信息，并明确标记 AEC 已关闭。

        Returns:
            包含 WinRT 麦克风名称和 RDP AEC 状态的设备信息。
        """

        microphone_name = self._selected_device.name if self._selected_device is not None else (
            str(getattr(self._default_microphone, "name", "")) or self._microphone_name or "RDP microphone"
        )
        return DeviceInfo(microphone_name=microphone_name, speaker_name="RDP direct input (AEC disabled)")

    @property
    def microphone_diagnostics(self) -> RdpMediaCaptureDiagnostics:
        """返回 MediaCapture worker 的诊断快照。"""

        worker = self._microphone_worker_instance
        diagnostics = self._last_microphone_diagnostics if worker is None else worker.diagnostics
        failure_reason = diagnostics.failure_reason
        if self._buffer.has_underflow and failure_reason is None:
            failure_reason = "TimeoutError: RDP microphone frame buffer underflow."
        return replace(
            diagnostics,
            buffered_output_samples=self._buffer.buffered_sample_count,
            buffer_underflow=self._buffer.has_underflow,
            buffer_overflow=self._buffer.has_overflow,
            failure_reason=failure_reason,
        )

    def __enter__(self) -> "RdpBufferedCapture":
        """映射 WinRT 设备、启动 worker 并等待约 2 秒麦克风预缓冲。

        Returns:
            已经可以稳定读取 10ms 麦克风帧的当前采集器。

        Raises:
            RuntimeError: 设备映射失败或 MediaCapture 无法启动。
            TimeoutError: 设备在启动时没有产生足够连续音频。
        """

        if self._has_started:
            raise RuntimeError("RDP capture instances cannot be reopened.")
        self._has_started = True
        devices: list[RdpAudioCaptureDevice] = []
        try:
            devices = self._winrt_device_lister()
            self._selected_device = select_rdp_audio_capture_device(
                devices,
                audio_device_mode=self._audio_device_mode,
                microphone_id=self._microphone_id,
                microphone_name=self._microphone_name,
                default_microphone_id=_device_id(self._default_microphone),
                default_microphone_name=str(getattr(self._default_microphone, "name", "")) or None,
            )
        except BaseException as exception:
            self._emit_device_error(
                exception,
                devices=devices,
                stage="rdp_mediacapture_microphone_select",
            )
            self.__exit__(type(exception), exception, exception.__traceback__)
            raise

        try:
            self._microphone_worker_instance = self._microphone_worker_factory(
                device_id=self._selected_device.device_id,
                frame_buffer=self._buffer,
                sample_rate=self._sample_rate,
                channels=self._channels,
                frame_samples=self._frame_samples,
            )
            self._stopping.clear()
            self._microphone_worker_instance.start()
            self._buffer.wait_until_ready(self._startup_timeout_seconds)
            self._microphone_worker_instance.ensure_healthy()
            self._buffer.raise_if_failed()
        except BaseException as exception:
            self.__exit__(type(exception), exception, exception.__traceback__)
            raise

        self._next_frame_deadline = time.monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """按 stop、join、释放引用的顺序关闭 MediaCapture worker。

        Args:
            exc_type: with 块内异常类型。
            exc_value: with 块内异常实例。
            traceback: with 块内异常堆栈。

        Returns:
            无返回值；with 块原始异常不会被吞掉。
        """

        worker = self._microphone_worker_instance
        close_exception: BaseException | None = None
        worker_exception: BaseException | None = None
        self._stopping.set()
        if worker is not None:
            try:
                worker.stop()
            except BaseException as exception:
                close_exception = exception
            try:
                if not worker.join():
                    if close_exception is None:
                        close_exception = RuntimeError("RDP MediaCapture worker did not stop within the shutdown budget.")
                else:
                    self._microphone_worker_instance = None
            except BaseException as exception:
                if close_exception is None:
                    close_exception = exception
            finally:
                self._last_microphone_diagnostics = worker.diagnostics
        self._next_frame_deadline = None
        if exc_value is None:
            try:
                self._buffer.raise_if_failed()
            except BaseException as exception:
                worker_exception = exception
        # worker 首次失败描述的是采集为何失效，stop/join 异常只是随后发生的清理问题。
        if exc_value is None and worker_exception is not None:
            raise worker_exception
        if exc_value is None and close_exception is not None:
            raise close_exception
        return None

    def read_frame_pair(self) -> tuple[np.ndarray, np.ndarray]:
        """按固定 10ms 节奏读取零参考帧和连续麦克风帧。

        Returns:
            依次返回零值参考帧和 RDP MediaCapture 原始麦克风帧。

        Raises:
            TimeoutError: 麦克风 ring buffer 发生下溢。
            BaseException: worker 已经报告采集失败。
        """

        worker = self._microphone_worker_instance
        if worker is None or self._next_frame_deadline is None:
            raise RuntimeError("read_frame_pair() must be called inside an open RDP capture context.")
        worker.ensure_healthy()
        wait_seconds = self._next_frame_deadline - time.monotonic()
        if wait_seconds > 0:
            self._stopping.wait(wait_seconds)
        microphone_frame = self._buffer.read_frame()
        frame_period_seconds = self._frame_samples / self._sample_rate
        now = time.monotonic()
        self._next_frame_deadline = _advance_frame_deadline(
            previous_deadline=self._next_frame_deadline,
            observed_after_read=now,
            frame_period_seconds=frame_period_seconds,
        )
        reference_frame = np.zeros_like(microphone_frame)
        return reference_frame, microphone_frame

    def _resolve_default_microphone(self) -> Any | None:
        """读取唯一的非 loopback 默认麦克风，用于获取稳定 endpoint ID。"""

        getter = getattr(self._soundcard, "default_microphone", None)
        try:
            microphone = getter() if callable(getter) else None
        except BaseException:
            microphone = None
        if microphone is not None and not bool(getattr(microphone, "isloopback", False)):
            return microphone
        try:
            microphones = [
                device
                for device in self._soundcard.all_microphones(include_loopback=True)
                if not bool(getattr(device, "isloopback", False))
            ]
        except BaseException:
            return None
        return microphones[0] if len(microphones) == 1 else None

    def _emit_device_error(self, exception: BaseException, *, devices: object, stage: str) -> None:
        """输出设备映射失败的结构化诊断。"""

        available_devices = [
            {"id": device.device_id, "name": device.name}
            for device in devices
            if isinstance(device, RdpAudioCaptureDevice)
        ]
        _emit_device_diagnostic(
            {
                "event": "aec-sidecar-device-error",
                "stage": stage,
                "error_type": type(exception).__name__,
                "requested_device": {"id": self._microphone_id, "name": self._microphone_name},
                "default_microphone": {
                    "id": _device_id(self._default_microphone),
                    "name": str(getattr(self._default_microphone, "name", "")),
                },
                "available_winrt_devices": available_devices,
                "error_message": str(exception),
                "suggestion": "Reconnect the RDP microphone and verify Windows microphone permission.",
            }
        )
