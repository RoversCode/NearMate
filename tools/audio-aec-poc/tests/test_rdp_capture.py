from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from audio_aec_poc.rdp_capture import RdpBufferedCapture, _advance_frame_deadline
from audio_aec_poc.rdp_mediacapture import RdpAudioCaptureDevice


class FakeSoundcard:
    """提供唯一普通麦克风的 soundcard 测试替身。"""

    def __init__(self) -> None:
        """创建与 WinRT 设备 GUID 对应的普通麦克风。"""

        self.microphone = type(
            "FakeMicrophone",
            (),
            {
                "name": "Remote Audio",
                "id": "{3.0.1.00000001}.{A3ED9185-1E02-411C-B11B-05D92F25CEF4}",
                "isloopback": False,
            },
        )()

    def default_microphone(self) -> object:
        """返回默认普通麦克风。"""

        return self.microphone

    def all_microphones(self, *, include_loopback: bool) -> list[object]:
        """返回普通麦克风，不提供 loopback。"""

        assert include_loopback
        return [self.microphone]


class FakeMediaCaptureWorker:
    """向固定 ring buffer 写入连续测试帧的 worker。"""

    def __init__(self, *, frame_buffer: Any, frame_samples: int, **kwargs: object) -> None:
        """保存输出缓冲和帧长。"""

        _ = kwargs
        self.frame_buffer = frame_buffer
        self.frame_samples = frame_samples
        self._alive = False
        self.stop_called = False
        self.join_called = False

    @property
    def is_alive(self) -> bool:
        """返回 worker 是否仍被认为存活。"""

        return self._alive

    @property
    def diagnostics(self) -> object:
        """返回最小诊断信息。"""

        return {"started": self._alive}

    def start(self) -> None:
        """写入连续样本并标记 worker 存活。"""

        self._alive = True
        self.frame_buffer.append(np.ones((self.frame_samples * 4, 1), dtype=np.float32))

    def stop(self) -> None:
        """记录停止请求。"""

        self.stop_called = True
        self._alive = False

    def join(self) -> bool:
        """报告 worker 已退出。"""

        self.join_called = True
        return True

    def ensure_healthy(self) -> None:
        """提前退出时报告失败。"""

        if not self._alive:
            raise RuntimeError("RDP MediaCapture worker stopped unexpectedly.")


class FailingShutdownWorker(FakeMediaCaptureWorker):
    """模拟 worker 已有采集根因，同时停止清理也发生异常。"""

    def stop(self) -> None:
        """先登记真实 worker 故障，再模拟停止接口失败。"""

        self.frame_buffer.mark_failed(RuntimeError("worker capture failure"))
        self._alive = False
        raise RuntimeError("stop cleanup failure")


def _capture_kwargs(worker_factory: Any) -> dict[str, object]:
    """构造不依赖真实 WinRT 的采集器参数。"""

    return {
        "sample_rate": 1000,
        "channels": 1,
        "frame_samples": 10,
        "soundcard_module": FakeSoundcard(),
        "winrt_device_lister": lambda: [
            RdpAudioCaptureDevice(
                "\\\\?\\SWD#MMDEVAPI#{A3ED9185-1E02-411C-B11B-05D92F25CEF4}",
                "Remote Audio",
            )
        ],
        "microphone_worker_factory": worker_factory,
        "target_buffer_ms": 20,
        "max_buffer_ms": 100,
    }


def test_rdp_capture_returns_raw_mic_and_zero_reference_without_aec() -> None:
    """RDP 采集器不打开扬声器 loopback，返回连续原始麦克风和零参考帧。"""

    capture = RdpBufferedCapture(**_capture_kwargs(FakeMediaCaptureWorker))

    with capture:
        reference, microphone = capture.read_frame_pair()

    assert np.count_nonzero(reference) == 0
    assert np.count_nonzero(microphone) > 0


def test_rdp_capture_shutdown_stops_and_joins_worker_before_release() -> None:
    """关闭顺序必须先 stop，再 join，避免 worker 访问已清理的缓冲。"""

    capture = RdpBufferedCapture(**_capture_kwargs(FakeMediaCaptureWorker))
    with capture:
        capture.read_frame_pair()
        worker = capture._microphone_worker_instance

    assert worker is not None
    assert worker.stop_called
    assert worker.join_called
    assert capture._microphone_worker_instance is None


def test_rdp_capture_fails_when_device_mapping_is_invalid() -> None:
    """WinRT 找不到已选择 endpoint 时必须在 ready 前失败。"""

    capture = RdpBufferedCapture(
        **{
            **_capture_kwargs(FakeMediaCaptureWorker),
            "winrt_device_lister": lambda: [RdpAudioCaptureDevice("other", "Remote Audio")],
        }
    )
    with pytest.raises(RuntimeError, match="not available"):
        with capture:
            pass


def test_rdp_capture_shutdown_preserves_worker_failure_over_cleanup_error() -> None:
    """关闭阶段必须优先抛出 worker 根因，不能被 stop 清理异常覆盖。"""

    capture = RdpBufferedCapture(**_capture_kwargs(FailingShutdownWorker))

    with pytest.raises(RuntimeError, match="worker capture failure"):
        with capture:
            capture.read_frame_pair()


def test_rdp_frame_deadline_does_not_accumulate_small_windows_wait_overshoot() -> None:
    """每帧等待略微超时时，固定节拍仍应回到原时间轴而不是越来越慢。"""

    frame_period_seconds = 0.01
    deadline = 0.0

    for _ in range(6_000):
        # Windows 调度器常让 10ms wait 多等约 0.4ms；这部分误差必须在下一帧吸收。
        observed_after_read = deadline + 0.0004
        deadline = _advance_frame_deadline(
            previous_deadline=deadline,
            observed_after_read=observed_after_read,
            frame_period_seconds=frame_period_seconds,
        )

    assert deadline == pytest.approx(60.0)


def test_rdp_frame_deadline_resets_after_a_full_frame_stall() -> None:
    """采集真正阻塞超过一帧时，应从当前时刻重建节拍，不能密集补发旧音频。"""

    assert _advance_frame_deadline(
        previous_deadline=1.0,
        observed_after_read=1.2,
        frame_period_seconds=0.01,
    ) == pytest.approx(1.2)
