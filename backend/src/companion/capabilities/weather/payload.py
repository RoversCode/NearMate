from __future__ import annotations

from typing import Any


def object_field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """安全读取对象字段。

    Args:
        payload: 和风响应对象。
        key: 需要读取的字段名。

    Returns:
        字段值为 dict 时返回原对象，否则返回空字典。
    """

    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def list_field(payload: dict[str, Any], key: str) -> list[Any]:
    """安全读取列表字段。

    Args:
        payload: 和风响应对象。
        key: 需要读取的字段名。

    Returns:
        字段值为 list 时返回原列表，否则返回空列表。
    """

    value = payload.get(key)
    return value if isinstance(value, list) else []


def object_items(value: Any) -> list[dict[str, Any]]:
    """从外部列表中过滤出对象条目。

    Args:
        value: 和风返回的列表或任意异常值。

    Returns:
        只包含 dict 条目的列表。
    """

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def text_field(payload: dict[str, Any], key: str) -> str:
    """安全读取短文本字段。

    Args:
        payload: 和风响应对象或条目对象。
        key: 需要读取的字段名。

    Returns:
        裁剪后的字符串；空值、None 或异常类型返回空字符串。
    """

    value = payload.get(key)
    if value is None:
        return ""
    return str(value).strip()


def first_useful_item(items: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any]:
    """选择首个包含有效核心字段的对象。

    Args:
        items: 已过滤出的对象列表。
        keys: 用于判断条目是否可用的字段名。

    Returns:
        首个至少包含一个非空核心字段的对象；没有命中时返回空字典。
    """

    for item in items:
        if any(text_field(item, key) for key in keys):
            return item
    return {}
