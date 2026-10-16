from __future__ import annotations

import asyncio
from pathlib import Path
import struct
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from audio_aec_poc import rdp_mediacapture as rdp_mediacapture_module
from audio_aec_poc.rdp_mediacapture import (
    RdpAudioCaptureDevice,
    RdpMediaCaptureMicrophone,
    list_winrt_audio_capture_devices,
    select_rdp_audio_capture_device,
)
from audio_aec_poc.rdp_wav_stream import RdpMicrophoneFrameBuffer


def _device(device_id: str, name: str) -> RdpAudioCaptureDevice:
    """创建测试用 WinRT 音频输入设备。"""

    return RdpAudioCaptureDevice(device_id=device_id, name=name)


def test_device_selection_matches_endpoint_guid_not_duplicate_name() -> None:
    """普通设备和 loopback 同名时，必须按 soundcard endpoint GUID 选择 WinRT 输入。"""

    devices = [
        _device("\\\\?\\SWD#MMDEVAPI#{11111111-2222-3333-4444-555555555555}", "Remote Audio"),
        _device(
            "\\\\?\\SWD#MMDEVAPI#{3.0.1.00000001}.{A3ED9185-1E02-411C-B11B-05D92F25CEF4}"
            "#{2eef81be-33fa-4800-9670-1cd474972c3f}",
            "Remote Audio",
        ),
    ]
    selected = select_rdp_audio_capture_device(
        devices,
        audio_device_mode="system_default",
        microphone_id=None,
        microphone_name=None,
        default_microphone_id="{3.0.1.00000001}.{A3ED9185-1E02-411C-B11B-05D92F25CEF4}",
        default_microphone_name="Remote Audio",
    )

    assert "{3.0.1.00000001}.{A3ED9185-1E02-411C-B11B-05D92F25CEF4}" in selected.device_id


def test_device_selection_rejects_ambiguous_name_and_missing_id() -> None:
    """没有稳定 endpoint ID 时必须失败，不能按同名设备猜测。"""

    devices = [_device("device-1", "Remote Audio"), _device("device-2", "Remote Audio")]
    with pytest.raises(RuntimeError, match="endpoint ID"):
        select_rdp_audio_capture_device(
            devices,
            audio_device_mode="system_default",
            microphone_id=None,
            microphone_name="Remote Audio",
            default_microphone_id=None,
            default_microphone_name="Remote Audio",
        )


def test_device_selection_rejects_single_device_without_endpoint_id() -> None:
    """即使 WinRT 只枚举到一个设备，也不能在缺少 endpoint ID 时直接猜中。"""

    with pytest.raises(RuntimeError, match="endpoint ID"):
        select_rdp_audio_capture_device(
            [_device("device-1", "Remote Audio")],
            audio_device_mode="system_default",
            microphone_id=None,
            microphone_name=None,
            default_microphone_id=None,
            default_microphone_name="Remote Audio",
        )


def test_device_selection_rejects_missing_manual_endpoint() -> None:
    """手动 ID 不存在时必须直接失败，不能回退到另一个设备。"""

    with pytest.raises(RuntimeError, match="not available"):
        select_rdp_audio_capture_device(
            [_device("device-1", "Remote Audio")],
            audio_device_mode="manual",
            microphone_id="{11111111-2222-3333-4444-555555555555}",
            microphone_name="Remote Audio",
            default_microphone_id=None,
            default_microphone_name=None,
        )


def test_device_selection_uses_current_default_in_system_default_mode() -> None:
    """系统默认模式必须忽略历史手动选择，始终映射当前默认麦克风。"""

    stale_device = _device(
        "\\\\?\\SWD#MMDEVAPI#{11111111-2222-3333-4444-555555555555}",
        "Old Microphone",
    )
    default_device = _device(
        "\\\\?\\SWD#MMDEVAPI#{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}",
        "Remote Audio",
    )

    selected = select_rdp_audio_capture_device(
        [stale_device, default_device],
        audio_device_mode="system_default",
        microphone_id="{11111111-2222-3333-4444-555555555555}",
        microphone_name="Old Microphone",
        default_microphone_id="{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}",
        default_microphone_name="Remote Audio",
    )

    assert selected is default_device


