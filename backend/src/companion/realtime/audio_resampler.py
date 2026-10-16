from __future__ import annotations

import numpy as np
import soxr


def resample_pcm16_mono(*, pcm16: bytes, source_sample_rate: int, target_sample_rate: int) -> bytes:
    """把单声道 PCM16 音频重采样到目标采样率。

    Args:
        pcm16: 小端序 16 位单声道 PCM 音频字节。
        source_sample_rate: 输入音频采样率。
        target_sample_rate: 输出音频采样率。

    Returns:
        目标采样率下的小端序 16 位单声道 PCM 音频字节。

    Raises:
        ValueError: 当采样率非正数，或 PCM16 字节长度不是 2 的倍数时抛出。
    """

    # 空音频没有播放价值，也不需要触发 soxr 的重采样边界。
    if not pcm16:
        return b""

    # 采样率必须能描述真实时间轴，否则重采样结果没有业务意义。
    if source_sample_rate <= 0 or target_sample_rate <= 0:
        raise ValueError("audio resampler expects positive sample rates")

    # PCM16 必须按 16 位样本对齐，避免把破损字节解释成错误波形。
    if len(pcm16) % 2 != 0:
        raise ValueError("audio resampler expects even-length PCM16 bytes")

    # 同采样率时保持原对象返回，让热路径不发生不必要的数组分配。
    if source_sample_rate == target_sample_rate:
        return pcm16

    source_audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32767.0
    # 经过测试今天约等于无耗时
    resampled_audio = soxr.resample(
        source_audio,
        source_sample_rate,
        target_sample_rate,
        quality="HQ",
    )
    clipped_audio = np.clip(resampled_audio, -1.0, 1.0)
    return (clipped_audio * 32767.0).astype(np.int16).tobytes()
