from __future__ import annotations

import pytest

from companion.realtime.playout_delivery import (
    PlayoutDelivery,
    PlayoutDeliveryConflictError,
    PlayoutDeliveryRegistry,
)


DELIVERY_ID = "123e4567-e89b-42d3-a456-426614174000"
CLIENT_ID = "123e4567-e89b-42d3-a456-426614174001"


def _start_delivery(registry: PlayoutDeliveryRegistry) -> None:
    """登记一条已经开始向 LiveKit 发布音频的测试交付。"""

    update = registry.start_publishing(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        occurred_at=1.0,
    )

    assert update.changed is True
    assert update.delivery.status == "awaiting_start"
    assert update.public_event == {
        "type": "assistant.playout_delivery_ready",
        "payload": {
            "delivery_id": DELIVERY_ID,
            "request_id": "request-a",
            "generation_id": "generation-a",
        },
    }


def test_desktop_playing_ack_and_backend_finish_form_one_shot_delivery() -> None:
    """Desktop 确认真实播放后，音频源排空应把同一交付收口为 delivered。"""

    registry = PlayoutDeliveryRegistry()
    _start_delivery(registry)

    playing = registry.acknowledge(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        client_instance_id=CLIENT_ID,
        outcome="playing",
        occurred_at=2.0,
    )
    duplicate = registry.acknowledge(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        client_instance_id=CLIENT_ID,
        outcome="playing",
        occurred_at=2.5,
    )
    finished = registry.finish(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        completion="drained",
        occurred_at=3.0,
    )

    assert playing.changed is True
    assert playing.delivery.status == "playing"
    assert playing.public_event is None
    assert duplicate.changed is False
    assert finished is not None
    assert finished.delivery.status == "delivered"
    assert finished.public_event is None

    with pytest.raises(PlayoutDeliveryConflictError):
        registry.acknowledge(
            delivery_id=DELIVERY_ID,
            request_id="request-a",
            generation_id="generation-a",
            client_instance_id=CLIENT_ID,
            outcome="playing",
            occurred_at=4.0,
        )


def test_audio_finish_without_desktop_ack_is_terminal_but_not_user_visible_failure() -> None:
    """没有回执的语音必须收口为 unconfirmed，但不能伪装成确定的播放失败。"""

    registry = PlayoutDeliveryRegistry()
    _start_delivery(registry)

    finished = registry.finish(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        completion="drained",
        occurred_at=3.0,
    )

    assert finished is not None
    assert finished.delivery.status == "unconfirmed"
    assert finished.public_event is None


def test_tts_failure_is_idempotent_and_exposes_only_safe_desktop_feedback() -> None:
    """TTS 原始异常不能广播，Desktop 只接收稳定错误码和可理解文案。"""

    registry = PlayoutDeliveryRegistry()
    failed = registry.fail(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        failure_code="tts_failed",
        failure_message="语音合成没有完成，文字回复仍可查看。",
        occurred_at=1.0,
    )
    duplicate = registry.fail(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        failure_code="tts_failed",
        failure_message="语音合成没有完成，文字回复仍可查看。",
        occurred_at=2.0,
    )

    assert failed.changed is True
    assert failed.delivery.status == "failed"
    assert failed.public_event == {
        "type": "assistant.playout_delivery_failed",
        "payload": {
            "delivery_id": DELIVERY_ID,
            "request_id": "request-a",
            "generation_id": "generation-a",
            "client_instance_id": None,
            "code": "tts_failed",
            "message": "语音合成没有完成，文字回复仍可查看。",
        },
    }
    assert duplicate.changed is False
    assert duplicate.public_event is None


def test_conflicting_identity_cannot_modify_existing_delivery() -> None:
    """相同 delivery_id 不能被其它 request 或 generation 劫持。"""

    registry = PlayoutDeliveryRegistry()
    _start_delivery(registry)

    with pytest.raises(PlayoutDeliveryConflictError):
        registry.acknowledge(
            delivery_id=DELIVERY_ID,
            request_id="request-b",
            generation_id="generation-a",
            client_instance_id=CLIENT_ID,
            outcome="playing",
            occurred_at=2.0,
        )


def test_cancelled_delivery_rejects_late_desktop_ack() -> None:
    """主动打断已经收口后，迟到的 Desktop 回执不能再制造播放失败。"""

    registry = PlayoutDeliveryRegistry()
    _start_delivery(registry)

    aborted = registry.abort(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
    )

    assert aborted is not None
    assert aborted.delivery.status == "aborted"
    assert aborted.public_event is None
    with pytest.raises(PlayoutDeliveryConflictError):
        registry.acknowledge(
            delivery_id=DELIVERY_ID,
            request_id="request-a",
            generation_id="generation-a",
            client_instance_id=CLIENT_ID,
            outcome="failed",
            occurred_at=3.0,
        )


def test_same_identity_late_delivery_events_are_idempotent_after_abort() -> None:
    """同一交付身份的迟到生命周期事件只能返回未变化，不能重新打开终态。"""

    registry = PlayoutDeliveryRegistry()
    _start_delivery(registry)

    aborted = registry.abort(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
    )
    late_start = registry.start_publishing(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        occurred_at=2.0,
    )
    late_finish = registry.finish(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        completion="drained",
        occurred_at=3.0,
    )
    duplicate_abort = registry.abort(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
    )

    assert aborted is not None and aborted.delivery.status == "aborted"
    assert late_start.changed is False and late_start.delivery.status == "aborted"
    assert late_finish is not None and late_finish.changed is False
    assert late_finish.delivery.status == "aborted"
    assert duplicate_abort is not None and duplicate_abort.changed is False


@pytest.mark.parametrize(
    "terminal_status",
    ["delivered", "aborted", "failed", "unconfirmed", "interrupted"],
)
@pytest.mark.parametrize(
    "completion",
    ["drained", "unconfirmed_timeout", "unconfirmed_error"],
)
def test_finish_never_rewrites_existing_terminal_status(
    terminal_status: str,
    completion: str,
) -> None:
    """任何迟到完成结果都只能读取既有终态，不能横向改写状态。"""

    registry = PlayoutDeliveryRegistry()
    registry._deliveries[DELIVERY_ID] = PlayoutDelivery(  # type: ignore[attr-defined]
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        status=terminal_status,  # type: ignore[arg-type]
    )

    update = registry.finish(
        delivery_id=DELIVERY_ID,
        request_id="request-a",
        generation_id="generation-a",
        completion=completion,  # type: ignore[arg-type]
        occurred_at=3.0,
    )

    assert update is not None
    assert update.changed is False
    assert update.delivery.status == terminal_status
    assert registry.get(DELIVERY_ID).status == terminal_status


def test_terminal_delivery_retention_is_bounded() -> None:
    """长期运行只保留最近的有限终态，不能让播放账本无限增长。"""

    registry = PlayoutDeliveryRegistry(max_terminal_records=2)
    delivery_ids = [f"delivery-{index}" for index in range(3)]
    for index, delivery_id in enumerate(delivery_ids):
        registry.start_publishing(
            delivery_id=delivery_id,
            request_id=f"request-{index}",
            generation_id=f"generation-{index}",
            occurred_at=float(index),
        )
        registry.finish(
            delivery_id=delivery_id,
            request_id=f"request-{index}",
            generation_id=f"generation-{index}",
            completion="drained",
            occurred_at=float(index + 1),
        )

    assert registry.get(delivery_ids[0]) is None
    assert registry.get(delivery_ids[1]) is not None
    assert registry.get(delivery_ids[2]) is not None
