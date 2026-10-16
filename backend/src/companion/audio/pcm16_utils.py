from __future__ import annotations

import io
import wave
from collections.abc import Iterable

import numpy as np


PCM16_SAMPLE_RATE = 16000
PCM16_SAMPLE_WIDTH_BYTES = 2
PCM16_CHANNEL_COUNT = 1
SPEAKER_VERIFICATION_CHUNK_BYTES = 1024
REQUIRED_SPEAKER_VERIFICATION_SPEECH_CHUNK_COUNT = 100
REQUIRED_SPEAKER_VERIFICATION_SPEECH_SECONDS = 3.2


def validate_pcm16_bytes(pcm16: bytes) -> None:
    """校验小端序 PCM16 字节结构。

    参数：
        pcm16: 待校验的原始 PCM16 字节。

    返回：
        None: 校验通过时不返回数据。
    """

    if not pcm16:
        raise ValueError("PCM16 音频不能为空")
    if len(pcm16) % PCM16_SAMPLE_WIDTH_BYTES != 0:
        raise ValueError("PCM16 字节长度必须是 2 的倍数")


def pcm16_to_float32(pcm16: bytes) -> np.ndarray:
    """把小端序 PCM16 转换为 float32 波形。

    参数：
        pcm16: 小端序单声道 PCM16 字节。

    返回：
        float32 一维波形，数值范围约为 [-1, 1]。
    """

    validate_pcm16_bytes(pcm16)
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0


def normalize_float32_peak(samples: np.ndarray) -> np.ndarray:
    """按绝对峰值归一化 float32 波形。

    参数：
        samples: 一维或可展平的 float32 波形。

    返回：
        峰值绝对值为 1 的一维 float32 波形；全零音频原样返回。
    """

    mono_samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if mono_samples.size == 0:
        raise ValueError("音频样本不能为空")

    peak = float(np.max(np.abs(mono_samples)))
    if peak < 1e-8:
        return mono_samples.astype(np.float32, copy=False)
    return (mono_samples / peak).astype(np.float32, copy=False)


def pcm16_to_normalized_float32(pcm16: bytes) -> np.ndarray:
    """把 PCM16 转换为峰值归一化后的 float32 波形。

    参数：
        pcm16: 小端序单声道 PCM16 字节。

    返回：
        峰值归一化后的一维 float32 波形。
    """

    return normalize_float32_peak(pcm16_to_float32(pcm16))


def float32_to_pcm16(samples: np.ndarray) -> bytes:
    """把 float32 波形转换为小端序 PCM16 字节。

    参数：
        samples: 一维 float32 波形。

    返回：
        小端序单声道 PCM16 字节。
    """

    clipped = np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0)
    int16_samples = np.where(clipped < 0, clipped * 32768.0, clipped * 32767.0).astype(np.int16)
    return int16_samples.tobytes()


def normalize_pcm16_peak(pcm16: bytes) -> bytes:
    """把 PCM16 音频按绝对峰值归一化后仍输出 PCM16。

    参数：
        pcm16: 小端序单声道 PCM16 字节。

    返回：
        归一化后的 PCM16 字节。
    """

    return float32_to_pcm16(pcm16_to_normalized_float32(pcm16))


def latest_speaker_verification_pcm16(pcm16: bytes) -> bytes:
    """截取说话人确认所需的最新 100 个有效人声 chunk。

    参数：
        pcm16: 已经只包含有效人声的 PCM16 字节。

    返回：
        最新最多 100 个 32ms chunk 的 PCM16 字节。
    """

    validate_pcm16_bytes(pcm16)
    max_bytes = REQUIRED_SPEAKER_VERIFICATION_SPEECH_CHUNK_COUNT * SPEAKER_VERIFICATION_CHUNK_BYTES
    return pcm16[-max_bytes:]


def combine_latest_speaker_verification_chunks(chunks: Iterable[bytes]) -> bytes:
    """拼接并截取最新 100 个有效人声 chunk。

    参数：
        chunks: VAD 判定为有效人声的 PCM16 chunk 序列。

    返回：
        最新最多 100 个 chunk 的 PCM16 字节。
    """

    return latest_speaker_verification_pcm16(b"".join(chunks))


def pcm16_to_wav_bytes(pcm16: bytes) -> bytes:
    """把 PCM16 编码为 16kHz 单声道 WAV。

    参数：
        pcm16: 小端序单声道 PCM16 字节。

    返回：
        可试听和可持久化的 WAV 字节。
    """

    normalized_pcm16 = normalize_pcm16_peak(pcm16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(PCM16_CHANNEL_COUNT)
        wav_file.setsampwidth(PCM16_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(PCM16_SAMPLE_RATE)
        wav_file.writeframes(normalized_pcm16)
    return buffer.getvalue()
