"""实际运行发行包的输入模型和启动预热，拦截模块导入发现不了的崩溃与卡死。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sys


def probe_native_audio_runtime(model_catalog_path: Path) -> dict[str, object]:
    """按正式 DLL 加载顺序执行输入模型与可选预热，再返回发行合同证据。

    Args:
        model_catalog_path: 当前发行目录中的固定 TTS 模型清单。

    Returns:
        运行 profile、模型目录和真实音频推理结果；任何加载或推理失败直接抛出。
    """
    from companion.core.native_runtime_env import configure_native_runtime_environment

    # 必须与正式后台一样先绑定唯一 ORT；单独导入 sherpa 会误用它自带的 DLL，掩盖冲突。
    configure_native_runtime_environment()
    import audio_aec_sidecar.main  # noqa: F401
    import audio_aec_poc  # noqa: F401
    import numpy as np
    import onnxruntime as ort
    from companion.audio.bootstrap.copied_assets import validate_minimum_realtime_assets
    from companion.audio.bootstrap.speaker_verification_runtime import (
        build_speaker_verification_runtime_config,
        create_speaker_embedding_extractor,
    )
    from companion.audio.bootstrap.wakeword_runtime import (
        build_wakeword_runtime_config,
        create_keyword_spotter,
    )
    from companion.core.runtime_profile import runtime_profile_defaults, resolve_runtime_profile
    from companion.core.voice_setup_assets import MODEL_NAMES, read_model_catalog

    defaults = runtime_profile_defaults()
    missing = validate_minimum_realtime_assets(llm_backend=defaults.llama_backend, llm_use_gpu=True)
    if missing:
        raise RuntimeError("实时语音资产缺失：" + json.dumps(missing, ensure_ascii=False))

    # 模型已经随包准备好。真正创建并执行会话，才能发现 C API 不兼容导致的进程崩溃。
    print("NEARMATE_NATIVE_AUDIO_STAGE=creating_wakeword", flush=True)
    spotter = create_keyword_spotter(build_wakeword_runtime_config(keywords=("你好",)))
    stream = spotter.create_stream()
    stream.accept_waveform(16000, np.zeros(32000, dtype=np.float32))
    decoded_frames = 0
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        spotter.get_result(stream)
        decoded_frames += 1
        if decoded_frames > 200:
            raise RuntimeError("唤醒模型处理固定测试音频时未能收口")
    if decoded_frames == 0:
        raise RuntimeError("唤醒模型没有真正执行任何音频帧")

    print("NEARMATE_NATIVE_AUDIO_STAGE=creating_speaker_verification", flush=True)
    extractor = create_speaker_embedding_extractor(build_speaker_verification_runtime_config())
    speaker_stream = extractor.create_stream()
    sample_positions = np.arange(32000, dtype=np.float32) / 16000
    waveform = (0.1 * np.sin(2 * np.pi * 440 * sample_positions)).astype(np.float32)
    speaker_stream.accept_waveform(sample_rate=16000, waveform=waveform)
    speaker_stream.input_finished()
    embedding = np.asarray(extractor.compute(speaker_stream), dtype=np.float32)
    if embedding.ndim != 1 or embedding.size == 0 or not np.isfinite(embedding).all():
        raise RuntimeError("声纹模型返回了无效嵌入")

    print("NEARMATE_NATIVE_AUDIO_STAGE=creating_asr", flush=True)
    from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths
    from companion.audio.bootstrap.qwen_asr_config import build_qwen_asr_config_from_model_dir
    from companion.audio.bootstrap.qwen_asr_engine_adapter import create_qwen_asr_engine
    from companion.core.runtime_paths import SEED_CHARACTER_CARDS_ROOT

    assets = resolve_local_audio_asset_paths()
    asr_config = build_qwen_asr_config_from_model_dir(assets.ckpt_dir / "qwen_asr")
    asr_config = replace(asr_config, engine=replace(
        asr_config.engine,
        onnx_provider=defaults.asr_onnx_provider,
        llm_backend=defaults.llama_backend,
        verbose=False,
    ))
    # 使用已随包的默认角色录音执行完整 encoder + GGUF 解码，不能让存在检查掩盖 ASR ABI 错误。
    engine = create_qwen_asr_engine(asr_config)
    try:
        print("NEARMATE_NATIVE_AUDIO_STAGE=transcribing_asr", flush=True)
        result = engine.transcribe(
            str(SEED_CHARACTER_CARDS_ROOT / "huohua/voice/reference.wav"),
            language="Chinese", duration=6.0,
        )
        transcript = result.text.strip()
        if not transcript:
            raise RuntimeError("ASR 没有从随包真实录音转写出任何文字")
        asr_evidence = {
            "transcript": transcript,
            "llama_backend": engine.model.actual_backend,
            "frontend_providers": engine.encoder.sess_fe.get_providers(),
            "backend_providers": engine.encoder.sess_be.get_providers(),
        }
    finally:
        engine.shutdown()

    # 空白 Bootstrap 不会装配助手，必须单独确认可选系统目录预热不会卡住启动线程。
    # 这里只读取应用目录，不打开任何应用；系统 COM 挂起时也应立即交还主线程。
    print("NEARMATE_NATIVE_AUDIO_STAGE=starting_application_catalog_prewarm", flush=True)
    from companion.foreground_tools.external.windows_application_tool import WindowsApplicationForegroundTool

    WindowsApplicationForegroundTool.from_system()

    catalog = read_model_catalog(model_catalog_path)
    return {
        "profile": resolve_runtime_profile(),
        "asr_onnx_provider": defaults.asr_onnx_provider,
        "streamvox_onnx_provider": defaults.streamvox_onnx_provider,
        "llama_backend": defaults.llama_backend,
        "available_onnx_providers": sorted(ort.get_available_providers()),
        "model_ids": sorted(catalog),
        "declared_model_ids": sorted(MODEL_NAMES),
        "missing_realtime_assets": missing,
        "application_catalog_prewarm_returned": True,
        "native_audio": {
            "wakeword_decoded_frames": decoded_frames,
            "speaker_embedding_size": int(embedding.size),
            "speaker_embedding_finite": True,
            "asr": asr_evidence,
        },
    }


if __name__ == "__main__":
    result = probe_native_audio_runtime(Path(sys.argv[1]))
    print("NEARMATE_RUNTIME_PROBE=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
