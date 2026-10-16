from __future__ import annotations

import re
from datetime import date

from companion.text.normalization.numbers import cardinal_to_chinese, year_to_chinese
from companion.text.normalization.patterns import (
    DATE_MDY_RE,
    DATE_YMD_RE,
    TIME_RANGE_RE,
    TIME_RE,
    YEAR_RANGE_RE,
    YEAR_RE,
)


_INVALID_DATE_OPEN = "\ue010"
_INVALID_DATE_CLOSE = "\ue011"


def protect_invalid_dates(text: str) -> tuple[str, tuple[str, ...]]:
    """保护外形像日期但日历值非法的 token。

    Args:
        text: 已完成全角数字转换的 TTS 文本。

    Returns:
        使用占位符替换非法日期后的文本，以及原始日期 token。
    """

    matches = sorted(
        (*DATE_YMD_RE.finditer(text), *DATE_MDY_RE.finditer(text)),
        key=lambda match: match.start(),
    )
    invalid_dates: list[str] = []
    output: list[str] = []
    cursor = 0
    for match in matches:
        if match.start() < cursor or _is_valid_date_match(match):
            continue
        output.append(text[cursor : match.start()])
        invalid_dates.append(match.group(0))
        output.append(_invalid_date_placeholder(len(invalid_dates) - 1))
        cursor = match.end()
    output.append(text[cursor:])
    return "".join(output), tuple(invalid_dates)


def restore_invalid_dates(text: str, invalid_dates: tuple[str, ...]) -> str:
    """恢复语义规则处理期间保护的非法日期 token。

    Args:
        text: 包含非法日期占位符的 TTS 文本。
        invalid_dates: 按出现顺序保存的原始 token。

    Returns:
        日期 token 已恢复且未被误读为减法的文本。
    """

    result = text
    for index, value in enumerate(invalid_dates):
        result = result.replace(_invalid_date_placeholder(index), value)
    return result


def replace_dates_and_times(text: str) -> str:
    """转换日期、年份、时间和时间范围。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换确定性日期时间表达的文本。
    """

    result = DATE_YMD_RE.sub(_replace_date, text)
    result = DATE_MDY_RE.sub(_replace_date, result)
    result = YEAR_RANGE_RE.sub(
        lambda match: (
            f"{year_to_chinese(match.group('start'))}年至"
            f"{year_to_chinese(match.group('end'))}年"
        ),
        result,
    )
    result = YEAR_RE.sub(lambda match: f"{year_to_chinese(match.group('year'))}年", result)
    result = TIME_RANGE_RE.sub(_replace_time_range, result)
    return TIME_RE.sub(_replace_time, result)


def _replace_date(match: re.Match[str]) -> str:
    """转换单个合法公历日期。

    Args:
        match: 包含年、月、日捕获组的正则匹配。

    Returns:
        合法日期的中文读法；日期非法时返回原文。
    """

    year = int(match.group("year"))
    month = int(match.group("month"))
    day = int(match.group("day"))
    if not _is_valid_date(year, month, day):
        return match.group(0)
    return f"{year_to_chinese(str(year).zfill(4))}年{cardinal_to_chinese(str(month))}月{cardinal_to_chinese(str(day))}日"


def _replace_time_range(match: re.Match[str]) -> str:
    """转换单个合法时间范围。

    Args:
        match: 包含起止时间捕获组的正则匹配。

    Returns:
        合法范围的中文读法；任一时间非法时返回原文。
    """

    start = _time_to_chinese(match.group("start"))
    end = _time_to_chinese(match.group("end"))
    if start is None or end is None:
        return match.group(0)
    return f"{start}至{end}"


def _replace_time(match: re.Match[str]) -> str:
    """转换单个合法时间。

    Args:
        match: 包含时间捕获组的正则匹配。

    Returns:
        合法时间的中文读法；时间非法时返回原文。
    """

    converted = _time_to_chinese(match.group("time"))
    return converted if converted is not None else match.group(0)


def _time_to_chinese(value: str) -> str | None:
    """把二十四小时制时间转换为中文读法。

    Args:
        value: `HH:MM` 或 `H:MM` 格式时间。

    Returns:
        合法时间的中文读法；二十四点后的非零分钟返回 None。
    """

    hour_text, minute_text = value.split(":", maxsplit=1)
    hour = int(hour_text)
    minute = int(minute_text)
    if hour == 24 and minute != 0:
        return None
    normalized_hour = 0 if hour == 24 else hour
    if minute == 0:
        return f"{cardinal_to_chinese(str(normalized_hour))}点整"
    return f"{cardinal_to_chinese(str(normalized_hour))}点{cardinal_to_chinese(str(minute))}分"


def _is_valid_date_match(match: re.Match[str]) -> bool:
    """判断日期正则匹配是否对应真实公历日期。

    Args:
        match: 包含年、月、日捕获组的日期匹配。

    Returns:
        日期能够由标准库构造时返回 `True`。
    """

    return _is_valid_date(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
    )


def _is_valid_date(year: int, month: int, day: int) -> bool:
    """使用标准库验证公历日期值。

    Args:
        year: 公历年份。
        month: 公历月份。
        day: 公历日期。

    Returns:
        日期合法时返回 `True`。
    """

    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _invalid_date_placeholder(index: int) -> str:
    """生成不含数字的非法日期占位符。

    Args:
        index: 非法日期的顺序编号。

    Returns:
        私用区字符包裹的稳定占位符。
    """

    value = index
    letters: list[str] = []
    while True:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("A") + remainder))
        if value == 0:
            break
        value -= 1
    return f"{_INVALID_DATE_OPEN}{''.join(reversed(letters))}{_INVALID_DATE_CLOSE}"
