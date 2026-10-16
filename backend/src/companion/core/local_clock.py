from __future__ import annotations

from datetime import datetime


WEEKDAY_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def local_wall_datetime() -> datetime:
    """返回当前本地真实时间。

    Returns:
        带本机本地时区信息的真实日期时间。
    """

    return datetime.now().astimezone()


def format_local_day(current_datetime: datetime) -> str:
    """格式化本地自然日文本。

    Args:
        current_datetime: 需要格式化的日期时间。

    Returns:
        包含年月日和星期的中文日期文本。
    """

    weekday_text = WEEKDAY_NAMES[current_datetime.weekday()]
    return f"{current_datetime.year}年{current_datetime.month}月{current_datetime.day}日 {weekday_text}"


def format_local_datetime_text(current_datetime: datetime) -> str:
    """返回包含日期、星期、时段、秒和时区偏移的本地时间文本。

    Args:
        current_datetime: 需要格式化的日期时间。

    Returns:
        适合直接注入提示词的中文本地时间文本。
    """

    localized_datetime = current_datetime
    if localized_datetime.tzinfo is None or localized_datetime.utcoffset() is None:
        localized_datetime = localized_datetime.astimezone()
    hour = localized_datetime.hour
    if 0 <= hour < 6:
        period = "凌晨"
    elif hour < 9:
        period = "早上"
    elif hour < 12:
        period = "上午"
    elif hour < 14:
        period = "中午"
    elif hour < 18:
        period = "下午"
    else:
        period = "晚上"

    utc_offset = localized_datetime.strftime("UTC%z")
    formatted_offset = f"{utc_offset[:-2]}:{utc_offset[-2:]}"
    return (
        f"{format_local_day(localized_datetime)} {period}{localized_datetime.hour}时"
        f"{localized_datetime.minute:02d}分{localized_datetime.second:02d}秒 "
        f"{formatted_offset}"
    )
