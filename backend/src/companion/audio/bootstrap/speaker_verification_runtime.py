from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from companion.audio.pcm16_utils import normalize_float32_peak
from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths
from companion.config.realtime_audio import RealtimeAudioYamlConfig

# 通过版本化 StreamVox wheel 共享原生库加载器，声纹与唤醒词使用同一套 CUDA。
from streamvox_runtime import ensure_single_onnxruntime_distribution, initialize_nvidia_runtime
from companion.observability.logging_config import get_backend_logger


if TYPE_CHECKING:
    from companion.core.settings import Settings
    
    
logger = get_backend_logger(__name__)


# cam++ 说话人嵌入模型按 16kHz 单声道训练，与实时输入链路采样率一致。
SPEAKER_VERIFICATION_SAMPLE_RATE = 16000

# 模型文件名集中声明，避免 provider 层重复硬编码。
SPEAKER_VERIFICATION_MODEL_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"


@dataclass(frozen=True)
class SpeakerVerificationRuntimeConfig:
    """本地说话人确认运行时配置。"""

    # cam++ 嵌入模型文件，位于仓库 backend/ckpt 之下。
    model_path: Path
    # 白名单音频路径元组，启动期一次性计算嵌入。
    whitelist_audio_paths: tuple[Path, ...]
    # sherpa-onnx 执行 provider，支持 cpu、cuda。
    provider: str = "cpu"
    # 神经网络计算线程数，CPU 推理可调优。
    num_threads: int = 1
    # 嵌入模型采样率，固定 16kHz。
    sample_rate: int = SPEAKER_VERIFICATION_SAMPLE_RATE


def build_speaker_verification_runtime_config(
    settings: Settings | None = None,
    realtime_audio_config: RealtimeAudioYamlConfig | None = None,
    *,
    provider: str | None = None,
    num_threads: int | None = None,
) -> SpeakerVerificationRuntimeConfig:
    """构建仓库内说话人确认运行时配置。"""

    asset_paths = resolve_local_audio_asset_paths()
    speaker_verification_config = (
        realtime_audio_config.speaker_verification
        if realtime_audio_config is not None
        else None
    )
    if settings is not None:
        raw_model_path = Path(settings.speaker_verification_model_path)
        raw_whitelist = tuple(
            Path(p) for p in settings.speaker_whitelist_audio_paths
        )
        resolved_provider = provider or (
            speaker_verification_config.provider if speaker_verification_config is not None else "cpu"
        )
        resolved_threads = (
            num_threads
            if num_threads is not None
            else (
                speaker_verification_config.num_threads
                if speaker_verification_config is not None
                else 1
            )
        )
    else:
        raw_model_path = asset_paths.ckpt_dir / SPEAKER_VERIFICATION_MODEL_NAME
        raw_whitelist = ()
        resolved_provider = provider or (
            speaker_verification_config.provider if speaker_verification_config is not None else "cpu"
        )
        resolved_threads = (
            num_threads
            if num_threads is not None
            else (
                speaker_verification_config.num_threads
                if speaker_verification_config is not None
                else 1
            )
        )

    model_path = _resolve_repo_local_path(raw_model_path, repo_root=asset_paths.repo_root)
    whitelist_paths = tuple(
        _resolve_repo_local_path(p, repo_root=asset_paths.repo_root)
        for p in raw_whitelist
    )
    return SpeakerVerificationRuntimeConfig(
        model_path=model_path,
        whitelist_audio_paths=whitelist_paths,
        provider=resolved_provider,
        num_threads=resolved_threads,
    )


def create_speaker_embedding_extractor(config: SpeakerVerificationRuntimeConfig):
    """延迟创建 sherpa-onnx 说话人嵌入 extractor。"""

    # 缺模型时先报清晰错误，避免被 sherpa 底层加载失败掩盖。
    if not config.model_path.exists():
        raise FileNotFoundError(f"说话人确认模型文件缺失：{config.model_path}")

    # Windows CUDA 依赖需在导入 sherpa-onnx 前准备 DLL 搜索路径。
    ensure_single_onnxruntime_distribution()
    if config.provider.lower() == "cuda":
        initialize_nvidia_runtime(consumer="onnx_cuda", required=True)

    # 资产存在后再导入 sherpa-onnx，避免缺模型时被 ModuleNotFoundError 掩盖。
    import sherpa_onnx
    logger.info(f"当前说话人加载的模型是：{str(config.model_path)}, device是：{config.provider}")
    extractor_config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(config.model_path),
        num_threads=config.num_threads,
        debug=False,
        provider=config.provider,
    )
    if not extractor_config.validate():
        raise ValueError(f"无效的说话人确认 extractor 配置：{extractor_config}")
    return sherpa_onnx.SpeakerEmbeddingExtractor(extractor_config)


def load_whitelist_embeddings(
    extractor, config: SpeakerVerificationRuntimeConfig
) -> dict[str, np.ndarray]:
    """读取白名单音频并计算嵌入，返回 name -> embedding 字典。"""

    # soundfile/soxr 是音频读取依赖，函数内导入保持路径校验 smoke test 轻量。
    import soundfile
    import soxr

    whitelist: dict[str, np.ndarray] = {}
    for audio_path in config.whitelist_audio_paths:
        # 白名单音频可能是 48kHz，必须重采样到 16kHz 再算嵌入。
        samples, sample_rate = soundfile.read(
            str(audio_path), dtype="float32", always_2d=False
        )
        if samples.ndim > 1:
            # 多声道取均值混成单声道，与 cam++ 训练约定一致。
            samples = samples.mean(axis=1)
        if sample_rate != config.sample_rate:
            samples = soxr.resample(
                samples, sample_rate, config.sample_rate, quality="HQ"
            ).astype(np.float32)
        samples = normalize_float32_peak(samples)

        embedding = _compute_embedding(extractor, samples, config.sample_rate)
        # 用文件名 stem 作为说话人标识；同名文件会覆盖，调用方应保证唯一。
        whitelist[audio_path.stem] = embedding
    return whitelist


def _compute_embedding(
    extractor, samples: np.ndarray, sample_rate: int
) -> np.ndarray:
    """用 extractor 计算一段音频的说话人嵌入。"""

    stream = extractor.create_stream()
    stream.accept_waveform(sample_rate=sample_rate, waveform=samples)
    stream.input_finished()
    embedding = extractor.compute(stream)
    return np.array(embedding, dtype=np.float32)


def _resolve_repo_local_path(path: Path, *, repo_root: Path) -> Path:
    """解析并限制路径只能位于当前仓库内。"""

    resolved_path = path.expanduser().resolve()
    resolved_repo_root = repo_root.resolve()
    if not resolved_path.is_relative_to(resolved_repo_root):
        raise ValueError(f"说话人确认资产路径必须位于仓库内：{resolved_path}")
    return resolved_path
