from __future__ import annotations

import asyncio
import time
from typing import Literal

from companion.audio.bootstrap.qwen_asr_config import QwenAsrConfig, get_decode_profile
from companion.audio.bootstrap.realtime_asr_runtime import ASR_SAMPLE_RATE
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


def prepare_asr_audio_window(audio, *, max_seconds: float, sample_rate: int = ASR_SAMPLE_RATE):
    """裁剪 ASR partial 使用的最近音频窗口。"""

    import numpy as np

    # ASR provider 只接收单声道 waveform，多声道处理必须在采集或缓冲层完成。
    audio_array = np.asarray(audio, dtype=np.float32)
    if audio_array.ndim != 1:
        raise ValueError(f"ASR 只支持一维单声道音频，收到维度 {audio_array.ndim}")

    # partial 只保留最近窗口，降低重复编码成本。
    max_samples = int(max_seconds * sample_rate)
    if audio_array.shape[0] > max_samples:
        return audio_array[-max_samples:].astype(np.float32, copy=False)
    return audio_array.astype(np.float32, copy=False)


class RealtimeAsrWorker:
    """从参考 ASR worker rehome 的串行执行器骨架。"""

    def __init__(self, config: QwenAsrConfig, engine):
        """保存配置、执行锁和本地 ASR 引擎。"""

        # ASR 大模型通常不能被多轮 partial/final 并发调用，锁用于把执行收敛成串行队列。
        self.config = config
        self.engine = engine
        self._run_lock = asyncio.Lock()

    def shutdown(self) -> None:
        """关闭底层 ASR 引擎。"""

        # 释放动作交给底层本地 engine，worker 不直接知道具体推理实现。
        self.engine.shutdown()

    async def run(self, audio, kind: Literal["partial", "final"]) -> dict[str, object]:
        """串行执行一次 ASR 并返回文本和性能指标。"""

        # 通过锁保证同一 worker 内只运行一个 ASR 调用，避免设备队列堆积导致延迟失控。
        async with self._run_lock:
            logger.info("开始执行实时 ASR kind=%s", kind)
            return await asyncio.to_thread(self._run_sync, audio, kind)

    def _run_sync(self, audio, kind: Literal["partial", "final"]) -> dict[str, object]:
        """同步执行底层 ASR 推理并整理性能指标。"""

        # partial 先裁剪最近窗口，复用参考 worker 的实时性能策略。
        started_at = time.perf_counter()
        profile = get_decode_profile(self.config, kind=kind)
        if profile.window_sec is not None:
            audio = prepare_asr_audio_window(
                audio,
                max_seconds=profile.window_sec,
                sample_rate=ASR_SAMPLE_RATE,
            )

        result = self.engine.asr(
            audio=audio,
            context=self.config.session.context,
            language=self.config.session.language,
            chunk_size_sec=profile.chunk_size_sec,
            memory_chunks=profile.memory_chunks,
            apply_hotwords=profile.apply_hotwords,
        )
        total_ms = (time.perf_counter() - started_at) * 1000.0
        perf = result.performance or {}
        return {
            "text": result.text,
            "performance": {
                "asr_total_ms": total_ms,
                "encode_time_ms": float(perf.get("encode_time", 0.0)) * 1000.0,
                "prefill_time_ms": float(perf.get("prefill_time", 0.0)) * 1000.0,
                "decode_time_ms": float(perf.get("decode_time", 0.0)) * 1000.0,
                "hotword_time_ms": float(perf.get("hotword_time", 0.0)) * 1000.0,
            },
        }
