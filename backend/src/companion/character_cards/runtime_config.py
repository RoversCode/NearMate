from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from companion.character_cards.models import CharacterCardDefinition


@dataclass(frozen=True)
class CharacterExperienceRuntimeConfig:
    """当前激活角色卡派生出的运行时体验配置。

    Args:
        wakeword_keywords: 当前角色唤醒词列表。
        wake_response_audio_dir: 当前角色唤醒回复音频目录。
        streamvox_model: 当前角色期望的 StreamVox 模型。
        streamvox_role_name: 当前角色期望的 StreamVox 音色名。
        streamvox_reference_audio_path: 当前角色注册音色使用的参考音频。
        streamvox_prompt_text: 当前角色注册音色使用的 prompt 文本。
        streamvox_seed: 当前角色期望的 StreamVox 推理种子。
        streamvox_inference_options: 当前角色期望的 StreamVox 推理参数。
        streamvox_audio_sha256: 当前角色参考音频的内容哈希。
    """

    wakeword_keywords: tuple[str, ...]
    wake_response_audio_dir: Path
    streamvox_model: str
    streamvox_role_name: str
    streamvox_reference_audio_path: Path | None
    streamvox_prompt_text: str | None
    streamvox_seed: int | None
    streamvox_inference_options: dict[str, Any]
    streamvox_audio_sha256: str | None


def resolve_character_experience_runtime_config(
    *,
    card: CharacterCardDefinition,
    wake_response_audio_dir: Path,
    streamvox_reference_audio_path: Path | None = None,
    streamvox_prompt_text: str | None = None,
    streamvox_audio_sha256: str | None = None,
) -> CharacterExperienceRuntimeConfig:
    """从角色卡生成运行时体验配置。

    Args:
        card: 当前激活角色卡。
        wake_response_audio_dir: 当前角色卡唤醒回复音频目录。
        streamvox_reference_audio_path: 当前角色卡可用于注册音色的参考音频。
        streamvox_prompt_text: 当前角色卡可用于注册音色的 prompt 文本。
        streamvox_audio_sha256: 当前角色卡语音参考音频的内容哈希。

    Returns:
        可直接注入输入链路和助手子进程的体验配置。
    """

    return CharacterExperienceRuntimeConfig(
        wakeword_keywords=tuple(card.meta.wakeword.keywords),
        wake_response_audio_dir=wake_response_audio_dir,
        streamvox_model=card.meta.voice_binding.streamvox_model,
        streamvox_role_name=card.meta.voice_binding.role_name,
        streamvox_reference_audio_path=streamvox_reference_audio_path,
        streamvox_prompt_text=streamvox_prompt_text,
        streamvox_seed=card.meta.voice_binding.seed,
        streamvox_inference_options=dict(card.meta.voice_binding.inference_options),
        streamvox_audio_sha256=card.meta.voice_binding.audio_sha256,
    )
