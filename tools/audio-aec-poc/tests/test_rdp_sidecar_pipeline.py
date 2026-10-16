from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from audio_aec_sidecar.config import SidecarConfig
from audio_aec_sidecar.pipeline import run_sidecar_pipeline_once


class RawRdpCapture:
    """提供固定原始麦克风帧，模拟 RDP MediaCapture 采集器。"""

    def __init__(self, *, sample_rate: int, channels: int, frame_samples: int) -> None:
        """保存管线传入的固定音频格式。"""

        self.frame_samples = frame_samples
        self.channels = channels
        self.sample_rate = sample_rate

    def __enter__(self) -> "RawRdpCapture":
        """打开测试采集器。"""

        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """关闭测试采集器。"""

        _ = exc_type, exc_value, traceback
        return None

    def read_frame_pair(self) -> tuple[np.ndarray, np.ndarray]:
        """返回零参考和有标记的原始麦克风帧。"""

        return (
            np.zeros((self.frame_samples, self.channels), dtype=np.float32),
            np.full((self.frame_samples, self.channels), 0.25, dtype=np.float32),
        )


class RecordingPublisher:
    """记录 sidecar 发布的 PCM16。"""

    def __init__(self) -> None:
        """初始化空发布列表。"""

        self.frames: list[bytes] = []

    async def publish_pcm16(self, pcm16: bytes) -> None:
        """记录一帧发布数据。"""

        self.frames.append(bytes(pcm16))


def test_rdp_sidecar_publishes_raw_mic_without_constructing_aec() -> None:
    """RDP 直通链路不创建 AEC，且不因默认 warmup_seconds 延迟或丢弃首帧。"""

    def fail_if_created(*, sample_rate: int, channels: int) -> None:
        """AEC 一旦被创建就让测试失败。"""

        _ = sample_rate, channels
        raise AssertionError("RDP pipeline must not construct AEC")

    publisher = RecordingPublisher()
    config = SidecarConfig(
        livekit_url="wss://livekit.example",
        token="token",
        room_name="room",
        identity="desktop-aec-worker",
        parent_pid=1234,
        sample_rate=1000,
        clean_output_sample_rate=1000,
        warmup_seconds=2.0,
    )
    result = asyncio.run(
        run_sidecar_pipeline_once(
            config,
            publisher,
            frame_count=1,
            capture_factory=RawRdpCapture,
            aec_factory=fail_if_created,
            use_aec=False,
        )
    )

    assert result.frames_published == 1
    assert publisher.frames == [np.full(10, int(0.25 * 32767), dtype="<i2").tobytes()]


def test_rdp_sidecar_captures_at_clean_output_rate_without_per_frame_resampling() -> None:
    """RDP 必须让持久 worker 直接产出发布采样率，避免每 10ms 重建重采样器。"""

    captures: list[RawRdpCapture] = []

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> RawRdpCapture:
        """记录 RDP 管线实际请求的采集格式。"""

        capture = RawRdpCapture(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
        )
        captures.append(capture)
        return capture

    publisher = RecordingPublisher()
    config = SidecarConfig(
        livekit_url="wss://livekit.example",
        token="token",
        room_name="room",
        identity="desktop-aec-worker",
        parent_pid=1234,
        sample_rate=48000,
        clean_output_sample_rate=16000,
        warmup_seconds=2.0,
    )

    asyncio.run(
        run_sidecar_pipeline_once(
            config,
            publisher,
            frame_count=1,
            capture_factory=capture_factory,
            use_aec=False,
        )
    )

    assert captures[0].sample_rate == 16000
    assert captures[0].frame_samples == 160
    assert len(publisher.frames[0]) == 160 * 2


def test_rdp_sidecar_writes_one_diagnostic_record_per_debug_run(tmp_path) -> None:
    """每个 debug run 都应记录发布结果和生命周期，而不是只留下三路 WAV。"""

    publisher = RecordingPublisher()
    config = SidecarConfig(
        livekit_url="wss://livekit.example",
        token="token",
        room_name="room",
        identity="desktop-aec-worker",
        parent_pid=1234,
        sample_rate=1000,
        clean_output_sample_rate=1000,
        debug_output_dir=tmp_path,
    )

    result = asyncio.run(
        run_sidecar_pipeline_once(
            config,
            publisher,
            frame_count=3,
            capture_factory=RawRdpCapture,
            use_aec=False,
        )
    )

    assert result.frames_published == 3
    run_directories = [path for path in tmp_path.iterdir() if path.is_dir()]
    assert len(run_directories) == 1
    diagnostics = json.loads((run_directories[0] / "diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["frames_published"] == 3
    assert diagnostics["stop_reason"] == "frame_count_reached"
    assert diagnostics["failure"] is None
    assert diagnostics["started_at"]
    assert diagnostics["finished_at"]


def test_rdp_sidecar_marks_failed_run_in_diagnostics(tmp_path) -> None:
    """采集异常时诊断不能把失败伪装成正常达到帧数。"""

    class FailingRdpCapture(RawRdpCapture):
        """在第一帧读取时失败的 RDP 采集替身。"""

        def read_frame_pair(self) -> tuple[np.ndarray, np.ndarray]:
            """报告底层采集异常。"""

            raise RuntimeError("RDP microphone worker failed")

    config = SidecarConfig(
        livekit_url="wss://livekit.example",
        token="token",
        room_name="room",
        identity="desktop-aec-worker",
        parent_pid=1234,
        sample_rate=1000,
        clean_output_sample_rate=1000,
        debug_output_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="worker failed"):
        asyncio.run(
            run_sidecar_pipeline_once(
                config,
                RecordingPublisher(),
                frame_count=1,
                capture_factory=FailingRdpCapture,
                use_aec=False,
            )
        )

    run_directories = [path for path in tmp_path.iterdir() if path.is_dir()]
    assert len(run_directories) == 1
    diagnostics = json.loads((run_directories[0] / "diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["stop_reason"] == "failed"
    assert diagnostics["failure"] == {
        "type": "RuntimeError",
        "message": "RDP microphone worker failed",
    }
