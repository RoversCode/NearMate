from __future__ import annotations

from typing import Any

from companion.llm.request_intent import LlmInvocationIntent
from companion.llm.request_profiles import DeepSeekChatRequestProfile


class DeepSeekChatRequestAdapter:
    """按具体模型请求配置把统一能力意图编译成 DeepSeek Chat 方言。

    Args:
        request_profile: 当前模型已经通过合同测试的请求参数配置。
    """

    def __init__(self, request_profile: DeepSeekChatRequestProfile) -> None:
        self._request_profile = request_profile

    @property
    def request_profile(self) -> DeepSeekChatRequestProfile:
        """返回当前 Adapter 使用的只读模型请求配置。

        Returns:
            Factory 注入的模型请求配置。
        """

        return self._request_profile

    def compile(
        self,
        *,
        intent: LlmInvocationIntent,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """生成 DeepSeek Chat 的顶层参数和私有请求体。

        Args:
            intent: 业务调用希望启用的能力。
            tools: 当前调用允许使用的标准函数工具。

        Returns:
            可以直接与 model、messages、stream 合并的独立字典。

        Raises:
            ValueError: 业务请求了尚未验证的 DeepSeek 能力。
        """

        if intent.web_search or intent.web_extract:
            raise ValueError("DeepSeek Chat 接入未验证联网搜索或网页抓取")
        if (
            intent.output_format == "json_object"
            and not self._request_profile.supports_json_object
        ):
            raise ValueError("当前 DeepSeek 模型请求配置不支持 JSON 对象输出")
        if (
            intent.thinking_level in {"minimal", "high"}
            and not self._request_profile.supports_reasoning_effort
        ):
            raise ValueError("当前 DeepSeek 模型请求配置不支持思考强度")

        request: dict[str, Any] = {}
        extra_body: dict[str, Any] = {}
        if intent.thinking_level == "off":
            extra_body["thinking"] = {"type": "disabled"}
        elif intent.thinking_level in {"minimal", "high"}:
            extra_body["thinking"] = {"type": "enabled"}
            request["reasoning_effort"] = (
                "low" if intent.thinking_level == "minimal" else "high"
            )

        if intent.output_format == "json_object":
            request["response_format"] = {"type": "json_object"}
        if intent.max_output_tokens is not None:
            request["max_tokens"] = intent.max_output_tokens

        if intent.tool_mode == "forbid":
            # DeepSeek 禁用函数工具时必须明确发送 none，避免模型输出 DSML 伪调用。
            request["tool_choice"] = "none"
        else:
            if not self._request_profile.supports_function_tools:
                raise ValueError("当前 DeepSeek 模型请求配置不支持函数工具")
            if not tools:
                raise ValueError("允许或强制工具调用时必须提供函数工具")
            request["tools"] = _copy_tools(tools)
            request["parallel_tool_calls"] = intent.allow_parallel_tools
            request["tool_choice"] = (
                "auto"
                if intent.tool_mode == "auto"
                else {
                    "type": "function",
                    "function": {"name": intent.required_tool_name},
                }
            )

        if extra_body:
            request["extra_body"] = extra_body
        return request


def _copy_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """复制函数工具声明，避免 SDK 或测试修改业务层对象。

    Args:
        tools: 业务层提供的标准函数工具列表。

    Returns:
        与输入不共享嵌套字典和列表的工具列表。
    """

    return [_copy_mapping(tool) for tool in tools]


def _copy_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """递归复制请求字典中的常见容器。

    Args:
        value: 需要复制的请求字典。

    Returns:
        独立的新字典。
    """

    return {key: _copy_value(item) for key, item in value.items()}


def _copy_value(value: Any) -> Any:
    """复制字典或列表，标量保持原值。

    Args:
        value: 任意请求参数值。

    Returns:
        复制后的值。
    """

    if isinstance(value, dict):
        return _copy_mapping(value)
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    return value
