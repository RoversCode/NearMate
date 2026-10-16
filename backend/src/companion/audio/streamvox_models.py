from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


StreamVoxModelFamily = Literal["qwen3", "moss", "s2", "voxcpm2", "unknown"]


@dataclass(frozen=True)
class StreamVoxModelProfile:
    """描述 StreamVox 模型族及其文本正则化策略。

    Attributes:
        model: 规范化后的完整模型名称。
        family: 统一后的模型族名称。
        force_remaining_digits: 是否强制转换语义规则未覆盖的阿拉伯数字。
    """

    model: str
    family: StreamVoxModelFamily
    force_remaining_digits: bool


def resolve_streamvox_model_profile(model: str) -> StreamVoxModelProfile:
    """解析 StreamVox 模型名称对应的共享运行画像。

    Args:
        model: StreamVox 配置中的模型名称。

    Returns:
        包含模型族和残留数字策略的不可变画像。
    """

    normalized_model = model.strip().lower()
    if normalized_model.startswith("qwen3-tts-clone-"):
        return StreamVoxModelProfile(
            model=normalized_model,
            family="qwen3",
            force_remaining_digits=True,
        )
    if normalized_model == "moss-tts-nano-onnx":
        return StreamVoxModelProfile(
            model=normalized_model,
            family="moss",
            force_remaining_digits=True,
        )
    if normalized_model == "s2-pro-4b-gguf":
        return StreamVoxModelProfile(
            model=normalized_model,
            family="s2",
            force_remaining_digits=False,
        )
    if normalized_model == "voxcpm2-gguf":
        return StreamVoxModelProfile(
            model=normalized_model,
            family="voxcpm2",
            force_remaining_digits=False,
        )
    return StreamVoxModelProfile(
        model=normalized_model,
        family="unknown",
        force_remaining_digits=False,
    )
