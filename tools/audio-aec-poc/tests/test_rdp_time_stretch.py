from __future__ import annotations

import numpy as np
import pytest

from audio_aec_poc.rdp_time_stretch import RdpWsolaTimeStretch


def _sine_wave(*, frequency: float, sample_rate: int, seconds: float) -> np.ndarray:
    """生成用于检测音高是否被时间伸缩改变的单声道正弦波。"""

    time_axis = np.arange(round(sample_rate * seconds), dtype=np.float64) / sample_rate
    return np.sin(2 * np.pi * frequency * time_axis).astype(np.float32).reshape(-1, 1)


def _estimate_frequency(samples: np.ndarray, sample_rate: int) -> float:
    """用窗口 FFT 估算稳定正弦片段的主频。"""

    mono = np.asarray(samples, dtype=np.float32).reshape(-1)
    window = np.hanning(mono.shape[0])
    spectrum = np.fft.rfft(mono * window)
    frequencies = np.fft.rfftfreq(mono.shape[0], 1 / sample_rate)
    return float(frequencies[int(np.argmax(np.abs(spectrum)))])


@pytest.mark.parametrize("speed", [42_000 / 48_000, 45_000 / 48_000, 1.0])
def test_wsola_preserves_220_hz_pitch_while_changing_duration(speed: float) -> None:
    """42/45/48kHz 到达速度下，WSOLA 改变时长但仍应保持约 220Hz 音高。"""

    timestretch = RdpWsolaTimeStretch(sample_rate=16_000, channels=1)
    timestretch.set_speed(speed)
    source = _sine_wave(frequency=220.0, sample_rate=16_000, seconds=4.0)
    chunks: list[np.ndarray] = []
    for start in range(0, source.shape[0], 1_136):
        chunks.append(timestretch.process(source[start : start + 1_136]))
    chunks.append(timestretch.finish())
    output = np.concatenate([chunk for chunk in chunks if chunk.shape[0] > 0], axis=0)

    assert output.shape[0] > source.shape[0] * (1 / speed) * 0.8
    stable_output = output[1_000:-1_000]
    assert _estimate_frequency(stable_output, 16_000) == pytest.approx(220.0, abs=8.0)


def test_wsola_speed_changes_keep_one_stream_and_do_not_clear_audio_state() -> None:
    """连续调速不能清空内部状态，也不能创建第二个 WSOLA 实例。"""

    timestretch = RdpWsolaTimeStretch(sample_rate=16_000, channels=1)
    stream_identity = timestretch.stream_identity
    source = _sine_wave(frequency=220.0, sample_rate=16_000, seconds=2.0)
    output_chunks: list[np.ndarray] = []
    for index, start in enumerate(range(0, source.shape[0], 320)):
        timestretch.set_speed(0.9 if index % 2 else 1.1)
        output_chunks.append(timestretch.process(source[start : start + 320]))
    output_chunks.append(timestretch.finish())

    output = np.concatenate([chunk for chunk in output_chunks if chunk.shape[0] > 0], axis=0)
    assert timestretch.stream_identity == stream_identity
    assert output.shape[0] > source.shape[0] * 0.7
    assert np.max(np.abs(output)) > 0.1


def test_wsola_rejects_non_finite_input() -> None:
    """损坏样本不能进入时间伸缩器。"""

    timestretch = RdpWsolaTimeStretch(sample_rate=16_000, channels=1)
    with pytest.raises(ValueError, match="finite"):
        timestretch.process(np.array([[np.nan]], dtype=np.float32))

