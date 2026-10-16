from __future__ import annotations

import re

from companion.text.normalization.numbers import force_number_to_chinese
from companion.text.normalization.entities import (
    replace_identifiers,
    replace_model_and_version_numbers,
)
from companion.text.normalization.formulas import replace_formulas
from companion.text.normalization.patterns import (
    CHINESE_BEFORE_PUNCTUATION_RE,
    CHINESE_INNER_SPACE_RE,
    CONTROL_TAG_RE,
    FULLWIDTH_TRANSLATION,
    INLINE_WHITESPACE_RE,
    LITERAL_RE,
    PUNCTUATION_BEFORE_CHINESE_RE,
    RESIDUAL_NUMBER_RE,
)
from companion.text.normalization.quantities import (
    replace_contextual_ratios,
    replace_currencies,
    replace_general_numbers,
    replace_measurements,
    replace_percentages_ratios_and_discounts,
    replace_ranges,
)
from companion.text.normalization.temporal import (
    protect_invalid_dates,
    replace_dates_and_times,
    restore_invalid_dates,
)


_CONTROL_PLACEHOLDER_OPEN = "\ue000"
_CONTROL_PLACEHOLDER_CLOSE = "\ue001"
_LITERAL_PLACEHOLDER_OPEN = "\ue002"
_LITERAL_PLACEHOLDER_CLOSE = "\ue003"


def normalize_tts_text(text: str, *, force_remaining_digits: bool) -> str:
    """将待送入 TTS 的句段转换为确定性中文读法。

    Args:
        text: 已经过 StreamVox 标签白名单清洗的完整句段。
        force_remaining_digits: 是否在语义规则完成后强制转换残留数字。

    Returns:
        适合当前语音模型朗读的文本。
    """

    if not text:
        return text

    normalized, control_tags = _protect_matches(
        text,
        CONTROL_TAG_RE,
        opener=_CONTROL_PLACEHOLDER_OPEN,
        closer=_CONTROL_PLACEHOLDER_CLOSE,
    )
    normalized, literals = _protect_matches(
        normalized,
        LITERAL_RE,
        opener=_LITERAL_PLACEHOLDER_OPEN,
        closer=_LITERAL_PLACEHOLDER_CLOSE,
    )
    normalized = normalized.translate(FULLWIDTH_TRANSLATION)
    normalized = replace_identifiers(normalized)
    normalized = replace_contextual_ratios(normalized)
    normalized, invalid_dates = protect_invalid_dates(normalized)
    normalized = replace_dates_and_times(normalized)
    normalized = replace_ranges(normalized)
    normalized = replace_currencies(normalized)
    normalized = replace_percentages_ratios_and_discounts(normalized)
    normalized = replace_measurements(normalized)
    normalized = replace_model_and_version_numbers(normalized)
    normalized = replace_formulas(normalized)
    normalized = replace_general_numbers(normalized)
    normalized = _clean_spacing(normalized)
    normalized = restore_invalid_dates(normalized, invalid_dates)
    normalized = _restore_matches(
        normalized,
        literals,
        opener=_LITERAL_PLACEHOLDER_OPEN,
        closer=_LITERAL_PLACEHOLDER_CLOSE,
    )

    # Qwen3 和 MOSS 无法稳定处理残留数字，最终兜底只改变读法，不猜测数字的业务含义。
    if force_remaining_digits:
        normalized = RESIDUAL_NUMBER_RE.sub(
            lambda match: force_number_to_chinese(match.group(0)),
            normalized,
        )
    normalized = _restore_matches(
        normalized,
        control_tags,
        opener=_CONTROL_PLACEHOLDER_OPEN,
        closer=_CONTROL_PLACEHOLDER_CLOSE,
    )
    return normalized


def _protect_matches(
    text: str,
    pattern: re.Pattern[str],
    *,
    opener: str,
    closer: str,
) -> tuple[str, tuple[str, ...]]:
    """用独立命名空间保护一类非语义匹配片段。

    Args:
        text: 当前待保护的 TTS 文本。
        pattern: 用于查找完整保护片段的正则表达式。
        opener: 当前保护类型的占位符起始字符。
        closer: 当前保护类型的占位符结束字符。

    Returns:
        使用占位符替换后的文本，以及按占位符顺序保存的原始片段。
    """

    protected_spans: list[str] = []
    output: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        output.append(text[cursor : match.start()])
        protected_spans.append(match.group(0))
        output.append(_placeholder(len(protected_spans) - 1, opener=opener, closer=closer))
        cursor = match.end()
    output.append(text[cursor:])
    return "".join(output), tuple(protected_spans)


def _restore_matches(
    text: str,
    protected_spans: tuple[str, ...],
    *,
    opener: str,
    closer: str,
) -> str:
    """恢复指定命名空间中的原始保护片段。

    Args:
        text: 包含保护占位符的正则化文本。
        protected_spans: 按编号保存的原始片段。
        opener: 当前保护类型的占位符起始字符。
        closer: 当前保护类型的占位符结束字符。

    Returns:
        恢复控制标签和字面量后的文本。
    """

    result = text
    for index, value in enumerate(protected_spans):
        result = result.replace(_placeholder(index, opener=opener, closer=closer), value)
    return result


def _placeholder(index: int, *, opener: str, closer: str) -> str:
    """生成不会被数字或字母规则命中的稳定占位符。

    Args:
        index: 被保护片段的顺序编号。
        opener: 当前保护类型的占位符起始字符。
        closer: 当前保护类型的占位符结束字符。

    Returns:
        由私用区字符包裹的无数字占位符。
    """

    value = index
    letters: list[str] = []
    while True:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("A") + remainder))
        if value == 0:
            break
        value -= 1
    return f"{opener}{''.join(reversed(letters))}{closer}"


def _clean_spacing(text: str) -> str:
    """清理由规则替换产生的冗余行内空白。

    Args:
        text: 完成语义转换的 TTS 文本。

    Returns:
        保留原始标点且空白稳定的文本。
    """

    normalized = INLINE_WHITESPACE_RE.sub(" ", text).strip()
    normalized = CHINESE_INNER_SPACE_RE.sub("", normalized)
    normalized = CHINESE_BEFORE_PUNCTUATION_RE.sub("", normalized)
    return PUNCTUATION_BEFORE_CHINESE_RE.sub("", normalized)
