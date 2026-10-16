"""把助手内部运行时事件收窄为稳定的公共控制事件。"""

from __future__ import annotations

from typing import Any

from companion.realtime.assistant_events import RuntimeEvent


_PUBLIC_PAYLOAD_FIELDS_BY_EVENT: dict[str, frozenset[str]] = {
    "assistant.text_chunk": frozenset(
        {"request_id", "session_id", "generation_id", "text"}
    ),
    "assistant.text_segment_final": frozenset(
        {"request_id", "session_id", "generation_id", "text"}
    ),
    "assistant.text_final": frozenset(
        {
            "session_id",
            "generation_id",
            "request_id",
            "text",
            "notice_delivery_failed",
            "notice_failure_reason",
        }
    ),
    "assistant.audio_finished": frozenset(
        {"request_id", "session_id", "generation_id"}
    ),
    "assistant.output_status_changed": frozenset(
        {"available", "code", "message"}
    ),
    "assistant.request_failed": frozenset(
        {"request_id", "code", "message"}
    ),
    "assistant.self_reflection_status_changed": frozenset(
        {
            "request_id",
            "session_id",
            "generation_id",
            "self_reflection_id",
            "episode_id",
            "revision",
            "active",
        }
    ),
    "assistant.playout_delivery_ready": frozenset(
        {"delivery_id", "request_id", "generation_id"}
    ),
    "assistant.playout_delivery_failed": frozenset(
        {
            "delivery_id",
            "request_id",
            "generation_id",
            "client_instance_id",
            "code",
            "message",
        }
    ),
}

PUBLIC_ASSISTANT_RUNTIME_EVENT_TYPES = frozenset(_PUBLIC_PAYLOAD_FIELDS_BY_EVENT)
_MISSING = object()


def project_public_assistant_event(event: RuntimeEvent) -> dict[str, Any]:
    """只复制当前事件明确允许公开的字段。

    Args:
        event: 仍包含请求身份、历史提交和内部诊断信息的运行时事件。

    Returns:
        只含事件类型和白名单载荷的公共控制事件。

    Raises:
        ValueError: 内部或未知事件被错误送入公共助手投影时抛出。
    """

    if event.type == "assistant.text_final":
        return {
            "type": event.type,
            "payload": _project_public_assistant_text_final(event.payload),
        }

    if event.type in {"assistant.text_chunk", "assistant.text_segment_final"}:
        return {
            "type": event.type,
            "payload": _project_public_assistant_text_chunk(event.payload),
        }
    if event.type == "assistant.audio_finished":
        public_payload = _project_request_bound_payload(
            event.payload,
            fields=("request_id", "session_id", "generation_id"),
        )
        public_payload.update(_project_auxiliary_reply_identity(event.payload))
        return {
            "type": event.type,
            "payload": public_payload,
        }
    if event.type == "assistant.output_status_changed":
        return {
            "type": event.type,
            "payload": _project_output_status(event.payload),
        }
    if event.type == "assistant.request_failed":
        return {
            "type": event.type,
            "payload": _project_request_failed(event.payload),
        }
    if event.type == "assistant.self_reflection_status_changed":
        return {
            "type": event.type,
            "payload": _project_self_reflection_status(event.payload),
        }
    if event.type in {
        "assistant.playout_delivery_ready",
        "assistant.playout_delivery_failed",
    }:
        return {
            "type": event.type,
            "payload": _project_playout_delivery(event.type, event.payload),
        }

    allowed_fields = _PUBLIC_PAYLOAD_FIELDS_BY_EVENT.get(event.type)
    if allowed_fields is None:
        raise ValueError(f"助手公共事件未登记 event_type={event.type}")
    public_payload = {
        field_name: event.payload[field_name]
        for field_name in allowed_fields
        if field_name in event.payload
    }
    return {"type": event.type, "payload": public_payload}


