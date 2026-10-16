from __future__ import annotations

from typing import Any, Protocol

from companion.llm.request_intent import LlmInvocationIntent


class RequestAdapter(Protocol):
    """定义语义调用意图到 Chat SDK 参数的编译端口。"""

    def compile(
        self,
        *,
        intent: LlmInvocationIntent,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """编译当前模型接入规格的请求参数。

        Args:
            intent: 业务层声明的调用意图。
            tools: 标准 OpenAI 函数工具声明。

        Returns:
            不包含 model、messages 和 stream 的 SDK 参数。
        """
