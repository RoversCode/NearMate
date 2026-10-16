"""VAD 长音频分段器的单元测试（合成波形 + 伪概率函数，不依赖 ONNX 资产）。"""

from __future__ import annotations

import numpy as np
import pytest

from companion.audio.vad_segments import (
    SpeechSegment,
    VadSegmentationConfig,
    collect_speech_segments,
    pack_transcription_chunks,
)

_FRAME_SAMPLES = 512
_SAMPLE_RATE = 16000
# 512 样本 / 16000 Hz = 每帧 0.032 秒，测试里的期望时间都从它推出来。
_FRAME_SEC = _FRAME_SAMPLES / _SAMPLE_RATE


def _waveform_with_speech_frames(total_frames: int, speech_frames: set[int]) -> np.ndarray:
    """构造合成波形：语音帧填 1.0，静音帧填 0.0，配合伪概率函数使用。"""
    waveform = np.zeros(total_frames * _FRAME_SAMPLES, dtype=np.float32)
    for frame_index in speech_frames:
        start = frame_index * _FRAME_SAMPLES
        waveform[start : start + _FRAME_SAMPLES] = 1.0
    return waveform


def _frame_probability(frame: np.ndarray) -> float:
    """伪 VAD：帧内首样本即概率（合成波形里语音帧全 1.0，静音帧全 0.0）。"""
    return float(frame[0]) if frame.shape[0] else 0.0


def _test_config(**overrides: float) -> VadSegmentationConfig:
    """构造测试用分段配置，默认阈值比生产更激进，方便用小波形表达场景。"""
    defaults = {
        "min_silence_sec": 0.5,
        "min_speech_sec": 0.1,
        "padding_sec": 0.2,
        "max_chunk_sec": 30.0,
    }
    return VadSegmentationConfig(**{**defaults, **overrides})


def test_collect_segments_splits_two_speech_runs_on_long_silence() -> None:
    # 第 10-19 帧和第 60-79 帧是语音，中间 40 帧静音（远超 16 帧的切段门槛）。
    waveform = _waveform_with_speech_frames(100, set(range(10, 20)) | set(range(60, 80)))

    segments = collect_speech_segments(
        waveform,
        frame_probability=_frame_probability,
        config=_test_config(),
    )

    assert len(segments) == 2
    # 段 1：语音 0.32s-0.64s，外扩 0.2s 后 0.12s-0.84s。
    assert segments[0].start_sec == pytest.approx(0.32 - 0.2)
    assert segments[0].end_sec == pytest.approx(0.64 + 0.2)
    # 段 2：语音 1.92s-2.56s，外扩 0.2s 后 1.72s-2.76s。
    assert segments[1].start_sec == pytest.approx(1.92 - 0.2)
    assert segments[1].end_sec == pytest.approx(2.56 + 0.2)


def test_collect_segments_merges_across_short_silence() -> None:
    # 两段语音之间只有 5 帧静音（不足 16 帧切段门槛），应合并成一段。
    waveform = _waveform_with_speech_frames(100, set(range(10, 20)) | set(range(25, 41)))

    segments = collect_speech_segments(
        waveform,
        frame_probability=_frame_probability,
        config=_test_config(),
    )

    assert len(segments) == 1
    assert segments[0].start_sec == pytest.approx(10 * _FRAME_SEC - 0.2)
    assert segments[0].end_sec == pytest.approx(41 * _FRAME_SEC + 0.2)


def test_collect_segments_drops_speech_shorter_than_min_duration() -> None:
    # 3 帧语音（0.096s）低于 0.1s 最短语音阈值，视为噪声丢弃；10 帧的保留。
    waveform = _waveform_with_speech_frames(100, set(range(10, 13)) | set(range(60, 70)))

    segments = collect_speech_segments(
        waveform,
        frame_probability=_frame_probability,
        config=_test_config(),
    )

    assert len(segments) == 1
    assert segments[0].start_sec == pytest.approx(60 * _FRAME_SEC - 0.2)


def test_collect_segments_handles_empty_and_all_silence_waveform() -> None:
    assert (
        collect_speech_segments(
            np.zeros(0, dtype=np.float32),
            frame_probability=_frame_probability,
            config=_test_config(),
        )
        == []
    )
    assert (
        collect_speech_segments(
            np.zeros(100 * _FRAME_SAMPLES, dtype=np.float32),
            frame_probability=_frame_probability,
            config=_test_config(),
        )
        == []
    )


def test_collect_segments_pads_incomplete_tail_frame() -> None:
    # 末尾只有半帧也要被处理：总长度不是帧长整数倍时走补零路径。
    # 语音在第 10-19 帧，之后音频直接结束，段终点外扩后应被夹在音频总时长上。
    speech_frames = set(range(10, 20))
    waveform = _waveform_with_speech_frames(100, speech_frames)[: 20 * _FRAME_SAMPLES + 256]

    segments = collect_speech_segments(
        waveform,
        frame_probability=_frame_probability,
        config=_test_config(),
    )

    assert len(segments) == 1
    total_duration_sec = (20 * _FRAME_SAMPLES + 256) / _SAMPLE_RATE
    assert segments[0].end_sec == pytest.approx(total_duration_sec)


def test_pack_transcription_chunks_merges_adjacent_segments_within_limit() -> None:
    segments = [SpeechSegment(0.0, 10.0), SpeechSegment(10.5, 20.0)]

    chunks = pack_transcription_chunks(segments, max_chunk_sec=30.0)

    assert chunks == [SpeechSegment(0.0, 20.0)]


def test_pack_transcription_chunks_splits_when_merge_would_exceed_limit() -> None:
    segments = [SpeechSegment(0.0, 20.0), SpeechSegment(20.5, 40.0)]

    chunks = pack_transcription_chunks(segments, max_chunk_sec=30.0)

    assert chunks == [SpeechSegment(0.0, 20.0), SpeechSegment(20.5, 40.0)]


def test_pack_transcription_chunks_hard_splits_oversized_single_segment() -> None:
    # 75 秒无停顿语音超过上限时只能硬切，保证引擎单次上下文预算不被撑爆。
    chunks = pack_transcription_chunks([SpeechSegment(0.0, 75.0)], max_chunk_sec=30.0)

    assert chunks == [
        SpeechSegment(0.0, 30.0),
        SpeechSegment(30.0, 60.0),
        SpeechSegment(60.0, 75.0),
    ]


def test_pack_transcription_chunks_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        pack_transcription_chunks([SpeechSegment(0.0, 1.0)], max_chunk_sec=0.0)
