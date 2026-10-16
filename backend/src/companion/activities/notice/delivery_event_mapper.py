"""把助手输出事件转换为明确的 Notice 交付命令。"""

from __future__ import annotations

from companion.activities.notice.models import NoticeDeliveryCommand, NoticeDeliveryOutcome


def map_notice_delivery_event(
    event_type: str,
    payload: dict[str, object],
) -> NoticeDeliveryCommand | None:
    """归一化一个助手输出闭环事件。

    Args:
        event_type: 助手输出边界产生的事件类型。
        payload: 包含请求身份与交付事实的事件载荷。

    Returns:
        可直接交给 Notice 协调器的命令；事件无关或请求身份无效时返回空。
    """

    request_id = _clean_text(payload.get("request_id"))
    if not request_id:
        return None
    if event_type == "assistant.request_enqueued":
        return NoticeDeliveryCommand(request_id=request_id, outcome="request_enqueued")
    if event_type == "assistant.request_enqueue_failed":
        return NoticeDeliveryCommand(request_id=request_id, outcome="request_enqueue_failed")
    if event_type == "assistant.text_final":
        return NoticeDeliveryCommand(
            request_id=request_id,
            outcome=_text_final_outcome(payload),
        )
    if event_type == "assistant.state_changed" and payload.get("assistant_state") == "failed":
        return NoticeDeliveryCommand(request_id=request_id, outcome="delivery_cancelled")
    if event_type in {"assistant.generation_interrupted", "assistant.generation_superseded"}:
        reason = payload.get("reason")
        if reason == "output_unavailable":
            outcome = "output_unavailable"
        elif reason == "activity_notice_superseded":
            outcome = "superseded"
        else:
            outcome = "delivery_cancelled"
        return NoticeDeliveryCommand(request_id=request_id, outcome=outcome)
    return None


def _text_final_outcome(payload: dict[str, object]) -> NoticeDeliveryOutcome:
    """根据 LLM final 与异常标记生成 Notice 生命周期结果。"""

    if payload.get("notice_delivery_failed") is True:
        if payload.get("notice_failure_reason") == "provider_protocol_leak":
            return "provider_protocol_failed"
        return "delivery_cancelled"
    if payload.get("interrupted") is True:
        return "delivery_cancelled"
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return "llm_output_completed"
    return "delivery_cancelled"


def _clean_text(value: object) -> str:
    """把事件中的请求身份清洗为非空字符串。"""

    return value.strip() if isinstance(value, str) else ""
