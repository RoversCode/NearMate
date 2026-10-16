from __future__ import annotations

from companion.audio.bootstrap.asset_registry import (
    TEXT_EOU_MODEL_FILE_NAMES,
    TEXT_EOU_TOKENIZER_RELATIVE_PATH,
    resolve_local_audio_asset_paths,
)
from companion.audio.bootstrap.qwen_asr_config import (
    QWEN_ENCODER_BACKEND_NAME,
    QWEN_ENCODER_FRONTEND_NAME,
    QWEN_LLM_NAME,
)
from companion.audio.bootstrap.realtime_asr_runtime import SILERO_VAD_NAME
from companion.audio.bootstrap.smart_turn_runtime import SMART_TURN_MODEL_NAME
from companion.audio.bootstrap.wakeword_runtime import (
    DECODER_MODEL_NAME,
    ENCODER_MODEL_NAME,
    JOINER_MODEL_NAME,
)
from companion.audio.qwen_asr_gguf.inference.llama_backend import (
    LlmBackend,
    build_required_backend_dll_paths,
    validate_backend_policy,
)


def validate_minimum_realtime_assets(
    *,
    llm_backend: LlmBackend = "auto",
    llm_use_gpu: bool = True,
) -> list[str]:
    """验证实时语音链路运行前必须存在的最小本地资产集合。"""

    # 复用统一的本地路径解析器，避免在校验逻辑中重复推导仓库根目录导致路径漂移。
    paths = resolve_local_audio_asset_paths()
    normalized_backend = validate_backend_policy(llm_backend)

    # 这里列出首次语音输入默认链路所需的最小资产集合；TTS 参考音频和声纹白名单属于显式功能配置，不能阻塞输入链路启动。
    qwen_asr_dir = paths.ckpt_dir / "qwen_asr"
    wakeword_dir = paths.ckpt_dir / "wakeword"
    text_eou_dir = paths.ckpt_dir / "turn_detector"
    required_paths = [
        *build_required_backend_dll_paths(
            paths.native_dir,
            use_gpu=llm_use_gpu,
            backend=normalized_backend,
        ),
        paths.ckpt_dir / SMART_TURN_MODEL_NAME,
        wakeword_dir / "keywords.txt",
        wakeword_dir / "tokens.txt",
        wakeword_dir / ENCODER_MODEL_NAME,
        wakeword_dir / DECODER_MODEL_NAME,
        wakeword_dir / JOINER_MODEL_NAME,
        qwen_asr_dir / SILERO_VAD_NAME,
        qwen_asr_dir / QWEN_ENCODER_FRONTEND_NAME,
        qwen_asr_dir / QWEN_ENCODER_BACKEND_NAME,
        qwen_asr_dir / QWEN_LLM_NAME,
        text_eou_dir / TEXT_EOU_TOKENIZER_RELATIVE_PATH,
        *(
            text_eou_dir / model_name
            for model_name in TEXT_EOU_MODEL_FILE_NAMES.values()
        ),
    ]

    missing_paths = [str(path) for path in required_paths if not path.exists()]
    # 只返回缺失路径字符串，方便 smoke test 和启动前诊断直接展示可执行的修复目标。
    return missing_paths
