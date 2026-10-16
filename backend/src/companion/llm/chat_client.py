from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from openai import OpenAI

from companion.llm.call_trace import ActivityLlmCallTrace, LlmCallTrace
from companion.llm.config import (
    LLM_SDK_MAX_RETRIES,
    LlmRuntimeConfig,
)
from companion.llm.foreground_models import (
    ForegroundModelStreamChunk,
    ForegroundToolCall,
    LlmTextStreamChunk,
)
from companion.llm.model_registry import ModelIntegrationSpec
from companion.llm.output_policies import LlmProtocolLeakError, OutputPolicy
from companion.llm.request_adapters import RequestAdapter
from companion.llm.request_intent import LlmInvocationIntent
from companion.llm.stream_completion import StreamCompletionTracker
from companion.llm.stream_lifecycle import (
    iter_cancellable_stream,
    resolve_cancellation_reason,
)
from companion.llm.tool_protocol import validate_tool_protocol_messages
from companion.observability.logging_config import get_backend_logger
from companion.observability.prompt_audit import PromptAuditRecorder
from companion.observability.structured_logging import log_event


logger = get_backend_logger(__name__)


@dataclass(slots=True)
class _ToolCallAccumulator:
    """暂存单个 Chat 工具调用在流式响应里的增量字段。"""

    tool_call_id: str = ""
    name: str = ""
    arguments: str = ""


