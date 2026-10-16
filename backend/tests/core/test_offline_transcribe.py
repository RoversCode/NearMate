"""离线转写 CLI 编排的单元测试（引擎与 VAD 均为伪实现，不加载真实模型）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from companion.audio import offline_transcribe
from companion.audio.offline_transcribe import TranscribedSegment

_FRAME_SAMPLES = 512
_SAMPLE_RATE = 16000


class _FakeVadSession:
    """按帧序号查表返回语音概率的伪 VAD 会话。"""

    def __init__(self, speech_frames: set[int]) -> None:
        self.config = SimpleNamespace(sample_rate=_SAMPLE_RATE, frame_samples=_FRAME_SAMPLES)
        self._speech_frames = speech_frames
        self._frame_index = -1
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1
        self._frame_index = -1

    def infer(self, frame: np.ndarray) -> tuple[float, float]:
        self._frame_index += 1
        return (0.9 if self._frame_index in self._speech_frames else 0.0), 0.0


class _FakeEngine:
    """记录调用参数并按调用顺序返回固定文本的伪 ASR 引擎。"""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.calls: list[dict[str, Any]] = []

    def asr(self, *, audio, context, language, chunk_size_sec, memory_chunks):
        self.calls.append(
            {
                "audio_samples": int(audio.shape[0]),
                "context": context,
                "language": language,
                "chunk_size_sec": chunk_size_sec,
                "memory_chunks": memory_chunks,
            }
        )
        return SimpleNamespace(text=self._texts[len(self.calls) - 1])


def _silent_waveform(total_frames: int) -> np.ndarray:
    return np.zeros(total_frames * _FRAME_SAMPLES, dtype=np.float32)


def test_transcribe_waveform_offsets_timestamps_and_chains_context() -> None:
    # 第 10-19 帧和第 60-69 帧是语音；打包上限设为 1 秒，强制拆成两个转写单元
    # （默认 30 秒上限会把这两段近距离语音合并成一次引擎调用，无法验证链式上下文）。
    vad_session = _FakeVadSession(set(range(10, 20)) | set(range(60, 70)))
    engine = _FakeEngine(["甲段内容。", "乙段内容。"])

    segments = offline_transcribe.transcribe_waveform(
        _silent_waveform(100),
        engine=engine,
        vad_session=vad_session,
        language="Chinese",
        max_chunk_sec=1.0,
    )

    assert vad_session.reset_count == 1
    assert [segment.text for segment in segments] == ["甲段内容。", "乙段内容。"]
    # 时间戳必须是相对原始音频的全局时间（含 VAD 外扩 0.2s），不能是段内局部时间。
    assert segments[0].start_sec == pytest.approx(0.12)
    assert segments[0].end_sec == pytest.approx(0.84)
    assert segments[1].start_sec == pytest.approx(1.72)
    assert segments[1].end_sec == pytest.approx(2.44)
    # 第一段不带前文提示；第二段应携带第一段文本作为上下文，保持跨段连贯。
    assert engine.calls[0]["context"] == ""
    assert engine.calls[1]["context"] == "甲段内容。"
    # 每段已被 VAD 打包进上限以内，引擎内部不应再二次切分。
    assert engine.calls[0]["chunk_size_sec"] == 1.0
    assert engine.calls[0]["memory_chunks"] == 0
    assert engine.calls[0]["language"] == "Chinese"


def test_transcribe_waveform_falls_back_to_engine_chunking_when_vad_finds_nothing() -> None:
    # VAD 全程未检出语音（如唱歌/音乐内容）时，应回退为覆盖全文的一个转写单元，
    # 并启用引擎内部的固定窗口分块与跨块记忆，而不是交付空结果。
    vad_session = _FakeVadSession(set())
    engine = _FakeEngine(["整段歌词文本。"])

    segments = offline_transcribe.transcribe_waveform(
        _silent_waveform(100),
        engine=engine,
        vad_session=vad_session,
        language="Chinese",
        max_chunk_sec=30.0,
    )

    assert [segment.text for segment in segments] == ["整段歌词文本。"]
    assert segments[0].start_sec == 0.0
    assert segments[0].end_sec == pytest.approx(100 * _FRAME_SAMPLES / _SAMPLE_RATE)
    assert engine.calls[0]["memory_chunks"] == 2


def test_transcribe_waveform_skips_blank_results() -> None:
    vad_session = _FakeVadSession(set(range(10, 20)) | set(range(60, 70)))
    engine = _FakeEngine(["   ", "有效内容。"])

    segments = offline_transcribe.transcribe_waveform(
        _silent_waveform(100),
        engine=engine,
        vad_session=vad_session,
        language="Chinese",
        max_chunk_sec=1.0,
    )

    assert [segment.text for segment in segments] == ["有效内容。"]


def test_write_transcription_outputs_produces_srt_json_txt(tmp_path) -> None:
    segments = [
        TranscribedSegment(start_sec=0.12, end_sec=0.84, text="你好"),
        TranscribedSegment(start_sec=65.72, end_sec=66.76, text="世界"),
    ]

    outputs = offline_transcribe.write_transcription_outputs(
        segments,
        input_path=tmp_path / "讲座录像.mp4",
        output_dir=tmp_path / "out",
        formats={"srt", "json", "txt"},
        duration_sec=66.76,
        language="Chinese",
    )

    srt_text = (tmp_path / "out" / "讲座录像.srt").read_text(encoding="utf-8")
    assert "00:00:00,120 --> 00:00:00,840" in srt_text
    assert "00:01:05,720 --> 00:01:06,760" in srt_text
    assert outputs["srt"].endswith("讲座录像.srt")

    payload = json.loads((tmp_path / "out" / "讲座录像.json").read_text(encoding="utf-8"))
    assert payload["language"] == "Chinese"
    assert payload["duration_sec"] == pytest.approx(66.76)
    assert payload["segments"][1] == {"start_sec": 65.72, "end_sec": 66.76, "text": "世界"}

    txt_text = (tmp_path / "out" / "讲座录像.txt").read_text(encoding="utf-8")
    assert txt_text == "你好\n世界"


def test_main_end_to_end_with_fakes(monkeypatch, tmp_path, capsys) -> None:
    input_file = tmp_path / "talk.wav"
    input_file.write_bytes(b"fake")
    output_dir = tmp_path / "delivery"
    vad_session = _FakeVadSession(set(range(10, 20)))
    engine = _FakeEngine(["唯一一段。"])

    # 重资产（模型资产装配、引擎、VAD、音频解码）全部换成伪实现，只验证编排链路。
    monkeypatch.setattr(
        offline_transcribe,
        "build_realtime_asr_runtime_config",
        lambda: SimpleNamespace(asr=object(), vad=object()),
    )
    monkeypatch.setattr(offline_transcribe, "create_qwen_asr_engine", lambda config: engine)
    monkeypatch.setattr(offline_transcribe, "create_vad_session", lambda config: vad_session)
    monkeypatch.setattr(
        offline_transcribe,
        "load_audio",
        lambda path, sample_rate=_SAMPLE_RATE: _silent_waveform(100),
    )

    exit_code = offline_transcribe.main(
        ["--input", str(input_file), "--output-dir", str(output_dir)]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["segment_count"] == 1
    assert summary["language"] == "Chinese"
    assert set(summary["outputs"]) == {"srt", "json", "txt"}
    for output_path in summary["outputs"].values():
        assert Path(output_path).is_file()


def test_main_rejects_unsupported_format(tmp_path) -> None:
    input_file = tmp_path / "talk.wav"
    input_file.write_bytes(b"fake")

    with pytest.raises(SystemExit):
        offline_transcribe.main(
            ["--input", str(input_file), "--output-dir", str(tmp_path), "--formats", "srt,bogus"]
        )
