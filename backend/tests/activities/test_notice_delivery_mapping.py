from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.activities.notice.delivery_event_mapper import map_notice_delivery_event
from companion.character_cards.models import CharacterCardDefinition
from companion.llm.foreground_models import ForegroundEvent
from companion.realtime.assistant_events import AssistantRequest
from companion.realtime.assistant_generation import AssistantRequestWorkItem
from companion.realtime.assistant_output_lanes import AssistantOutputLanes

from backend.tests.activities.notice_delivery_test_support import (
    RecordingCoordinator,
    _apply_delivery_event,
)


def test_non_empty_text_final_confirms_llm_output_completed() -> None:
    """非空 final 表示 LLM 已正常输出完毕，不依赖后续交付结果。"""

    coordinator = RecordingCoordinator()
    _apply_delivery_event(coordinator,
        "assistant.text_final",
        {"request_id": "request-a", "text": "已经处理好了。"},
    )

    assert coordinator.llm_completed == ["request-a"]
    assert coordinator.cancelled == []


def test_empty_text_final_rolls_strong_notice_back_to_pending() -> None:
    """空文本终态没有完成表达，必须按取消路径回滚强提醒。"""

    coordinator = RecordingCoordinator()
    _apply_delivery_event(coordinator,
        "assistant.text_final",
        {"request_id": "request-a", "text": "  "},
    )

    assert coordinator.llm_completed == []
    assert coordinator.cancelled == ["request-a"]


def test_interrupted_text_final_uses_cancellation_compensation() -> None:
    """打断时提交的可见片段不是正常 LLM 终点，必须进入取消补偿。"""

    coordinator = RecordingCoordinator()
    _apply_delivery_event(
        coordinator,
        "assistant.text_final",
        {
            "request_id": "request-a",
            "text": "已经生成的片段……",
            "interrupted": True,
        },
    )

    assert coordinator.llm_completed == []
    assert coordinator.cancelled == ["request-a"]


@pytest.mark.parametrize(
    ("reason", "expected_text"),
    [
        (
            "activity_notice_superseded",
            "我正在处理这项工作。【状态已更新，此回复已中断】",
        ),
        (
            "user_barge_in",
            "我正在处理这项工作。【user打断了此次的回复】",
        ),
    ],
)
def test_interrupted_visible_text_uses_reason_specific_chat_suffix(
    reason: str,
    expected_text: str,
) -> None:
    """状态变化和用户插话必须使用不同尾注，且尾注只能写入聊天。"""

    class InterruptedResponseAgent:
        """只负责把中断结算文本包装成前台事件。"""

        def create_interrupted_response_event(
            self,
            display_text: str,
            *,
            model_text: str,
            request_id: str,
            history_policy: str,
            progress_references: tuple[object, ...],
            interruption_reason: str = "",
        ) -> ForegroundEvent:
            """创建不带额外副作用的中断 final。"""

            _ = history_policy, progress_references, interruption_reason
            return ForegroundEvent(
                type="assistant.text_final",
                payload={
                    "text": display_text,
                    "settlement_text": model_text,
                    "interrupted": True,
                    "owner_request_id": request_id,
                },
            )

    runner = AssistantOutputLanes(
        agent=InterruptedResponseAgent(),  # type: ignore[arg-type]
        tts_provider=None,  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-a",
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="汇报提醒",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)
    work_item.context.visible_parts.append("我正在处理这项工作。")
    work_item.context.cancellation_event.cancel(reason)

    runner._emit_interrupted_text_final(work_item.context)  # type: ignore[attr-defined]

    final_event = runner.event_queue.get_nowait()
    assert final_event.type == "assistant.text_final"
    assert final_event.payload["text"] == expected_text
    assert final_event.payload["owner_request_id"] == "request-a"
    assert runner._tts_queue.empty()  # type: ignore[attr-defined]


def test_superseded_notice_without_visible_text_has_no_empty_final() -> None:
    """System Notice 尚未出字就失效时，应静默结束而不是生成空气泡。"""

    runner = AssistantOutputLanes(
        agent=cast(Any, SimpleNamespace()),
        tts_provider=None,  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-a",
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="汇报提醒",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)
    work_item.context.cancellation_event.cancel("activity_notice_superseded")

    runner._emit_interrupted_text_final(work_item.context)  # type: ignore[attr-defined]

    assert runner.event_queue.empty()


