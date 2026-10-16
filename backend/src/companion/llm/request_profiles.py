from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class DeepSeekChatRequestProfile:
    """描述一个 DeepSeek 模型已经验证的 Chat 请求能力。

    Args:
        profile_id: 只在 Backend 内部使用的请求配置编号。
        model: 该配置对应的真实模型名。
        supports_json_object: 是否支持顶层 JSON Object 参数。
        supports_function_tools: 是否支持标准函数工具参数。
        supports_reasoning_effort: 是否支持思考强度参数。
    """

    profile_id: str
    model: str
    supports_json_object: bool
    supports_function_tools: bool
    supports_reasoning_effort: bool


@dataclass(frozen=True, slots=True)
class QwenChatRequestProfile:
    """描述一个 Qwen 模型已经验证的 Chat 请求能力。

    Args:
        profile_id: 只在 Backend 内部使用的请求配置编号。
        model: 该配置对应的真实模型名。
        thinking_mode: 支持布尔思考开关，或只允许关闭思考。
        required_output_format: 该接入已经验证且必须使用的输出格式。
        supports_web_search: 是否支持百炼联网搜索开关。
    """

    profile_id: str
    model: str
    thinking_mode: Literal["boolean_switch", "off_only"]
    required_output_format: Literal["text", "json_object"]
    supports_web_search: bool


ModelRequestProfile: TypeAlias = DeepSeekChatRequestProfile | QwenChatRequestProfile

DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID = "deepseek-flash-chat-v1"
DEEPSEEK_V4_PRO_CHAT_REQUEST_PROFILE_ID = "deepseek-v4-pro-chat-v1"
QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID = "qwen3.6-plus-chat-v1"
QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID = "qwen3.6-flash-chat-v1"


MODEL_REQUEST_PROFILES: tuple[ModelRequestProfile, ...] = (
    DeepSeekChatRequestProfile(
        profile_id=DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID,
        model="deepseek-flash",
        supports_json_object=True,
        supports_function_tools=True,
        supports_reasoning_effort=True,
    ),
    DeepSeekChatRequestProfile(
        profile_id=DEEPSEEK_V4_PRO_CHAT_REQUEST_PROFILE_ID,
        model="deepseek-v4-pro",
        supports_json_object=True,
        supports_function_tools=True,
        supports_reasoning_effort=True,
    ),
    QwenChatRequestProfile(
        profile_id=QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID,
        model="qwen3.6-plus",
        thinking_mode="boolean_switch",
        required_output_format="text",
        supports_web_search=True,
    ),
    QwenChatRequestProfile(
        profile_id=QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID,
        model="qwen3.6-flash",
        thinking_mode="off_only",
        required_output_format="json_object",
        supports_web_search=False,
    ),
)


def get_model_request_profile(profile_id: str) -> ModelRequestProfile:
    """读取一个已经通过合同测试的模型请求配置。

    Args:
        profile_id: 模型接入规格内部引用的配置编号。

    Returns:
        与编号对应的强类型请求配置。

    Raises:
        ValueError: 编号为空或没有登记。
    """

    normalized_profile_id = profile_id.strip()
    for profile in MODEL_REQUEST_PROFILES:
        if profile.profile_id == normalized_profile_id:
            return profile
    raise ValueError(f"未知 LLM 模型请求配置：{normalized_profile_id}")
