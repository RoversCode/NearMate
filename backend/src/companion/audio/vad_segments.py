"""离线长音频的 VAD 语音分段与转写单元打包。

实时链路里的 SileroVadSession 只回答“单帧是否语音”。离线转写面对的是
任意长度的完整波形，需要先找出语音边界：本模块逐帧扫描波形聚合出语音段，
再把相邻语音段在静音处打包成不超过时长上限的转写单元，避免 ASR 引擎的
固定窗口把一句话拦腰切断。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import numpy as np

# 帧长与采样率沿用实时链路的统一约定，保证离线与实时的 VAD 行为一致。
from companion.audio.bootstrap.realtime_asr_runtime import ASR_SAMPLE_RATE, SILERO_FRAME_SAMPLES

if TYPE_CHECKING:
    from companion.audio.bootstrap.realtime_asr_runtime import SileroVadSession


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    """一段连续语音在原始音频中的时间范围。

    Args:
        start_sec: 段起点（秒，全局时间轴）。
        end_sec: 段终点（秒，全局时间轴）。
    """

    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        """段时长（秒）。"""
        return self.end_sec - self.start_sec


@dataclass(frozen=True, slots=True)
class VadSegmentationConfig:
    """VAD 分段与打包的阈值配置。

    Args:
        speech_threshold: 单帧语音概率阈值，沿用实时链路的默认值。
        min_silence_sec: 静音持续超过该秒数才切断语音段；太短会把正常换气切成两段。
        min_speech_sec: 短于该秒数的语音段视为抖动噪声直接丢弃。
        padding_sec: 段首尾各向外扩张的秒数，防止切掉字头字尾。
        max_chunk_sec: 打包后单个转写单元的时长上限，须低于引擎 40 秒的舒适窗口。
    """

    speech_threshold: float = 0.5
    min_silence_sec: float = 0.5
    min_speech_sec: float = 0.25
    padding_sec: float = 0.2
    max_chunk_sec: float = 30.0


def collect_speech_segments(
    waveform: np.ndarray,
    *,
    frame_probability: Callable[[np.ndarray], float],
    config: VadSegmentationConfig | None = None,
    sample_rate: int = ASR_SAMPLE_RATE,
    frame_samples: int = SILERO_FRAME_SAMPLES,
) -> list[SpeechSegment]:
    """逐帧扫描波形，聚合出带全局时间戳的语音段。

    Args:
        waveform: 单声道波形，一维 float 数组。
        frame_probability: 输入定长帧、返回语音概率（0~1）的回调，便于测试注入伪实现。
        config: 分段阈值配置；为空时使用默认值。
        sample_rate: 波形采样率。
        frame_samples: VAD 单帧样本数。

    Returns:
        按时间顺序排列的语音段列表，已应用边缘外扩与短段过滤。
    """
    if config is None:
        config = VadSegmentationConfig()
    total_samples = int(waveform.shape[0])
    if total_samples == 0:
        return []

    total_duration_sec = total_samples / sample_rate
    frame_sec = frame_samples / sample_rate
    total_frames = math.ceil(total_samples / frame_samples)
    min_silence_frames = max(1, math.ceil(config.min_silence_sec / frame_sec))

    # 第一遍扫描：用“挂起静音”状态机收集语音段。语音开始后遇到的静音先记数，
    # 只有连续静音长度达标才真正切段，这样正常停顿不会把一句话拆散。
    raw_segments: list[tuple[int, int]] = []
    speech_start_frame: int | None = None
    silence_run_frames = 0
    for frame_index in range(total_frames):
        frame_start = frame_index * frame_samples
        frame = waveform[frame_start : frame_start + frame_samples]
        if frame.shape[0] < frame_samples:
            # 末尾不足一帧时补零：VAD 模型只接受定长输入。
            frame = np.pad(frame, (0, frame_samples - frame.shape[0]))
        if frame_probability(frame) >= config.speech_threshold:
            if speech_start_frame is None:
                speech_start_frame = frame_index
            silence_run_frames = 0
            continue
        if speech_start_frame is None:
            continue
        silence_run_frames += 1
        if silence_run_frames >= min_silence_frames:
            # 切段时把挂起的静音尾巴从段里剔除，段终点停在真实语音结束处。
            raw_segments.append((speech_start_frame, frame_index - silence_run_frames + 1))
            speech_start_frame = None
            silence_run_frames = 0
    if speech_start_frame is not None:
        raw_segments.append((speech_start_frame, total_frames))

    segments: list[SpeechSegment] = []
    for start_frame, end_frame in raw_segments:
        start_sec = start_frame * frame_sec
        end_sec = min(end_frame * frame_sec, total_duration_sec)
        if end_sec - start_sec < config.min_speech_sec:
            # 短促的“语音”多为咳嗽、磕碰等噪声，保留只会污染转写结果。
            continue
        segments.append(
            SpeechSegment(
                start_sec=max(0.0, start_sec - config.padding_sec),
                end_sec=min(total_duration_sec, end_sec + config.padding_sec),
            )
        )
    return segments


def pack_transcription_chunks(
    segments: list[SpeechSegment],
    *,
    max_chunk_sec: float,
) -> list[SpeechSegment]:
    """把相邻语音段在静音边界处合并成不超过时长上限的转写单元。

    Args:
        segments: collect_speech_segments 输出的语音段列表。
        max_chunk_sec: 单个转写单元的时长上限（秒）。

    Returns:
        按时间顺序排列的转写单元列表。

    Raises:
        ValueError: 时长上限不是正数。
    """
    if max_chunk_sec <= 0:
        raise ValueError("转写单元时长上限必须大于零")

    # 极端情况下单段无停顿语音也会超过上限（引擎上下文预算装不下），
    # 这种段只能硬切成上限长度的小段，宁可断句也不能撑爆引擎。
    bounded_segments: list[SpeechSegment] = []
    for segment in segments:
        piece_start = segment.start_sec
        while segment.end_sec - piece_start > max_chunk_sec:
            bounded_segments.append(SpeechSegment(piece_start, piece_start + max_chunk_sec))
            piece_start += max_chunk_sec
        bounded_segments.append(SpeechSegment(piece_start, segment.end_sec))

    chunks: list[SpeechSegment] = []
    current: SpeechSegment | None = None
    for segment in bounded_segments:
        if current is None:
            current = segment
            continue
        if segment.end_sec - current.start_sec <= max_chunk_sec:
            current = SpeechSegment(current.start_sec, segment.end_sec)
        else:
            chunks.append(current)
            current = segment
    if current is not None:
        chunks.append(current)
    return chunks


def segment_waveform_with_vad(
    waveform: np.ndarray,
    *,
    vad_session: SileroVadSession,
    config: VadSegmentationConfig | None = None,
) -> list[SpeechSegment]:
    """用真实 Silero VAD 会话扫描波形，返回打包好的转写单元。

    Args:
        waveform: 16kHz 单声道 float32 波形。
        vad_session: 已加载的 Silero VAD 会话；扫描前会重置其跨帧状态。
        config: 分段与打包阈值；为空时使用默认配置。

    Returns:
        按时间顺序排列的转写单元列表，时间戳为相对波形开头的全局秒。
    """
    if config is None:
        config = VadSegmentationConfig()
    # VAD 的 state/context 跨帧累积，扫描新音频前必须重置，否则上一段音频
    # 的残留状态会污染本段开头若干帧的判断。
    vad_session.reset()
    segments = collect_speech_segments(
        waveform,
        frame_probability=lambda frame: vad_session.infer(frame)[0],
        config=config,
        sample_rate=vad_session.config.sample_rate,
        frame_samples=vad_session.config.frame_samples,
    )
    return pack_transcription_chunks(segments, max_chunk_sec=config.max_chunk_sec)
