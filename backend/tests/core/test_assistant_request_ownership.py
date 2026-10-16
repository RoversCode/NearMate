from __future__ import annotations

from typing import cast

import pytest

from companion.character_cards.models import CharacterCardDefinition
from companion.realtime.assistant_events import AssistantRequest, RuntimeEvent
from companion.realtime.assistant_request_ownership import (
    AssistantRequestOwnershipLedger,
)
from companion.realtime.runtime_identity import AssistantRequestIdentityConflict


def _request(request_id: str) -> AssistantRequest:
    """构造所有权交接测试使用的最小助手请求。"""

    return AssistantRequest(
        request_id=request_id,
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="请回答",
        source="text_input",
        source_turn_id="turn-1",
        requested_at=1.0,
    )


def test_queued_request_can_move_from_old_bridge_to_new_bridge() -> None:
    """媒体会话重建时，尚未提交的请求必须保留并交给新 Bridge。"""

    ledger = AssistantRequestOwnershipLedger()
    ledger.claim(_request("request-a"), bridge_id="bridge-old")

    handed_off = ledger.release_queued_requests("bridge-old")
    adopted = ledger.adopt_queued_requests("bridge-new")

    assert [record.request.request_id for record in handed_off] == ["request-a"]
    assert [record.request.request_id for record in adopted] == ["request-a"]


def test_submitted_request_rejects_event_from_different_runtime_epoch() -> None:
    """旧请求携带新世代事件时必须报身份冲突，不能静默吸收。"""

    ledger = AssistantRequestOwnershipLedger()
    ledger.claim(_request("request-a"), bridge_id="bridge-old")
    ledger.mark_submitted("request-a", bridge_id="bridge-old")
    ledger.bind_runtime_epoch("request-a", 4, bridge_id="bridge-old")

    with pytest.raises(AssistantRequestIdentityConflict, match="runtime_epoch"):
        ledger.observe_event(
            RuntimeEvent(
                type="assistant.generation_interrupted",
                source="assistant_runtime",
                runtime_epoch=5,
                payload={"request_id": "request-a"},
            )
        )
