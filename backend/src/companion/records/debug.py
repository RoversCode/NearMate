from __future__ import annotations

import json
import shutil
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


DEFAULT_DEBUG_AUDIO_SAMPLE_RATE = 16000
DEFAULT_DEBUG_AUDIO_CHANNELS = 1
DEFAULT_DEBUG_AUDIO_SAMPLE_WIDTH = 2


class InteractionDebugRecorderProtocol(Protocol):
    """交互调试记录器协议。"""

    def observe_pre_wake_frame(self, item: object) -> None:
        """观察一帧唤醒前音频。"""
        ...

    def record_wake_hit(self, item: object, queue_lag_ms: float) -> None:
        """记录唤醒命中的音频帧。"""
        ...

    def record_final_asr_input(self, turn_id: str | None, pcm16: bytes, metadata: dict[str, Any]) -> None:
        """记录最终提交给 ASR 的完整音频。"""
        ...

    def record_final_asr_result(
        self,
        turn_id: str | None,
        *,
        text: str,
        request_id: str | None,
        is_empty: bool,
        metadata: dict[str, Any],
    ) -> None:
        """记录 final ASR 的文本结果。"""
        ...

    def record_assistant_request_enqueued(self, request: object) -> None:
        """记录助手请求已经进入输出侧队列。"""
        ...

    def record_generation_started(self, context: object) -> None:
        """记录助手 generation 启动。"""
        ...

    def record_generation_cancelled(self, context: object, *, reason: str) -> None:
        """记录助手 generation 被取消。"""
        ...

    def record_llm_text_chunk(self, context: object, *, text: str) -> None:
        """记录 LLM 可见文本增量。"""
        ...

    def record_llm_text_final(self, context: object, *, text: str) -> None:
        """记录 LLM 完整最终文本。"""
        ...

    def record_tts_segment(self, context: object, *, text: str) -> None:
        """记录送入 TTS 的句段文本。"""
        ...

    def record_tts_audio_chunk(
        self,
        context: object,
        *,
        pcm16: bytes,
        sample_rate: int,
        is_final: bool,
    ) -> None:
        """记录 TTS provider 生成出的音频 chunk。"""
        ...

    def record_generation_finished(self, context: object) -> None:
        """记录助手 generation 自然完成。"""
        ...

    def reset(self, reason: str) -> None:
        """重置录音器内部缓存。"""
        ...


@dataclass(frozen=True)
class _PreWakeFrame:
    """唤醒前环形缓存帧。"""

    # 唤醒前原始 PCM16 字节，后续会按顺序拼接成 WAV。
    pcm16: bytes
    # 采样率来自 AudioInputItem；缺失时使用默认 16k。
    sample_rate: int
    # 通道数来自 AudioInputItem；缺失时使用默认单声道。
    num_channels: int
    # 帧时长仅作为观测信息保留；唤醒前音频当前不再按时长裁剪。
    duration_ms: float


class NoopInteractionDebugRecorder:
    """关闭态交互调试记录器。"""

    def observe_pre_wake_frame(self, item: object) -> None:
        """关闭态忽略唤醒前音频。"""

    def record_wake_hit(self, item: object, queue_lag_ms: float) -> None:
        """关闭态忽略唤醒命中帧。"""

    def record_final_asr_input(self, turn_id: str | None, pcm16: bytes, metadata: dict[str, Any]) -> None:
        """关闭态忽略 final ASR 输入音频。"""

    def record_final_asr_result(
        self,
        turn_id: str | None,
        *,
        text: str,
        request_id: str | None,
        is_empty: bool,
        metadata: dict[str, Any],
    ) -> None:
        """关闭态忽略 final ASR 文本结果。"""

    def record_assistant_request_enqueued(self, request: object) -> None:
        """关闭态忽略助手请求入队事实。"""

    def record_generation_started(self, context: object) -> None:
        """关闭态忽略 generation 启动事实。"""

    def record_generation_cancelled(self, context: object, *, reason: str) -> None:
        """关闭态忽略 generation 取消事实。"""

    def record_llm_text_chunk(self, context: object, *, text: str) -> None:
        """关闭态忽略 LLM 增量文本。"""

    def record_llm_text_final(self, context: object, *, text: str) -> None:
        """关闭态忽略 LLM 完整文本。"""

    def record_tts_segment(self, context: object, *, text: str) -> None:
        """关闭态忽略 TTS 句段。"""

    def record_tts_audio_chunk(
        self,
        context: object,
        *,
        pcm16: bytes,
        sample_rate: int,
        is_final: bool,
    ) -> None:
        """关闭态忽略 TTS 音频 chunk。"""

    def record_generation_finished(self, context: object) -> None:
        """关闭态忽略 generation 完成事实。"""

    def reset(self, reason: str) -> None:
        """关闭态无需清理任何缓存。"""


