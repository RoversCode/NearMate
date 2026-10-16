"""验证可见 Self-Reflection Recovery Turn 的输出边界。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.tests.foreground_processor_factory import build_foreground_processor_for_test
from companion.character_cards.store import CharacterCardStore
from companion.foreground_self_reflection.contracts import SelfReflectionRecoveryContext
from companion.foreground_self_reflection.models import (
    SelfReflectionOutcomeFact,
    SelfReflectionOutcomeKind,
    SelfReflectionToolFact,
    SelfReflectionToolStatus,
)
from companion.foreground_self_reflection.prompt import RecoveryPromptFacts
from companion.llm.call_trace import ForegroundRequestTrace
from companion.llm.foreground_models import ForegroundModelStreamChunk, ForegroundToolCall
from companion.llm.request_intent import LlmInvocationIntent
from companion.prompt.builder import PromptBuilder
from companion.session.context import SessionContext


CHARACTER_CARD = CharacterCardStore(
    Path(__file__).resolve().parents[2] / "data" / "character-cards"
).load_card("xiaolian")


class RecordingRecoveryClient:
    """记录 Recovery 的每次模型调用，并返回指定流。"""

    def __init__(self, streams: list[list[ForegroundModelStreamChunk]]) -> None:
        self.streams = streams
        self.requests: list[dict[str, Any]] = []

    def stream_message(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        """保存本次调用参数，并逐项返回预先配置的模型分片。"""

        self.requests.append(dict(kwargs))
        yield from self.streams[len(self.requests) - 1]


class StartedBackgroundExecutor:
    """返回后台执行器已经真实接管的 canonical 事实。"""

    def execute_tool(self, **kwargs: object) -> SelfReflectionOutcomeFact:
        """把工具调用转换成只含真实 started 状态的结果。"""

        context = kwargs["context"]
        assert isinstance(context, SelfReflectionRecoveryContext)
        return SelfReflectionOutcomeFact(
            self_reflection_id=context.self_reflection_id,
            source_activity_id=context.source_activity_id,
            kind=SelfReflectionOutcomeKind.ACTIVITY_STARTED,
            tool_fact=SelfReflectionToolFact(
                tool_name="start_background_work",
                status=SelfReflectionToolStatus.STARTED,
                safe_result="后台执行器已经接管这项工作。",
                activity_id="background-recovery-1",
            ),
        )


def recovery_context() -> SelfReflectionRecoveryContext:
    """构造带原始请求、失败事实和工具能力的冻结 Recovery 上下文。"""

    return SelfReflectionRecoveryContext(
        request_id="self-reflection-recovery-reflection-visible",
        self_reflection_id="reflection-visible",
        episode_id="episode-visible",
        runtime_epoch=1,
        source_activity_id="cognitive-visible",
        source_request_id="request-source",
        source_protocol_transaction_id="transaction-source",
        deadline=45.0,
        prompt_facts=RecoveryPromptFacts(
            original_user_request="把会议材料整理成文档。",
            previous_method_capability="只做分析",
            failure_explanation="旧方法不能生成并交付文件。",
            tools=(),
        ),
        fallback_text="我重新梳理时出了点问题，暂时没有处理好。",
    )


def run_recovery(processor: object, context: SelfReflectionRecoveryContext) -> list[object]:
    """执行一次可见 Recovery，并传入稳定请求与 generation 身份。"""

    return list(
        processor.stream_user_text(  # type: ignore[attr-defined]
            context.prompt_facts.original_user_request,
            now=1.0,
            interaction_session_id="interaction-recovery",
            character_card=CHARACTER_CARD,
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            request_mode="self_reflection_recovery",
            request_trace=ForegroundRequestTrace(
                request_id="request-recovery",
                generation_id="generation-recovery",
                request_source="self_reflection_recovery",
            ),
            self_reflection_recovery_context=context,
        )
    )


def test_recovery_first_text_is_visible_and_high_thinking_is_not_text() -> None:
    """首轮正文立即流式公开，reasoning 只由临时状态事件覆盖。"""

    client = RecordingRecoveryClient(
        [[ForegroundModelStreamChunk(kind="text_delta", text="我重新梳理一下。")]]
    )
    processor = build_foreground_processor_for_test(
        client=client,
        prompt_builder=PromptBuilder(),
        session_context=SessionContext(),
    )
    events = run_recovery(processor, recovery_context())

    assert events[0].type == "assistant.self_reflection_status_changed"
    assert events[0].payload["active"] is True
    first_text_index = next(i for i, event in enumerate(events) if event.type == "assistant.text_chunk")
    assert events[first_text_index].payload["text"] == "我重新梳理一下。"
    assert events[first_text_index - 1].type == "assistant.self_reflection_status_changed"
    # 直接运行 ForegroundTurnProcessor 时只会产生进入思考状态事件；首段正文
    # 的 active=false 交接由 GenerationSupervisor 在真实输出 lane 原子发送。
    assert events[first_text_index - 1].payload["active"] is True
    assert events[-1].type == "assistant.text_final"
    assert client.requests[0]["intent"] == LlmInvocationIntent(
        tool_mode="auto",
        allow_parallel_tools=False,
        thinking_level="high",
    )


def test_recovery_prompt_uses_natural_language_contract() -> None:
    """Recovery 使用自然语言协议，不再要求模型输出 JSON 决策标签。"""

    client = RecordingRecoveryClient(
        [[ForegroundModelStreamChunk(kind="text_delta", text="可以直接回答。")]]
    )
    processor = build_foreground_processor_for_test(
        client=client,
        prompt_builder=PromptBuilder(),
        session_context=SessionContext(),
    )
    run_recovery(processor, recovery_context())

    prompt = "\n".join(str(message.get("content") or "") for message in client.requests[0]["messages"])
    assert "不要输出内部判断标签或协议字段" in prompt
    assert "no_action_needed" not in prompt
    assert "clarification_required" not in prompt
    assert "会议材料整理成文档" in prompt


def test_recovery_tool_followup_turns_thinking_off_and_explains_canonical_started() -> None:
    """工具事实确认后，二轮关闭 Thinking 并说明旧法、新法和 started 边界。"""

    client = RecordingRecoveryClient(
        [
            [
                ForegroundModelStreamChunk(kind="tool_call_started"),
                ForegroundModelStreamChunk(
                    kind="tool_calls",
                    tool_calls=[
                        ForegroundToolCall(
                            tool_call_id="tool-call-1",
                            name="start_background_work",
                            arguments={
                                "instruction": "整理材料",
                                "deliverable": True,
                                "include_current_scene": False,
                            },
                        )
                    ],
                ),
            ],
            [ForegroundModelStreamChunk(kind="text_delta", text="我想清楚了，已经改用后台继续处理。")],
        ]
    )
    processor = build_foreground_processor_for_test(
        client=client,
        prompt_builder=PromptBuilder(),
        session_context=SessionContext(),
        self_reflection_tool_executor=StartedBackgroundExecutor(),
    )
    events = run_recovery(processor, recovery_context())

    assert len(client.requests) == 2
    assert client.requests[1]["tools"] == []
    assert client.requests[1]["intent"] == LlmInvocationIntent.text(thinking_level="off")
    followup_messages = client.requests[1]["messages"]
    followup_prompt = "\n".join(str(message.get("content") or "") for message in followup_messages)
    assert "<previous_method_capability>" in followup_prompt
    assert "只做分析" in followup_prompt
    assert "started" in followup_prompt
    assert "并不表示已经完成" in followup_prompt
    # 首轮没有公开正文时，闭环轮不能虚构 assistant 前缀消息。
    assert not any(
        message.get("role") == "assistant" and message.get("content")
        for message in followup_messages
    )
    assert any(event.type == "foreground.self_reflection_outcome" for event in events)
    assert events[-1].type == "assistant.text_final"