def test_winrt_device_enumeration_works_inside_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sidecar 事件循环运行时，WinRT 枚举必须转到独立线程执行。"""

    class FakeDeviceClass:
        """提供 WinRT 音频采集枚举常量。"""

        AUDIO_CAPTURE = object()

    class FakeDeviceInformation:
        """模拟 Python/WinRT 的异步设备枚举接口。"""

        @staticmethod
        async def find_all_async_device_class(device_class: object) -> list[object]:
            """返回一个带 endpoint GUID 的音频输入设备。"""

            assert device_class is FakeDeviceClass.AUDIO_CAPTURE
            return [
                SimpleNamespace(
                    id="\\\\?\\SWD#MMDEVAPI#{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}",
                    name="Remote Audio",
                )
            ]

    fake_module = SimpleNamespace(
        DeviceClass=FakeDeviceClass,
        DeviceInformation=FakeDeviceInformation,
    )
    monkeypatch.setitem(sys.modules, "winrt.windows.devices.enumeration", fake_module)

    async def enumerate_from_sidecar_loop() -> list[RdpAudioCaptureDevice]:
        """模拟 run_sidecar_pipeline_once 已经占用当前线程事件循环。"""

        return list_winrt_audio_capture_devices()

    devices = asyncio.run(enumerate_from_sidecar_loop())

    assert [device.name for device in devices] == ["Remote Audio"]


class FakeMediaCaptureSession:
    """模拟 MediaCapture，把连续 PCM 逐步写入增量 WAV 文件。"""

    def __init__(self, wav_path: Path, payload: bytes) -> None:
        """保存输出路径和 WAV 数据。"""

        self.wav_path = wav_path
        self.payload = payload
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.closed = False

    def start(self) -> None:
        """写入 WAV 头并持续追加完整样本块。"""

        self.wav_path.write_bytes(self.payload[:44])
        self.started.set()
        for offset in range(44, len(self.payload), 20):
            if self.stopped.is_set():
                break
            with self.wav_path.open("ab") as output:
                output.write(self.payload[offset : offset + 20])
            time.sleep(0.002)

    def stop(self) -> None:
        """停止模拟录音。"""

        self.stopped.set()

    def close(self) -> None:
        """记录会话关闭。"""

        self.closed = True


class ImmediateMediaCaptureSession(FakeMediaCaptureSession):
    """一次写完较大的 WAV，让 worker 通过多次增量读取完成消费。"""

    def start(self) -> None:
        """立即写完整个 WAV，避免测试耗时受模拟墙钟影响。"""

        self.wav_path.write_bytes(self.payload)
        self.started.set()


class PassThroughClockController:
    """让 worker 生命周期测试只观察样本顺序，不依赖真实速率控制。"""

    def __init__(self, **kwargs: object) -> None:
        """接受真实控制器的格式参数，但不改变测试样本。"""

        _ = kwargs
        self.observed_times: list[float] = []
        self.diagnostics = SimpleNamespace(
            estimated_input_rate=None,
            wsola_speed=1.0,
            clock_recovery_active=False,
        )

    def observe(
        self,
        *,
        sample_count: int,
        observed_at: float,
        buffered_output_samples: int,
        target_buffer_samples: int,
    ) -> float:
        """记录输入块到达时间并保持 WSOLA 正常速度。"""

        _ = sample_count, buffered_output_samples, target_buffer_samples
        self.observed_times.append(observed_at)
        return 1.0


class PassThroughFixedResampler:
    """让 worker 测试跳过真实采样率转换，只验证线程和样本编排。"""

    def __init__(self, **kwargs: object) -> None:
        """接受固定重采样器格式参数。"""

        _ = kwargs

    def process(self, samples: np.ndarray) -> np.ndarray:
        """原样返回输入样本。"""

        return samples

    def finish(self) -> np.ndarray:
        """直通替身没有滤波延迟，因此结束时返回空块。"""

        return np.empty((0, 1), dtype=np.float32)


class PassThroughTimeStretch:
    """让 worker 生命周期测试不依赖 WSOLA 的窗口延迟。"""

    def __init__(self, **kwargs: object) -> None:
        """接受 WSOLA 格式参数并提供稳定诊断。"""

        _ = kwargs
        self.diagnostics = SimpleNamespace(speed=1.0)

    def set_speed(self, speed: float) -> None:
        """记录 worker 选择的时间伸缩速度。"""

        self.diagnostics.speed = speed

    def process(self, samples: np.ndarray) -> np.ndarray:
        """原样返回输入样本。"""

        return samples

    def finish(self) -> np.ndarray:
        """直通替身没有内部尾部。"""

        return np.empty((0, 1), dtype=np.float32)


def _pcm_wav(samples: np.ndarray, sample_rate: int = 1000) -> bytes:
    """构造 16-bit PCM WAV 数据。"""

    payload = np.asarray(samples, dtype="<i2").tobytes()
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    data = struct.pack("<4sI", b"data", len(payload)) + payload
    return struct.pack("<4sI4s", b"RIFF", 4 + len(fmt) + len(data), b"WAVE") + fmt + data


def test_mediacapture_worker_resamples_once_and_keeps_sample_order(tmp_path: Path) -> None:
    """worker 应在后台处理 WAV，连续数据不能因轮询边界断裂。"""

    payload = _pcm_wav(np.arange(90, dtype=np.int16))
    sessions: list[FakeMediaCaptureSession] = []

    def factory(*, wav_path: Path, device_id: str) -> FakeMediaCaptureSession:
        """创建测试 MediaCapture 会话。"""

        assert device_id == "device-1"
        session = FakeMediaCaptureSession(wav_path, payload)
        sessions.append(session)
        return session

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=20,
        max_buffer_ms=200,
        underflow_timeout_seconds=0.2,
    )
    worker = RdpMediaCaptureMicrophone(
        device_id="device-1",
        frame_buffer=buffer,
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        temp_directory=tmp_path,
        session_factory=factory,
        clock_controller_factory=PassThroughClockController,
        fixed_resampler_factory=PassThroughFixedResampler,
        time_stretch_factory=PassThroughTimeStretch,
        poll_interval_seconds=0.001,
    )
    worker.start()
    buffer.wait_until_ready(1.0)
    frames = [buffer.read_frame()[:, 0] for _ in range(5)]
    worker.stop()
    assert worker.join()

    assert np.concatenate(frames).tolist() == pytest.approx(np.arange(50, dtype=np.float32) / 32768.0)
    assert sessions[0].closed


def test_mediacapture_worker_surfaces_session_failure(tmp_path: Path) -> None:
    """MediaCapture 初始化或录音异常必须唤醒消费者并原样失败。"""

    def factory(*, wav_path: Path, device_id: str) -> FakeMediaCaptureSession:
        """模拟设备打开失败。"""

        _ = wav_path, device_id
        raise RuntimeError("microphone permission denied")

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=10,
        max_buffer_ms=100,
    )
    worker = RdpMediaCaptureMicrophone(
        device_id="device-1",
        frame_buffer=buffer,
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        temp_directory=tmp_path,
        session_factory=factory,
    )
    worker.start()
    with pytest.raises(RuntimeError, match="permission denied"):
        buffer.wait_until_ready(1.0)
    assert worker.join()


def test_mediacapture_worker_preserves_wav_failure_over_close_failure(tmp_path: Path) -> None:
    """WAV 主故障发生后，MediaCapture 清理异常不能覆盖真正根因。"""

    sessions: list[InvalidWavSession] = []

    class InvalidWavSession:
        """写入损坏 WAV，并在关闭时额外抛出清理异常。"""

        def __init__(self, wav_path: Path) -> None:
            """保存 worker 分配的临时文件路径。"""

            self.wav_path = wav_path
            self.stop_called = False
            self.close_called = False

        def start(self) -> None:
            """写入足以触发 RIFF 校验的损坏文件。"""

            self.wav_path.write_bytes(b"not-a-wave-file")

        def stop(self) -> None:
            """记录异常路径仍按顺序停止录音。"""

            self.stop_called = True

        def close(self) -> None:
            """模拟主故障之后发生的句柄清理失败。"""

            self.close_called = True
            raise RuntimeError("close cleanup failure")

    def factory(*, wav_path: Path, device_id: str) -> InvalidWavSession:
        """创建损坏 WAV 会话。"""

        assert device_id == "device-1"
        session = InvalidWavSession(wav_path)
        sessions.append(session)
        return session

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=10,
        max_buffer_ms=100,
    )
    worker = RdpMediaCaptureMicrophone(
        device_id="device-1",
        frame_buffer=buffer,
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        temp_directory=tmp_path,
        session_factory=factory,
        time_stretch_factory=PassThroughTimeStretch,
        poll_interval_seconds=0.001,
    )

    worker.start()
    with pytest.raises(ValueError, match="RIFF/WAVE"):
        buffer.wait_until_ready(1.0)
    assert worker.join()
    assert sessions[0].stop_called
    assert sessions[0].close_called


def test_mediacapture_worker_drains_all_samples_flushed_by_stop(tmp_path: Path) -> None:
    """stop 一次刷新超过五个读取块时，worker 必须排空全部尾部样本。"""

    source_samples = np.arange(50_000, dtype=np.int16)
    payload = _pcm_wav(source_samples, sample_rate=1_000)
    sessions: list[StopFlushMediaCaptureSession] = []

    class StopFlushMediaCaptureSession:
        """启动时只写 WAV 头，停止时一次刷新全部音频。"""

        def __init__(self, wav_path: Path) -> None:
            """保存临时 WAV，并创建启动同步事件。"""

            self.wav_path = wav_path
            self.started = threading.Event()
            self.closed = False

        def start(self) -> None:
            """只写已经声明最终 data 长度的完整 WAV 头。"""

            self.wav_path.write_bytes(payload[:44])
            self.started.set()

        def stop(self) -> None:
            """模拟 MediaCapture 在停止完成时一次写入全部尾包。"""

            with self.wav_path.open("ab") as output:
                output.write(payload[44:])

        def close(self) -> None:
            """记录句柄已经释放。"""

            self.closed = True

    def session_factory(*, wav_path: Path, device_id: str) -> StopFlushMediaCaptureSession:
        """创建停止时刷新大量尾包的会话。"""

        assert device_id == "device-1"
        session = StopFlushMediaCaptureSession(wav_path)
        sessions.append(session)
        return session

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1_000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=20,
        max_buffer_ms=60_000,
    )
    worker = RdpMediaCaptureMicrophone(
        device_id="device-1",
        frame_buffer=buffer,
        sample_rate=1_000,
        channels=1,
        frame_samples=10,
        temp_directory=tmp_path,
        session_factory=session_factory,
        clock_controller_factory=PassThroughClockController,
        fixed_resampler_factory=PassThroughFixedResampler,
        time_stretch_factory=PassThroughTimeStretch,
        poll_interval_seconds=0.001,
    )

    worker.start()
    session_deadline = time.monotonic() + 1.0
    while not sessions and time.monotonic() < session_deadline:
        time.sleep(0.001)
    assert sessions
    assert sessions[0].started.wait(1.0)
    worker.stop()
    assert worker.join()

    assert worker.diagnostics.output_sample_count == source_samples.shape[0]
    assert buffer.buffered_sample_count == source_samples.shape[0]
    assert sessions[0].closed


def test_mediacapture_worker_reuses_one_resampler_across_incremental_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WAV 分多块读取时必须复用同一个 SoXR 流，保持跨块滤波状态。"""

    resampler_instances: list[object] = []

    class FakeResampleStream:
        """记录构造与调用次数，并用稳定抽点代替真实重采样。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """保存当前实例，供测试确认整个 worker 只构造一次。"""

            _ = args
            assert "vr" not in kwargs
            self.call_count = 0
            resampler_instances.append(self)

        def resample_chunk(self, samples: np.ndarray, *, last: bool) -> np.ndarray:
            """非结尾块按二比一抽点，结尾刷新不再产生样本。"""

            self.call_count += 1
            if last:
                return np.empty((0, 1), dtype=np.float32)
            return samples[::2]

        def set_io_ratio(self, *args: object, **kwargs: object) -> None:
            """如果生产代码尝试变更采样率比例，立即让测试失败。"""

            _ = args, kwargs
            raise AssertionError("RDP fixed SoXR must not change its IO ratio.")

    monkeypatch.setattr(rdp_mediacapture_module.soxr, "ResampleStream", FakeResampleStream)
    source_samples = np.arange(20000, dtype=np.int16)
    payload = _pcm_wav(source_samples, sample_rate=2000)

    def factory(*, wav_path: Path, device_id: str) -> ImmediateMediaCaptureSession:
        """创建会立即写完测试 WAV 的 MediaCapture 会话。"""

        assert device_id == "device-1"
        return ImmediateMediaCaptureSession(wav_path, payload)

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=20,
        max_buffer_ms=12000,
    )
    worker = RdpMediaCaptureMicrophone(
        device_id="device-1",
        frame_buffer=buffer,
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        temp_directory=tmp_path,
        session_factory=factory,
        time_stretch_factory=PassThroughTimeStretch,
        poll_interval_seconds=0.001,
    )

    worker.start()
    deadline = time.monotonic() + 1.0
    while worker.diagnostics.output_sample_count < 10000 and time.monotonic() < deadline:
        time.sleep(0.001)
    worker.stop()
    assert worker.join()

    assert worker.diagnostics.output_sample_count == 10000
    assert len(resampler_instances) == 1
    assert resampler_instances[0].call_count >= 3
    first_frame = buffer.read_frame()[:, 0]
    assert first_frame.tolist() == pytest.approx(source_samples[:20:2] / 32768.0)


def test_mediacapture_worker_routes_all_wav_chunks_through_one_clock_recovery(
    tmp_path: Path,
) -> None:
    """worker 必须复用唯一时钟恢复器，并为每块 WAV 输入提供单调到达时刻。"""

    recovery_instances: list[object] = []

    class FakeClockController:
        """记录输入格式、块顺序和观察时间的控制器替身。"""

        def __init__(
            self,
            *,
            nominal_input_rate: float,
            output_rate: float,
        ) -> None:
            """保存 worker 从 WAV 头和发布配置传入的格式。"""

            self.nominal_input_rate = nominal_input_rate
            self.output_rate = output_rate
            self.observed_times: list[float] = []
            self.diagnostics = SimpleNamespace(
                estimated_input_rate=None,
                wsola_speed=1.0,
                clock_recovery_active=False,
            )
            recovery_instances.append(self)

        def observe(
            self,
            *,
            sample_count: int,
            observed_at: float,
            buffered_output_samples: int,
            target_buffer_samples: int,
        ) -> float:
            """记录每次增量读取的到达时刻并保持正常速度。"""

            assert sample_count > 0
            assert buffered_output_samples >= 0
            assert target_buffer_samples == 20
            self.observed_times.append(observed_at)
            return 1.0

    source_samples = np.arange(20_000, dtype=np.int16)
    payload = _pcm_wav(source_samples, sample_rate=2_000)

    def session_factory(*, wav_path: Path, device_id: str) -> ImmediateMediaCaptureSession:
        """创建会立即写完测试 WAV 的 MediaCapture 会话。"""

        assert device_id == "device-1"
        return ImmediateMediaCaptureSession(wav_path, payload)

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1_000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=20,
        max_buffer_ms=25_000,
    )
    worker = RdpMediaCaptureMicrophone(
        device_id="device-1",
        frame_buffer=buffer,
        sample_rate=1_000,
        channels=1,
        frame_samples=10,
        temp_directory=tmp_path,
        session_factory=session_factory,
        clock_controller_factory=FakeClockController,
        fixed_resampler_factory=PassThroughFixedResampler,
        time_stretch_factory=PassThroughTimeStretch,
        poll_interval_seconds=0.001,
    )

    worker.start()
    deadline = time.monotonic() + 1.0
    while worker.diagnostics.output_sample_count < source_samples.shape[0] and time.monotonic() < deadline:
        time.sleep(0.001)
    worker.stop()
    assert worker.join()

    assert len(recovery_instances) == 1
    recovery = recovery_instances[0]
    assert recovery.nominal_input_rate == 2_000
    assert recovery.output_rate == 1_000
    assert len(recovery.observed_times) >= 3
    assert recovery.observed_times == sorted(recovery.observed_times)
