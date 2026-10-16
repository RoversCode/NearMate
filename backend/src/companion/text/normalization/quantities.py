from __future__ import annotations

import re

from companion.text.normalization.lexicons import (
    CURRENCY_SYMBOLS,
    MEASUREMENT_UNITS,
    RANGE_UNITS,
    SPECIAL_DIGIT_NUMBERS,
    TWO_MEASURE_WORDS,
)
from companion.text.normalization.numbers import (
    cardinal_to_chinese,
    digits_to_chinese,
    year_to_chinese,
)
from companion.text.normalization.patterns import (
    BARE_TILDE_RANGE_RE,
    CURRENCY_PREFIX_RE,
    CURRENCY_SUFFIX_RE,
    CURRENCY_TEXT_RE,
    CONTEXTUAL_RATIO_RE,
    DISCOUNT_RE,
    GENERAL_NUMBER_RE,
    MEASUREMENT_RE,
    NUMBER_RANGE_RE,
    PERCENTAGE_RE,
    RATIO_RE,
)


def replace_contextual_ratios(text: str) -> str:
    """优先转换带显式语义前缀的比例。

    Args:
        text: 尚未执行时间规则的 TTS 文本。

    Returns:
        已转换显式比例且保留上下文前缀的文本。
    """

    return CONTEXTUAL_RATIO_RE.sub(
        lambda match: (
            f"{match.group('context')}{cardinal_to_chinese(match.group('left'))}比"
            f"{cardinal_to_chinese(match.group('right'))}"
        ),
        text,
    )


def replace_ranges(text: str) -> str:
    """转换带单位的数字范围。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换单位和范围连接词的文本。
    """

    result = BARE_TILDE_RANGE_RE.sub(
        lambda match: (
            f"{cardinal_to_chinese(match.group('start'))}至"
            f"{cardinal_to_chinese(match.group('end'))}"
        ),
        text,
    )
    return NUMBER_RANGE_RE.sub(_replace_number_range, result)


def replace_currencies(text: str) -> str:
    """转换货币符号及中文货币单位金额。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换货币读法的文本。
    """

    result = CURRENCY_PREFIX_RE.sub(_replace_prefixed_currency, text)
    result = CURRENCY_SUFFIX_RE.sub(_replace_suffixed_currency, result)
    return CURRENCY_TEXT_RE.sub(
        lambda match: (
            f"{cardinal_to_chinese(match.group('number'))}"
            f"{match.group('magnitude') or ''}{match.group('unit')}"
        ),
        result,
    )


def replace_percentages_ratios_and_discounts(text: str) -> str:
    """转换百分比、折扣和比例。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换比例类表达的文本。
    """

    result = PERCENTAGE_RE.sub(
        lambda match: f"百分之{cardinal_to_chinese(match.group('number'))}",
        text,
    )
    result = DISCOUNT_RE.sub(
        lambda match: f"{cardinal_to_chinese(match.group('number'))}折",
        result,
    )
    return RATIO_RE.sub(
        lambda match: (
            f"{cardinal_to_chinese(match.group('left'))}比"
            f"{cardinal_to_chinese(match.group('right'))}"
        ),
        result,
    )


def replace_measurements(text: str) -> str:
    """转换数字和计量单位组合。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换单位读法的文本。
    """

    def replace_measurement(match: re.Match[str]) -> str:
        """转换单个计量值并消除“零下负数”的重复语义。

        Args:
            match: 包含数值和单位捕获组的正则匹配。

        Returns:
            数值及单位的中文读法。
        """

        number = match.group("number")
        if number.startswith("-") and text[max(0, match.start() - 2) : match.start()] == "零下":
            number = number[1:]
        return f"{cardinal_to_chinese(number)}{MEASUREMENT_UNITS[match.group('unit')]}"

    return MEASUREMENT_RE.sub(replace_measurement, text)


