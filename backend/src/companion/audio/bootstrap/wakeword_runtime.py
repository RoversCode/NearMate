from __future__ import annotations

from streamvox_runtime import ensure_single_onnxruntime_distribution, initialize_nvidia_runtime
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths

if TYPE_CHECKING:
    from companion.core.settings import Settings


# 以下三个模型文件名来自已复制的 sherpa-onnx KWS 资产，集中声明可避免后续 provider 重复硬编码。
ENCODER_MODEL_NAME = "encoder-epoch-13-avg-2-chunk-16-left-64.onnx"
DECODER_MODEL_NAME = "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"
JOINER_MODEL_NAME = "joiner-epoch-13-avg-2-chunk-16-left-64.onnx"



@dataclass(frozen=True)
class WakewordRuntimeConfig:
    """本地唤醒词运行时配置。"""

    # 本仓库内的唤醒词模型目录，用于后续诊断和资产校验。
    model_dir: Path
    # sherpa-onnx KeywordSpotter 初始化阶段要求存在基准关键词文件；这里只作为底层模型启动依赖，不作为业务配置源。
    keywords_file: Path
    # sherpa-onnx tokens 文件，文本转 token 和 KWS 模型必须共用同一份。
    tokens_file: Path
    # Zipformer encoder 模型路径。
    encoder: Path
    # Zipformer decoder 模型路径。
    decoder: Path
    # Zipformer joiner 模型路径。
    joiner: Path
    # 唤醒词文本列表来自控制台用户设置，是产品侧监听词的唯一事实源。
    keywords: tuple[str, ...] = ()
    # 文本转 token 的类型沿用参考项目配置。
    tokens_type: str = "ppinyin"
    # sherpa-onnx KeywordSpotter 的执行 provider，当前支持 cpu、cuda 和 coreml。
    provider: str = "cpu"
    # sherpa-onnx 的设备编号，CUDA 场景用于选择 GPU。
    device: int = 0
    # sherpa-onnx 神经网络计算线程数，CPU 推理可通过该字段调优。
    num_threads: int = 2

    def as_sherpa_kwargs(self) -> dict[str, str | int]:
        """转换为 sherpa-onnx KeywordSpotter 的关键字参数。"""

        # sherpa-onnx 接收字符串路径和设备参数；这里集中转换，避免 provider 层重复拼接或遗漏调优字段。
        return {
            "keywords_file": str(self.keywords_file),
            "tokens": str(self.tokens_file),
            "encoder": str(self.encoder),
            "decoder": str(self.decoder),
            "joiner": str(self.joiner),
            "provider": self.provider,
            "device": self.device,
            "num_threads": self.num_threads,
        }


def build_wakeword_runtime_config(
    settings: Settings | None = None,
    *,
    keywords: Sequence[str] = (),
    provider: str = "cpu",
    device: int = 0,
    num_threads: int = 2,
) -> WakewordRuntimeConfig:
    """构建仓库内唤醒词运行时配置。"""

    # settings 允许后续 provider 使用环境覆盖；默认路径仍由资产注册器锚定在当前仓库。
    asset_paths = resolve_local_audio_asset_paths()
    raw_model_dir = Path(settings.wakeword_model_dir) if settings is not None else asset_paths.ckpt_dir / "wakeword"
    model_dir = _resolve_repo_local_path(raw_model_dir, repo_root=asset_paths.repo_root)

    # KWS 参考实现直接在 import 时拼接这些文件；这里改为显式配置，避免导入即加载模型。
    # 唤醒词文本来自控制台用户设置，而模型目录下的 keywords.txt 只保留为 sherpa 初始化依赖。
    return WakewordRuntimeConfig(
        model_dir=model_dir,
        keywords_file=model_dir / "keywords.txt",
        tokens_file=model_dir / "tokens.txt",
        encoder=model_dir / ENCODER_MODEL_NAME,
        decoder=model_dir / DECODER_MODEL_NAME,
        joiner=model_dir / JOINER_MODEL_NAME,
        keywords=tuple(keyword.strip() for keyword in keywords if keyword.strip()),
        provider=provider,
        device=device,
        num_threads=num_threads,
    )


def validate_wakeword_runtime_files(config: WakewordRuntimeConfig) -> list[str]:
    """校验唤醒词运行时需要的本地文件是否存在。"""

    # 后续 provider 可以在启动前调用该函数，把缺失资产以可读路径暴露给使用者。
    if not config.keywords:
        return ["wakeword_keywords"]

    required_files = [
        config.keywords_file,
        config.tokens_file,
        config.encoder,
        config.decoder,
        config.joiner,
    ]
    return [str(path) for path in required_files if not path.exists()]


def create_keyword_spotter(config: WakewordRuntimeConfig):
    """延迟创建 sherpa-onnx KeywordSpotter。"""

    # 创建前先做轻量文件校验，错误信息比底层模型加载失败更直接。
    missing_files = validate_wakeword_runtime_files(config)
    if missing_files:
        raise FileNotFoundError(f"唤醒词模型文件缺失：{missing_files}")

    # CUDA 模式在导入原生扩展前加载发行包内的 NVIDIA 运行库。
    ensure_single_onnxruntime_distribution()
    if config.provider.lower() == "cuda":
        initialize_nvidia_runtime(consumer="onnx_cuda", required=True)

    # 现在必须在资产存在后再导入重型依赖，避免缺模型时被 ModuleNotFoundError 掩盖真正的配置问题。
    import sherpa_onnx

    return sherpa_onnx.KeywordSpotter(**config.as_sherpa_kwargs())


def keywords_to_tokens(keywords: Sequence[str], config: WakewordRuntimeConfig) -> str:
    """把唤醒词文本转换为 sherpa-onnx 关键词 token 串。"""

    # 空关键词没有可转换内容，提前返回，避免为无效业务配置导入重型 sherpa 依赖。
    if not keywords:
        return ""

    # text2token 也会导入 sherpa-onnx 扩展；CUDA 环境下仍需提前准备 DLL 搜索路径，保证导入链一致。
    ensure_single_onnxruntime_distribution()
    if config.provider.lower() == "cuda":
        initialize_nvidia_runtime(consumer="onnx_cuda", required=True)

    # 参考项目使用 sherpa_onnx.text2token；这里保留算法形态，但延迟到函数调用时才导入依赖。
    import sherpa_onnx

    tokens = sherpa_onnx.text2token(
        list(keywords),
        tokens=str(config.tokens_file),
        tokens_type=config.tokens_type,
    )
    return "/".join(" ".join(token) for token in tokens)






def _resolve_repo_local_path(path: Path, *, repo_root: Path) -> Path:
    """解析并限制路径只能位于当前仓库内。"""

    # 路径可能来自 env.yaml 或 Settings 初始化参数，先统一转成绝对路径再做归属判断。
    resolved_path = path.expanduser().resolve()
    resolved_repo_root = repo_root.resolve()

    # Windows 下 Path.is_relative_to 可正确处理盘符；失败时说明配置指向了仓库外。
    if not resolved_path.is_relative_to(resolved_repo_root):
        raise ValueError(f"唤醒词模型目录必须位于当前仓库内：{resolved_path}")
    return resolved_path
