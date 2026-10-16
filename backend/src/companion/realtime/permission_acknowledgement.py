"""提供鼠标权限卡授权后的确定性短确认。"""

from __future__ import annotations

from hashlib import sha256


PERMISSION_ACKNOWLEDGEMENT_TEXTS = (
    "好，本次授权已经生效。",
    "收到，这次操作已经允许。",
    "嗯，这次权限已经放行。",
)


def permission_acknowledgement_text(interaction_request_id: str) -> str:
    """按交互请求编号稳定选择一句权限确认。

    Args:
        interaction_request_id: 鼠标权限卡对应的后台交互请求编号。

    Returns:
        只确认本次授权已经生效的固定短句。

    Raises:
        ValueError: 交互请求编号为空时抛出。
    """

    digest = _request_digest(interaction_request_id)
    sentence_index = int.from_bytes(digest[:8], "big") % len(
        PERMISSION_ACKNOWLEDGEMENT_TEXTS
    )
    return PERMISSION_ACKNOWLEDGEMENT_TEXTS[sentence_index]


def permission_acknowledgement_request_id(interaction_request_id: str) -> str:
    """生成可跨重放保持一致的助手输出请求编号。

    Args:
        interaction_request_id: 鼠标权限卡对应的后台交互请求编号。

    Returns:
        不直接暴露原请求正文的稳定助手请求编号。

    Raises:
        ValueError: 交互请求编号为空时抛出。
    """

    return f"permission-ack-{_request_digest(interaction_request_id).hex()[:24]}"


def _request_digest(interaction_request_id: str) -> bytes:
    """把非空交互编号转换成稳定摘要。

    Args:
        interaction_request_id: 等待生成摘要的交互请求编号。

    Returns:
        请求编号的 SHA-256 二进制摘要。

    Raises:
        ValueError: 交互请求编号为空时抛出。
    """

    normalized_request_id = interaction_request_id.strip()
    if not normalized_request_id:
        raise ValueError("权限交互请求编号不能为空")
    return sha256(normalized_request_id.encode("utf-8")).digest()
