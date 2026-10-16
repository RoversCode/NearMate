from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any
import uuid

import numpy as np
from audiotsm import wsola
from audiotsm.io.array import ArrayReader


_DEFAULT_FRAME_LENGTH_MS = 40
_DEFAULT_SYNTHESIS_HOP_MS = 20
_DEFAULT_TOLERANCE_MS = 10
_MIN_WSOLA_SPEED = 0.85
_MAX_WSOLA_SPEED = 1.15


class _ChunkWriter:
    """收集一次 worker 调用产生的 WSOLA 输出，不把收集器暴露给业务代码。"""

    def __init__(self, channels: int) -> None:
        """保存声道数并创建当前调用的输出块列表。"""

        self._channels = channels
        self._chunks: list[np.ndarray] = []

    @property
    def channels(self) -> int:
        """返回输出声道数。"""

        return self._channels

    def write(self, buffer: np.ndarray) -> int:
        """复制一块 WSOLA 输出，返回写入的样本数。"""

        if buffer.ndim != 2 or buffer.shape[0] != self._channels:
            raise ValueError("WSOLA writer received an invalid audio shape.")
        copied = np.ascontiguousarray(buffer, dtype=np.float32).copy()
        self._chunks.append(copied)
        return copied.shape[1]

    def take(self) -> np.ndarray:
        """返回当前调用收集的 `(channels, samples)` 数组。"""

        if not self._chunks:
            return np.empty((self._channels, 0), dtype=np.float32)
        return np.concatenate(self._chunks, axis=1)


@dataclass(frozen=True)
class RdpTimeStretchDiagnostics:
    """保存 WSOLA 时间伸缩器的状态。

    Args:
        speed: 当前 WSOLA 速度。
        input_sample_count: 已送入 WSOLA 的样本数。
        output_sample_count: 已从 WSOLA 取出的样本数。
        process_call_count: 已处理的输入块次数。

    Returns:
        一个不可变的时间伸缩诊断快照。
    """

    speed: float
    input_sample_count: int
    output_sample_count: int
    process_call_count: int


