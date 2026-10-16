from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from companion.llm.chat_client import ChatCompletionsClient
from companion.llm.config import LlmRuntimeConfig
from companion.llm.model_registry import get_model_integration
from companion.llm.output_policies import (
    DeepSeekDsmlOutputPolicy,
    LlmProtocolLeakError,
)
from companion.llm.request_adapters import DeepSeekChatRequestAdapter
from companion.llm.request_intent import LlmInvocationIntent
from companion.llm.request_profiles import get_model_request_profile


class RecordingStream:
    """记录 Transport 是否主动关闭底层 SDK 流。

    Args:
        chunks: 本次流按顺序返回的 SDK 分片。
        cancellation_event: 可选取消信号；返回首个分片后立即触发。
    """

    def __init__(
        self,
        chunks: list[object],
        *,
        cancellation_event: Event | None = None,
    ) -> None:
        self.chunks = chunks
        self.cancellation_event = cancellation_event
        self.close_calls = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        """逐个返回测试分片，并在指定位置模拟业务取消。"""

        for index, chunk in enumerate(self.chunks):
            yield chunk
            if index == 0 and self.cancellation_event is not None:
                self.cancellation_event.set()

    def close(self) -> None:
        """记录一次底层连接关闭。"""

        self.close_calls += 1


class RecordingOpenAiClient:
    """提供可检查的 Chat SDK 替身。

    Args:
        stream: 每次请求返回的固定对象。
    """

    def __init__(self, stream: object) -> None:
        self.stream = stream
        self.requests: list[dict[str, Any]] = []
        self.close_calls = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs: Any) -> object:
        """记录最终 SDK 参数并返回固定响应。

        Args:
            **kwargs: Chat Transport 发送给 SDK 的完整参数。

        Returns:
            构造时注入的流或非流式响应。
        """

        self.requests.append(kwargs)
        return self.stream

    def close(self) -> None:
        """记录显式客户端关闭。"""

        self.close_calls += 1


def test_dsml_gate_rejects_buffered_text_before_any_release() -> None:
    """禁用工具时发现 DSML，完整正文必须在释放任何片段前被拒绝。"""

    stream = RecordingStream(
        [
            _chat_chunk(content="正常前缀"),
            _chat_chunk(content="<|DSML|tool_calls>"),
            _chat_chunk(finish_reason="stop"),
        ]
    )
    iterator = iter(
        _client(RecordingOpenAiClient(stream)).stream_message(
            messages=[],
            tools=[],
            intent=LlmInvocationIntent.text(),
            audit_source="text_input",
            audit_operation="initial",
        )
    )

    with pytest.raises(LlmProtocolLeakError) as error:
        next(iterator)

    assert error.value.protocol == "dsml"
    assert stream.close_calls == 1


def test_disabled_tools_reject_structured_tool_call_before_release() -> None:
    """禁用工具时厂商即使返回结构化调用，也不能向业务层泄漏开始事件。"""

    stream = RecordingStream(
        [
            _chat_chunk(content="不会释放"),
            _chat_chunk(
                tool_calls=[
                    _tool_delta(
                        index=0,
                        tool_call_id="call-forbidden",
                        name="forbidden_tool",
                        arguments="{}",
                    )
                ]
            ),
            _chat_chunk(finish_reason="tool_calls"),
        ]
    )
    iterator = iter(
        _client(RecordingOpenAiClient(stream)).stream_message(
            messages=[],
            tools=[],
            intent=LlmInvocationIntent.text(),
            audit_source="text_input",
            audit_operation="initial",
        )
    )

    with pytest.raises(LlmProtocolLeakError) as error:
        next(iterator)

    assert error.value.protocol == "structured_tool_call"
    assert stream.close_calls == 1


def test_tool_deltas_accumulate_into_one_standard_tool_call() -> None:
    """同一索引的工具身份、名称和参数增量必须合并成一个项目工具调用。"""

    stream = RecordingStream(
        [
            _chat_chunk(
                tool_calls=[
                    _tool_delta(
                        index=0,
                        tool_call_id="call-1",
                        name="lookup_weather",
                        arguments='{"city":',
                    )
                ]
            ),
            _chat_chunk(
                tool_calls=[
                    _tool_delta(
                        index=0,
                        arguments='"Wuhan"}',
                    )
                ]
            ),
            _chat_chunk(finish_reason="tool_calls"),
        ]
    )
    chunks = list(
        _client(RecordingOpenAiClient(stream)).stream_message(
            messages=[],
            tools=[_function_tool("lookup_weather")],
            intent=LlmInvocationIntent(tool_mode="auto"),
            audit_source="text_input",
            audit_operation="initial",
        )
    )

    assert [chunk.kind for chunk in chunks] == [
        "tool_call_started",
        "tool_calls",
    ]
    assert len(chunks[1].tool_calls) == 1
    assert chunks[1].tool_calls[0].tool_call_id == "call-1"
    assert chunks[1].tool_calls[0].name == "lookup_weather"
    assert chunks[1].tool_calls[0].arguments == {"city": "Wuhan"}
    assert stream.close_calls == 1