class InteractionDebugRecorder:
    """本地完整交互调试记录器。"""

    def __init__(
        self,
        *,
        enabled: bool,
        output_dir: str | Path,
        session_id_provider: Callable[[], str],
        max_session_files: int,
    ) -> None:
        """初始化完整交互调试记录器。"""

        # 调试录音必须显式启用；关闭实例仍保留接口，便于测试和组合根统一注入。
        self.enabled = enabled
        # output_dir 保存为 Path，所有产物都限制在该根目录之下。
        self.output_dir = Path(output_dir)
        # session_id_provider 延迟读取运行时 session id，避免构造时绑定旧会话。
        self.session_id_provider = session_id_provider

        # 文件上限必须为正，保证每次写入后都有确定的保留策略。
        if max_session_files <= 0:
            raise ValueError("max_session_files 必须是正整数")

        # max_session_files 用于控制单个 session 目录内的文件总量。
        self.max_session_files = max_session_files
        # _pre_wake_frames 保存本次监听期内所有 dormant 阶段音频帧，命中后一次性写出。
        self._pre_wake_frames: list[_PreWakeFrame] = []
        # _tts_generation_buffers 保存每个 generation 已生成的 TTS PCM，便于随时刷新 generated.wav。
        self._tts_generation_buffers: dict[str, _TtsGenerationBuffer] = {}

    def observe_pre_wake_frame(self, item: object) -> None:
        """观察一帧唤醒前音频。"""

        # 关闭态不保留环境音，保护隐私并减少热路径内存占用。
        if not self.enabled:
            return

        frame = _PreWakeFrame(
            pcm16=bytes(getattr(item, "pcm16")),
            sample_rate=int(getattr(item, "sample_rate", DEFAULT_DEBUG_AUDIO_SAMPLE_RATE)),
            num_channels=int(getattr(item, "num_channels", DEFAULT_DEBUG_AUDIO_CHANNELS)),
            duration_ms=float(getattr(item, "duration_ms", 0.0)),
        )
        self._pre_wake_frames.append(frame)

    def record_wake_hit(self, item: object, queue_lag_ms: float) -> None:
        """记录唤醒命中帧，并把唤醒前缓存落盘。"""

        # 关闭态不能创建目录或写文件，保持调试能力默认无副作用。
        if not self.enabled:
            return

        wake_dir = self._stage_dir("wake")
        if self._pre_wake_frames:
            pre_wake_pcm16 = b"".join(frame.pcm16 for frame in self._pre_wake_frames)
            first_frame = self._pre_wake_frames[0]
            self._write_stage(
                stage_dir=wake_dir,
                stage="pre_wake",
                filename="pre_wake.wav",
                pcm16=pre_wake_pcm16,
                sample_rate=first_frame.sample_rate,
                num_channels=first_frame.num_channels,
                metadata={"frame_count": len(self._pre_wake_frames)},
            )

        # 唤醒命中帧是排查误唤醒和截断问题的关键证据，必须与唤醒前缓存一起落盘。
        # self._write_stage(
        #     stage_dir=wake_dir,
        #     stage="wake_hit",
        #     filename="wake_hit.wav",
        #     pcm16=bytes(getattr(item, "pcm16")),
        #     sample_rate=int(getattr(item, "sample_rate", DEFAULT_DEBUG_AUDIO_SAMPLE_RATE)),
        #     num_channels=int(getattr(item, "num_channels", DEFAULT_DEBUG_AUDIO_CHANNELS)),
        #     metadata={
        #         "duration_ms": float(getattr(item, "duration_ms", 0.0)),
        #         "queue_lag_ms": queue_lag_ms,
        #     },
        # )
        self._pre_wake_frames.clear()

    def record_final_asr_input(self, turn_id: str | None, pcm16: bytes, metadata: dict[str, Any]) -> None:
        """记录最终提交给 ASR 的完整音频。"""

        # 关闭态直接返回，保持默认无副作用。
        if not self.enabled:
            return

        self._write_stage(
            stage_dir=self._input_dir(turn_id),
            stage="asr_input",
            filename="final_asr_input.wav",
            pcm16=bytes(pcm16),
            sample_rate=DEFAULT_DEBUG_AUDIO_SAMPLE_RATE,
            num_channels=DEFAULT_DEBUG_AUDIO_CHANNELS,
            metadata=metadata,
        )

    def record_final_asr_result(
        self,
        turn_id: str | None,
        *,
        text: str,
        request_id: str | None,
        is_empty: bool,
        metadata: dict[str, Any],
    ) -> None:
        """记录 final ASR 文本结果。"""

        if not self.enabled:
            return

        input_dir = self._input_dir(turn_id)
        payload = {
            "event": "final_asr_result",
            "is_empty": is_empty,
            "metadata": metadata,
            "recorded_at": self._utc_now(),
            "request_id": request_id,
            "text": text,
            "turn_id": turn_id,
        }
        self._write_json(input_dir / "final_asr.json", payload)
        self._append_manifest(input_dir / "events.jsonl", payload)
        self._trim_session_files(input_dir.parents[2])

    def record_assistant_request_enqueued(self, request: object) -> None:
        """记录助手请求已经进入输出侧队列。"""

        if not self.enabled:
            return

        assistant_dir = self._assistant_dir_for_request(request)
        payload = {
            "event": "assistant_request_enqueued",
            "interaction_session_id": getattr(request, "interaction_session_id", None),
            "recorded_at": self._utc_now(),
            "request_id": getattr(request, "request_id"),
            "session_id": getattr(request, "source_session_id"),
            "source": getattr(request, "source", None),
            "source_turn_id": getattr(request, "source_turn_id", None),
            "text_length": len(str(getattr(request, "user_text"))),
            "user_text": getattr(request, "user_text"),
        }
        self._write_json(assistant_dir / "request.json", payload)
        self._append_manifest(assistant_dir / "events.jsonl", payload)
        self._trim_session_files(assistant_dir.parents[2])

    def record_generation_started(self, context: object) -> None:
        """记录助手 generation 启动。"""

        if not self.enabled:
            return

        request = getattr(context, "request")
        assistant_dir = self._assistant_dir_for_request(request)
        generation_id = str(getattr(context, "generation_id"))
        self._reset_assistant_debug_task_results(assistant_dir)
        self._tts_generation_buffers.pop(generation_id, None)
        self._write_json(
            assistant_dir / "request.json",
            {
                "event": "generation_started",
                "generation_id": getattr(context, "generation_id"),
                "interaction_session_id": getattr(request, "interaction_session_id", None),
                "recorded_at": self._utc_now(),
                "request_id": getattr(request, "request_id"),
                "session_id": getattr(request, "source_session_id"),
                "source": getattr(request, "source", None),
                "source_turn_id": getattr(request, "source_turn_id", None),
                "text_length": len(str(getattr(request, "user_text"))),
                "user_text": getattr(request, "user_text"),
            },
        )
        self._append_assistant_event(context, "generation_started")

    def record_generation_cancelled(self, context: object, *, reason: str) -> None:
        """记录助手 generation 被取消。"""

        if not self.enabled:
            return

        self._write_generated_tts_if_present(context)
        self._append_assistant_event(context, "generation_cancelled", {"reason": reason})
        self._drop_tts_generation_buffer(context)

    def record_llm_text_chunk(self, context: object, *, text: str) -> None:
        """记录 LLM 可见文本增量。"""

        if not self.enabled:
            return

        assistant_dir = self._assistant_dir_for_context(context)
        self._append_manifest(
            assistant_dir / "llm_chunks.jsonl",
            {
                "event": "llm_text_chunk",
                "generation_id": getattr(context, "generation_id"),
                "recorded_at": self._utc_now(),
                "request_id": getattr(getattr(context, "request"), "request_id"),
                "text": text,
            },
        )

    def record_llm_text_final(self, context: object, *, text: str) -> None:
        """记录 LLM 完整最终文本。"""

        if not self.enabled:
            return

        assistant_dir = self._assistant_dir_for_context(context)
        self._write_text(assistant_dir / "llm_final.txt", text)
        self._append_assistant_event(context, "llm_text_final", {"text_length": len(text)})

    def record_tts_segment(self, context: object, *, text: str) -> None:
        """记录送入 TTS 的句段文本。"""

        if not self.enabled:
            return

        self._append_assistant_event(context, "tts_segment", {"text": text, "text_length": len(text)})

    def record_tts_audio_chunk(
        self,
        context: object,
        *,
        pcm16: bytes,
        sample_rate: int,
        is_final: bool,
    ) -> None:
        """记录 TTS provider 生成的音频 chunk。"""

        if not self.enabled:
            return

        assistant_dir = self._assistant_dir_for_context(context)
        tts_dir = assistant_dir / "tts"
        generation_id = str(getattr(context, "generation_id"))
        buffer = self._tts_generation_buffers.setdefault(
            generation_id,
            _TtsGenerationBuffer(sample_rate=sample_rate),
        )
        buffer.chunk_count += 1
        buffer.sample_rate = sample_rate
        chunk_pcm16 = bytes(pcm16)
        chunk_filename: str | None = None

        # StreamVox 的空 final marker 只表达句段结束事实，不应该伪装成可试听的空 WAV 文件。
        if chunk_pcm16:
            buffer.pcm16_parts.append(chunk_pcm16)
            buffer.audio_chunk_count += 1
            chunk_filename = f"{buffer.audio_chunk_count:06d}.wav"
            self._write_wav(
                tts_dir / "chunks" / chunk_filename,
                pcm16=chunk_pcm16,
                sample_rate=sample_rate,
                num_channels=DEFAULT_DEBUG_AUDIO_CHANNELS,
            )
        self._write_generated_tts_if_present(context)
        payload = {
            "byte_count": len(chunk_pcm16),
            "chunk_index": buffer.chunk_count,
            "is_final": is_final,
            "sample_rate": sample_rate,
        }
        if chunk_filename is not None:
            payload["relative_path"] = f"tts/chunks/{chunk_filename}"
        self._append_assistant_event(context, "tts_audio_chunk", payload)
        self._trim_session_files(assistant_dir.parents[2])

    def record_generation_finished(self, context: object) -> None:
        """记录助手 generation 自然完成。"""

        if not self.enabled:
            return

        self._write_generated_tts_if_present(context)
        self._append_assistant_event(context, "generation_finished")
        self._drop_tts_generation_buffer(context)

    def reset(self, reason: str) -> None:
        """重置录音器缓存。"""

        # 当前第一版只需要清理唤醒前累计音频，reason 预留给后续记录 reset marker。
        _ = reason
        self._pre_wake_frames.clear()

    def _input_dir(self, turn_id: str | None) -> Path:
        """返回某个用户 turn 的输入侧调试目录。"""

        return self._interaction_dir(self.session_id_provider(), turn_id or "unknown-turn") / "input"

    def _assistant_dir_for_context(self, context: object) -> Path:
        """返回某个 generation 的助手侧调试目录。"""

        return self._assistant_dir_for_request(getattr(context, "request"))

    def _assistant_dir_for_request(self, request: object) -> Path:
        """返回某个助手请求的助手侧调试目录。"""

        source_session_id = (
            str(getattr(request, "source_session_id", "")).strip()
            or self.session_id_provider()
        )
        request_id = str(getattr(request, "request_id"))
        source_turn_id = getattr(request, "source_turn_id", None)
        interaction_id = str(source_turn_id).strip() if source_turn_id else f"text-{request_id}"
        return self._interaction_dir(source_session_id, interaction_id) / "assistant"

    def _interaction_dir(self, source_session_id: str, interaction_id: str) -> Path:
        """返回标准 interaction 调试目录。"""

        safe_session_id = _safe_path_part(source_session_id.strip() or "unknown-session")
        safe_interaction_id = _safe_path_part(interaction_id.strip() or "unknown-interaction")
        return self.output_dir / safe_session_id / "interactions" / safe_interaction_id

    def _stage_dir(self, stage_id: str) -> Path:
        """生成某个调试阶段的目录。"""

        # 来源会话可能来自测试或尚未绑定真实会话的运行时，兜底值能保持目录结构稳定。
        source_session_id = self.session_id_provider().strip() or "unknown-session"
        return self.output_dir / _safe_path_part(source_session_id) / _safe_path_part(stage_id)

    def _write_stage(
        self,
        *,
        stage_dir: Path,
        stage: str,
        filename: str,
        pcm16: bytes,
        sample_rate: int,
        num_channels: int,
        metadata: dict[str, Any],
    ) -> None:
        """写入单个调试阶段的 WAV 和 manifest。"""

        # 每个阶段目录独立创建，方便用户直接按 turn 打开文件夹试听。
        stage_dir.mkdir(parents=True, exist_ok=True)
        wav_path = stage_dir / filename
        self._write_wav(wav_path, pcm16=pcm16, sample_rate=sample_rate, num_channels=num_channels)
        self._append_manifest(
            stage_dir / "manifest.jsonl",
            {
                "byte_count": len(pcm16),
                "metadata": metadata,
                "recorded_at": self._utc_now(),
                "relative_path": filename,
                "sample_rate": sample_rate,
                "stage": stage,
            },
        )
        self._trim_session_files(self._session_dir_for_stage(stage_dir))

    def _session_dir_for_stage(self, stage_dir: Path) -> Path:
        """根据阶段目录返回所属 session 目录。"""

        # 新 interaction 结构为 session/interactions/turn/input，旧 wake 结构为 session/wake。
        if stage_dir.parent.parent.name == "interactions":
            return stage_dir.parents[2]
        return stage_dir.parent

    def _write_wav(self, path: Path, *, pcm16: bytes, sample_rate: int, num_channels: int) -> None:
        """把 PCM16 字节写成 WAV 文件。"""

        # wave 标准库足够写入 PCM16，不引入额外音频依赖可以降低后端安装复杂度。
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(num_channels)
            wav_file.setsampwidth(DEFAULT_DEBUG_AUDIO_SAMPLE_WIDTH)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm16)

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        """写入单个 JSON 文件。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_text(self, path: Path, text: str) -> None:
        """写入 UTF-8 文本文件。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _append_assistant_event(
        self,
        context: object,
        event_name: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """追加助手侧 generation 事件。"""

        request = getattr(context, "request")
        event_payload = {
            "event": event_name,
            "generation_id": getattr(context, "generation_id"),
            "recorded_at": self._utc_now(),
            "request_id": getattr(request, "request_id"),
            "source_turn_id": getattr(request, "source_turn_id", None),
        }
        if payload:
            event_payload.update(payload)
        assistant_dir = self._assistant_dir_for_request(request)
        self._append_manifest(assistant_dir / "events.jsonl", event_payload)
        self._trim_session_files(assistant_dir.parents[2])

    def _write_generated_tts_if_present(self, context: object) -> None:
        """把当前 generation 已生成的所有 TTS PCM 刷新为 generated.wav。"""

        generation_id = str(getattr(context, "generation_id"))
        buffer = self._tts_generation_buffers.get(generation_id)
        if buffer is None or not buffer.pcm16_parts:
            return

        assistant_dir = self._assistant_dir_for_context(context)
        self._write_wav(
            assistant_dir / "tts" / "generated.wav",
            pcm16=b"".join(buffer.pcm16_parts),
            sample_rate=buffer.sample_rate,
            num_channels=DEFAULT_DEBUG_AUDIO_CHANNELS,
        )

    def _reset_assistant_debug_task_results(self, assistant_dir: Path) -> None:
        """清理同一 turn 旧的助手侧调试产物。"""

        # assistant 目录按 source_turn_id 复用；新一轮 generation 开始前必须整体清空旧样本，避免多次运行混写。
        if assistant_dir.exists():
            shutil.rmtree(assistant_dir)

    def _drop_tts_generation_buffer(self, context: object) -> None:
        """释放一个 generation 的 TTS 缓冲。"""

        generation_id = str(getattr(context, "generation_id"))
        self._tts_generation_buffers.pop(generation_id, None)

    def _append_manifest(self, path: Path, payload: dict[str, Any]) -> None:
        """追加 manifest JSONL 行。"""

        # JSONL 便于按阶段逐行追加，也方便后续控制台或脚本增量读取。
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as manifest_file:
            manifest_file.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _trim_session_files(self, session_dir: Path) -> None:
        """裁剪单个 session 目录内的调试文件数量。"""

        files = list(_iter_regular_files(session_dir))
        if len(files) <= self.max_session_files:
            return

        interactions_dir = session_dir / "interactions"
        interaction_dirs = sorted(
            (path for path in interactions_dir.iterdir() if path.is_dir()) if interactions_dir.exists() else [],
            key=lambda path: path.stat().st_mtime,
        )
        # 按完整 interaction 目录裁剪，避免留下缺音频或缺 manifest 的半截排查样本。
        while len(files) > self.max_session_files and len(interaction_dirs) > 1:
            oldest_dir = interaction_dirs.pop(0)
            shutil.rmtree(oldest_dir)
            files = list(_iter_regular_files(session_dir))

    def _utc_now(self) -> str:
        """返回当前 UTC ISO 时间字符串。"""

        return datetime.now(UTC).isoformat()


@dataclass
class _TtsGenerationBuffer:
    """保存一个 generation 已生成的 TTS PCM。"""

    # sample_rate 用于写出 generated.wav；StreamVox 单 generation 理论上保持稳定。
    sample_rate: int
    # pcm16_parts 按生成顺序保存每个 chunk 的 PCM。
    pcm16_parts: list[bytes] = field(default_factory=list)
    # chunk_count 是当前 generation 已观察到的 TTS chunk 事件序号，包含空 final marker。
    chunk_count: int = 0
    # audio_chunk_count 只统计真实落盘的音频 chunk，保证磁盘文件编号连续可试听。
    audio_chunk_count: int = 0


def _iter_regular_files(root: Path) -> Iterable[Path]:
    """遍历目录下的普通文件。"""

    # 裁剪发生在写入后，正常目录应存在；这里保留兜底让单元测试和异常恢复路径更稳。
    if not root.exists():
        return []
    return (path for path in root.rglob("*") if path.is_file())


def _safe_path_part(value: str) -> str:
    """把运行时标识转换为安全目录名。"""

    stripped = value.strip() or "unknown"
    return "".join(char if char not in '<>:"/\\|?*' else "_" for char in stripped)


# 兼容现有组合根命名；实际实现已经升级为完整交互记录器。
DebugAudioRecorderProtocol = InteractionDebugRecorderProtocol
NoopDebugAudioRecorder = NoopInteractionDebugRecorder
DebugAudioRecorder = InteractionDebugRecorder
