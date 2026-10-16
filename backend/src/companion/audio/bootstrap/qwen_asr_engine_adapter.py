from __future__ import annotations

from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths
from companion.audio.bootstrap.qwen_asr_config import QwenAsrConfig, build_qwen_asr_config_from_model_dir
from companion.audio.qwen_asr_gguf.inference.llama_backend import (
    build_required_backend_dll_paths,
    resolve_llama_backend_policy,
)
from companion.core.runtime_paths import NATIVE_ROOT


def to_engine_config(config: QwenAsrConfig):
    """把业务层 Qwen ASR 配置转换为底层推理引擎配置。"""

    from companion.audio.qwen_asr_gguf.inference.schema import ASREngineConfig

    return ASREngineConfig(
        model_dir=str(config.assets.model_dir),
        encoder_frontend_fn=config.assets.encoder_frontend_fn,
        encoder_backend_fn=config.assets.encoder_backend_fn,
        llm_fn=config.assets.llm_fn,
        onnx_provider=config.engine.onnx_provider,
        llm_use_gpu=config.engine.llm_use_gpu,
        llm_backend=config.engine.llm_backend,
        chunk_size=config.session.final.chunk_size_sec,
        memory_num=config.session.final.memory_chunks,
        hotwords=list(config.session.hotwords),
        encoder_intra_threads=config.engine.encoder_intra_threads,
        encoder_inter_threads=config.engine.encoder_inter_threads,
        verbose=config.engine.verbose,
    )


def create_qwen_asr_engine(config: QwenAsrConfig | None = None):
    """延迟创建本地 Qwen ASR engine。"""

    # 默认路径来自资产注册器，确保真实 ASR 不会回指 ref_code 或开发机上的绝对参考目录。
    runtime_config = config
    if runtime_config is None:
        asset_paths = resolve_local_audio_asset_paths()
        runtime_config = build_qwen_asr_config_from_model_dir(asset_paths.ckpt_dir / "qwen_asr")

    missing_files = [str(path) for path in runtime_config.required_files() if not path.exists()]
    llama_backend_resolution = resolve_llama_backend_policy(
        NATIVE_ROOT,
        requested_backend=runtime_config.engine.llm_backend,
    )
    required_llama_backend = (
        llama_backend_resolution.backend
        if llama_backend_resolution.use_gpu
        else runtime_config.engine.llm_backend
    )
    missing_files.extend(
        str(path)
        for path in build_required_backend_dll_paths(
            NATIVE_ROOT,
            use_gpu=llama_backend_resolution.use_gpu,
            backend=required_llama_backend,
        )
        if not path.exists()
    )
    if missing_files:
        raise FileNotFoundError("缺失 Qwen ASR 本地运行文件：\n" + "\n".join(missing_files))

    # 真实推理模块导入会加载 onnxruntime、gguf 与 llama.cpp DLL，因此必须留在 factory 内延迟执行。
    from companion.audio.qwen_asr_gguf.inference.asr import QwenASREngine

    return QwenASREngine(to_engine_config(runtime_config))
