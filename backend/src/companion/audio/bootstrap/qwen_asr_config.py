from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths


# Qwen ASR 文件名来自已复制到 backend/ckpt/qwen_asr 的本地资产，集中声明避免分散硬编码。
QWEN_ENCODER_FRONTEND_NAME = "qwen3_asr_encoder_frontend.fp16.onnx"
QWEN_ENCODER_BACKEND_NAME = "qwen3_asr_encoder_backend.fp16.onnx"
QWEN_LLM_NAME = "qwen3_asr_llm.f16.gguf"


@dataclass(frozen=True)
class QwenAsrAssetsConfig:
    """Qwen ASR 资产定位配置。"""

    # Qwen ASR 本地模型目录，必须位于当前仓库内。
    model_dir: Path
    # Encoder 前端 ONNX 文件名，来自参考项目 ASREngineConfig 默认值。
    encoder_frontend_fn: str = QWEN_ENCODER_FRONTEND_NAME
    # Encoder 后端 ONNX 文件名，来自参考项目 ASREngineConfig 默认值。
    encoder_backend_fn: str = QWEN_ENCODER_BACKEND_NAME
    # LLM GGUF 文件名，来自参考项目 ASREngineConfig 默认值。
    llm_fn: str = QWEN_LLM_NAME

    def required_files(self) -> list[Path]:
        """列出 Qwen ASR 启动需要的最小本地文件。"""

        return [
            self.model_dir / self.encoder_frontend_fn,
            self.model_dir / self.encoder_backend_fn,
            self.model_dir / self.llm_fn,
        ]


@dataclass(frozen=True)
class QwenAsrEngineConfig:
    """Qwen ASR 底层执行参数。"""

    # Qwen ASR encoder 的 ONNX provider 策略，auto 会在引擎初始化时解析成真实 provider。
    onnx_provider: str = "auto"
    # LLM 是否使用 GPU 由 ASR 引擎 resolver 最终决定；显式 backend 场景默认允许 GPU。
    llm_use_gpu: bool = True
    # Qwen ASR 的 GGUF LLM backend 策略。
    llm_backend: Literal["vulkan", "cuda", "cpu", "auto"] = "auto"
    # 是否打印 Qwen ASR 引擎内部日志，默认保持当前工程既有观测行为。
    verbose: bool = True
    # Encoder 单算子线程数，None 表示沿用底层默认。
    encoder_intra_threads: int | None = None
    # Encoder 算子间调度线程数，None 表示沿用底层默认。
    encoder_inter_threads: int | None = None


@dataclass(frozen=True)
class QwenAsrDecodeProfile:
    """单类识别请求的策略配置。"""

    # 当前识别类型的分块时长。
    chunk_size_sec: float
    # 当前识别类型保留的历史 chunk 数量。
    memory_chunks: int
    # 当前识别类型是否应用热词。
    apply_hotwords: bool
    # 可选最近窗口长度；仅 partial 使用，final 置为 None。
    window_sec: float | None = None


def _default_partial_profile() -> QwenAsrDecodeProfile:
    """构造默认 partial 解码策略。"""

    return QwenAsrDecodeProfile(
        chunk_size_sec=8.0,
        memory_chunks=1,
        apply_hotwords=False,
        window_sec=12.0,
    )


def _default_final_profile() -> QwenAsrDecodeProfile:
    """构造默认 final 解码策略。"""

    return QwenAsrDecodeProfile(
        chunk_size_sec=12.0,
        memory_chunks=1,
        apply_hotwords=True,
        window_sec=None,
    )


@dataclass(frozen=True)
class QwenAsrSessionConfig:
    """Qwen ASR 会话业务参数。"""

    # 识别语言沿用参考 webapp 默认值。
    language: str = "Chinese"
    # 参考 webapp 中用于约束模型保持原文转写的上下文。
    context: str = ""
    # final ASR 使用的热词列表；partial 是否应用由 decode profile 决定。
    hotwords: tuple[str, ...] = ()
    # partial 使用较短 chunk，优先降低首字延迟。
    partial: QwenAsrDecodeProfile = field(default_factory=_default_partial_profile)
    # final 使用较长 chunk，优先保证最终结果稳定。
    final: QwenAsrDecodeProfile = field(default_factory=_default_final_profile)


@dataclass(frozen=True)
class QwenAsrConfig:
    """Qwen ASR 业务层统一配置。"""

    # 模型资产边界只负责路径与文件名，不掺杂业务策略。
    assets: QwenAsrAssetsConfig
    # 引擎执行参数只表达运行设备与线程，不直接暴露推理包 schema。
    engine: QwenAsrEngineConfig = field(default_factory=QwenAsrEngineConfig)
    # 会话业务参数集中维护 partial/final 策略与语言上下文。
    session: QwenAsrSessionConfig = field(default_factory=QwenAsrSessionConfig)

    def required_files(self) -> list[Path]:
        """列出 Qwen ASR 启动需要的最小本地文件。"""

        return self.assets.required_files()


def build_qwen_asr_config_from_model_dir(model_dir: Path | str) -> QwenAsrConfig:
    """从模型目录构建仓库内 Qwen ASR 业务配置。"""

    # 该入口是 production factory 与 provider 共享的路径边界，避免其中一边绕过仓库内限制。
    asset_paths = resolve_local_audio_asset_paths()
    raw_model_dir = Path(model_dir).expanduser()
    resolved_model_dir = (
        raw_model_dir.resolve()
        if raw_model_dir.is_absolute()
        else (asset_paths.repo_root / raw_model_dir).resolve()
    )
    repo_local_model_dir = _resolve_repo_local_path(
        resolved_model_dir,
        repo_root=asset_paths.repo_root,
        error_label="实时 ASR 模型目录",
    )
    return QwenAsrConfig(
        assets=QwenAsrAssetsConfig(model_dir=repo_local_model_dir),
    )


def get_decode_profile(
    config: QwenAsrConfig,
    *,
    kind: Literal["partial", "final"],
) -> QwenAsrDecodeProfile:
    """按识别类型返回 decode profile。"""

    if kind == "partial":
        return config.session.partial
    if kind == "final":
        return config.session.final
    raise ValueError(f"ASR 识别类型必须是 partial 或 final，当前收到：{kind}")


def _resolve_repo_local_path(path: Path, *, repo_root: Path, error_label: str = "实时 ASR 资产路径") -> Path:
    """解析并限制路径只能位于当前仓库内。"""

    # ASR 资产体积较大且来自 ref_code 复制，运行时必须只读取当前仓库内的 backend/ckpt。
    resolved_path = path.expanduser().resolve()
    resolved_repo_root = repo_root.resolve()
    if not resolved_path.is_relative_to(resolved_repo_root):
        raise ValueError(f"{error_label}必须位于当前仓库内：{resolved_path}")
    return resolved_path
