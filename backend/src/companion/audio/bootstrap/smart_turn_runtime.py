from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from companion.core.settings import Settings

from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths


# Smart Turn 参考模型按 16kHz、8 秒窗口训练，集中声明便于后续 provider 和测试复用。
SMART_TURN_SAMPLE_RATE = 16000
SMART_TURN_WINDOW_SECONDS = 8
SMART_TURN_MODEL_NAME = "smart-turn-v3.2-gpu.onnx"


@dataclass(frozen=True)
class SmartTurnRuntimeConfig:
    """本地 Smart Turn 运行时配置。"""

    # Smart Turn ONNX 模型文件，必须位于当前仓库的 backend/ckpt 之下。
    model_path: Path
    # Smart Turn 参考模型按 16kHz 单声道音频训练。
    sample_rate: int = SMART_TURN_SAMPLE_RATE
    # 参考实现使用 WhisperFeatureExtractor 的 8 秒 chunk。
    window_seconds: int = SMART_TURN_WINDOW_SECONDS
    # 默认优先 CPU，后续真实 provider 可按环境扩展为 CUDA/DML。
    provider: str = "CPUExecutionProvider"


def build_smart_turn_runtime_config(
    settings: Settings | None = None,
    *,
    provider: str = "CPUExecutionProvider",
) -> SmartTurnRuntimeConfig:
    """构建仓库内 Smart Turn 运行时配置。"""

    # 参考实现硬编码外部 ONNX 路径；这里统一改为仓库内资产注册器或 Settings 覆盖。
    asset_paths = resolve_local_audio_asset_paths()
    raw_model_path = (
        Path(settings.smart_turn_model_path)
        if settings is not None
        else asset_paths.ckpt_dir / SMART_TURN_MODEL_NAME
    )
    model_path = _resolve_repo_local_path(raw_model_path, repo_root=asset_paths.repo_root)
    return SmartTurnRuntimeConfig(model_path=model_path, provider=provider)


def prepare_smart_turn_pcm16(
    pcm16: bytes,
    *,
    sample_rate: int = SMART_TURN_SAMPLE_RATE,
    window_seconds: int = SMART_TURN_WINDOW_SECONDS,
):
    """把 16k 单声道 PCM16 字节转换为 Smart Turn 的 8 秒 float32 输入。 """

    # numpy 是音频预处理必需但相对轻量的依赖，放在函数内可以让纯路径 smoke test 更稳。
    import numpy as np

    # 当前实时链路约定只接收 16kHz 单声道 PCM；采样率错误会直接影响 Smart Turn 判断。
    if sample_rate != SMART_TURN_SAMPLE_RATE:
        raise ValueError(f"Smart Turn 仅支持 {SMART_TURN_SAMPLE_RATE}Hz PCM，收到 {sample_rate}Hz")

    # PCM16 必须按两个字节对齐，否则最后一个孤立字节无法被可靠解码。
    if len(pcm16) % 2 != 0:
        raise ValueError("PCM16 字节长度必须是 2 的倍数")

    # int16 归一化到 [-1, 1] 附近，保持与参考推理脚本输入 numpy waveform 的语义一致。
    audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    # 静音或空输入没有可用于归一化的峰值；保持全零，避免 NaN 进入 Smart Turn。
    peak = audio.max() if audio.size else 0.0
    if peak != 0.0:
        audio = audio / peak
    return prepare_smart_turn_audio_window(
        audio,
        sample_rate=sample_rate,
        window_seconds=window_seconds,
    )


def prepare_smart_turn_audio_window(
    audio,
    *,
    sample_rate: int = SMART_TURN_SAMPLE_RATE,
    window_seconds: int = SMART_TURN_WINDOW_SECONDS,
):
    """裁剪或前置补零为 Smart Turn 需要的固定窗口。"""

    import numpy as np

    # Smart Turn 只处理单声道一维 waveform；多声道混音应该在更上游完成。
    audio_array = np.asarray(audio, dtype=np.float32)
    if audio_array.ndim != 1:
        raise ValueError(f"Smart Turn 只支持一维单声道音频，收到维度 {audio_array.ndim}")

    # 参考工具 truncate_audio_to_last_n_seconds 的关键行为：过长取末尾，过短在前方补零。
    target_samples = int(sample_rate * window_seconds)
    if audio_array.shape[0] >= target_samples:
        return audio_array[-target_samples:].astype(np.float32, copy=False)

    # 前置补零保留当前 turn 的尾部语义，让模型始终看到最近发言结束处。
    padding = np.zeros(target_samples - audio_array.shape[0], dtype=np.float32)
    return np.concatenate([padding, audio_array.astype(np.float32, copy=False)])


def create_onnx_session(config: SmartTurnRuntimeConfig):
    """创建 Smart Turn ONNX Runtime session。"""

    if not config.model_path.exists():
        raise FileNotFoundError(f"Smart Turn 模型文件缺失：{config.model_path}")

    # 现在必须在本地模型存在后再导入 onnxruntime，避免缺资产时被依赖安装问题掩盖。
    from streamvox_runtime import ensure_single_onnxruntime_distribution, initialize_nvidia_runtime
    ensure_single_onnxruntime_distribution()
    import onnxruntime as ort

    if config.provider == "CUDAExecutionProvider":
        initialize_nvidia_runtime(consumer="onnx_cuda", required=True)

    # 线程和图优化参数沿用参考实现的低抖动配置，适合实时链路串行推理。
    session_options = ort.SessionOptions()
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.inter_op_num_threads = 1
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(
        str(config.model_path),
        sess_options=session_options,
        providers=[config.provider],
    )


def create_feature_extractor(config: SmartTurnRuntimeConfig):
    """延迟创建 Smart Turn 使用的 Whisper 特征提取器。"""

    # 参考实现使用 WhisperFeatureExtractor(chunk_length=8)，这里保留同一输入特征约定。
    from transformers import WhisperFeatureExtractor

    return WhisperFeatureExtractor(chunk_length=config.window_seconds)


def build_smart_turn_input_features(audio, config: SmartTurnRuntimeConfig):
    """构建 Smart Turn ONNX 模型输入特征。"""

    import numpy as np

    # 先统一窗口长度，再交给 Whisper 特征提取器，保证后续 provider 不重复实现裁剪补零逻辑。
    prepared_audio = prepare_smart_turn_audio_window(
        audio,
        sample_rate=config.sample_rate,
        window_seconds=config.window_seconds,
    )
    feature_extractor = create_feature_extractor(config)
    inputs = feature_extractor(
        prepared_audio,
        sampling_rate=config.sample_rate,
        return_tensors="np",
        padding="max_length",
        max_length=config.window_seconds * config.sample_rate,
        truncation=True,
        do_normalize=True,
    )

    # ONNX 模型期望 batch 维度，参考实现中 squeeze 后再 expand_dims。
    input_features = inputs.input_features.squeeze(0).astype(np.float32)
    return np.expand_dims(input_features, axis=0)


def _resolve_repo_local_path(path: Path, *, repo_root: Path) -> Path:
    """解析并限制路径只能位于当前仓库内。"""

    # Settings 可能被 env.yaml 覆盖，因此所有模型路径都要重新解析并做仓库归属校验。
    resolved_path = path.expanduser().resolve()
    resolved_repo_root = repo_root.resolve()
    if not resolved_path.is_relative_to(resolved_repo_root):
        raise ValueError(f"Smart Turn 模型路径必须位于当前仓库内：{resolved_path}")
    return resolved_path
