"""静音边界不允许向 Smart Turn 模型传入 NaN。"""

import numpy as np
import pytest

from companion.audio.bootstrap.smart_turn_runtime import prepare_smart_turn_pcm16


@pytest.mark.parametrize("audio", [b"", bytes(320), bytes(256000)], ids=["empty", "short", "eight_seconds"])
def test_silence_is_finite_zero_window(audio):
    """空输入和静音都会得到模型约定的八秒全零窗口。"""
    prepared = prepare_smart_turn_pcm16(audio)
    assert prepared.shape == (128000,)
    assert prepared.dtype == np.float32
    assert np.array_equal(prepared, np.zeros(128000, dtype=np.float32))


@pytest.mark.parametrize("samples", [[-6, -3, -1], [-4, 2, 3], [0, 1, 0]])
def test_nonzero_peak_retains_existing_normalization(samples):
    """正常输入仍执行原有峰值除法，不借修复静音改变其他数值。"""
    pcm = np.array(samples, dtype=np.int16)
    previous = pcm.astype(np.float32) / 32768.0
    previous = previous / previous.max()
    actual = prepare_smart_turn_pcm16(pcm.tobytes())
    assert np.array_equal(actual[-len(samples):], previous)
