"""让首次试听与正式语音共用同一份模型参数规则。"""

from __future__ import annotations

from typing import Any

from companion.audio.streamvox_models import resolve_streamvox_model_profile


def normalize_streamvox_inference_options(model: str, inference_options: dict[str, Any] | None) -> dict[str, Any]:
    """按模型族归一化 StreamVox 推理参数。

    Args:
        model: 当前 StreamVox 模型名。
        inference_options: 控制台提交的模型族专属参数。

    Returns:
        可传给 StreamVox SDK 的参数字典。
    """

    raw_options = inference_options or {}
    family = resolve_streamvox_model_profile(model).family
    if family == "qwen3":
        language = str(raw_options.get("language", "chinese")).strip().lower() or "chinese"
        icl = raw_options.get("icl", False)
        supported_languages = {
            "auto",
            "chinese",
            "english",
            "japanese",
            "korean",
            "german",
            "french",
            "russian",
            "portuguese",
            "spanish",
            "italian",
        }
        if language not in supported_languages:
            raise ValueError("StreamVox qwen3 language is not supported")
        if not isinstance(icl, bool):
            raise ValueError("StreamVox qwen3 icl must be true or false")
        return {"language": language, "icl": icl}

    if family == "moss":
        mode = raw_options.get("mode", 0)
        if mode not in {0, 1}:
            raise ValueError("StreamVox moss mode must be 0 or 1")
        return {"mode": mode}

    if family == "voxcpm2":
        mode = raw_options.get("mode", 1)
        if mode not in {1, 2, 3}:
            raise ValueError("StreamVox voxcpm2 mode must be 1, 2 or 3")
        normalized_options: dict[str, Any] = {"mode": mode}
        control_text = str(raw_options.get("control_text", "")).strip()
        if mode == 1 and control_text:
            normalized_options["control_text"] = control_text
        return normalized_options

    return {}
