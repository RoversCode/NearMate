from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from companion.core.runtime_paths import BACKEND_ROOT, NATIVE_ROOT


# 文本 EOU 的模型文件名由运行时 provider 与启动资产校验共同使用，避免两边各写一份容易漂移的清单。
TEXT_EOU_MODEL_FILE_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "chinese": "chinese_best_model_q8.onnx",
        "multilingual": "multilingual_best_model_q8.onnx",
    }
)
TEXT_EOU_TOKENIZER_RELATIVE_PATH = Path("tokenizer") / "tokenizer.json"


@dataclass(frozen=True)
class LocalAudioAssetPaths:
    """本项目语音链路使用的本地资产目录。"""

    # Backend 根目录，用于限制 Settings 覆盖的模型路径不会越过发行清单边界。
    repo_root: Path
    # 本地模型权重目录，后续权重文件只能放在此目录且不提交到版本库。
    ckpt_dir: Path
    # 原生推理库目录，安装态是 runtime/native，开发态是 backend/bin。
    native_dir: Path


def resolve_local_audio_asset_paths() -> LocalAudioAssetPaths:
    """解析本仓库内的语音资产路径。"""

    return LocalAudioAssetPaths(
        repo_root=BACKEND_ROOT,
        ckpt_dir=BACKEND_ROOT / "ckpt",
        native_dir=NATIVE_ROOT,
    )
