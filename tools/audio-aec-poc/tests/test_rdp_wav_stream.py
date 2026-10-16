from __future__ import annotations

from pathlib import Path
import struct
import threading
import time

import numpy as np
import pytest

from audio_aec_poc.rdp_wav_stream import IncrementalWavReader, RdpMicrophoneFrameBuffer


def _wav_bytes(samples: np.ndarray, *, sample_rate: int = 1000) -> bytes:
    """构造一段最小 PCM WAV，方便测试增量读取。"""

    audio = np.asarray(samples, dtype="<i2")
    if audio.ndim == 1:
        audio = audio.reshape(-1, 1)
    channels = audio.shape[1]
    payload = audio.tobytes()
    fmt = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        sample_rate * channels * 2,
        channels * 2,
        16,
    )
    data = struct.pack("<4sI", b"data", len(payload)) + payload
    return struct.pack("<4sI4s", b"RIFF", 4 + len(fmt) + len(data), b"WAVE") + fmt + data


def test_incremental_wav_reader_reads_only_complete_frames(tmp_path: Path) -> None:
    """WAV 正在写入时，只交付完整样本帧，后续调用再拿到新增数据。"""

    path = tmp_path / "mic.wav"
    payload = _wav_bytes(np.arange(6, dtype=np.int16), sample_rate=1000)
    path.write_bytes(payload[:44] + payload[44:46] + payload[46:47])
    reader = IncrementalWavReader(path)

    first_sample = reader.read_available()
    assert first_sample is not None
    assert first_sample[:, 0].tolist() == pytest.approx([0.0])
    with path.open("ab") as output:
        output.write(payload[47:])

    samples = reader.read_available()
    assert samples is not None
    assert samples[:, 0].tolist() == pytest.approx(np.arange(1, 6, dtype=np.float32) / 32768.0)
    assert reader.read_available() is None


def test_incremental_wav_reader_rejects_truncated_final_frame(tmp_path: Path) -> None:
    """录音停止后若 WAV 尾部只剩半个样本帧，最终校验必须明确失败。"""

    path = tmp_path / "truncated.wav"
    payload = _wav_bytes(np.arange(2, dtype=np.int16), sample_rate=1000)
    path.write_bytes(payload[:-1])
    reader = IncrementalWavReader(path)

    first_sample = reader.read_available()
    assert first_sample is not None
    assert first_sample[:, 0].tolist() == [0.0]

    with pytest.raises(ValueError, match="declared data length"):
        reader.read_available(final=True)


def test_incremental_wav_reader_refreshes_growing_data_chunk_size(tmp_path: Path) -> None:
    """录制端持续回写有限 data 长度时，读取器必须继续读取后来增长的音频。"""

    path = tmp_path / "growing-mic.wav"
    payload = bytearray(_wav_bytes(np.arange(6, dtype=np.int16), sample_rate=1000))
    data_size_offset = 40
    payload[data_size_offset : data_size_offset + 4] = (2).to_bytes(4, "little")
    path.write_bytes(payload[:46])
    reader = IncrementalWavReader(path)

    first_sample = reader.read_available()
    assert first_sample is not None
    assert first_sample[:, 0].tolist() == [0.0]

    with path.open("ab") as output:
        output.write(payload[46:])
    with path.open("r+b") as output:
        output.seek(data_size_offset)
        output.write((12).to_bytes(4, "little"))

    remaining_samples = reader.read_available()
    assert remaining_samples is not None
    assert remaining_samples[:, 0].tolist() == pytest.approx(np.arange(1, 6, dtype=np.float32) / 32768.0)


def test_incremental_wav_reader_rejects_non_pcm_or_invalid_header(tmp_path: Path) -> None:
    """格式不是 16-bit PCM 时必须失败，不能把损坏数据当语音继续发。"""

    path = tmp_path / "invalid.wav"
    payload = bytearray(_wav_bytes(np.zeros(2, dtype=np.int16)))
    payload[20:22] = (3).to_bytes(2, "little")
    path.write_bytes(payload)

    with pytest.raises(ValueError, match="PCM"):
        IncrementalWavReader(path).read_available()