def test_self_reflection_takeover_seals_generation_before_visible_text() -> None:
    """主动接管发生在首个正文前时，也必须产生可归档的中断终态。"""

    class RecordingAgent:
        """记录输出 lane 提交的主动接管事实。"""

        def __init__(self) -> None:
            """初始化空记录。"""

            self.calls: list[dict[str, object]] = []

        def create_interrupted_response_event(
            self,
            display_text: str,
            *,
            model_text: str,
            request_id: str,
            history_policy: str,
            progress_references: tuple[object, ...],
            interruption_reason: str = "",
        ) -> ForegroundEvent:
            """保存中断身份并返回空正文终态。"""

            self.calls.append(
                {
                    "display_text": display_text,
                    "model_text": model_text,
                    "request_id": request_id,
                    "history_policy": history_policy,
                    "progress_references": progress_references,
                    "interruption_reason": interruption_reason,
                }
            )
            return ForegroundEvent(
                type="assistant.text_final",
                payload={"text": "", "owner_request_id": request_id},
            )

    agent = RecordingAgent()
    runner = AssistantOutputLanes(
        agent=agent,  # type: ignore[arg-type]
        tts_provider=None,  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-before-text",
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="解释配置",
        source="text_input",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)
    work_item.context.cancellation_event.cancel("self_reflection_takeover")

    runner._emit_interrupted_text_final(work_item.context)  # type: ignore[attr-defined]

    assert agent.calls == [
        {
            "display_text": "",
            "model_text": "",
            "request_id": "request-before-text",
            "history_policy": "stable_dialogue",
            "progress_references": (),
            "interruption_reason": "self_reflection_takeover",
        }
    ]
    final_event = runner.event_queue.get_nowait()
    assert final_event.type == "assistant.text_final"
    assert final_event.payload["text"] == ""


def test_interrupted_old_generation_keeps_original_request_identity() -> None:
    """新请求替换旧 generation 后，中断结算仍必须归属旧请求。"""

    class RequestIdentityRecordingAgent:
        """记录输出 lane 传入的中断请求编号。"""

        def __init__(self) -> None:
            """初始化请求编号记录。"""

            self.interrupted_request_ids: list[str] = []

        def create_interrupted_response_event(
            self,
            display_text: str,
            *,
            model_text: str,
            request_id: str,
            history_policy: str,
            progress_references: tuple[object, ...],
            interruption_reason: str = "",
        ) -> ForegroundEvent:
            """用调用方显式传入的请求编号构造中断事件。"""

            _ = history_policy, progress_references, interruption_reason
            self.interrupted_request_ids.append(request_id)
            return ForegroundEvent(
                type="assistant.text_final",
                payload={
                    "text": display_text,
                    "settlement_text": model_text,
                    "owner_request_id": request_id,
                },
            )

    agent = RequestIdentityRecordingAgent()
    runner = AssistantOutputLanes(
        agent=agent,  # type: ignore[arg-type]
        tts_provider=None,  # type: ignore[arg-type]
        now=lambda: 1.0,
    )

    def request(request_id: str) -> AssistantRequest:
        """创建用于代际替换测试的最小助手请求。"""

        return AssistantRequest(
            request_id=request_id,
            source_session_id="session-a",
            interaction_session_id="interaction-a",
            character_card=cast(CharacterCardDefinition, None),
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            user_text="汇报提醒",
            source="system_notice",
            source_turn_id=None,
            requested_at=1.0,
        )

    runner._supervisor.start_generation(request("request-old"))  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    old_work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(old_work_item, AssistantRequestWorkItem)
    old_work_item.context.visible_parts.append("旧提醒已经出字。")

    runner._supervisor.start_generation(request("request-new"))  # type: ignore[attr-defined]
    runner._emit_interrupted_text_final(old_work_item.context)  # type: ignore[attr-defined]

    queued_events = []
    while not runner.event_queue.empty():
        queued_events.append(runner.event_queue.get_nowait())
    final_event = next(
        event for event in queued_events if event.type == "assistant.text_final"
    )
    assert agent.interrupted_request_ids == ["request-old"]
    assert final_event.payload["owner_request_id"] == "request-old"


def test_error_fallback_text_final_rolls_strong_notice_back_to_pending() -> None:
    """异常兜底文字不代表提醒已表达，强终态必须保留给后续请求。"""

    coordinator = RecordingCoordinator()
    _apply_delivery_event(coordinator,
        "assistant.text_final",
        {
            "request_id": "request-a",
            "text": "抱歉，我刚才生成回复时遇到了问题，请稍后再试。",
            "notice_delivery_failed": True,
        },
    )

    assert coordinator.llm_completed == []
    assert coordinator.cancelled == ["request-a"]


def test_provider_protocol_leak_uses_dedicated_notice_failure_path() -> None:
    """协议泄漏必须独立计数，不能退化成普通取消。"""

    coordinator = RecordingCoordinator()
    _apply_delivery_event(coordinator,
        "assistant.text_final",
        {
            "request_id": "request-a",
            "text": "",
            "notice_delivery_failed": True,
            "notice_failure_reason": "provider_protocol_leak",
        },
    )

    assert coordinator.protocol_failed == ["request-a"]
    assert coordinator.cancelled == []


def test_audio_finished_does_not_create_notice_delivery_command() -> None:
    """音频完成只收口媒体状态，不再决定 Notice 生命周期。"""

    command = map_notice_delivery_event(
        "assistant.audio_finished",
        {"request_id": "request-a"},
    )

    assert command is None
