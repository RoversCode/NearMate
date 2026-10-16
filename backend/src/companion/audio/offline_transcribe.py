"""离线音视频转写命令行入口。

受管 Skill `media-transcribe` 经本模块把任意长度的音频/视频文件转写成带
时间戳的文本：先用 Silero VAD 在静音处把长音频切成引擎舒适长度的语音段，
再逐段调用 Qwen ASR 引擎，最后按全局时间轴导出 SRT/JSON/TXT。

用法：
    python -m companion.audio.offline_transcribe \
        --input <音视频文件> --output-dir <交付目录>

为什么不复用实时链路：RealtimeAsrWorker 绑定 LiveKit 会话与 asyncio 串行锁，
且只透传纯文本不带时间戳。离线批处理在独立子进程里加载自己的引擎实例，
与实时会话完全隔离，长音频转写不会阻塞实时对话。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# 引擎工厂与 VAD 装配都是轻量导入；onnxruntime/GGUF 等重依赖在工厂调用时才加载。
from companion.audio.bootstrap.qwen_asr_engine_adapter import create_qwen_asr_engine
from companion.audio.bootstrap.realtime_asr_runtime import (
    ASR_SAMPLE_RATE,
    build_realtime_asr_runtime_config,
    create_vad_session,
)
from companion.audio.qwen_asr_gguf.inference.audio import load_audio
from companion.audio.vad_segments import (
    SpeechSegment,
    VadSegmentationConfig,
    segment_waveform_with_vad,
)

_SUPPORTED_FORMATS = ("srt", "json", "txt")
# 传给下一段的前文提示长度上限；太长会挤占引擎 n_ctx 里的文本 token 预算。
_CONTEXT_TAIL_CHARS = 100


@dataclass(frozen=True, slots=True)
class TranscribedSegment:
    """一段带全局时间戳的识别文本。

    Args:
        start_sec: 段起点（秒，相对原始文件开头）。
        end_sec: 段终点（秒，相对原始文件开头）。
        text: 该段识别出的文本。
    """

    start_sec: float
    end_sec: float
    text: str


def transcribe_waveform(
    waveform: np.ndarray,
    *,
    engine,
    vad_session,
    language: str,
    max_chunk_sec: float,
) -> list[TranscribedSegment]:
    """把整段波形按 VAD 分段逐段送引擎识别，返回带全局时间戳的文本段。

    Args:
        waveform: 16kHz 单声道 float32 波形。
        engine: 已加载的 Qwen ASR 引擎。
        vad_session: 已加载的 Silero VAD 会话（扫描前其内部状态会被重置）。
        language: 识别语言（Qwen ASR 支持的语言名）。
        max_chunk_sec: 单个转写单元的时长上限（秒）。

    Returns:
        按时间顺序排列的识别段列表；识别为空白的段会被丢弃。
    """
    chunks = segment_waveform_with_vad(
        waveform,
        vad_session=vad_session,
        config=VadSegmentationConfig(max_chunk_sec=max_chunk_sec),
    )
    # Silero VAD 对歌声/音乐的判定保守，可能全程检不出语音。此时回退为一段
    # 覆盖全文的转写单元，交给引擎内部的固定窗口分块处理：宁可边界粗糙，
    # 也不能把有可懂内容的文件交付成空白结果。
    vad_empty = not chunks
    if vad_empty:
        print("[离线转写] VAD 未检出语音段，回退为引擎固定窗口分块", file=sys.stderr, flush=True)
        chunks = [SpeechSegment(0.0, float(waveform.size) / ASR_SAMPLE_RATE)]
    segments: list[TranscribedSegment] = []
    context = ""
    for index, chunk in enumerate(chunks, start=1):
        # 进度走 stderr，保持 stdout 只承载最终的机器可读结果。
        print(
            f"[离线转写] 第 {index}/{len(chunks)} 段（{chunk.start_sec:.1f}s - {chunk.end_sec:.1f}s）",
            file=sys.stderr,
            flush=True,
        )
        start_sample = int(chunk.start_sec * ASR_SAMPLE_RATE)
        end_sample = int(chunk.end_sec * ASR_SAMPLE_RATE)
        result = engine.asr(
            audio=waveform[start_sample:end_sample],
            context=context,
            language=language,
            chunk_size_sec=max_chunk_sec,
            # 回退路径的单元远超引擎舒适窗口，必须启用引擎内部分块与跨块记忆；
            # 正常路径每段已在时长上限以内，无需引擎再切。
            memory_chunks=2 if vad_empty else 0,
        )
        text = result.text.strip()
        if not text:
            continue
        segments.append(TranscribedSegment(chunk.start_sec, chunk.end_sec, text))
        # 把上一段的结尾文本作为下一段的提示上下文（Whisper 式前文提示），
        # 缓解跨段切分造成的语义断层；引擎会把 context 放进 system 角色提示。
        context = (context + text)[-_CONTEXT_TAIL_CHARS:]
    return segments


def write_transcription_outputs(
    segments: list[TranscribedSegment],
    *,
    input_path: Path,
    output_dir: Path,
    formats: set[str],
    duration_sec: float,
    language: str,
) -> dict[str, str]:
    """把识别段列表导出为 SRT/JSON/TXT 文件。

    Args:
        segments: transcribe_waveform 输出的识别段列表。
        input_path: 原始输入文件，用于派生输出文件名。
        output_dir: 输出目录，不存在时会创建。
        formats: 需要导出的格式集合，只允许 srt/json/txt。
        duration_sec: 原始音频总时长（秒），写入 JSON 元数据。
        language: 识别语言，写入 JSON 元数据。

    Returns:
        格式名到输出文件绝对路径的映射。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    outputs: dict[str, str] = {}

    if "srt" in formats:
        srt_path = output_dir / f"{stem}.srt"
        blocks = [
            f"{index}\n"
            f"{_format_srt_timestamp(segment.start_sec)} --> {_format_srt_timestamp(segment.end_sec)}\n"
            f"{segment.text}\n"
            for index, segment in enumerate(segments, start=1)
        ]
        srt_path.write_text("\n".join(blocks), encoding="utf-8")
        outputs["srt"] = str(srt_path)

    if "json" in formats:
        json_path = output_dir / f"{stem}.json"
        payload = {
            "source_file": str(input_path),
            "duration_sec": round(duration_sec, 3),
            "language": language,
            "segments": [
                {
                    "start_sec": round(segment.start_sec, 3),
                    "end_sec": round(segment.end_sec, 3),
                    "text": segment.text,
                }
                for segment in segments
            ],
        }
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs["json"] = str(json_path)

    if "txt" in formats:
        txt_path = output_dir / f"{stem}.txt"
        txt_path.write_text(
            "\n".join(segment.text for segment in segments),
            encoding="utf-8",
        )
        outputs["txt"] = str(txt_path)

    return outputs


