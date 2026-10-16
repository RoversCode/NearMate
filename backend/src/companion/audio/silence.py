from __future__ import annotations

import numpy as np


def is_silence_fast(pcm_data: bytes, threshold: float = 100.0) -> bool:
    """快速检测 PCM16 音频是否接近静音。

    Args:
        pcm_data: PCM16 音频字节。
        threshold: 静音阈值，低于此值判定为静音。

    Returns:
        True 表示接近静音，False 表示存在可感知音量。
    """

    # 使用 int16 直接计算，避免 float 转换开销
    audio_array = np.frombuffer(pcm_data, dtype=np.int16)
    # 绝对值均值，比 RMS 快约 30%
    mean_abs = np.mean(np.abs(audio_array))
    return mean_abs < threshold