def _project_public_assistant_text_chunk(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """校验流式正文，空 chunk 不得进入公共事件。"""

    public_payload = _project_request_bound_payload(
        payload,
        fields=("request_id", "session_id", "generation_id"),
    )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("assistant_public_chunk_empty_text")
    if any(field in payload for field in ("notice_delivery_failed", "notice_failure_reason")):
        raise ValueError("assistant_public_chunk_contains_final_failure")
    public_payload["text"] = text
    public_payload.update(_project_auxiliary_reply_identity(payload))
    return public_payload


def _project_request_bound_payload(
    payload: dict[str, Any],
    *,
    fields: tuple[str, ...],
) -> dict[str, Any]:
    """要求 request-bound 事件携带完整、非空身份。"""

    public_payload: dict[str, Any] = {}
    for field_name in fields:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("assistant_public_missing_identity")
        public_payload[field_name] = value
    return public_payload


def _project_output_status(payload: dict[str, Any]) -> dict[str, Any]:
    """校验可用与不可用状态的 code/message 组合。"""

    available = payload.get("available")
    code = payload.get("code")
    message = payload.get("message")
    if not isinstance(available, bool):
        raise ValueError("assistant_public_health_invalid_available")
    if available:
        if code is not None or message is not None:
            raise ValueError("assistant_public_health_available_has_failure")
    else:
        if code not in {"output_unavailable", "request_identity_conflict"}:
            raise ValueError("assistant_public_health_invalid_code")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("assistant_public_health_missing_message")
    return {"available": available, "code": code, "message": message}


def _project_request_failed(payload: dict[str, Any]) -> dict[str, Any]:
    """校验请求失败事件只包含稳定错误码和用户文案。"""

    request_id = payload.get("request_id")
    code = payload.get("code")
    message = payload.get("message")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("assistant_public_failure_missing_request")
    if code not in {
        "request_start_failed",
        "output_unavailable",
        "request_identity_conflict",
    }:
        raise ValueError("assistant_public_failure_invalid_code")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("assistant_public_failure_missing_message")
    return {"request_id": request_id, "code": code, "message": message}


def _project_self_reflection_status(payload: dict[str, Any]) -> dict[str, Any]:
    """校验反思临时状态，确保迟到 generation 不能污染桌宠。"""

    public_payload = _project_request_bound_payload(
        payload,
        fields=("request_id", "session_id", "generation_id"),
    )
    active = payload.get("active")
    if not isinstance(active, bool):
        raise ValueError("assistant_public_reflection_status_invalid_active")
    self_reflection_id = payload.get("self_reflection_id")
    episode_id = payload.get("episode_id")
    revision = payload.get("revision")
    if not isinstance(self_reflection_id, str) or not self_reflection_id.strip():
        raise ValueError("assistant_public_reflection_status_missing_reflection")
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise ValueError("assistant_public_reflection_status_missing_episode")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision <= 0
    ):
        raise ValueError("assistant_public_reflection_status_invalid_revision")
    public_payload.update(
        self_reflection_id=self_reflection_id,
        episode_id=episode_id,
        revision=revision,
        active=active,
    )
    return public_payload


def _project_auxiliary_reply_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """只为辅助打扰回应公开其临时历史归属。

    Args:
        payload: 仍包含内部请求来源和 Self-Reflection 身份的输出载荷。

    Returns:
        普通输出返回空字典；辅助回应返回 Desktop 所需的最小身份。
    """

    if payload.get("source") != "self_reflection_interruption_reply":
        return {}
    self_reflection_id = payload.get("self_reflection_id")
    episode_id = payload.get("episode_id")
    if not isinstance(self_reflection_id, str) or not self_reflection_id.strip():
        raise ValueError("assistant_public_auxiliary_missing_reflection")
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise ValueError("assistant_public_auxiliary_missing_episode")
    return {
        "request_source": "self_reflection_interruption_reply",
        "self_reflection_id": self_reflection_id,
        "episode_id": episode_id,
    }


def _project_playout_delivery(
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """校验音频播放交付事件的完整身份和安全失败字段。"""

    public_payload = _project_request_bound_payload(
        payload,
        fields=("delivery_id", "request_id", "generation_id"),
    )
    if event_type == "assistant.playout_delivery_failed":
        client_instance_id = payload.get("client_instance_id")
        code = payload.get("code")
        message = payload.get("message")
        if client_instance_id is not None and (
            not isinstance(client_instance_id, str) or not client_instance_id.strip()
        ):
            raise ValueError("assistant_public_playout_invalid_client")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("assistant_public_playout_missing_code")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("assistant_public_playout_missing_message")
        public_payload.update(
            client_instance_id=client_instance_id,
            code=code,
            message=message,
        )
    return public_payload


def _project_public_assistant_text_final(payload: dict[str, Any]) -> dict[str, Any]:
    """把内部 Final 收窄为 Desktop 能完整接收的联合载荷。

    Args:
        payload: 助手运行时产生的 Final 载荷，可能还包含内部结算字段。

    Returns:
        正常正文，或带稳定失败标记的公开 Final 载荷。没有可见正文时会统一
        规范成空文本交付失败，确保 Desktop 能完成流式草稿收口。

    Raises:
        ValueError: 字段类型、身份或失败标记组合不符合公共协议时抛出。异常只含
            稳定原因码，不回显载荷内容。
    """

    text = payload["text"] if "text" in payload else _MISSING
    if not isinstance(text, str):
        raise ValueError("assistant_public_final_invalid_text_type")

    public_payload = _project_request_bound_payload(
        payload,
        fields=("request_id", "session_id", "generation_id"),
    )
    public_payload.update(_project_auxiliary_reply_identity(payload))

    normalized_text = text if text.strip() else ""
    delivery_failed = payload.get("notice_delivery_failed", _MISSING)
    failure_reason = payload.get("notice_failure_reason", _MISSING)

    # 正常 Final 没有失败字段。若运行时没有生成任何可见正文，则在公共边界把它
    # 规范成一次明确的交付失败，让 UI 清掉流式草稿，而不是收到自己必然拒绝的空消息。
    if delivery_failed is _MISSING and failure_reason is _MISSING:
        public_payload["text"] = normalized_text
        if normalized_text == "":
            public_payload["notice_delivery_failed"] = True
        return public_payload

    if delivery_failed is not True:
        raise ValueError("assistant_public_final_invalid_failure_flag")

    public_payload["text"] = normalized_text
    public_payload["notice_delivery_failed"] = True
    if failure_reason is _MISSING:
        return public_payload
    if failure_reason != "provider_protocol_leak":
        raise ValueError("assistant_public_final_invalid_failure_reason")
    if normalized_text != "":
        raise ValueError("assistant_public_final_invalid_failure_combination")
    public_payload["notice_failure_reason"] = "provider_protocol_leak"
    return public_payload
