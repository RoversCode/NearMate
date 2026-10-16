"""定义 Activity 在后端各入口共享的 UUID 契约。"""

from __future__ import annotations

from uuid import RFC_4122, UUID


def canonical_activity_uuid(value: object) -> str | None:
    """校验并规范化 RFC 变体 v1-v8 UUID。

    Args:
        value: 来源事件或 HTTP 路径中的候选活动编号。

    Returns:
        标准小写 UUID 文本；版本或变体不合法时返回空。
    """

    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value.strip())
    except (ValueError, AttributeError):
        return None
    if parsed.variant != RFC_4122 or parsed.version not in range(1, 9):
        return None
    return str(parsed)
