from __future__ import annotations

import importlib.util

from fastapi import APIRouter

from companion.core.runtime_profile import resolve_runtime_profile, runtime_profile_defaults


router = APIRouter()


@router.get("/api/runtime/profile")
async def get_runtime_profile() -> dict[str, object]:
    """返回当前安装包实际选择的推理 profile 和可见 provider。

    Returns:
        供 Console、安装验收和诊断页面读取的 profile 快照。
    """

    defaults = runtime_profile_defaults()
    available_providers: list[str] = []
    if importlib.util.find_spec("onnxruntime") is not None:
        import onnxruntime as ort

        available_providers = list(ort.get_available_providers())
    return {
        "profile": resolve_runtime_profile(),
        "asr_onnx_provider": defaults.asr_onnx_provider,
        "streamvox_onnx_provider": defaults.streamvox_onnx_provider,
        "text_eou_provider": defaults.text_eou_provider,
        "llama_backend": defaults.llama_backend,
        "cpu_components": ["wakeword", "vad", "smart_turn", "speaker_verification"],
        "available_onnx_providers": available_providers,
    }
