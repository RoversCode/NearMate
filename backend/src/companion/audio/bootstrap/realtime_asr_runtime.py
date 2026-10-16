from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths
from companion.audio.bootstrap.qwen_asr_config import QwenAsrConfig, build_qwen_asr_config_from_model_dir


# 实时 ASR 参考链路统一使用 16kHz 音频和 512 samples 的 Silero VAD 单帧输入。
ASR_SAMPLE_RATE = 16000
SILERO_FRAME_SAMPLES = 512
SILERO_VAD_NAME = "silero_vad.onnx"


@dataclass(frozen=True)
class SileroVadRuntimeConfig:
    """本地 Silero VAD 运行时配置。"""

    # Silero VAD ONNX 模型文件，随 Qwen ASR 资产放在 backend/ckpt/qwen_asr。
    model_path: Path
    # 当前 ASR/VAD 链路统一使用 16kHz。
    sample_rate: int = ASR_SAMPLE_RATE
    # Silero VAD 在 16kHz 下每帧使用 512 samples。
    frame_samples: int = SILERO_FRAME_SAMPLES
    # 语音概率阈值沿用参考实时会话默认值。
    speech_threshold: float = 0.5
    # ONNX Runtime provider 名称，默认 CPU，生产可由 realtime_audio.yaml 覆盖。
    provider: str = "CPUExecutionProvider"


@dataclass(frozen=True)
class RealtimeAsrRuntimeConfig:
    """实时 ASR 引导配置总入口。"""

    # 每个实时会话可以独立创建 VAD，因为 VAD 有连续音频状态。
    vad: SileroVadRuntimeConfig
    # ASR worker 后续可以进程级共享，避免重复加载大模型。
    asr: QwenAsrConfig


class SileroVadSession:
    """从参考 VAD 代码 rehome 的逐帧 ONNX VAD 会话。"""

    def __init__(self, config: SileroVadRuntimeConfig):
        """创建 VAD ONNX session 并初始化连续音频状态。"""

        if not config.model_path.exists():
            raise FileNotFoundError(f"Silero VAD 模型文件缺失：{config.model_path}")

        # 现在必须在本地模型存在后再导入重型运行时依赖，确保缺资产时优先得到可诊断错误。
        import numpy as np
        from streamvox_runtime import ensure_single_onnxruntime_distribution, initialize_nvidia_runtime
        ensure_single_onnxruntime_distribution()
        if config.provider == "CUDAExecutionProvider":
            initialize_nvidia_runtime(consumer="onnx_cuda", required=True)
        import onnxruntime as ort

        # 保存 numpy 模块引用，避免每帧 infer 重复导入，同时不影响模块级轻量导入。
        self._np = np
        self.config = config
        self._session = ort.InferenceSession(
            str(config.model_path),
            providers=[config.provider],
        )
        self.reset()

    def reset(self) -> None:
        """重置 VAD 连续状态。"""

        # Silero VAD 的 state/context 会跨帧累积，重置能避免上一段音频影响下一段判断。
        self._state = self._np.zeros((2, 1, 128), dtype=self._np.float32)
        self._context = self._np.zeros((1, 64), dtype=self._np.float32)

    def infer(self, frame) -> tuple[float, float]:
        """对单帧 PCM 执行 VAD 推理。"""

        # VAD 模型对单帧长度有严格要求，提前校验可以给上游音频切帧提供明确反馈。
        if frame.shape[0] != self.config.frame_samples:
            raise ValueError(
                f"Silero VAD 仅支持 {self.config.frame_samples} samples 单帧输入，"
                f"当前收到 {frame.shape[0]}"
            )

        # 参考实现把上一帧 context 与当前帧拼接，再把新 state/context 保存回会话。
        pcm_frame = frame.astype(self._np.float32, copy=False).reshape(1, -1)
        started_at = time.perf_counter()
        model_input = self._np.concatenate([self._context, pcm_frame], axis=1)
        outputs = self._session.run(
            None,
            {
                "input": model_input,
                "state": self._state,
                "sr": self._np.array(self.config.sample_rate, dtype=self._np.int64),
            },
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        out, state = outputs
        self._state = state.astype(self._np.float32, copy=False)
        self._context = model_input[:, -64:].astype(self._np.float32, copy=False)
        return float(out.squeeze()), elapsed_ms


def build_realtime_asr_runtime_config() -> RealtimeAsrRuntimeConfig:
    """构建仓库内实时 ASR 引导配置。"""

    # Qwen ASR 相关资产统一放在 backend/ckpt/qwen_asr，运行时不得回指 ref_code 目录。
    asset_paths = resolve_local_audio_asset_paths()
    model_dir = _resolve_repo_local_path(asset_paths.ckpt_dir / "qwen_asr", repo_root=asset_paths.repo_root)
    return RealtimeAsrRuntimeConfig(
        vad=SileroVadRuntimeConfig(model_path=model_dir / SILERO_VAD_NAME),
        asr=build_qwen_asr_config_from_model_dir(model_dir),
    )


def validate_realtime_asr_runtime_files(config: RealtimeAsrRuntimeConfig) -> list[str]:
    """校验实时 ASR 运行时需要的本地文件是否存在。"""

    # VAD 与 ASR 文件一起校验，方便启动前一次性展示所有缺失资产。
    required_files = [config.vad.model_path, *config.asr.required_files()]
    return [str(path) for path in required_files if not path.exists()]


def create_vad_session(config: SileroVadRuntimeConfig) -> SileroVadSession:
    """延迟创建本地 Silero VAD 会话。"""

    # 保留一个短入口，方便后续 provider 不直接依赖具体类名，也避免在 import 时加载 ONNX。
    return SileroVadSession(config)


def _resolve_repo_local_path(path: Path, *, repo_root: Path, error_label: str = "实时 ASR 资产路径") -> Path:
    """解析并限制路径只能位于当前仓库内。"""

    # ASR 资产体积较大且来自 ref_code 复制，运行时必须只读取当前仓库内的 backend/ckpt。
    resolved_path = path.expanduser().resolve()
    resolved_repo_root = repo_root.resolve()
    if not resolved_path.is_relative_to(resolved_repo_root):
        raise ValueError(f"{error_label}必须位于当前仓库内：{resolved_path}")
    return resolved_path