class RdpWsolaTimeStretch:
    """用一个可连续调速的 WSOLA 流改变音频时长，但保持人声音高。

    每个采集轮次只创建一个 audiotsm TSM 对象。新的输入块沿用同一个分析和合成状态，
    `set_speed` 只改变后续分析步长，不清空内部缓冲；SoXR 的固定采样率转换发生在本类之外。

    Args:
        sample_rate: 输入和输出音频的固定采样率。
        channels: 音频声道数。
        frame_length_ms: WSOLA 分析窗口长度，默认约 40ms。
        synthesis_hop_ms: WSOLA 合成步长，默认约 20ms。
        tolerance_ms: WSOLA 搜索范围，限制波形匹配不会跨越过大的位置。

    Returns:
        一个提供 process、set_speed、finish 的流式时间伸缩器。
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        frame_length_ms: int = _DEFAULT_FRAME_LENGTH_MS,
        synthesis_hop_ms: int = _DEFAULT_SYNTHESIS_HOP_MS,
        tolerance_ms: int = _DEFAULT_TOLERANCE_MS,
    ) -> None:
        """校验音频参数并创建初始速度为 1 的唯一 WSOLA 流。"""

        if sample_rate <= 0 or channels <= 0:
            raise ValueError("WSOLA audio format must be positive.")
        if frame_length_ms <= 0 or synthesis_hop_ms <= 0 or tolerance_ms <= 0:
            raise ValueError("WSOLA frame parameters must be positive.")
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_length = max(2, round(sample_rate * frame_length_ms / 1000))
        self._synthesis_hop = max(1, round(sample_rate * synthesis_hop_ms / 1000))
        self._tolerance = max(1, round(sample_rate * tolerance_ms / 1000))
        if self._synthesis_hop >= self._frame_length:
            raise ValueError("WSOLA synthesis hop must be shorter than frame length.")
        self._stream: Any = wsola(
            channels,
            speed=1.0,
            frame_length=self._frame_length,
            synthesis_hop=self._synthesis_hop,
            tolerance=self._tolerance,
        )
        self._stream_identity = uuid.uuid4().hex
        self._speed = 1.0
        self._input_sample_count = 0
        self._output_sample_count = 0
        self._process_call_count = 0
        self._finished = False

    @property
    def speed(self) -> float:
        """返回当前 WSOLA 速度。"""

        return self._speed

    @property
    def stream_identity(self) -> str:
        """返回当前采集轮次唯一的 WSOLA 流身份。"""

        return self._stream_identity

    @property
    def diagnostics(self) -> RdpTimeStretchDiagnostics:
        """返回输入、输出和处理次数诊断。"""

        return RdpTimeStretchDiagnostics(
            speed=self._speed,
            input_sample_count=self._input_sample_count,
            output_sample_count=self._output_sample_count,
            process_call_count=self._process_call_count,
        )

    def set_speed(self, speed: float) -> None:
        """更新后续输出速度，但保留当前 WSOLA 的全部内部状态。

        Args:
            speed: 速度比例；小于 1 会延长音频，大于 1 会缩短音频。

        Raises:
            ValueError: 速度不是有限数，或超出 0.85 到 1.15 的边界。
            RuntimeError: 当前流已经结束。
        """

        if self._finished:
            raise RuntimeError("RDP WSOLA cannot change speed after finish().")
        if not math.isfinite(speed) or not _MIN_WSOLA_SPEED <= speed <= _MAX_WSOLA_SPEED:
            raise ValueError("RDP WSOLA speed must be finite and between 0.85 and 1.15.")
        if speed == self._speed:
            return
        self._stream.set_speed(float(speed))
        self._speed = float(speed)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """把一块固定采样率音频送入 WSOLA，并返回当前已经就绪的输出。

        Args:
            samples: 形状为 `(samples, channels)` 的 float32 音频块。

        Returns:
            形状为 `(output_samples, channels)` 的连续 float32 音频块；启动阶段可能为空。

        Raises:
            ValueError: 音频形状或数值非法。
            RuntimeError: 当前流已经结束或无法继续消费输入。
        """

        if self._finished:
            raise RuntimeError("RDP WSOLA cannot process samples after finish().")
        normalized = np.asarray(samples, dtype=np.float32)
        if normalized.ndim == 1 and self._channels == 1:
            normalized = normalized.reshape(-1, 1)
        if normalized.ndim != 2 or normalized.shape[1] != self._channels:
            raise ValueError("RDP WSOLA samples have invalid channels.")
        if not np.all(np.isfinite(normalized)):
            raise ValueError("RDP WSOLA samples must be finite.")
        if normalized.shape[0] == 0:
            return np.empty((0, self._channels), dtype=np.float32)

        reader = ArrayReader(np.ascontiguousarray(normalized.T, dtype=np.float32))
        writer = _ChunkWriter(self._channels)
        while not reader.empty:
            read_count = self._stream.read_from(reader)
            written_count, _finished = self._stream.write_to(writer)
            if read_count == 0 and written_count == 0:
                raise RuntimeError("RDP WSOLA made no progress while consuming input.")
        # 输入刚好耗尽时，仍可能有已经就绪但尚未写出的 overlap-add 输出。
        while True:
            written_count, _finished = self._stream.write_to(writer)
            if written_count == 0:
                break

        output = writer.take().T
        self._input_sample_count += normalized.shape[0]
        self._output_sample_count += output.shape[0]
        self._process_call_count += 1
        return np.ascontiguousarray(output, dtype=np.float32)

    def finish(self) -> np.ndarray:
        """排空 WSOLA 尾部并结束当前采集轮次。

        Returns:
            当前流剩余的连续 float32 音频。

        Raises:
            RuntimeError: 当前流已经结束，或库无法推进到完成状态。
        """

        if self._finished:
            raise RuntimeError("RDP WSOLA finish() can only be called once.")
        writer = _ChunkWriter(self._channels)
        finished = False
        while not finished:
            written_count, finished = self._stream.flush_to(writer)
            if written_count == 0 and not finished:
                raise RuntimeError("RDP WSOLA made no progress while flushing.")
        output = writer.take().T
        self._output_sample_count += output.shape[0]
        self._finished = True
        return np.ascontiguousarray(output, dtype=np.float32)
