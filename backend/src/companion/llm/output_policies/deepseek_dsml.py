from __future__ import annotations

import re

from companion.llm.request_intent import LlmInvocationIntent


_DSML_PROTOCOL_PATTERN = re.compile(
    r"<\s*/?\s*[|｜]+\s*DSML\s*[|｜]+\s*(?:tool_calls|invoke|parameter)\b",
    re.IGNORECASE,
)


class LlmProtocolLeakError(RuntimeError):
    """表示模型把内部工具协议泄漏到了普通正文。

    Args:
        provider_id: 发生泄漏的厂商标识。
        protocol: 被识别出的内部协议名称。
    """

    def __init__(self, *, provider_id: str, protocol: str) -> None:
        self.provider_id = provider_id
        self.protocol = protocol
        super().__init__(
            f"LLM provider protocol leaked into text: {provider_id}:{protocol}"
        )


class DeepSeekDsmlOutputPolicy:
    """阻止禁用工具时的 DeepSeek DSML 或结构化调用泄漏。"""

    def requires_buffering(self, intent: LlmInvocationIntent) -> bool:
        """判断是否需要在门禁通过前暂存正文。

        Args:
            intent: 当前业务调用意图。

        Returns:
            禁止工具时返回真。
        """

        return intent.tool_mode == "forbid"

    def validate_before_release(
        self,
        *,
        intent: LlmInvocationIntent,
        text: str,
        has_structured_tool_calls: bool,
    ) -> None:
        """校验禁用工具响应没有绕过工具门禁。

        Args:
            intent: 当前业务调用意图。
            text: 已拼接的完整正文。
            has_structured_tool_calls: 响应是否出现结构化工具调用。

        Raises:
            LlmProtocolLeakError: 发现结构化调用或 DSML 伪调用。
        """

        if intent.tool_mode != "forbid":
            return
        if has_structured_tool_calls:
            raise LlmProtocolLeakError(
                provider_id="deepseek",
                protocol="structured_tool_call",
            )
        if _DSML_PROTOCOL_PATTERN.search(text) is not None:
            raise LlmProtocolLeakError(provider_id="deepseek", protocol="dsml")
