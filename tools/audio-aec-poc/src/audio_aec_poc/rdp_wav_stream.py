from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import struct
import threading
import time

import numpy as np


_RIFF_HEADER_SIZE = 12
_WAV_CHUNK_HEADER_SIZE = 8
_MAX_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class WavAudioFormat:
    """描述 MediaCapture 写出的 PCM 音频格式。

    Args:
        sample_rate: 音频采样率。
        channels: 音频声道数。
        block_align: 一个完整样本帧占用的字节数。
        bits_per_sample: 每个声道样本的位数。

    Returns:
        一个不可变的 WAV 格式描述。
    """

    sample_rate: int
    channels: int
    block_align: int
    bits_per_sample: int


class IncrementalWavReader:
    """从仍在增长的 WAV 文件中读取已经落盘的完整 PCM 样本。

    MediaCapture 没有直接把音频帧交给 Python 的稳定接口，因此这里读取它持续写入的
    临时 WAV。文件头只解析一次，后续只读取 data 区新增的完整样本帧，不依据墙钟或文件
    到达速度改写样本顺序。

    Args:
        path: MediaCapture 正在写入的 WAV 文件路径。
        max_read_bytes: 单次最多读取的原始字节数，防止文件突发增长时产生大对象。

    Returns:
        提供 read_available 和 audio_format 属性的增量读取器。
    """

    def __init__(self, path: Path, *, max_read_bytes: int = 16 * 1024) -> None:
        """创建尚未解析文件头的读取器。"""

        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be greater than 0.")
        self._path = Path(path)
        self._max_read_bytes = max_read_bytes
        self._format: WavAudioFormat | None = None
        self._data_offset: int | None = None
        self._data_size_offset: int | None = None
        self._declared_data_size: int | None = None
        self._data_bytes_read = 0

    @property
    def audio_format(self) -> WavAudioFormat | None:
        """返回已经解析出的 WAV PCM 格式。"""

        return self._format

    @property
    def data_bytes_read(self) -> int:
        """返回已经交付给 worker 的 data 字节数。"""

        return self._data_bytes_read

    def read_available(self, *, final: bool = False) -> np.ndarray | None:
        """读取当前文件中新增的完整 PCM 样本。

        Args:
            final: 是否已经停止录音并要求校验文件尾部；为 True 时，残余不完整样本或
                声明长度大于实际文件长度都会直接报错。

        Returns:
            有新样本时返回二维 float32 数组；文件头尚未完整或没有新样本时返回 None。

        Raises:
            ValueError: WAV 头、PCM 格式或样本数据损坏。
        """

        if self._format is None:
            parsed_header = self._try_parse_header()
            if parsed_header is None:
                return None
            self._format, self._data_offset, self._data_size_offset, self._declared_data_size = parsed_header

        assert self._format is not None
        assert self._data_offset is not None
        assert self._data_size_offset is not None
        assert self._declared_data_size is not None

        try:
            current_file_size = self._path.stat().st_size
        except FileNotFoundError:
            return None

        available_data_bytes = max(0, current_file_size - self._data_offset)
        self._declared_data_size = self._read_declared_data_size()
        if (
            final
            and self._declared_data_size not in {0, 0xFFFFFFFF}
            and current_file_size < self._data_offset + self._declared_data_size
        ):
            raise ValueError("RDP microphone WAV ended before its declared data length.")
        # MediaCapture 录音期间可能暂时把 data size 写成 0；此时实际文件长度是唯一可靠边界。
        if self._declared_data_size not in {0, 0xFFFFFFFF}:
            available_data_bytes = min(available_data_bytes, self._declared_data_size)

        unread_data_bytes = available_data_bytes - self._data_bytes_read
        complete_data_bytes = unread_data_bytes - unread_data_bytes % self._format.block_align
        if complete_data_bytes <= 0:
            if final and unread_data_bytes > 0:
                raise ValueError("RDP microphone WAV ended with an incomplete PCM sample frame.")
            return None
        read_size = min(complete_data_bytes, self._max_read_bytes)
        read_size -= read_size % self._format.block_align
        if read_size <= 0:
            return None

        with self._path.open("rb") as input_file:
            input_file.seek(self._data_offset + self._data_bytes_read)
            raw_data = input_file.read(read_size)
        complete_raw_size = len(raw_data) - len(raw_data) % self._format.block_align
        if complete_raw_size <= 0:
            if final and raw_data:
                raise ValueError("RDP microphone WAV ended with an incomplete PCM sample frame.")
            return None
        if final and complete_raw_size != len(raw_data):
            raise ValueError("RDP microphone WAV ended with an incomplete PCM sample frame.")
        raw_data = raw_data[:complete_raw_size]
        self._data_bytes_read += complete_raw_size

        samples = np.frombuffer(raw_data, dtype="<i2")
        return samples.reshape(-1, self._format.channels).astype(np.float32) / 32768.0

    def _read_declared_data_size(self) -> int:
        """读取 MediaCapture 当前回写的 data chunk 长度。

        Returns:
            WAV 头中最新的无符号 data 字节数。

        Raises:
            ValueError: data 长度字段在录制期间被截断。
        """

        assert self._data_size_offset is not None
        with self._path.open("rb") as input_file:
            input_file.seek(self._data_size_offset)
            raw_size = input_file.read(4)
        if len(raw_size) != 4:
            raise ValueError("RDP microphone WAV data size field was truncated.")
        return struct.unpack("<I", raw_size)[0]

    def _try_parse_header(self) -> tuple[WavAudioFormat, int, int, int] | None:
        """在文件头可能仍未写完时返回 None，否则解析 PCM 和 data 区边界。"""

        try:
            with self._path.open("rb") as input_file:
                header = input_file.read(_MAX_HEADER_BYTES)
        except FileNotFoundError:
            return None
        if len(header) < _RIFF_HEADER_SIZE:
            return None
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ValueError("RDP microphone file is not a RIFF/WAVE file.")

        offset = _RIFF_HEADER_SIZE
        audio_format: WavAudioFormat | None = None
        data_offset: int | None = None
        data_size_offset: int | None = None
        declared_data_size: int | None = None
        while offset + _WAV_CHUNK_HEADER_SIZE <= len(header):
            chunk_id = header[offset : offset + 4]
            chunk_size = struct.unpack_from("<I", header, offset + 4)[0]
            chunk_data_start = offset + _WAV_CHUNK_HEADER_SIZE
            chunk_data_end = chunk_data_start + chunk_size
            # data chunk 的头一旦完整，后面的 PCM 仍可能尚未写入，不能把它误判成损坏。
            if chunk_id == b"data":
                data_offset = chunk_data_start
                data_size_offset = offset + 4
                declared_data_size = chunk_size
                break
            if chunk_data_end > len(header):
                return None
            if chunk_id == b"fmt ":
                if chunk_size < 16:
                    raise ValueError("RDP microphone WAV fmt chunk is too short.")
                encoding, channels, sample_rate, _byte_rate, block_align, bits_per_sample = struct.unpack_from(
                    "<HHIIHH", header, chunk_data_start
                )
                if encoding != 1 or bits_per_sample != 16:
                    raise ValueError("RDP microphone WAV must contain 16-bit PCM.")
                if channels <= 0 or sample_rate <= 0 or block_align != channels * 2:
                    raise ValueError("RDP microphone WAV PCM format is invalid.")
                audio_format = WavAudioFormat(
                    sample_rate=sample_rate,
                    channels=channels,
                    block_align=block_align,
                    bits_per_sample=bits_per_sample,
                )
            offset = chunk_data_end + chunk_size % 2

        if audio_format is None or data_offset is None or data_size_offset is None or declared_data_size is None:
            # 录音刚启动时 fmt/data chunk 可能还没有全部落盘，继续轮询即可。
            return None
        return audio_format, data_offset, data_size_offset, declared_data_size


