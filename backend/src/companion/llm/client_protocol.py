from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import Event
from typing import Any, Protocol

from companion.llm.call_trace import ActivityLlmCallTrace, LlmCallTrace
from companion.llm.config import LlmRuntimeConfig
from companion.llm.foreground_models import (
    ForegroundModelStreamChunk,
    LlmTextStreamChunk,
)
from companion.llm.model_registry import ModelIntegrationSpec
from companion.llm.request_intent import LlmInvocationIntent


class LlmClientProtocol(Protocol):
    """定义所有已验证 LLM 接入共同提供的最小客户端接口。"""

    runtime_config: LlmRuntimeConfig
    integration: ModelIntegrationSpec
    model: str

    def warmup(self) -> None:
        """执行一次最小连通性预热。"""

    def stream_message(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        intent: LlmInvocationIntent,
        audit_source: str,
        audit_operation: str,
        trace: LlmCallTrace | None = None,
        cancellation_event: Event | None = None,
    ) -> Iterable[ForegroundModelStreamChunk]:
        """流式生成正文和标准函数工具调用。

        Args:
            messages: 本次请求的完整 Chat 消息。
            tools: 当前业务允许使用的标准函数工具。
            intent: 不包含 SDK 参数的业务调用意图。
            audit_source: 触发本次调用的业务来源。
            audit_operation: 当前调用阶段。
            trace: 可选调用追踪身份。
            cancellation_event: 可选协作取消信号。

        Returns:
            按到达顺序生成的标准前台片段。
        """

    def stream_text(
        self,
        *,
        messages: list[dict[str, Any]],
        intent: LlmInvocationIntent,
        audit_source: str,
        audit_operation: str,
        audit_messages_factory: Callable[[], list[dict[str, Any]]] | None = None,
        trace: ActivityLlmCallTrace | None = None,
        cancellation_event: Event | None = None,
    ) -> Iterable[LlmTextStreamChunk]:
        """流式生成普通文本、思考增量和用量。

        Args:
            messages: 本次请求的完整 Chat 消息。
            intent: 不包含 SDK 参数的业务调用意图。
            audit_source: 触发本次调用的业务来源。
            audit_operation: 当前调用阶段。
            audit_messages_factory: 仅在审查开启时生成脱敏消息副本的惰性工厂。
            trace: 可选 Activity 调用追踪身份。
            cancellation_event: 可选协作取消信号。

        Returns:
            按到达顺序生成的普通文本流片段。
        """

    def complete_text(
        self,
        *,
        messages: list[dict[str, Any]],
        intent: LlmInvocationIntent,
        audit_source: str,
        audit_operation: str,
    ) -> str:
        """生成一段非空文本。

        Args:
            messages: 本次请求的完整 Chat 消息。
            intent: 不包含 SDK 参数的业务调用意图。
            audit_source: 触发本次调用的业务来源。
            audit_operation: 当前调用阶段。
        Returns:
            模型返回的非空正文。
        """

    def close(self) -> None:
        """显式关闭 SDK 客户端持有的网络资源。"""
