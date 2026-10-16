from __future__ import annotations

from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_output_event_handler import AssistantOutputEventHandler
from companion.realtime.assistant_public_event_projection import (
    project_public_assistant_event,
)
from companion.realtime.assistant_output_settlement_service import (
    AssistantOutputSettlementService,
)
from companion.realtime.turn_material_release_service import TurnMaterialReleaseService
from types import SimpleNamespace
import pytest


def _identity() -> dict[str, object]:
    """构造公共助手正文必须携带的完整执行身份。"""

    return {
        "request_id": "request-a",
        "session_id": "session-a",
        "generation_id": "generation-a",
    }


def test_public_assistant_event_removes_raw_exception_fields() -> None:
    """公共控制事件不得包含 Provider 原始异常、traceback 或配置正文。"""

    with pytest.raises(ValueError, match="assistant.state_changed"):
        project_public_assistant_event(
            RuntimeEvent(
                type="assistant.state_changed",
                source="assistant_runtime",
                payload={"error": "SECRET_PROVIDER_TOKEN=abc"},
            )
        )


@pytest.mark.parametrize(
    ("payload", "expected_payload"),
    [
        (
            {**_identity(), "text": "正文"},
            {**_identity(), "text": "正文"},
        ),
        (
            {**_identity(), "text": ""},
            {
                **_identity(),
                "text": "",
                "notice_delivery_failed": True,
            },
        ),
        (
            {**_identity(), "text": "  \n"},
            {
                **_identity(),
                "text": "",
                "notice_delivery_failed": True,
            },
        ),
        (
            {
                **_identity(),
                "text": "暂时无法继续回复。",
                "notice_delivery_failed": True,
            },
            {
                **_identity(),
                "text": "暂时无法继续回复。",
                "notice_delivery_failed": True,
            },
        ),
        (
            {
                **_identity(),
                "text": "  ",
                "notice_delivery_failed": True,
            },
            {
                **_identity(),
                "text": "",
                "notice_delivery_failed": True,
            },
        ),
    ],
)
def test_public_assistant_final_normalizes_supported_payloads(
    payload: dict[str, object],
    expected_payload: dict[str, object],
) -> None:
    """Final 公共边界必须把无可见正文统一成可收口的失败终态。"""

    assert project_public_assistant_event(
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            payload=payload,
        )
    ) == {"type": "assistant.text_final", "payload": expected_payload}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**_identity(), "text": None},
        {**_identity(), "text": "正文", "request_id": ""},
        {**_identity(), "text": "正文", "generation_id": None},
        {**_identity(), "text": "正文", "notice_delivery_failed": False},
        {**_identity(), "text": "正文", "notice_delivery_failed": None},
        {**_identity(), "text": "正文", "notice_failure_reason": "provider_protocol_leak"},
        {
            **_identity(),
            "text": "",
            "notice_delivery_failed": True,
            "notice_failure_reason": "unknown_reason",
        },
        {
            **_identity(),
            "text": "正文",
            "notice_delivery_failed": True,
            "notice_failure_reason": "provider_protocol_leak",
        },
        {
            **_identity(),
            "text": "",
            "notice_delivery_failed": True,
            "notice_failure_reason": None,
        },
    ],
)
def test_public_assistant_final_rejects_invalid_payload_combinations(
    payload: dict[str, object],
) -> None:
    """Final 非法联合必须在 Backend 出口失败，不能留给 Desktop 静默丢弃。"""

    with pytest.raises(ValueError, match=r"^assistant_public_"):
        project_public_assistant_event(
            RuntimeEvent(
                type="assistant.text_final",
                source="assistant_runtime",
                payload=payload,
            )
        )


def test_public_assistant_final_preserves_sanitized_notice_failure_metadata() -> None:
    """运行时兜底 Final 必须保留 Desktop 已登记的稳定失败元数据。"""

    assert project_public_assistant_event(
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            payload={
                **_identity(),
                "generation_id": "generation-a",
                "text": "",
                "notice_delivery_failed": True,
                "notice_failure_reason": "provider_protocol_leak",
                "traceback": "SECRET_PROVIDER_TOKEN",
            },
        )
    ) == {
        "type": "assistant.text_final",
        "payload": {
            **_identity(),
            "generation_id": "generation-a",
            "text": "",
            "notice_delivery_failed": True,
            "notice_failure_reason": "provider_protocol_leak",
        },
    }


@pytest.mark.asyncio
async def test_settled_late_text_is_absorbed_before_handler_broadcast() -> None:
    """Handler 不得把 Journal 墓碑之后的迟到正文送进公共广播。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    journal_service = AssistantOutputSettlementService()
    handler = AssistantOutputEventHandler(
        publish_interaction=broadcast,
        publish_assistant_output=broadcast,
        realtime_input_runtime_provider=lambda: SimpleNamespace(),
        activity_event_handler=SimpleNamespace(),
        turn_material_release_service=TurnMaterialReleaseService(
            finish_turn_materials=None,
            broadcast_ephemeral=lambda _event: _noop_async(),
        ),
        output_settlement_service=journal_service,
    )
    payload = {
        "request_id": "request-late",
        "session_id": "session-a",
        "generation_id": "generation-a",
        "text": "正文",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
    }

    await handler.handle(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            payload=payload,
        )
    )
    journal_service.settle(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            payload={**payload, "outcome": "completed"},
            produced_at=2.0,
        )
    )
    await handler.handle(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            payload={**payload, "text": "迟到正文"},
        )
    )

    assert [event["payload"]["text"] for event in broadcasts] == ["正文"]


@pytest.mark.asyncio
async def test_whitespace_chunk_bypassing_foreground_does_not_end_generation() -> None:
    """绕过 Foreground 的纯空白 chunk 应被吸收，后续正文和 Final 仍可公开。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    handler = AssistantOutputEventHandler(
        publish_interaction=broadcast,
        publish_assistant_output=broadcast,
        realtime_input_runtime_provider=lambda: SimpleNamespace(),
        activity_event_handler=SimpleNamespace(),
        turn_material_release_service=TurnMaterialReleaseService(
            finish_turn_materials=None,
            broadcast_ephemeral=lambda _event: _noop_async(),
        ),
    )
    identity = {
        **_identity(),
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
    }

    await handler.handle(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            payload={**identity, "text": " \n\t"},
        )
    )
    await handler.handle(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            payload={**identity, "text": "正文"},
        )
    )
    await handler.handle(
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            payload={**identity, "text": "完整正文"},
        )
    )

    assert [event["payload"]["text"] for event in broadcasts] == [
        "正文",
        "完整正文",
    ]


async def _noop_async() -> None:
    """提供材料释放测试需要的无副作用异步广播端口。"""
