from __future__ import annotations

import logging
from threading import Event
from types import SimpleNamespace

import pytest

from companion.llm.call_trace import ForegroundRequestTrace, LlmCallTrace
from companion.llm.chat_client import ChatCompletionsClient
from companion.llm.config import LlmRuntimeConfig
from companion.llm.model_registry import get_model_integration
from companion.llm.output_policies import DeepSeekDsmlOutputPolicy
from companion.llm.request_adapters import DeepSeekChatRequestAdapter
from companion.llm.request_intent import LlmInvocationIntent
from companion.llm.request_profiles import get_model_request_profile
from companion.llm.stream_completion import ProviderStreamInterruptedError


class RecordCollector(logging.Handler):
    """收集单个客户端测试产生的结构化日志。"""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """保存一条日志记录。

        Args:
            record: 客户端产生的日志记录。
        """

        self.records.append(record)


class FakeStream:
    """提供可关闭的最小 SDK 流。"""

    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.closed = False

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._events)

    def close(self) -> None:
        """记录流已经由统一生命周期关闭。"""

        self.closed = True


def test_chat_stream_emits_correlated_call_lifecycle_events() -> None:
    """Chat Transport 必须使用同一身份记录完整流生命周期。"""

    stream = FakeStream(
        [
            _chat_chunk(content="你好"),
            _chat_chunk(finish_reason="stop"),
        ]
    )
    client = _client(lambda **_kwargs: stream)
    trace = LlmCallTrace(
        request_id="request-a",
        generation_id="generation-a",
        llm_call_id="generation-a:initial",
        round="initial",
        request_source="text_input",
    )

    collector = _capture_logger()
    try:
        chunks = list(
            client.stream_message(
                messages=[],
                tools=[],
                intent=LlmInvocationIntent.text(),
                audit_source="text_input",
                audit_operation="initial",
                trace=trace,
            )
        )
    finally:
        _release_logger(collector)

    assert [chunk.text for chunk in chunks if chunk.kind == "text_delta"] == [
        "你好"
    ]
    assert stream.closed is True
    assert [record.event_name for record in collector.records] == [
        "llm_call_started",
        "llm_stream_opened",
        "llm_first_event",
        "llm_first_text",
        "llm_call_completed",
    ]
    for record in collector.records:
        assert record.structured_fields["request_id"] == "request-a"
        assert record.structured_fields["generation_id"] == "generation-a"
        assert record.structured_fields["llm_call_id"] == "generation-a:initial"
        assert record.structured_fields["protocol"] == "chat_completions"


def test_precancelled_call_never_opens_sdk_stream() -> None:
    """请求开始前已经取消时不得调用模型 SDK。"""

    sdk_calls = 0

    def create(**_kwargs: object) -> FakeStream:
        nonlocal sdk_calls
        sdk_calls += 1
        return FakeStream([])

    cancellation = Event()
    cancellation.set()
    collector = _capture_logger()
    try:
        chunks = list(
            _client(create).stream_message(
                messages=[],
                tools=[],
                intent=LlmInvocationIntent.text(),
                audit_source="text_input",
                audit_operation="initial",
                cancellation_event=cancellation,
            )
        )
    finally:
        _release_logger(collector)

    assert chunks == []
    assert sdk_calls == 0
    assert [record.event_name for record in collector.records] == [
        "llm_call_skipped"
    ]


def test_sdk_open_failure_is_logged_without_secret() -> None:
    """SDK 建流失败必须记录阶段和异常类型，不能记录异常正文。"""

    def create(**_kwargs: object) -> FakeStream:
        raise RuntimeError("secret-provider-message")

    collector = _capture_logger()
    try:
        with pytest.raises(RuntimeError, match="secret-provider-message"):
            list(
                _client(create).stream_message(
                    messages=[],
                    tools=[],
                    intent=LlmInvocationIntent.text(),
                    audit_source="text_input",
                    audit_operation="initial",
                )
            )
    finally:
        _release_logger(collector)

    failure_record = collector.records[-1]
    assert failure_record.event_name == "llm_call_failed"
    assert failure_record.structured_fields["phase"] == "open"
    assert failure_record.structured_fields["error_type"] == "RuntimeError"
    assert "secret-provider-message" not in failure_record.getMessage()


def test_stream_without_finish_reason_fails_as_interrupted() -> None:
    """流在没有 finish_reason 时结束必须进入稳定中断错误。"""

    stream = FakeStream([_chat_chunk(content="半截")])

    with pytest.raises(
        ProviderStreamInterruptedError,
        match="missing_finish_reason",
    ):
        list(
            _client(lambda **_kwargs: stream).stream_message(
                messages=[],
                tools=[],
                intent=LlmInvocationIntent.text(),
                audit_source="text_input",
                audit_operation="initial",
            )
        )

    assert stream.closed is True


def test_same_foreground_round_gets_unique_call_ids() -> None:
    """同一代际同一轮次的重复调用也必须能在日志中区分。"""

    request_trace = ForegroundRequestTrace(
        request_id="request-a",
        generation_id="generation-a",
        request_source="text_input",
    )

    first = request_trace.llm_call("tool_followup")
    second = request_trace.llm_call("tool_followup")

    assert first.request_id == second.request_id == "request-a"
    assert first.generation_id == second.generation_id == "generation-a"
    assert first.round == second.round == "tool_followup"
    assert first.llm_call_id != second.llm_call_id


def _client(create) -> ChatCompletionsClient:  # type: ignore[no-untyped-def]
    """构造绑定 DeepSeek Adapter 的测试 Chat Client。"""

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
        openai_client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        ),
    )


def _chat_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
) -> object:
    """构造一个最小 Chat Completions 流片段。"""

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content,
                    tool_calls=None,
                ),
                finish_reason=finish_reason,
            )
        ]
    )


def _capture_logger() -> RecordCollector:
    """把测试收集器挂到 Chat Client logger。"""

    collector = RecordCollector()
    target_logger = logging.getLogger("companion.llm.chat_client")
    collector.original_level = target_logger.level  # type: ignore[attr-defined]
    collector.original_propagate = target_logger.propagate  # type: ignore[attr-defined]
    target_logger.addHandler(collector)
    target_logger.setLevel(logging.INFO)
    target_logger.propagate = False
    return collector


def _release_logger(collector: RecordCollector) -> None:
    """恢复 Chat Client logger 的原始配置。"""

    target_logger = logging.getLogger("companion.llm.chat_client")
    target_logger.removeHandler(collector)
    target_logger.setLevel(collector.original_level)  # type: ignore[attr-defined]
    target_logger.propagate = collector.original_propagate  # type: ignore[attr-defined]
