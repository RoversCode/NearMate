from __future__ import annotations

from companion.llm.request_intent import LlmInvocationIntent


class NoOpOutputPolicy:
    """表示当前模型没有额外的正文输出方言。"""

    def requires_buffering(self, intent: LlmInvocationIntent) -> bool:
        """返回当前策略不需要缓冲正文。

        Args:
            intent: 当前业务调用意图。

        Returns:
            始终返回假。
        """

        del intent
        return False

    def validate_before_release(
        self,
        *,
        intent: LlmInvocationIntent,
        text: str,
        has_structured_tool_calls: bool,
    ) -> None:
        """接受当前模型输出，不增加额外限制。

        Args:
            intent: 当前业务调用意图。
            text: 已拼接的完整正文。
            has_structured_tool_calls: 响应是否出现结构化工具调用。
        """

        del intent, text, has_structured_tool_calls