def replace_general_numbers(text: str) -> str:
    """转换剩余的普通数字和高概率按位数字。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换普通数字读法的文本。
    """

    def replace_number(match: re.Match[str]) -> str:
        """按上下文选择普通数字的基数或逐位读法。

        Args:
            match: 包含剩余普通数字的正则匹配。

        Returns:
            适合当前数字上下文的中文读法。
        """

        number = match.group("number")
        unsigned = number.lstrip("+-").replace(",", "")
        integer_part = unsigned.split(".", maxsplit=1)[0]
        sign = "负" if number.startswith("-") else "正" if number.startswith("+") else ""
        if "." not in unsigned and (integer_part.startswith("0") or len(integer_part) > 8):
            return f"{sign}{digits_to_chinese(integer_part)}"

        next_text = text[match.end() : match.end() + 5].lstrip()
        if unsigned == "2" and any(next_text.startswith(word) for word in TWO_MEASURE_WORDS):
            return f"{sign}两"
        if any(next_text.startswith(word) for word in TWO_MEASURE_WORDS):
            return cardinal_to_chinese(number)
        if unsigned in SPECIAL_DIGIT_NUMBERS:
            return f"{sign}{digits_to_chinese(unsigned)}"
        return cardinal_to_chinese(number)

    return GENERAL_NUMBER_RE.sub(replace_number, text)


def _replace_number_range(match: re.Match[str]) -> str:
    """转换一个带明确结束单位的数字范围。

    Args:
        match: 包含起止数值和单位捕获组的正则匹配。

    Returns:
        单位一致时返回中文范围；单位冲突时返回原文。
    """

    start_unit = match.group("start_unit")
    end_unit = match.group("end_unit")
    if start_unit is not None and start_unit != end_unit:
        return match.group(0)

    start_reading = _range_number_to_chinese(match.group("start"), end_unit)
    end_reading = _range_number_to_chinese(match.group("end"), end_unit)
    if end_unit == "%":
        return f"百分之{start_reading}至百分之{end_reading}"
    if end_unit == "折":
        return f"{start_reading}折至{end_reading}折"

    unit_reading = RANGE_UNITS[end_unit]
    if start_unit is None:
        return f"{start_reading}至{end_reading}{unit_reading}"
    return f"{start_reading}{unit_reading}至{end_reading}{unit_reading}"


def _range_number_to_chinese(number: str, unit: str) -> str:
    """按区间单位选择数字读法。

    Args:
        number: 区间端点的数字文本。
        unit: 区间使用的语义单位。

    Returns:
        年份逐位读法或普通基数读法。
    """

    unsigned = number.lstrip("+-")
    if unit == "年" and len(unsigned) == 4 and unsigned.isdigit():
        prefix = "负" if number.startswith("-") else ""
        return f"{prefix}{year_to_chinese(unsigned)}"
    return cardinal_to_chinese(number)


def _replace_prefixed_currency(match: re.Match[str]) -> str:
    """转换货币符号位于数字前的金额。

    Args:
        match: 包含货币符号和金额捕获组的正则匹配。

    Returns:
        数值在前、货币名称在后的中文读法。
    """

    return f"{cardinal_to_chinese(match.group('number'))}{_currency_reading(match.group('symbol'))}"


def _replace_suffixed_currency(match: re.Match[str]) -> str:
    """转换货币符号位于数字后的金额。

    Args:
        match: 包含金额和货币符号捕获组的正则匹配。

    Returns:
        数值在前、货币名称在后的中文读法。
    """

    return f"{cardinal_to_chinese(match.group('number'))}{_currency_reading(match.group('symbol'))}"


def _currency_reading(symbol: str) -> str:
    """按大小写不敏感方式查找货币读法。

    Args:
        symbol: 货币符号或货币代码。

    Returns:
        已登记的中文货币名称；未知符号原样返回。
    """

    direct = CURRENCY_SYMBOLS.get(symbol)
    if direct is not None:
        return direct
    folded = symbol.casefold()
    for candidate, reading in CURRENCY_SYMBOLS.items():
        if candidate.casefold() == folded:
            return reading
    return symbol