def _format_srt_timestamp(seconds: float) -> str:
    """把秒数格式化为 SRT 时间戳（HH:MM:SS,mmm）。"""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数，执行一次离线转写并把产出路径打印到 stdout。

    Returns:
        转写成功返回零；参数、输入文件或本地模型资产缺失时由异常终止。
    """
    parser = argparse.ArgumentParser(description="把音频/视频文件离线转写成带时间戳的文本")
    parser.add_argument("--input", required=True, help="输入音频或视频文件路径")
    parser.add_argument("--output-dir", required=True, help="转写结果输出目录")
    parser.add_argument("--language", default="Chinese", help="识别语言，默认 Chinese")
    parser.add_argument(
        "--formats",
        default=",".join(_SUPPORTED_FORMATS),
        help="逗号分隔的输出格式，可选 srt,json,txt，默认全部导出",
    )
    parser.add_argument(
        "--max-chunk-sec",
        type=float,
        default=30.0,
        help="单个转写单元的时长上限（秒），默认 30",
    )
    arguments = parser.parse_args(argv)

    formats = {item.strip().lower() for item in arguments.formats.split(",") if item.strip()}
    unsupported_formats = formats - set(_SUPPORTED_FORMATS)
    if unsupported_formats:
        parser.error(f"不支持的输出格式：{sorted(unsupported_formats)}，可选：{list(_SUPPORTED_FORMATS)}")
    if not formats:
        parser.error("至少需要指定一种输出格式")
    if arguments.max_chunk_sec <= 0:
        parser.error("--max-chunk-sec 必须大于零")

    input_path = Path(arguments.input).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{input_path}")
    output_dir = Path(arguments.output_dir).expanduser().resolve()

    # 资产装配复用实时链路的统一入口，保证只读取仓库内 backend/ckpt 的模型文件。
    runtime_config = build_realtime_asr_runtime_config()
    waveform = load_audio(str(input_path), sample_rate=ASR_SAMPLE_RATE)
    if waveform.size == 0:
        raise RuntimeError(f"未能从输入文件解码出任何音频：{input_path}")
    duration_sec = float(waveform.size) / ASR_SAMPLE_RATE

    engine = create_qwen_asr_engine(runtime_config.asr)
    vad_session = create_vad_session(runtime_config.vad)
    segments = transcribe_waveform(
        waveform,
        engine=engine,
        vad_session=vad_session,
        language=arguments.language,
        max_chunk_sec=arguments.max_chunk_sec,
    )
    outputs = write_transcription_outputs(
        segments,
        input_path=input_path,
        output_dir=output_dir,
        formats=formats,
        duration_sec=duration_sec,
        language=arguments.language,
    )

    # 机器可读的最终结果只放 stdout 最后一行，供 Skill 脚本与 Agent 解析。
    print(
        json.dumps(
            {
                "input": str(input_path),
                "duration_sec": round(duration_sec, 3),
                "language": arguments.language,
                "segment_count": len(segments),
                "outputs": outputs,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
