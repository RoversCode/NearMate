from __future__ import annotations

from typing import cast
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from companion.character_cards.models import CharacterCardDefinition
from companion.realtime.assistant_events import AssistantRequest
from companion.realtime.runtime_identity import (
    AssistantRequestIdentityConflict,
    AssistantRequestIdentityRegistry,
    RuntimeIdentityFactory,
)


def _assistant_request(request_id: str = "request-a") -> AssistantRequest:
    """构造身份登记测试使用的最小助手请求。

    Args:
        request_id: 本次测试希望登记的助手请求编号。

    Returns:
        只携带身份校验所需字段的助手请求。
    """

    return AssistantRequest(
        request_id=request_id,
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="请回答",
        source="final_asr",
        source_turn_id="turn-9",
        requested_at=1.0,
    )


def test_runtime_identity_factory_uses_prefixed_uuid_v4() -> None:
    """跨边界运行期身份必须使用可读前缀和 UUID v4。

    Returns:
        None: 通过断言验证生成结果。
    """

    expected_uuid = UUID("12345678-1234-4234-9234-1234567890ab")
    factory = RuntimeIdentityFactory(uuid_factory=lambda: expected_uuid)

    assert factory.new("final-asr") == f"final-asr-{expected_uuid}"


def test_request_identity_registry_permanently_rejects_duplicate_claim() -> None:
    """同一 Backend 进程内已经 claim 的 request id 永远不能复用。

    Returns:
        None: 通过冲突异常验证永久门禁。
    """

    registry = AssistantRequestIdentityRegistry()
    request = _assistant_request()
    registry.claim(request)

    with pytest.raises(AssistantRequestIdentityConflict, match="request-a"):
        registry.claim(request)


def test_request_identity_registry_only_allows_execution_identity_to_bind_once() -> None:
    """runtime epoch 和 generation 只能从空值补齐一次，之后不得改写。

    Returns:
        None: 通过绑定和冲突断言验证执行身份不可变。
    """

    registry = AssistantRequestIdentityRegistry()
    registry.claim(_assistant_request())

    registry.bind_runtime_epoch("request-a", 3)
    registry.bind_generation("request-a", "generation-a")
    registry.validate_event(
        request_id="request-a",
        runtime_epoch=3,
        generation_id="generation-a",
    )

    with pytest.raises(AssistantRequestIdentityConflict, match="runtime_epoch"):
        registry.bind_runtime_epoch("request-a", 4)
    with pytest.raises(AssistantRequestIdentityConflict, match="generation_id"):
        registry.validate_event(
            request_id="request-a",
            runtime_epoch=3,
            generation_id="generation-b",
        )


def test_registry_get_returns_immutable_identity_without_changing_execution_binding() -> None:
    """来源查询只读，补齐生成编号后旧快照也不能被调用方改写。"""

    registry = AssistantRequestIdentityRegistry()
    registry.claim(_assistant_request())
    before = registry.get(" request-a ")
    assert before is not None
    assert before.source == "final_asr"
    assert before.generation_id is None
    registry.bind_generation("request-a", "generation-a")
    after = registry.get("request-a")
    assert after is not None and after.generation_id == "generation-a"
    assert before.generation_id is None
    with pytest.raises(FrozenInstanceError):
        after.source = "text_input"  # type: ignore[misc]
    assert registry.get("unknown") is None
