from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from companion.audio.bootstrap.realtime_asr_runtime import SileroVadRuntimeConfig, create_vad_session


@dataclass(frozen=True)
class SileroVadProviderConfig:
    """真实 Silero VAD provider 配置。"""

    # 真实 VAD 运行时配置，包含模型路径、采样率、帧长和语音阈值。
    runtime_config: SileroVadRuntimeConfig
    # 测试或替代实现可以注入 session factory，生产默认调用 bootstrap 的 create_vad_session。
    session_factory: Callable[[SileroVadRuntimeConfig], object] | None = None


class SileroVadProvider:
    """基于本仓库 Silero ONNX 模型的真实 VAD provider。"""

    def __init__(self, config: SileroVadProviderConfig) -> None:
        """初始化真实 Silero VAD provider。"""

        # 配置保存在实例上，避免 RealtimeInputRuntime 直接依赖 Silero 的帧长和阈值细节。
        self.config = config
        # VAD 是生产语音链路的必需组件，构造阶段即创建 session，保证依赖错误尽早暴露。
        factory = self.config.session_factory or create_vad_session
        self._session = factory(self.config.runtime_config)

    def is_speech_frame(self, pcm16: bytes) -> bool:
        """判断单个 Silero 固定长度 PCM16 帧是否包含用户语音。"""

        # 空帧没有语音活动，直接返回 False，方便输入重置后的边界调用保持安全。
        if not pcm16:
            return False

        frame_bytes = self.config.runtime_config.frame_samples * 2
        if len(pcm16) != frame_bytes:
            raise ValueError(f"Silero VAD 单帧入口只接受 {frame_bytes} 字节，当前收到 {len(pcm16)} 字节")

        # 单帧入口不切窗也不补零，实时 worker 必须在进入 provider 前完成固定窗口聚合。
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        speech_probability, _elapsed_ms = self._session.infer(samples.astype(np.float32, copy=False))
        return speech_probability >= self.config.runtime_config.speech_threshold

    def is_speech_chunk(self, pcm16: bytes) -> bool:
        """判断任意长度 PCM16 音频块是否包含用户语音。"""

        # 空音频没有语音活动，直接返回 False，避免无意义地推进 VAD 流式状态。
        if not pcm16:
            return False

        # PCM16 必须按 int16 对齐，避免真实 VAD 收到错误采样。
        if len(pcm16) % 2 != 0:
            raise ValueError("Silero VAD provider 只接受按 int16 对齐的 PCM16 字节")

        # 任意一帧达到语音阈值，就说明当前音频块包含用户语音；尾帧补零只属于 chunk 兼容入口。
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        for frame in self._iter_frames(samples):
            speech_probability, _elapsed_ms = self._session.infer(frame)
            if speech_probability >= self.config.runtime_config.speech_threshold:
                return True
        return False

    def reset(self) -> None:
        """重置底层 VAD 连续状态。"""

        # Silero VAD 持有跨帧状态，每次新 turn 或重新监听前都需要显式清理。
        self._session.reset()

    def _iter_frames(self, samples: np.ndarray):
        """把音频样本切成 Silero 固定长度帧。"""

        # Silero VAD 只能处理固定帧长，因此实时块需要按帧拆开推理。
        frame_samples = self.config.runtime_config.frame_samples
        for start in range(0, samples.shape[0], frame_samples):
            frame = samples[start:start + frame_samples]
            if frame.shape[0] == frame_samples:
                yield frame.astype(np.float32, copy=False)
                continue

            # 尾帧不足时补零保留最后一点用户语音，避免短句尾巴被直接丢弃。
            padded = np.zeros(frame_samples, dtype=np.float32)
            padded[:frame.shape[0]] = frame.astype(np.float32, copy=False)
            yield padded
