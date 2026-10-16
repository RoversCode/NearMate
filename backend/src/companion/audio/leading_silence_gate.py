from __future__ import annotations

import io
import wave
from dataclasses import dataclass

from companion.observability.logging_config import get_backend_logger
from companion.audio.silence import is_silence_fast


logger = get_backend_logger(__name__)

PCM16_SAMPLE_WIDTH_BYTES = 2
DEFAULT_MAX_PRESERVED_SILENCE_MS = 200.0
DEFAULT_DETECTION_FRAME_MS = 10.0
DEFAULT_SILENCE_THRESHOLD = 100.0


@dataclass
class LeadingSilenceGate:
    """缓存一轮 TTS 开头音频，并在首响出现后限幅释放前导静音。

    Args:
        sample_rate: PCM16 音频采样率。
        max_preserved_silence_ms: 首响前最多保留的静音时长。
        frame_ms: 静音检测窗口时长。
        threshold: 传给静音检测器的幅度阈值。
    """

    sample_rate: int
    max_preserved_silence_ms: float = DEFAULT_MAX_PRESERVED_SILENCE_MS
    frame_ms: float = DEFAULT_DETECTION_FRAME_MS
    threshold: float = DEFAULT_SILENCE_THRESHOLD

    def __post_init__(self) -> None:
        """校验门控参数并初始化缓存状态。"""

        if self.sample_rate <= 0:
            raise ValueError("sample_rate 必须是正整数")
        if self.max_preserved_silence_ms < 0:
            raise ValueError("max_preserved_silence_ms 必须是非负数")
        if self.frame_ms <= 0:
            raise ValueError("frame_ms 必须是正数")

        frame_sample_count = int(self.sample_rate * self.frame_ms / 1000.0)
        self._frame_bytes = max(1, frame_sample_count) * PCM16_SAMPLE_WIDTH_BYTES
        preserved_sample_count = int(self.sample_rate * self.max_preserved_silence_ms / 1000.0)
        self._max_preserved_bytes = max(0, preserved_sample_count) * PCM16_SAMPLE_WIDTH_BYTES
        self._buffer = b""
        self._scan_offset_bytes = 0
        # 输入 chunk 序号按 TTS provider 推给门控的非空 PCM16 块计数，用于定位首响出现位置。
        self._input_chunk_count = 0
        self.completed = False

    def push(self, pcm16: bytes) -> list[bytes]:
        """接收一个音频块，并返回当前允许播放的 PCM16 块。

        Args:
            pcm16: 本次到达的 PCM16 音频块。

        Returns:
            当前可向播放队列释放的 PCM16 块列表。
        """

        if not pcm16:
            return []
        if len(pcm16) % PCM16_SAMPLE_WIDTH_BYTES != 0:
            raise ValueError("PCM16 字节长度必须是 2 的倍数")

        if self.completed:
            return [pcm16]

        self._input_chunk_count += 1
        self._buffer += pcm16
        voice_start_byte = self._find_first_voice_byte()
        if voice_start_byte is None:
            return []

        # 首响很快出现时保留原始起音；只有超过预算的长静音才裁掉更早部分。
        start_byte = 0
        if voice_start_byte > self._max_preserved_bytes:
            start_byte = voice_start_byte - self._max_preserved_bytes

        released = self._buffer[start_byte:]
        logger.info(
            "TTS 首响门控释放音频 sample_rate=%s threshold=%.2f frame_ms=%.2f "
            "voice_input_chunk_index=%s "
            "voice_start_ms=%.2f preserved_silence_ms=%.2f trimmed_leading_ms=%.2f "
            "input_buffer_ms=%.2f output_ms=%.2f",
            self.sample_rate,
            self.threshold,
            self.frame_ms,
            self._input_chunk_count,
            self._bytes_to_ms(voice_start_byte),
            self._bytes_to_ms(voice_start_byte - start_byte),
            self._bytes_to_ms(start_byte),
            self._bytes_to_ms(len(self._buffer)),
            self._bytes_to_ms(len(released)),
        )
        self._buffer = b""
        self._scan_offset_bytes = 0
        self.completed = True
        return [released] if released else []

    def finish(self) -> list[bytes]:
        """收束门控状态，丢弃尚未出现首响的静音缓存。

        Returns:
            需要释放的 PCM16 块列表；全静音场景返回空列表。
        """

        if self.completed:
            return []

        # 整轮没有检测到首响时，静音缓存没有播放价值，直接丢弃以避免拖慢状态机。
        logger.info(
            "TTS 首响门控未检测到非静音，丢弃缓存 sample_rate=%s threshold=%.2f frame_ms=%.2f "
            "input_chunk_count=%s discarded_ms=%.2f",
            self.sample_rate,
            self.threshold,
            self.frame_ms,
            self._input_chunk_count,
            self._bytes_to_ms(len(self._buffer)),
        )
        self._buffer = b""
        self._scan_offset_bytes = 0
        self.completed = True
        return []

    def _find_first_voice_byte(self) -> int | None:
        """按检测窗口查找首个非静音窗口的起始字节。"""

        complete_frame_count = len(self._buffer) // self._frame_bytes
        start_frame_index = self._scan_offset_bytes // self._frame_bytes
        for frame_index in range(start_frame_index, complete_frame_count):
            start = frame_index * self._frame_bytes
            frame = self._buffer[start : start + self._frame_bytes]
            if not is_silence_fast(frame, threshold=self.threshold):
                return start
        # 已确认静音的完整窗口不再重复检测；不足一个窗口的尾部残留留给下一次 push 拼满。
        self._scan_offset_bytes = complete_frame_count * self._frame_bytes
        return None

    def _bytes_to_ms(self, byte_count: int) -> float:
        """把 PCM16 字节数转换成毫秒，供裁剪诊断日志使用。"""

        sample_count = max(0, byte_count) / PCM16_SAMPLE_WIDTH_BYTES
        return sample_count / self.sample_rate * 1000.0


