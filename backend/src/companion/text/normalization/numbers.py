from __future__ import annotations

import cn2an


def digits_to_chinese(digits: str) -> str:
    """把数字串逐位转换为中文读法。

    Args:
        digits: 只包含阿拉伯数字的字符串。

    Returns:
        逐位转换后的中文数字。
    """

    return "".join(cn2an.an2cn(digit) for digit in digits)


def cardinal_to_chinese(number: str) -> str:
    """把整数或小数转换为中文基数读法。

    Args:
        number: 可包含负号、千位逗号和小数点的数字。

    Returns:
        转换后的中文读法；转换失败时原样返回。
    """

    normalized = number.strip().replace(",", "")
    if not normalized:
        return number

    sign = ""
    unsigned = normalized
    if unsigned.startswith("-"):
        sign = "负"
        unsigned = unsigned[1:]
    elif unsigned.startswith("+"):
        sign = "正"
        unsigned = unsigned[1:]

    if not unsigned:
        return number

    if "." not in unsigned and len(unsigned) > 1 and unsigned.startswith("0"):
        return f"{sign}{digits_to_chinese(unsigned)}"

    try:
        return f"{sign}{cn2an.an2cn(unsigned)}"
    except (ValueError, TypeError):
        return number


def force_number_to_chinese(number: str) -> str:
    """强制把合法数字文本转换为不含阿拉伯数字的中文读法。

    Args:
        number: 正则规则识别出的整数或小数。

    Returns:
        优先使用基数读法；超出 cn2an 范围时退化为逐位读法。
    """

    converted = cardinal_to_chinese(number)
    if not any(character.isdigit() for character in converted):
        return converted

    replacements = {"-": "负", "+": "正", ".": "点", ",": ""}
    return "".join(
        digits_to_chinese(character) if character.isdigit() else replacements.get(character, character)
        for character in number
    )


def identifier_to_chinese(identifier: str) -> str:
    """把编号中的每一位数字转成中文并保留分隔符。

    Args:
        identifier: 数字以及编号分隔符组成的文本。

    Returns:
        数字逐位转换后的编号。
    """

    return "".join(digits_to_chinese(character) if character.isdigit() else character for character in identifier)


def year_to_chinese(year: str) -> str:
    """把四位年份转换为逐位中文读法。

    Args:
        year: 四位阿拉伯数字年份。

    Returns:
        逐位转换后的年份。
    """

    return digits_to_chinese(year)
