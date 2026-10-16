from __future__ import annotations

from typing import Protocol

from companion.llm.request_intent import LlmInvocationIntent


class OutputPolicy(Protocol):
    """定义厂商输出在对外释放前必须执行的门禁。"""

    def requires_buffering(self, intent: LlmInvocationIntent) -> bool:
        """判断本次正文是否必须完整缓冲后再释放。

        Args:
            intent: 当前调用的语义意图。

        Returns:
            需要完整正文校验时返回真。
        """

    def validate_before_release(
        self,
        *,
        intent: LlmInvocationIntent,
        text: str,
        has_structured_tool_calls: bool,
    ) -> None:
        """校验完整输出是否可以对外释放。

        Args:
            intent: 当前调用的语义意图。
            text: 已拼接的完整正文。
            has_structured_tool_calls: 响应是否包含结构化函数工具调用。

        Raises:
            RuntimeError: 输出违反当前厂商协议合同。
        """
