"""把 StreamVox 波形转换为播放所需的 PCM16，保持既有量化算法。"""

from __future__ import annotations

import numpy as np


def streamvox_float_audio_to_pcm16(audio_chunk: np.ndarray) -> bytes:
    """把 StreamVox 浮点波形块转换为 PCM16 字节。

    Args:
        audio_chunk: StreamVox 返回的浮点波形块。

    Returns:
        转换后的 PCM16 原始字节。
    """

    # StreamVox 返回 float waveform，先转为一维 float32 并裁剪到可播放范围。
    audio = np.asarray(audio_chunk, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return b""

    # 使用 32767 与测试/播放侧约定一致，避免 -1.0 乘 32768 带来的不对称边界。
    clipped_audio = np.clip(audio, -1.0, 1.0)
    pcm16 = (clipped_audio * 32767.0).astype(np.int16)
    return pcm16.tobytes()