class ChatCompletionsClient:
    """负责 Chat Completions 传输、流解析和统一观测。"""

    def __init__(
        self,
        *,
        runtime_config: LlmRuntimeConfig,
        integration: ModelIntegrationSpec,
        request_adapter: RequestAdapter,
        output_policy: OutputPolicy,
        role_name: str,
        prompt_audit_recorder: PromptAuditRecorder | None = None,
        openai_client: Any | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        """创建绑定到一个已解析职责的 Chat 客户端。

        Args:
            runtime_config: 已完成凭据与模型解析的运行时配置。
            integration: 当前职责绑定的模型接入规格。
            request_adapter: 把业务调用意图编译成 SDK 参数的 Adapter。
            output_policy: 正文对外释放前执行的厂商输出门禁。
            role_name: 当前客户端服务的 LLM 职责。
            prompt_audit_recorder: 可选本地 Prompt 审查记录器。
            openai_client: 测试可注入的 OpenAI 兼容客户端。
            now: 测试可注入的单调时间函数。
        Raises:
            ValueError: 运行时配置与模型接入规格不一致。
        """

        _validate_runtime_binding(
            runtime_config=runtime_config,
            integration=integration,
        )
        self.runtime_config = runtime_config
        self.integration = integration
        self.role_name = role_name
        self.model = runtime_config.model
        self._request_adapter = request_adapter
        self._output_policy = output_policy
        self._prompt_audit_recorder = prompt_audit_recorder
        client_kwargs: dict[str, Any] = {
            "base_url": runtime_config.base_url,
            "api_key": runtime_config.api_key,
            "max_retries": LLM_SDK_MAX_RETRIES,
        }
        if runtime_config.timeout_s is not None:
            client_kwargs["timeout"] = runtime_config.timeout_s
        self._client = (
            openai_client
            if openai_client is not None
            else OpenAI(**client_kwargs)
        )
        self._warmup_done = False
        self._last_activity_at: float | None = None
        self._now = now or time.monotonic

    def warmup(self) -> None:
        """用一条最小请求预热连接与模型路径。"""

        if self._warmup_done:
            return
        self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": "ping"}],
            stream=False,
            max_tokens=1,
        )
        self._warmup_done = True
        self._last_activity_at = self._now()

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
        """流式返回正文和完整函数工具调用。

        Args:
            messages: 本次请求的完整 Chat 消息。
            tools: 当前业务允许使用的标准函数工具。
            intent: 不包含 SDK 字段的业务调用意图。
            audit_source: 触发本次调用的业务来源。
            audit_operation: 当前调用阶段。
            trace: 可选调用追踪身份。
            cancellation_event: 可选协作取消信号。

        Returns:
            按到达顺序生成的标准前台片段。
        """

        trace_fields = (
            trace.log_fields(protocol="chat_completions")
            if trace is not None
            else {"protocol": "chat_completions"}
        )
        if cancellation_event is not None and cancellation_event.is_set():
            log_event(
                logger,
                logging.INFO,
                "llm_call_skipped",
                **trace_fields,
                reason="generation_cancelled",
                phase="before_open",
            )
            return

        request_kwargs = self._request_kwargs(
            messages=messages,
            tools=tools,
            intent=intent,
            stream=True,
        )
        stream, start_time, audit_path = self._open_stream(
            request_kwargs=request_kwargs,
            trace_fields=trace_fields,
            audit_source=audit_source,
            audit_operation=audit_operation,
        )
        if cancellation_event is not None and cancellation_event.is_set():
            # SDK 建流和取消信号存在竞态；统一关闭器会立即回收刚建立的流。
            for _chunk in iter_cancellable_stream(stream, cancellation_event):
                pass
            self._log_stream_result(
                trace_fields=trace_fields,
                start_time=start_time,
                cancelled=True,
                cancellation_reason="generation_cancelled_after_open",
            )
            return

        tool_call_accumulators: dict[int, _ToolCallAccumulator] = {}
        buffered_text_chunks: list[str] = []
        should_buffer = self._output_policy.requires_buffering(intent)
        structured_tool_call_seen = False
        tool_call_started_emitted = False
        first_event_logged = False
        first_text_logged = False
        stream_opened_at = time.monotonic()
        stream_failed = False
        stream_closed_early = False
        completion_tracker = StreamCompletionTracker("chat_completions")
        audit_text_chunks: list[str] | None = [] if audit_path is not None else None
        model_result_audited = False
        try:
            for chunk in iter_cancellable_stream(stream, cancellation_event):
                completion_tracker.observe_chat_chunk(chunk)
                if not first_event_logged:
                    log_event(
                        logger,
                        logging.INFO,
                        "llm_first_event",
                        **trace_fields,
                        elapsed_ms=round(
                            (time.monotonic() - stream_opened_at) * 1000,
                            4,
                        ),
                    )
                    first_event_logged = True
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = choices[0].delta
                if delta is None:
                    continue

                content = getattr(delta, "content", None)
                if content:
                    if not first_text_logged:
                        log_event(
                            logger,
                            logging.INFO,
                            "llm_first_text",
                            **trace_fields,
                            elapsed_ms=round(
                                (time.monotonic() - stream_opened_at) * 1000,
                                4,
                            ),
                        )
                        first_text_logged = True
                    text = str(content)
                    if audit_text_chunks is not None:
                        audit_text_chunks.append(text)
                    if should_buffer:
                        buffered_text_chunks.append(text)
                    else:
                        yield ForegroundModelStreamChunk(
                            kind="text_delta",
                            text=text,
                        )

                tool_call_deltas = getattr(delta, "tool_calls", None)
                if not tool_call_deltas:
                    continue
                structured_tool_call_seen = True
                if not tool_call_started_emitted:
                    # 需要完整输出门禁时，任何调用信号也必须等校验通过。
                    if not should_buffer:
                        yield ForegroundModelStreamChunk(kind="tool_call_started")
                    tool_call_started_emitted = True
                _accumulate_tool_calls(
                    tool_call_accumulators,
                    tool_call_deltas,
                )

            if cancellation_event is not None and cancellation_event.is_set():
                return
            completion_tracker.require_completed()
            complete_text = (
                "".join(audit_text_chunks)
                if audit_text_chunks is not None
                else "".join(buffered_text_chunks)
            )
            audit_tool_calls = _audit_tool_calls_from_accumulators(
                tool_call_accumulators
            )
            recorder = self._prompt_audit_recorder
            if recorder is not None:
                recorder.record_model_result(
                    audit_path=audit_path,
                    content_text=complete_text,
                    tool_calls=audit_tool_calls,
                )
                model_result_audited = True
            self._output_policy.validate_before_release(
                intent=intent,
                text=complete_text,
                has_structured_tool_calls=structured_tool_call_seen,
            )
            tool_calls = _build_tool_calls(tool_call_accumulators)
            if intent.tool_mode == "required":
                if not any(
                    tool_call.name == intent.required_tool_name
                    for tool_call in tool_calls
                ):
                    raise RuntimeError(
                        "LLM 没有调用职责合同要求的探测工具："
                        f"{intent.required_tool_name}"
                    )

            if should_buffer:
                for text in buffered_text_chunks:
                    yield ForegroundModelStreamChunk(
                        kind="text_delta",
                        text=text,
                    )
                if tool_calls:
                    yield ForegroundModelStreamChunk(kind="tool_call_started")
            if tool_calls:
                yield ForegroundModelStreamChunk(
                    kind="tool_calls",
                    tool_calls=tool_calls,
                )
        except GeneratorExit:
            stream_closed_early = True
            raise
        except Exception as exception:
            stream_failed = True
            recorder = self._prompt_audit_recorder
            if recorder is not None and not model_result_audited:
                recorder.record_model_result(
                    audit_path=audit_path,
                    content_text=(
                        "".join(audit_text_chunks)
                        if audit_text_chunks is not None
                        else "".join(buffered_text_chunks)
                    ),
                    tool_calls=_audit_tool_calls_from_accumulators(
                        tool_call_accumulators
                    ),
                    error=exception,
                )
            self._log_stream_failure(
                exception=exception,
                trace_fields=trace_fields,
                start_time=start_time,
            )
            raise
        finally:
            if not stream_failed:
                cancelled = (
                    stream_closed_early
                    or cancellation_event is not None
                    and cancellation_event.is_set()
                )
                self._log_stream_result(
                    trace_fields=trace_fields,
                    start_time=start_time,
                    cancelled=cancelled,
                    cancellation_reason=(
                        "consumer_closed"
                        if stream_closed_early
                        else resolve_cancellation_reason(
                            cancellation_event,
                            fallback="generation_cancelled",
                        )
                        if cancelled
                        else None
                    ),
                )

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
        """流式返回思考、正文和 Token 用量。

        Args:
            messages: 本次请求的完整 Chat 消息。
            intent: 不包含 SDK 字段的业务调用意图。
            audit_source: 触发本次调用的业务来源。
            audit_operation: 当前调用阶段。
            audit_messages_factory: 仅在审查开启时生成脱敏消息副本的惰性工厂。
            trace: 可选 Activity 调用追踪身份。
            cancellation_event: 可选协作取消信号。

        Returns:
            按到达顺序生成的普通文本流片段。
        """

        trace_fields = (
            trace.log_fields(protocol="chat_completions")
            if trace is not None
            else {"protocol": "chat_completions"}
        )
        trace_fields["role"] = self.role_name
        if cancellation_event is not None and cancellation_event.is_set():
            log_event(
                logger,
                logging.INFO,
                "llm_call_skipped",
                **trace_fields,
                reason="activity_cancelled",
                phase="before_open",
            )
            return

        request_kwargs = self._request_kwargs(
            messages=messages,
            tools=[],
            intent=intent,
            stream=True,
        )
        stream, start_time, audit_path = self._open_stream(
            request_kwargs=request_kwargs,
            trace_fields=trace_fields,
            audit_source=audit_source,
            audit_operation=audit_operation,
            audit_messages_factory=audit_messages_factory,
        )
        buffered_chunks: list[LlmTextStreamChunk] = []
        buffered_text: list[str] = []
        should_buffer = self._output_policy.requires_buffering(intent)
        stream_failed = False
        stream_closed_early = False
        completion_tracker = StreamCompletionTracker("chat_completions")
        audit_text_chunks: list[str] | None = [] if audit_path is not None else None
        model_result_audited = False
        try:
            for chunk in iter_cancellable_stream(stream, cancellation_event):
                completion_tracker.observe_chat_chunk(chunk)
                choices = getattr(chunk, "choices", None)
                if choices:
                    delta = choices[0].delta
                    if delta is not None:
                        reasoning_content = getattr(
                            delta,
                            "reasoning_content",
                            None,
                        )
                        if reasoning_content:
                            reasoning_chunk = LlmTextStreamChunk(
                                kind="reasoning_delta",
                                text=str(reasoning_content),
                            )
                            if should_buffer:
                                buffered_chunks.append(reasoning_chunk)
                            else:
                                yield reasoning_chunk
                        content = getattr(delta, "content", None)
                        if content:
                            text = str(content)
                            buffered_text.append(text)
                            if audit_text_chunks is not None:
                                audit_text_chunks.append(text)
                            text_chunk = LlmTextStreamChunk(
                                kind="text_delta",
                                text=text,
                            )
                            if should_buffer:
                                buffered_chunks.append(text_chunk)
                            else:
                                yield text_chunk

                usage_chunk = _usage_chunk_from_object(
                    getattr(chunk, "usage", None)
                )
                if usage_chunk is not None:
                    if should_buffer:
                        buffered_chunks.append(usage_chunk)
                    else:
                        yield usage_chunk

            if cancellation_event is not None and cancellation_event.is_set():
                return
            completion_tracker.require_completed()
            recorder = self._prompt_audit_recorder
            if recorder is not None:
                recorder.record_model_result(
                    audit_path=audit_path,
                    content_text=(
                        "".join(audit_text_chunks)
                        if audit_text_chunks is not None
                        else "".join(buffered_text)
                    ),
                    tool_calls=(),
                )
                model_result_audited = True
            self._output_policy.validate_before_release(
                intent=intent,
                text="".join(buffered_text),
                has_structured_tool_calls=False,
            )
            yield from buffered_chunks
        except GeneratorExit:
            stream_closed_early = True
            raise
        except Exception as exception:
            stream_failed = True
            recorder = self._prompt_audit_recorder
            if recorder is not None and not model_result_audited:
                recorder.record_model_result(
                    audit_path=audit_path,
                    content_text=(
                        "".join(audit_text_chunks)
                        if audit_text_chunks is not None
                        else "".join(buffered_text)
                    ),
                    tool_calls=(),
                    error=exception,
                )
            self._log_stream_failure(
                exception=exception,
                trace_fields=trace_fields,
                start_time=start_time,
            )
            raise
        finally:
            if not stream_failed:
                cancelled = (
                    stream_closed_early
                    or cancellation_event is not None
                    and cancellation_event.is_set()
                )
                self._log_stream_result(
                    trace_fields=trace_fields,
                    start_time=start_time,
                    cancelled=cancelled,
                    cancellation_reason=(
                        "consumer_closed"
                        if stream_closed_early
                        else resolve_cancellation_reason(
                            cancellation_event,
                            fallback="activity_cancelled",
                        )
                        if cancelled
                        else None
                    ),
                )

    def complete_text(
        self,
        *,
        messages: list[dict[str, Any]],
        intent: LlmInvocationIntent,
        audit_source: str,
        audit_operation: str,
    ) -> str:
        """执行一次非流式 Chat 请求并返回非空正文。

        Args:
            messages: 本次请求的完整 Chat 消息。
            intent: 不包含 SDK 字段的业务调用意图。
            audit_source: 触发本次调用的业务来源。
            audit_operation: 当前调用阶段。
        Returns:
            通过 Output Policy 的非空正文。

        Raises:
            RuntimeError: 厂商返回空响应或输出违反协议合同。
        """

        request_kwargs = self._request_kwargs(
            messages=messages,
            tools=[],
            intent=intent,
            stream=False,
        )
        response, audit_path = self._create_completion(
            request_kwargs=request_kwargs,
            trace_fields={"protocol": "chat_completions"},
            source=audit_source,
            operation=audit_operation,
        )
        self._last_activity_at = self._now()
        content = ""
        audit_tool_calls: object = ()
        model_result_audited = False
        try:
            choices = getattr(response, "choices", None)
            if not choices:
                raise RuntimeError("LLM 文本生成返回空响应")
            message = getattr(choices[0], "message", None)
            raw_content = getattr(message, "content", None)
            if isinstance(raw_content, str):
                content = raw_content
            audit_tool_calls = _audit_tool_calls_from_message(message)
            if not content.strip():
                raise RuntimeError("LLM 文本生成返回空正文")
            recorder = self._prompt_audit_recorder
            if recorder is not None:
                recorder.record_model_result(
                    audit_path=audit_path,
                    content_text=content,
                    tool_calls=audit_tool_calls,
                )
                model_result_audited = True
            self._output_policy.validate_before_release(
                intent=intent,
                text=content,
                has_structured_tool_calls=bool(
                    getattr(message, "tool_calls", None)
                ),
            )
            return content
        except Exception as exception:
            recorder = self._prompt_audit_recorder
            if recorder is not None and not model_result_audited:
                recorder.record_model_result(
                    audit_path=audit_path,
                    content_text=content,
                    tool_calls=audit_tool_calls,
                    error=exception,
                )
            raise

    def close(self) -> None:
        """显式关闭 OpenAI SDK 客户端持有的网络资源。"""

        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _request_kwargs(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        intent: LlmInvocationIntent,
        stream: bool,
    ) -> dict[str, Any]:
        """构造最终发送给 Chat Completions SDK 的参数。

        Args:
            messages: 本次调用的完整 Chat 消息。
            tools: 标准函数工具声明。
            intent: 业务调用意图。
            stream: 是否请求流式响应。

        Returns:
            只包含当前接入规格允许字段的独立请求字典。
        """

        validate_tool_protocol_messages(messages)
        request_kwargs = self._request_adapter.compile(
            intent=intent,
            tools=tools,
        )
        request_kwargs.update(
            {
                "model": self.model,
                "messages": messages,
                "stream": stream,
            }
        )
        return request_kwargs

    def _create_completion(
        self,
        *,
        request_kwargs: dict[str, Any],
        source: str,
        operation: str,
        trace_fields: dict[str, object],
        audit_messages_factory: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> tuple[Any, Path | None]:
        """在需要时把最终请求交给审计临界区，并打开一次 SDK 请求。

        Args:
            request_kwargs: Adapter 已编译完成的真实 SDK 参数。
            source: 触发本次调用的业务来源。
            operation: 当前调用阶段。
            trace_fields: 请求或 Activity 关联身份。
            audit_messages_factory: 仅在审查开启时生成脱敏消息副本的惰性工厂。

        Returns:
            SDK `create` 返回的流或响应对象，以及同一调用的审计文件路径。
        """

        recorder = self._prompt_audit_recorder
        if recorder is None or not recorder.enabled:
            # 关闭态不复制请求、不求值工厂，也不获取审计锁。
            return self._client.chat.completions.create(**request_kwargs), None
        return recorder.record_request_and_call(
            role=self.role_name,
            source=source,
            protocol="chat_completions",
            model=self.model,
            operation=operation,
            request=request_kwargs,
            trace_fields=trace_fields,
            audit_messages_factory=audit_messages_factory,
            sdk_call=lambda: self._client.chat.completions.create(**request_kwargs),
        )

    def _open_stream(
        self,
        *,
        request_kwargs: dict[str, Any],
        trace_fields: dict[str, object],
        audit_source: str,
        audit_operation: str,
        audit_messages_factory: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> tuple[Any, float, Path | None]:
        """打开流并记录统一开始、失败和打开日志。

        Args:
            request_kwargs: 已编译的完整 SDK 参数。
            trace_fields: 当前调用的结构化追踪字段。
            audit_source: 触发本次调用的业务来源。
            audit_operation: 当前调用阶段。
            audit_messages_factory: 仅在审查开启时生成脱敏消息的惰性工厂。

        Returns:
            SDK 流对象、开始时刻和同一调用的审计文件路径。
        """

        log_event(logger, logging.INFO, "llm_call_started", **trace_fields)
        start_time = time.monotonic()
        try:
            stream, audit_path = self._create_completion(
                request_kwargs=request_kwargs,
                trace_fields=trace_fields,
                source=audit_source,
                operation=audit_operation,
                audit_messages_factory=audit_messages_factory,
            )
        except Exception as exception:
            log_event(
                logger,
                logging.ERROR,
                "llm_call_failed",
                **trace_fields,
                phase="open",
                elapsed_ms=round(
                    (time.monotonic() - start_time) * 1000,
                    4,
                ),
                error_type=type(exception).__name__,
            )
            raise
        self._last_activity_at = self._now()
        log_event(
            logger,
            logging.INFO,
            "llm_stream_opened",
            **trace_fields,
            elapsed_ms=round((time.monotonic() - start_time) * 1000, 4),
        )
        return stream, start_time, audit_path

    def _log_stream_failure(
        self,
        *,
        exception: Exception,
        trace_fields: dict[str, object],
        start_time: float,
    ) -> None:
        """记录流消费失败和可识别的厂商协议泄漏。

        Args:
            exception: 导致流失败的异常。
            trace_fields: 当前调用的结构化追踪字段。
            start_time: 本次调用开始的单调时刻。
        """

        if isinstance(exception, LlmProtocolLeakError):
            log_event(
                logger,
                logging.WARNING,
                "llm_provider_protocol_leak",
                request_id=trace_fields.get("request_id"),
                generation_id=trace_fields.get("generation_id"),
                provider_id=exception.provider_id,
                model=self.model,
                protocol=exception.protocol,
            )
        log_event(
            logger,
            logging.ERROR,
            "llm_call_failed",
            **trace_fields,
            phase="stream",
            elapsed_ms=round((time.monotonic() - start_time) * 1000, 4),
            error_type=type(exception).__name__,
        )

    def _log_stream_result(
        self,
        *,
        trace_fields: dict[str, object],
        start_time: float,
        cancelled: bool,
        cancellation_reason: str | None,
    ) -> None:
        """记录流的正常完成或协作取消。

        Args:
            trace_fields: 当前调用的结构化追踪字段。
            start_time: 本次调用开始的单调时刻。
            cancelled: 当前流是否因取消或消费者关闭结束。
            cancellation_reason: 取消原因；正常完成时为空。
        """

        log_event(
            logger,
            logging.INFO,
            "llm_call_cancelled" if cancelled else "llm_call_completed",
            **trace_fields,
            elapsed_ms=round((time.monotonic() - start_time) * 1000, 4),
            cancellation_reason=cancellation_reason,
        )

def _validate_runtime_binding(
    *,
    runtime_config: LlmRuntimeConfig,
    integration: ModelIntegrationSpec,
) -> None:
    """保证 Factory 没有把凭据和模型规格交叉接错。

    Args:
        runtime_config: 已解析的运行时配置。
        integration: 当前绑定的模型接入规格。

    Raises:
        ValueError: 接入编号、厂商或模型不一致。
    """

    if runtime_config.integration_id != integration.integration_id:
        raise ValueError("LLM 运行时配置与模型接入规格编号不一致")
    if runtime_config.provider_id != integration.provider_id:
        raise ValueError("LLM 运行时配置与模型接入规格厂商不一致")
    if runtime_config.model != integration.model:
        raise ValueError("LLM 运行时配置与模型接入规格模型不一致")


def _accumulate_tool_calls(
    accumulators: dict[int, _ToolCallAccumulator],
    tool_call_deltas: Any,
) -> None:
    """把一批流式工具增量合并到对应编号。

    Args:
        accumulators: 按流式索引保存的工具调用中间状态。
        tool_call_deltas: OpenAI SDK 返回的工具调用增量列表。
    """

    for tool_call_delta in tool_call_deltas:
        index = getattr(tool_call_delta, "index", None)
        if index is None:
            continue
        accumulator = accumulators.setdefault(index, _ToolCallAccumulator())
        tool_call_id = getattr(tool_call_delta, "id", None)
        if tool_call_id:
            accumulator.tool_call_id = tool_call_id
        function_delta = getattr(tool_call_delta, "function", None)
        if function_delta is None:
            continue
        name = getattr(function_delta, "name", None)
        if name:
            accumulator.name = name
        arguments = getattr(function_delta, "arguments", None)
        if arguments:
            accumulator.arguments += arguments


def _build_tool_calls(
    accumulators: dict[int, _ToolCallAccumulator],
) -> list[ForegroundToolCall]:
    """把完整工具增量转换成项目标准工具调用。

    Args:
        accumulators: 按流式索引保存的工具调用中间状态。

    Returns:
        只包含身份和名称完整的工具调用列表。
    """

    return [
        ForegroundToolCall(
            tool_call_id=accumulator.tool_call_id,
            name=accumulator.name,
            arguments=_parse_tool_arguments(accumulator.arguments),
        )
        for _, accumulator in sorted(accumulators.items())
        if accumulator.tool_call_id and accumulator.name
    ]


def _audit_tool_calls_from_accumulators(
    accumulators: dict[int, _ToolCallAccumulator],
) -> list[dict[str, object]]:
    """把流式工具调用保留为 Prompt Audit 可脱敏的原始结构。

    Args:
        accumulators: 按流式索引保存的工具调用中间状态。

    Returns:
        保留原始参数字符串的 Chat 工具调用列表。
    """

    return [
        {
            "id": accumulator.tool_call_id,
            "type": "function",
            "function": {
                "name": accumulator.name,
                "arguments": accumulator.arguments,
            },
        }
        for _, accumulator in sorted(accumulators.items())
        if accumulator.tool_call_id or accumulator.name or accumulator.arguments
    ]


def _audit_tool_calls_from_message(message: object) -> list[dict[str, object]]:
    """从非流式 SDK 消息中提取可脱敏的工具调用。

    Args:
        message: OpenAI 兼容 SDK 返回的消息对象。

    Returns:
        只包含调用身份、类型、函数名和原始参数的普通字典列表。
    """

    raw_tool_calls = _field(message, "tool_calls")
    if not isinstance(raw_tool_calls, (list, tuple)):
        return []
    audit_tool_calls: list[dict[str, object]] = []
    for raw_tool_call in raw_tool_calls:
        raw_function = _field(raw_tool_call, "function")
        audit_tool_calls.append(
            {
                "id": _field(raw_tool_call, "id") or "",
                "type": _field(raw_tool_call, "type") or "function",
                "function": {
                    "name": _field(raw_function, "name") or "",
                    "arguments": _field(raw_function, "arguments") or "",
                },
            }
        )
    return audit_tool_calls


def _parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    """解析模型流式拼接后的工具参数。

    Args:
        raw_arguments: 工具参数 JSON 文本。

    Returns:
        JSON 对象；空白、损坏或非对象输入返回空字典。
    """

    if not raw_arguments.strip():
        return {}
    try:
        parsed_arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {}
    return parsed_arguments if isinstance(parsed_arguments, dict) else {}


def _usage_chunk_from_object(usage: Any) -> LlmTextStreamChunk | None:
    """从 OpenAI 兼容 usage 对象宽松提取 Token 统计。

    Args:
        usage: SDK 返回的 usage 对象或字典。

    Returns:
        至少包含一种 Token 数时返回用量片段，否则返回空。
    """

    if usage is None:
        return None
    output_tokens = _optional_int_field(usage, "completion_tokens")
    if output_tokens is None:
        output_tokens = _optional_int_field(usage, "output_tokens")
    details = _field(usage, "completion_tokens_details")
    if details is None:
        details = _field(usage, "output_tokens_details")
    reasoning_tokens = _optional_int_field(details, "reasoning_tokens")
    if output_tokens is None and reasoning_tokens is None:
        return None
    return LlmTextStreamChunk(
        kind="usage",
        reasoning_tokens=reasoning_tokens,
        output_tokens=output_tokens,
    )


def _optional_int_field(value: Any, field_name: str) -> int | None:
    """按对象属性或字典键读取可选整数。

    Args:
        value: SDK 对象或字典。
        field_name: 需要读取的字段名。

    Returns:
        字段是整数时返回该值，否则返回空。
    """

    raw_value = _field(value, field_name)
    return raw_value if isinstance(raw_value, int) else None


def _field(value: Any, field_name: str) -> Any:
    """按对象属性或字典键读取一个未知字段。

    Args:
        value: SDK 对象、字典或空值。
        field_name: 需要读取的字段名。

    Returns:
        字段值；对象为空或字段不存在时返回空。
    """

    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(field_name)
    return getattr(value, field_name, None)
