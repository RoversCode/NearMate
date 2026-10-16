from __future__ import annotations

from typing import Protocol

from companion.audio.providers.base import (
    AsrProvider,
    PartialTranscript,
    SmartTurnDecision,
    SmartTurnProvider,
    TextEouProvider,
    TtsChunk,
    TtsProvider,
    WakewordProvider,
)


class SpeechPipeline(Protocol):
    """语音链路抽象协议。

    当前阶段所有方法都允许由 fake/stub 实现完成，
    目的是先打通产品逻辑而不是先绑定真实推理代码。
    """

    def wake(self, pcm16: bytes) -> bool: ...
    def vad(self, pcm16: bytes) -> bool: ...
    def smart_turn(self, pcm16: bytes) -> bool: ...
    def asr(self, pcm16: bytes) -> str: ...
    def tts(self, text: str) -> list[bytes]: ...


__all__ = [
    "AsrProvider",
    "PartialTranscript",
    "SmartTurnDecision",
    "SmartTurnProvider",
    "TextEouProvider",
    "SpeechPipeline",
    "TtsChunk",
    "TtsProvider",
    "WakewordProvider",
]
