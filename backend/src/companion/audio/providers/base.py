from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(frozen=True)
class PartialTranscript:
    """实时 ASR 的中间转写结果。"""

    # 当前音频窗口识别出的局部文本，仅在本次返回对象内有效。
    text: str


@dataclass(frozen=True)
class SmartTurnDecision:
    """Smart Turn 的轮次结束判断结果。"""

    # 当前用户轮次是否已经结束，运行时据此决定是否收束当前 turn。
    is_turn_finished: bool
    # 轮次结束的置信度，fake 实现使用稳定值，真实实现可映射模型分数。
    probability: float


@dataclass(frozen=True)
class TtsChunk:
    """TTS 流式音频块。"""

    # 本次助理回复生成的唯一标识，用于处理中断与过期音频块。
    generation_id: str
    # PCM16 原始字节；fake 实现可用稳定字节模拟，真实实现应返回可播放音频。
    pcm16: bytes
    # 当前 PCM16 音频块的采样率，前端播放队列用它构造 AudioBuffer，避免猜测 TTS 输出格式。
    sample_rate: int = 24000
    # 当前 chunk 是否为该 segment 的最后一块。
    is_final: bool = False


class WakewordProvider(Protocol):
    """唤醒词检测 provider 协议。"""

    def detect(self, pcm16: bytes) -> bool:
        """检测 PCM16 音频是否包含唤醒词。"""
        ...

    def reset(self) -> None:
        """重置唤醒词检测状态。"""
        ...


class AsrProvider(Protocol):
    """实时 ASR provider 协议。"""

    async def transcribe_partial(self, pcm16: bytes) -> PartialTranscript:
        """生成实时中间转写结果。"""
        ...

    async def transcribe_final(self, pcm16: bytes) -> str:
        """生成当前用户轮次的最终转写文本。"""
        ...


class VadProvider(Protocol):
    """语音活动检测 provider 协议。"""

    def is_speech_frame(self, pcm16: bytes) -> bool:
        """判断单个固定长度 VAD 帧是否包含用户语音。"""
        ...

    def is_speech_chunk(self, pcm16: bytes) -> bool:
        """判断任意长度 PCM16 音频块是否包含用户语音。"""
        ...

    def is_speech(self, pcm16: bytes) -> bool:
        """兼容旧调用的任意长度语音检测入口。"""
        ...


class SmartTurnProvider(Protocol):
    """用户轮次结束判断 provider 协议。"""

    def decide_turn_end(self, pcm16: bytes) -> SmartTurnDecision:
        """判断当前用户轮次是否已经结束。"""
        ...


class TextEouProvider(Protocol):
    """文本轮次结束判断 provider 协议。"""

    def predict_end_probability(self, text: str) -> float:
        """判断 final ASR 文本是否已经表达完毕。"""
        ...


@dataclass(frozen=True)
class SpeakerVerificationResult:
    """说话人确认 provider 的单次检测结果。"""

    # 命中的白名单标识；未命中为 None，便于日志追溯。
    matched_name: str | None = None
    # 与白名单最高余弦相似度，供调参；未启用时为 None。
    best_similarity: float | None = None


class SpeakerVerificationProvider(Protocol):
    """说话人白名单确认 provider 协议。"""

    def verify(self, pcm16: bytes) -> SpeakerVerificationResult:
        """判断 PCM16 音频是否属于白名单说话人。"""
        ...


class TtsProvider(Protocol):
    """流式 TTS provider 协议。"""

    def stream_segment(self, text: str, generation_id: str) -> Iterable[TtsChunk]:
        """把单段文本转换为流式 TTS 音频块。"""
        ...