def test_text_stream_preserves_reasoning_text_usage_and_terminal_state() -> None:
    """普通文本流必须保留思考、正文、用量和合法终态。"""

    stream = RecordingStream(
        [
            _chat_chunk(reasoning_content="分析"),
            _chat_chunk(content="答案"),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(
                    completion_tokens=7,
                    completion_tokens_details=SimpleNamespace(
                        reasoning_tokens=3,
                    ),
                ),
            ),
            _chat_chunk(finish_reason="stop"),
        ]
    )
    chunks = list(
        _client(RecordingOpenAiClient(stream)).stream_text(
            messages=[],
            intent=LlmInvocationIntent.text(thinking_level="high"),
            audit_source="activity",
            audit_operation="execute",
        )
    )

    assert [(chunk.kind, chunk.text) for chunk in chunks[:2]] == [
        ("reasoning_delta", "分析"),
        ("text_delta", "答案"),
    ]
    assert chunks[2].kind == "usage"
    assert chunks[2].reasoning_tokens == 3
    assert chunks[2].output_tokens == 7
    assert stream.close_calls == 1


def test_midstream_cancellation_stops_output_and_closes_stream_once() -> None:
    """流消费中触发取消后必须停止输出，并且底层连接只关闭一次。"""

    cancellation_event = Event()
    stream = RecordingStream(
        [
            _chat_chunk(content="取消前暂存"),
            _chat_chunk(content="不得继续读取"),
        ],
        cancellation_event=cancellation_event,
    )
    chunks = list(
        _client(RecordingOpenAiClient(stream)).stream_text(
            messages=[],
            intent=LlmInvocationIntent.text(),
            audit_source="activity",
            audit_operation="execute",
            cancellation_event=cancellation_event,
        )
    )

    assert chunks == []
    assert cancellation_event.is_set() is True
    assert stream.close_calls == 1


def test_close_releases_openai_sdk_client() -> None:
    """职责客户端退役时必须显式关闭 SDK 持有的连接池。"""

    openai_client = RecordingOpenAiClient(RecordingStream([]))
    client = _client(openai_client)

    client.close()

    assert openai_client.close_calls == 1


def _client(openai_client: RecordingOpenAiClient) -> ChatCompletionsClient:
    """构造绑定 DeepSeek 方言的测试 Transport。

    Args:
        openai_client: 记录 SDK 请求和关闭动作的替身。

    Returns:
        使用真实 Adapter 与 Output Policy 的 Chat 客户端。
    """

    integration = get_model_integration("deepseek:deepseek-flash:chat")
    return ChatCompletionsClient(
        runtime_config=LlmRuntimeConfig(
            integration_id=integration.integration_id,
            provider_id=integration.provider_id,
            model=integration.model,
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
        ),
        integration=integration,
        request_adapter=DeepSeekChatRequestAdapter(
            get_model_request_profile("deepseek-v4-pro-chat-v1")
        ),
        output_policy=DeepSeekDsmlOutputPolicy(),
        role_name="foreground",
        openai_client=openai_client,
    )


def _chat_chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list[object] | None = None,
    finish_reason: str | None = None,
) -> object:
    """构造一个最小 Chat Completions 流分片。

    Args:
        content: 正文增量。
        reasoning_content: 思考增量。
        tool_calls: 工具调用增量列表。
        finish_reason: 可选流终态。

    Returns:
        符合 Transport 宽松读取方式的 SDK 对象。
    """

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning_content,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def _tool_delta(
    *,
    index: int,
    tool_call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> object:
    """构造一个可分片拼接的 SDK 工具调用增量。

    Args:
        index: 本轮工具调用的稳定序号。
        tool_call_id: 首个增量携带的调用身份。
        name: 首个增量携带的函数名。
        arguments: 本次返回的 JSON 参数片段。

    Returns:
        Chat Transport 可以读取的工具调用增量。
    """

    return SimpleNamespace(
        index=index,
        id=tool_call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _function_tool(name: str) -> dict[str, object]:
    """构造一个标准 OpenAI 函数工具声明。

    Args:
        name: 函数工具名。

    Returns:
        不包含厂商扩展字段的工具字典。
    """

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "测试工具。",
            "parameters": {"type": "object", "properties": {}},
        },
    }
