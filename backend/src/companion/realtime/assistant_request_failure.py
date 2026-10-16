"""定义用户消息已提交但助手请求未启动时的公共失败事件。"""

from __future__ import annotations

from typing import Literal

AssistantRequestFailureCode = Literal[
    "request_start_failed",
    "output_unavailable",
    "request_identity_conflict",
]

ASSISTANT_REQUEST_FAILURE_MESSAGES: dict[AssistantRequestFailureCode, str] = {
    "request_start_failed": "消息已经发送，但我现在没能开始回复，请稍后再试。",
    "output_unavailable": "消息已经发送，但助手输出暂时不可用，请重新连接后再试。",
    "request_identity_conflict": "消息已经发送，但助手输出身份发生冲突，已暂停继续发送。",
}


def build_assistant_request_failed_event(
    request_id: str,
    *,
    code: AssistantRequestFailureCode,
) -> dict[str, object]:
    """构造只供聊天界面展示的助手启动失败事件。

    Args:
        request_id: 已显示用户消息对应的助手请求编号。
        code: 不包含内部异常详情的稳定失败分类。

    Returns:
        严格包含请求编号和用户可见文案的控制事件。
    """

    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        raise ValueError("assistant request failure requires request_id")
    return {
        "type": "assistant.request_failed",
        "payload": {
            "request_id": normalized_request_id,
            "code": code,
            "message": ASSISTANT_REQUEST_FAILURE_MESSAGES[code],
        },
    }