def trim_pcm16_leading_silence(
    pcm16: bytes,
    *,
    sample_rate: int,
    max_preserved_silence_ms: float = DEFAULT_MAX_PRESERVED_SILENCE_MS,
    frame_ms: float = DEFAULT_DETECTION_FRAME_MS,
    threshold: float = DEFAULT_SILENCE_THRESHOLD,
) -> bytes:
    """把完整 PCM16 音频开头的长静音限幅到首响前最多 200ms。

    Args:
        pcm16: 完整 PCM16 音频。
        sample_rate: PCM16 音频采样率。
        max_preserved_silence_ms: 首响前最多保留的静音时长。
        frame_ms: 静音检测窗口时长。
        threshold: 静音判断阈值。

    Returns:
        裁剪后的 PCM16；全静音时返回空字节串。
    """

    gate = LeadingSilenceGate(
        sample_rate=sample_rate,
        max_preserved_silence_ms=max_preserved_silence_ms,
        frame_ms=frame_ms,
        threshold=threshold,
    )
    released = gate.push(pcm16)
    if released:
        return b"".join(released)
    return b"".join(gate.finish())


def trim_wav_leading_silence(
    wav_bytes: bytes,
    *,
    max_preserved_silence_ms: float = DEFAULT_MAX_PRESERVED_SILENCE_MS,
    frame_ms: float = DEFAULT_DETECTION_FRAME_MS,
    threshold: float = DEFAULT_SILENCE_THRESHOLD,
) -> bytes:
    """把单声道 PCM16 WAV 的开头长静音限幅到首响前最多 200ms。

    Args:
        wav_bytes: 原始 WAV 文件字节。
        max_preserved_silence_ms: 首响前最多保留的静音时长。
        frame_ms: 静音检测窗口时长。
        threshold: 静音判断阈值。

    Returns:
        裁剪后的 WAV 文件字节；非单声道 PCM16 WAV 原样返回。
    """

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        pcm16 = wav_file.readframes(wav_file.getnframes())

    if channels != 1 or sample_width != PCM16_SAMPLE_WIDTH_BYTES:
        logger.info(
            "WAV 前导静音裁剪跳过：仅支持单声道 PCM16 channels=%s sample_width=%s sample_rate=%s",
            channels,
            sample_width,
            sample_rate,
        )
        return wav_bytes

    trimmed_pcm16 = trim_pcm16_leading_silence(
        pcm16,
        sample_rate=sample_rate,
        max_preserved_silence_ms=max_preserved_silence_ms,
        frame_ms=frame_ms,
        threshold=threshold,
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(trimmed_pcm16)
    return buffer.getvalue()