def test_frame_buffer_preserves_contiguous_order_and_prebuffers() -> None:
    """环形缓冲按样本顺序切出固定 10ms 帧，并先积累目标预缓冲。"""

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=30,
        max_buffer_ms=100,
        underflow_timeout_seconds=0.05,
    )
    buffer.append(np.arange(25, dtype=np.float32).reshape(-1, 1))
    assert not buffer.is_ready
    buffer.append(np.arange(25, 35, dtype=np.float32).reshape(-1, 1))
    assert buffer.is_ready
    assert buffer.read_frame()[:, 0].tolist() == list(range(10))
    assert buffer.read_frame()[:, 0].tolist() == list(range(10, 20))
    assert buffer.read_frame()[:, 0].tolist() == list(range(20, 30))


def test_frame_buffer_fails_instead_of_dropping_on_overflow() -> None:
    """缓冲满时必须报告溢出，不能悄悄丢掉用户语音。"""

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=10,
        max_buffer_ms=20,
    )
    buffer.append(np.ones((20, 1), dtype=np.float32))

    with pytest.raises(BufferError, match="overflow"):
        buffer.append(np.ones((1, 1), dtype=np.float32))
    assert buffer.has_overflow


def test_frame_buffer_surfaces_underflow_and_first_failure() -> None:
    """消费者追上生产者时必须报告下溢，后续失败不能覆盖首次根因。"""

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=10,
        max_buffer_ms=100,
        underflow_timeout_seconds=0.01,
    )
    buffer.append(np.ones((10, 1), dtype=np.float32))
    assert buffer.read_frame().shape == (10, 1)

    with pytest.raises(TimeoutError, match="underflow"):
        buffer.read_frame()
    assert buffer.has_underflow
    buffer.mark_failed(RuntimeError("first failure"))
    buffer.mark_failed(RuntimeError("second failure"))
    with pytest.raises(RuntimeError, match="first failure"):
        buffer.raise_if_failed()


def test_frame_buffer_rejects_oversized_or_invalid_chunks() -> None:
    """非法数组形状必须立即失败，避免 worker 把错误数据写入固定槽位。"""

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=10,
        max_buffer_ms=100,
        max_append_samples=12,
    )
    with pytest.raises(ValueError, match="chunk"):
        buffer.append(np.zeros((13, 1), dtype=np.float32))
    with pytest.raises(ValueError, match="channels"):
        buffer.append(np.zeros((10, 2), dtype=np.float32))


def test_frame_buffer_finalizes_partial_tail_without_silence_padding() -> None:
    """固定 10ms 帧无法承载的尾部样本必须记账，不能悄悄补静音或伪装成完整帧。"""

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=10,
        max_buffer_ms=100,
    )
    buffer.append(np.ones((3, 1), dtype=np.float32))

    buffer.finalize()

    assert buffer.terminal_partial_sample_count == 3
    assert buffer.buffered_sample_count == 0
    with pytest.raises(RuntimeError, match="already finalized"):
        buffer.append(np.ones((1, 1), dtype=np.float32))


def test_frame_buffer_waits_for_worker_data_without_busy_loop() -> None:
    """消费者等待期间应被生产通知唤醒，而不是固定 sleep 忙等。"""

    buffer = RdpMicrophoneFrameBuffer(
        sample_rate=1000,
        channels=1,
        frame_samples=10,
        target_buffer_ms=10,
        max_buffer_ms=100,
        underflow_timeout_seconds=0.2,
    )

    def produce() -> None:
        """延迟写入一帧，模拟后台录音 worker。"""

        time.sleep(0.02)
        buffer.append(np.ones((10, 1), dtype=np.float32))

    producer = threading.Thread(target=produce)
    producer.start()
    assert np.count_nonzero(buffer.read_frame()) == 10
    producer.join()
