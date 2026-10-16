from __future__ import annotations

from datetime import datetime


def describe_captured_at(value: str) -> str:
    """把环境快照采集时间转换成中文时间描述。

    Args:
        value: Desktop 上传的 ISO 时间文本。

    Returns:
        适合直接进入 prompt 的中文时间文本；解析失败时回退原文。
    """

    normalized_value = value.strip()
    if not normalized_value:
        return value

    iso_value = normalized_value[:-1] + "+00:00" if normalized_value.endswith("Z") else normalized_value
    try:
        captured_at = datetime.fromisoformat(iso_value)
    except ValueError:
        return normalized_value

    timezone_suffix = ""
    offset = captured_at.utcoffset()
    if offset is not None:
        total_seconds = int(offset.total_seconds())
        sign = "+" if total_seconds >= 0 else "-"
        total_seconds = abs(total_seconds)
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        timezone_suffix = f"（UTC{sign}{hours:02d}:{minutes:02d}）"

    return (
        f"{captured_at.year}年{captured_at.month}月{captured_at.day}日 "
        f"{captured_at.hour:02d}时{captured_at.minute:02d}分{captured_at.second:02d}秒"
        f"{timezone_suffix}"
    )
