from __future__ import annotations

from companion.text.normalization.numbers import (
    cardinal_to_chinese,
    digits_to_chinese,
    identifier_to_chinese,
)
from companion.text.normalization.patterns import (
    ALPHA_HYPHEN_DIGITS_RE,
    ALPHA_PREFIX_NUMBER_RE,
    HTTP_STATUS_RE,
    HYPHENATED_LANDLINE_RE,
    IDENTIFIER_CONTEXT_RE,
    MOBILE_PHONE_RE,
    MODEL_CONTEXT_RE,
    NUMBER_ALPHA_SUFFIX_RE,
    ORDINAL_RE,
    PHONE_CONTEXT_RE,
    ROOM_RE,
)


def replace_identifiers(text: str) -> str:
    """转换电话、业务编号、房间号和序数。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换明确编号语义的文本。
    """

    result = MOBILE_PHONE_RE.sub(lambda match: identifier_to_chinese(match.group(0)), text)
    result = HYPHENATED_LANDLINE_RE.sub(
        lambda match: identifier_to_chinese(match.group(0)).replace("-", ""),
        result,
    )
    result = PHONE_CONTEXT_RE.sub(
        lambda match: f"{match.group('context')}{identifier_to_chinese(match.group('number'))}",
        result,
    )
    result = IDENTIFIER_CONTEXT_RE.sub(
        lambda match: (
            f"{match.group('context')}{identifier_to_chinese(match.group('identifier'))}"
        ),
        result,
    )
    result = ROOM_RE.sub(
        lambda match: f"{digits_to_chinese(match.group('number'))}{match.group('suffix')}",
        result,
    )
    return ORDINAL_RE.sub(
        lambda match: f"第{cardinal_to_chinese(match.group('number'))}{match.group('suffix')}",
        result,
    )


def replace_model_and_version_numbers(text: str) -> str:
    """转换模型名、版本号和字母数字组合中的数字。

    Args:
        text: 待处理的 TTS 文本。

    Returns:
        已转换混排数字读法的文本。
    """

    result = MODEL_CONTEXT_RE.sub(
        lambda match: (
            f"{match.group('context')}{match.group('label') or ''}"
            f"{_model_context_number_to_chinese(match.group('number'))}"
        ),
        text,
    )
    result = HTTP_STATUS_RE.sub(
        lambda match: f"{match.group('prefix')}{digits_to_chinese(match.group('number'))}",
        result,
    )
    result = ALPHA_HYPHEN_DIGITS_RE.sub(
        lambda match: f"{match.group('prefix')}{digits_to_chinese(match.group('number'))}",
        result,
    )
    result = ALPHA_PREFIX_NUMBER_RE.sub(
        lambda match: f"{match.group('prefix')}{_alpha_number_to_chinese(match.group('number'))}",
        result,
    )
    return NUMBER_ALPHA_SUFFIX_RE.sub(
        lambda match: (
            f"{_version_number_to_chinese(match.group('number'))}{match.group('suffix')}"
        ),
        result,
    )


def _version_number_to_chinese(number: str) -> str:
    """转换可能包含多个点分段的版本数字。

    Args:
        number: 整数、小数或多段版本数字。

    Returns:
        使用“点”连接各数字段的中文读法。
    """

    return "点".join(cardinal_to_chinese(part) for part in number.split("."))


def _model_context_number_to_chinese(number: str) -> str:
    """按模型或型号上下文转换数字。

    Args:
        number: 型号上下文中的整数或点分数字。

    Returns:
        整数逐位、点分值按版本格式转换后的文本。
    """

    if "." in number:
        return _version_number_to_chinese(number)
    return digits_to_chinese(number)


def _alpha_number_to_chinese(number: str) -> str:
    """转换紧邻字母前缀的型号或版本数字。

    Args:
        number: 字母前缀后的整数或点分数字。

    Returns:
        整数逐位、点分值按版本格式转换后的文本。
    """

    if "." in number:
        return _version_number_to_chinese(number)
    return digits_to_chinese(number)