class RdpMicrophoneFrameBuffer:
    """保存固定容量的 RDP 麦克风 10ms 帧环形队列。

    Args:
        sample_rate: 输出给 sidecar 的采样率。
        channels: 输出声道数。
        frame_samples: 每个 10ms 帧的样本数。
        target_buffer_ms: 首次 ready 前需要积累的音频时长。
        max_buffer_ms: 环形队列最多允许积累的音频时长。
        underflow_timeout_seconds: 消费者等待下一帧的最长时间。
        max_append_samples: 单次 worker 入队允许的最大样本数。

    Returns:
        提供固定槽位写入、阻塞读取和首次失败记录的线程安全缓冲。
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        frame_samples: int,
        target_buffer_ms: int,
        max_buffer_ms: int,
        underflow_timeout_seconds: float = 0.25,
        max_append_samples: int | None = None,
    ) -> None:
        """校验固定音频格式并分配全部环形槽位。"""

        if sample_rate <= 0 or channels <= 0 or frame_samples <= 0:
            raise ValueError("RDP microphone audio format must be positive.")
        if target_buffer_ms <= 0 or max_buffer_ms < target_buffer_ms:
            raise ValueError("RDP microphone buffer limits are invalid.")
        if underflow_timeout_seconds <= 0:
            raise ValueError("underflow_timeout_seconds must be greater than 0.")
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_samples = frame_samples
        self._target_frames = max(1, math.ceil(sample_rate * target_buffer_ms / 1000 / frame_samples))
        self._capacity_frames = max(
            self._target_frames,
            math.ceil(sample_rate * max_buffer_ms / 1000 / frame_samples),
        )
        self._max_append_samples = max_append_samples or max(frame_samples * 32, 8192)
        if self._max_append_samples <= 0:
            raise ValueError("max_append_samples must be greater than 0.")
        self._underflow_timeout_seconds = underflow_timeout_seconds
        self._frames = np.zeros(
            (self._capacity_frames, frame_samples, channels),
            dtype=np.float32,
        )
        self._pending_frame = np.zeros((frame_samples, channels), dtype=np.float32)
        self._pending_sample_count = 0
        self._head = 0
        self._tail = 0
        self._frame_count = 0
        self._failure: BaseException | None = None
        self._underflow = False
        self._overflow = False
        self._terminal_partial_sample_count = 0
        self._finalized = False
        self._condition = threading.Condition()

    @property
    def is_ready(self) -> bool:
        """返回是否已经积累目标预缓冲。"""

        with self._condition:
            return self._frame_count >= self._target_frames

    @property
    def buffered_frame_count(self) -> int:
        """返回当前尚未消费的完整帧数。"""

        with self._condition:
            return self._frame_count

    @property
    def target_sample_count(self) -> int:
        """返回 ready 后希望长期保留的目标格式样本数。"""

        return self._target_frames * self._frame_samples

    @property
    def buffered_sample_count(self) -> int:
        """返回完整帧和未满槽位中的总样本数。"""

        with self._condition:
            return self._frame_count * self._frame_samples + self._pending_sample_count

    @property
    def has_underflow(self) -> bool:
        """返回消费者是否曾经因为没有新帧而下溢。"""

        with self._condition:
            return self._underflow

    @property
    def has_overflow(self) -> bool:
        """返回生产者是否曾经因为固定容量已满而上溢。"""

        with self._condition:
            return self._overflow

    @property
    def terminal_partial_sample_count(self) -> int:
        """返回录音结束时不足一个 10ms 帧、被明确记账的尾部样本数。"""

        with self._condition:
            return self._terminal_partial_sample_count

    def append(self, samples: np.ndarray) -> None:
        """把一块已经规整的 float32 样本写入固定槽位。

        Args:
            samples: 形状为样本数乘声道数的二维数组。

        Returns:
            无返回值。

        Raises:
            BufferError: 环形队列已满。
            ValueError: 样本形状、大小或数值非法。
        """

        normalized = np.asarray(samples, dtype=np.float32)
        if normalized.ndim == 1 and self._channels == 1:
            normalized = normalized.reshape(-1, 1)
        if normalized.ndim != 2 or normalized.shape[1] != self._channels:
            raise ValueError("RDP microphone samples have invalid channels.")
        if normalized.shape[0] == 0:
            return
        if normalized.shape[0] > self._max_append_samples:
            raise ValueError("RDP microphone sample chunk exceeds the fixed chunk limit.")
        if not np.all(np.isfinite(normalized)):
            raise ValueError("RDP microphone samples contain non-finite values.")

        with self._condition:
            if self._finalized:
                raise RuntimeError("RDP microphone frame buffer is already finalized.")
            offset = 0
            while offset < normalized.shape[0]:
                # 完整帧槽位已满时连一个 pending 样本也不能接收，保证配置的最大容量是真实硬上限。
                if self._frame_count >= self._capacity_frames:
                    exception = BufferError("RDP microphone frame buffer overflow.")
                    self._overflow = True
                    if self._failure is None:
                        self._failure = exception
                    self._condition.notify_all()
                    raise exception
                copy_count = min(
                    normalized.shape[0] - offset,
                    self._frame_samples - self._pending_sample_count,
                )
                pending_end = self._pending_sample_count + copy_count
                self._pending_frame[self._pending_sample_count : pending_end, :] = normalized[
                    offset : offset + copy_count, :
                ]
                self._pending_sample_count = pending_end
                offset += copy_count
                if self._pending_sample_count != self._frame_samples:
                    continue
                self._frames[self._tail, :, :] = self._pending_frame
                self._tail = (self._tail + 1) % self._capacity_frames
                self._frame_count += 1
                self._pending_sample_count = 0
                self._condition.notify_all()

    def finalize(self) -> None:
        """结束生产并记录不足一个完整帧的尾部样本。

        录音停止时，最后一块音频可能不足 10ms，无法在不补静音的情况下满足固定帧契约。
        这里不把它伪装成完整音频，而是明确记录数量，便于诊断文件尾部是否被截断。
        """

        with self._condition:
            if self._finalized:
                return
            self._terminal_partial_sample_count = self._pending_sample_count
            self._pending_sample_count = 0
            self._finalized = True
            self._condition.notify_all()

    def read_frame(self) -> np.ndarray:
        """等待并消费下一组完整 10ms 音频。

        Returns:
            固定形状的 float32 音频帧副本。

        Raises:
            TimeoutError: 在限定时间内没有新的完整帧，说明采集发生下溢。
            BaseException: worker 已经记录采集失败时抛出首次异常。
        """

        deadline = time.monotonic() + self._underflow_timeout_seconds
        with self._condition:
            while self._frame_count == 0:
                self._raise_failure_locked()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    exception = TimeoutError("RDP microphone frame buffer underflow.")
                    self._underflow = True
                    raise exception
                self._condition.wait(remaining)
            self._raise_failure_locked()
            frame = self._frames[self._head].copy()
            self._head = (self._head + 1) % self._capacity_frames
            self._frame_count -= 1
            self._condition.notify_all()
            return frame

    def wait_until_ready(self, timeout_seconds: float) -> None:
        """等待首轮预缓冲完成或 worker 报错。

        Args:
            timeout_seconds: 等待 ready 的最长秒数。

        Returns:
            缓冲达到目标时正常返回。

        Raises:
            TimeoutError: 设备没有及时产生足够音频。
            BaseException: worker 已经记录采集失败时抛出首次异常。
        """

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while self._frame_count < self._target_frames:
                self._raise_failure_locked()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("RDP microphone did not reach the target prebuffer in time.")
                self._condition.wait(remaining)
            self._raise_failure_locked()

    def mark_failed(self, exception: BaseException) -> None:
        """记录第一条 worker 失败并唤醒所有等待者。

        Args:
            exception: 采集、解析或缓冲边界异常。

        Returns:
            无返回值；后续清理异常不会覆盖首次失败。
        """

        with self._condition:
            if self._failure is None:
                self._failure = exception
            self._condition.notify_all()

    def raise_if_failed(self) -> None:
        """在当前线程立即抛出首次 worker 异常。"""

        with self._condition:
            self._raise_failure_locked()

    def _raise_failure_locked(self) -> None:
        """在已经持有条件锁时抛出首次失败。"""

        if self._failure is not None:
            raise self._failure
