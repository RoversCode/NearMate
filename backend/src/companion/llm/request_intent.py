from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ToolMode = Literal["auto", "required", "forbid"]
ThinkingLevel = Literal["inherit", "off", "minimal", "high"]
OutputFormat = Literal["text", "json_object"]


@dataclass(frozen=True, slots=True)
class LlmInvocationIntent:
    """描述业务调用想要的模型行为，不包含任何 SDK 参数。

    Args:
        tool_mode: 自动选择工具、强制一个工具或禁止工具。
        required_tool_name: 强制工具模式下必须调用的函数工具名。
        allow_parallel_tools: 是否允许模型一次返回多个函数工具调用。
        thinking_level: 本次调用需要的思考档位。
        web_search: 是否需要厂商联网搜索能力。
        web_extract: 是否需要厂商抓取网页正文能力。
        output_format: 返回普通文本或 JSON 对象文本。
        max_output_tokens: 可选的调用级输出上限。
    """

    tool_mode: ToolMode = "forbid"
    required_tool_name: str | None = None
    allow_parallel_tools: bool = False
    thinking_level: ThinkingLevel = "inherit"
    web_search: bool = False
    web_extract: bool = False
    output_format: OutputFormat = "text"
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        """拒绝无法被 Adapter 准确翻译的矛盾意图。

        Raises:
            ValueError: 工具名、抓取依赖或输出上限无效。
        """

        normalized_tool_name = (self.required_tool_name or "").strip()
        if self.tool_mode == "required" and not normalized_tool_name:
            raise ValueError("强制工具模式必须提供 required_tool_name")
        if self.tool_mode != "required" and normalized_tool_name:
            raise ValueError("只有强制工具模式可以提供 required_tool_name")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens 必须大于 0")

    @classmethod
    def text(
        cls,
        *,
        thinking_level: ThinkingLevel = "inherit",
        max_output_tokens: int | None = None,
        web_search: bool = False,
        web_extract: bool = False,
    ) -> LlmInvocationIntent:
        """创建禁止工具的普通文本调用意图。

        Args:
            thinking_level: 本次普通文本调用的思考档位。
            max_output_tokens: 可选输出上限。
            web_search: 是否需要厂家联网搜索能力。
            web_extract: 是否需要厂家抓取网页正文能力。

        Returns:
            可直接交给 Client 的语义意图。
        """

        return cls(
            tool_mode="forbid",
            thinking_level=thinking_level,
            web_search=web_search,
            web_extract=web_extract,
            output_format="text",
            max_output_tokens=max_output_tokens,
        )

    @classmethod
    def json_object(
        cls,
        *,
        thinking_level: ThinkingLevel = "off",
        max_output_tokens: int | None = None,
        web_search: bool = False,
        web_extract: bool = False,
    ) -> LlmInvocationIntent:
        """创建禁止函数工具的 JSON 对象调用意图。

        Args:
            thinking_level: 本次结构化调用的思考档位。
            max_output_tokens: 可选输出上限。
            web_search: 是否允许厂商联网搜索。
            web_extract: 是否允许厂商抓取网页正文。

        Returns:
            可直接交给 Client 的语义意图。
        """

        return cls(
            tool_mode="forbid",
            thinking_level=thinking_level,
            web_search=web_search,
            web_extract=web_extract,
            output_format="json_object",
            max_output_tokens=max_output_tokens,
        )
