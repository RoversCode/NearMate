from __future__ import annotations

import re

from companion.text.normalization.lexicons import GREEK_READINGS, OPERATOR_READINGS
from companion.text.normalization.numbers import cardinal_to_chinese
from companion.text.normalization.patterns import FORMULA_RE, PLUS_MINUS_NUMBER_RE, POWER_SUFFIX_RE


_FORMULA_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_FORMULA_OPERATOR_RE = re.compile(
    "|".join(re.escape(value) for value in sorted(OPERATOR_READINGS, key=len, reverse=True))
)
_UNARY_NEGATIVE_RE = re.compile(
    r"(?P<prefix>^|!=|>=|<=|≥|≤|≈|≠|[+*×÷/=><(-])\s*-\s*(?P<number>[0-9]+(?:\.[0-9]+)?)"
)
_AMBIGUOUS_NUMERIC_HYPHEN_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?-[0-9]+(?:\.[0-9]+)?")
_AMBIGUOUS_WORD_HYPHEN_RE = re.compile(
    r"(?:[A-Za-z]{2,}-[A-Za-z]+|[A-Za-z]+-[A-Za-z]{2,})"
)


def replace_formulas(text: str) -> str:
    """转换能够确定为公式的运算符、上标和数字。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换公式读法的文本；裸数字短横线表达保持原连接符。
    """

    result = PLUS_MINUS_NUMBER_RE.sub(
        lambda match: f"正负{cardinal_to_chinese(match.group('number'))}",
        text,
    )
    result = FORMULA_RE.sub(_replace_formula, result)
    return POWER_SUFFIX_RE.sub(_replace_power_suffix, result)


def _replace_formula(match: re.Match[str]) -> str:
    """转换单个已识别公式并保留括号结构。

    Args:
        match: 包含完整公式捕获组的正则匹配。

    Returns:
        运算符和数字已口语化的公式；裸数字短横线表达返回原文。
    """

    formula = match.group("formula")
    if (
        _AMBIGUOUS_NUMERIC_HYPHEN_RE.fullmatch(formula)
        or _AMBIGUOUS_WORD_HYPHEN_RE.fullmatch(formula)
    ):
        return formula

    converted = _UNARY_NEGATIVE_RE.sub(
        lambda negative_match: (
            f"{negative_match.group('prefix')}负"
            f"{cardinal_to_chinese(negative_match.group('number'))}"
        ),
        formula,
    )
    converted = _FORMULA_NUMBER_RE.sub(
        lambda number_match: cardinal_to_chinese(number_match.group(0)),
        converted,
    )
    converted = _FORMULA_OPERATOR_RE.sub(
        lambda operator_match: OPERATOR_READINGS[operator_match.group(0)],
        converted,
    )
    for symbol, reading in GREEK_READINGS.items():
        converted = converted.replace(symbol, reading)
    return re.sub(r"\s+", "", converted)


def _replace_power_suffix(match: re.Match[str]) -> str:
    """转换变量或数字后的平方、立方上标。

    Args:
        match: 包含底数和上标捕获组的正则匹配。

    Returns:
        保留底数并口语化上标的表达。
    """

    base = match.group("base")
    if base.isdigit():
        base = cardinal_to_chinese(base)
    return f"{base}{'平方' if match.group('power') == '²' else '立方'}"
