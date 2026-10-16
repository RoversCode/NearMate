from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.playout_delivery import PlayoutDeliveryRegistry
from backend.tests.runtime_public_event_support import runtime_event_router


def _event(event_type: str, payload: dict[str, object]) -> RuntimeEvent:
    """构造当前助手世代产生的播放交付事件。"""

    return RuntimeEvent(
        type=event_type,
        source="assistant_runtime",
        payload=payload,
        produced_at=10.0,
        runtime_epoch=1,
    )


@pytest.mark.asyncio
async def test_audio_publishing_start_prepares_delivery_without_notice_command() -> None:
    """首帧发布只建立 Playout Delivery，并继续推进产品 Turn，不得写 Notice。"""

    broadcasts: list[dict[str, object]] = []
    notice_commands: list[object] = []
    runtime_calls: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    def apply_publishing_started(**kwargs: object) -> list[dict[str, object]]:
        runtime_calls.append(dict(kwargs))
        return [{"type": "turn.state_changed", "payload": {"turn_state": "speaking"}}]

    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(
            apply_assistant_audio_publishing_started=apply_publishing_started,
        ),
        notice_delivery_handler=notice_commands.append,
        playout_delivery_registry=PlayoutDeliveryRegistry(),
    )

    await router.dispatch(
        _event(
            "assistant.audio_publishing_started",
            {
                "delivery_id": "123e4567-e89b-42d3-a456-426614174000",
                "request_id": "request-a",
                "generation_id": "generation-a",
            },
        )
    )

    assert runtime_calls == [{
        "now": 10.0,
        "generation_id": "generation-a",
        "request_id": "request-a",
    }]
    assert broadcasts == [
        {"type": "turn.state_changed", "payload": {"turn_state": "speaking"}},
        {
            "type": "assistant.playout_delivery_ready",
            "payload": {
                "delivery_id": "123e4567-e89b-42d3-a456-426614174000",
                "request_id": "request-a",
                "generation_id": "generation-a",
            },
        },
    ]
    assert notice_commands == []


@pytest.mark.asyncio
async def test_tts_failure_becomes_safe_playout_failure_without_raw_error_broadcast() -> None:
    """TTS 失败只广播稳定产品反馈，不能把句段正文和异常原文发给 Desktop。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        playout_delivery_registry=PlayoutDeliveryRegistry(),
    )

    await router.dispatch(
        _event(
            "assistant.tts_segment_failed",
            {
                "delivery_id": "123e4567-e89b-42d3-a456-426614174000",
                "request_id": "request-a",
                "generation_id": "generation-a",
                "text": "不应公开的句段",
                "error": "provider secret failure",
            },
        )
    )

    assert broadcasts == [{
        "type": "assistant.playout_delivery_failed",
        "payload": {
            "delivery_id": "123e4567-e89b-42d3-a456-426614174000",
            "request_id": "request-a",
            "generation_id": "generation-a",
            "client_instance_id": None,
            "code": "tts_failed",
            "message": "语音合成没有完成，文字回复仍可查看。",
        },
    }]


@pytest.mark.asyncio
async def test_audio_publishing_cancelled_closes_playout_without_failure_feedback() -> None:
    """主动清空音频队列只收口播放事实，不能误报成用户设备失败。"""

    broadcasts: list[dict[str, object]] = []
    registry = PlayoutDeliveryRegistry()

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(
            apply_assistant_audio_publishing_started=lambda **_kwargs: [],
            apply_assistant_audio_publishing_cancelled=lambda **_kwargs: [],
        ),
        playout_delivery_registry=registry,
    )
    identity = {
        "delivery_id": "123e4567-e89b-42d3-a456-426614174000",
        "request_id": "request-a",
        "generation_id": "generation-a",
    }

    await router.dispatch(_event("assistant.audio_publishing_started", identity))
    broadcasts.clear()
    await router.dispatch(_event("assistant.audio_publishing_cancelled", identity))

    delivery = registry.get(identity["delivery_id"])
    assert delivery is not None
    assert delivery.status == "aborted"
    assert broadcasts == []
