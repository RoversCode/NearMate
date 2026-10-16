from __future__ import annotations

import re

from companion.text.normalization.lexicons import (
    CURRENCY_SYMBOLS,
    CURRENCY_UNITS,
    IDENTIFIER_CONTEXTS,
    MAGNITUDE_UNITS,
    MEASUREMENT_UNITS,
    RANGE_UNITS,
)


UNSIGNED_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
SIGNED_NUMBER = rf"[+-]?{UNSIGNED_NUMBER}"

CONTROL_TAG_RE = re.compile(r"\[[A-Za-z][^\[\]\r\n]{0,79}\]")
LITERAL_RE = re.compile(
    r"^[ \t]*```[^\r\n]*\n.*?^[ \t]*```[ \t]*$|"
    r"`[^`\r\n]*`|"
    r"https?://[^\s，。！？；]+|"
    r"www\.[^\s，。！？；]+|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
    r"[A-Za-z]:\\[^\s，。！？；]+|"
    r"(?:[A-Za-z0-9._-]+\\)+[A-Za-z0-9._-]+|"
    r"(?<![A-Za-z0-9])(?:\.{1,2}/|/)(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+|"
    r"(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+\.[A-Za-z0-9._-]+|"
    r"(?<=(?:路径|目录|文件))[ \t]*(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

DATE_YMD_RE = re.compile(
    r"(?<![0-9])(?P<year>[0-9]{4})[-/](?P<month>[0-9]{1,2})[-/](?P<day>[0-9]{1,2})(?![0-9])"
)
DATE_MDY_RE = re.compile(
    r"(?<![0-9])(?P<month>[0-9]{1,2})/(?P<day>[0-9]{1,2})/(?P<year>[0-9]{4})(?![0-9])"
)
YEAR_RE = re.compile(r"(?<![0-9])(?P<year>[0-9]{4})\s*年")
YEAR_RANGE_RE = re.compile(
    r"(?<![0-9])(?P<start>[0-9]{4})\s*年\s*(?:-|~|至|到)\s*(?P<end>[0-9]{4})\s*年"
)

TIME_ATOM = r"(?:[01]?[0-9]|2[0-4]):[0-5][0-9]"
TIME_RANGE_RE = re.compile(
    rf"(?<![0-9])(?P<start>{TIME_ATOM})\s*(?:-|~|至|到)\s*(?P<end>{TIME_ATOM})(?![0-9])"
)
TIME_RE = re.compile(rf"(?<![0-9:])(?P<time>{TIME_ATOM})(?![0-9:])")

MOBILE_PHONE_RE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
HYPHENATED_LANDLINE_RE = re.compile(r"(?<![0-9])0[0-9]{2,3}-[0-9]{7,8}(?![0-9])")
PHONE_CONTEXT_RE = re.compile(
    r"(?P<context>电话|手机|热线|联系方式)\s*[:：]?\s*(?P<number>[0-9]{7,12})(?![0-9])"
)

IDENTIFIER_CONTEXT_PATTERN = "|".join(
    re.escape(value) for value in sorted(IDENTIFIER_CONTEXTS, key=len, reverse=True)
)
IDENTIFIER_CONTEXT_RE = re.compile(
    rf"(?P<context>{IDENTIFIER_CONTEXT_PATTERN})\s*[:：]?\s*"
    rf"(?P<identifier>[A-Za-z0-9][A-Za-z0-9_.:/-]*)"
)
ROOM_RE = re.compile(r"(?<![A-Za-z0-9])(?P<number>[0-9]+)(?P<suffix>号房|房间|房|室)")
ORDINAL_RE = re.compile(r"第\s*(?P<number>[0-9]+(?:\.[0-9]+)?)(?P<suffix>[\u4e00-\u9fff]?)")

CURRENCY_SYMBOL_PATTERN = "|".join(
    re.escape(value) for value in sorted(CURRENCY_SYMBOLS, key=len, reverse=True)
)
CURRENCY_PREFIX_RE = re.compile(
    rf"(?P<symbol>{CURRENCY_SYMBOL_PATTERN})\s*(?P<number>{SIGNED_NUMBER})",
    re.IGNORECASE,
)
CURRENCY_SUFFIX_RE = re.compile(
    rf"(?P<number>{SIGNED_NUMBER})\s*(?P<symbol>{CURRENCY_SYMBOL_PATTERN})(?![A-Za-z])",
    re.IGNORECASE,
)
CURRENCY_UNIT_PATTERN = "|".join(
    re.escape(value) for value in sorted(CURRENCY_UNITS, key=len, reverse=True)
)
MAGNITUDE_PATTERN = "|".join(
    re.escape(value) for value in sorted(MAGNITUDE_UNITS, key=len, reverse=True)
)
CURRENCY_TEXT_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<number>{SIGNED_NUMBER})\s*"
    rf"(?P<magnitude>{MAGNITUDE_PATTERN})?\s*(?P<unit>{CURRENCY_UNIT_PATTERN})"
)

RANGE_UNIT_PATTERN = "|".join(
    re.escape(value) for value in sorted(RANGE_UNITS, key=len, reverse=True)
)
NUMBER_RANGE_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<start>{SIGNED_NUMBER})\s*(?P<start_unit>{RANGE_UNIT_PATTERN})?"
    rf"\s*(?:~|-)\s*(?P<end>{SIGNED_NUMBER})\s*(?P<end_unit>{RANGE_UNIT_PATTERN})(?![A-Za-z0-9_])"
)

PERCENTAGE_RE = re.compile(rf"(?<![A-Za-z0-9_])(?P<number>{SIGNED_NUMBER})\s*%")
DISCOUNT_RE = re.compile(rf"(?<![A-Za-z0-9_])(?P<number>{SIGNED_NUMBER})\s*折")
RATIO_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<left>{SIGNED_NUMBER})\s*:\s*(?P<right>{SIGNED_NUMBER})(?![A-Za-z0-9_])"
)
BARE_TILDE_RANGE_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<start>{SIGNED_NUMBER})\s*~\s*"
    rf"(?P<end>{SIGNED_NUMBER})(?![A-Za-z0-9_])"
)
CONTEXTUAL_RATIO_RE = re.compile(
    rf"(?P<context>比例|比值|竞争比)\s*[:：]?\s*"
    rf"(?P<left>{SIGNED_NUMBER})\s*:\s*(?P<right>{SIGNED_NUMBER})(?![A-Za-z0-9_])"
)

MEASUREMENT_UNIT_PATTERN = "|".join(
    re.escape(value) for value in sorted(MEASUREMENT_UNITS, key=len, reverse=True)
)
MEASUREMENT_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<number>{SIGNED_NUMBER})\s*(?P<unit>{MEASUREMENT_UNIT_PATTERN})(?![A-Za-z0-9_])"
)

ALPHA_HYPHEN_DIGITS_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9_]*)-(?P<number>[0-9]+)(?![A-Za-z0-9_])"
)
ALPHA_PREFIX_NUMBER_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z_-]*?)(?P<number>[0-9]+(?:\.[0-9]+)*)(?![A-Za-z0-9_.])"
)
NUMBER_ALPHA_SUFFIX_RE = re.compile(
    rf"(?<![A-Za-z0-9_.])(?P<number>{UNSIGNED_NUMBER})(?P<suffix>[A-Za-z]{{1,8}})(?![A-Za-z0-9_])"
)
MODEL_CONTEXT_RE = re.compile(
    r"(?P<context>型号|模型|版本)\s*(?P<label>[A-Za-z][A-Za-z_-]*)?\s*"
    r"(?P<number>[0-9]+(?:\.[0-9]+)*)(?![0-9.])"
)
HTTP_STATUS_RE = re.compile(r"(?P<prefix>HTTP\s+)(?P<number>[0-9]{3})(?![0-9])", re.IGNORECASE)

PLUS_MINUS_NUMBER_RE = re.compile(rf"±\s*(?P<number>{UNSIGNED_NUMBER})")
POWER_SUFFIX_RE = re.compile(
    r"(?P<base>[A-Za-zα-ωΑ-Ω]|[0-9]+|[零一二三四五六七八九十百千万亿两]+)(?P<power>[²³])"
)

FORMULA_ATOM = (
    r"(?:-?[0-9]+(?:\.[0-9]+)?[A-Za-z]?[²³]?|"
    r"[A-Za-zα-ωΑ-Ω]+(?:\([A-Za-z0-9α-ωΑ-Ω]+"
    r"(?:\s*,\s*[A-Za-z0-9α-ωΑ-Ω]+)*\))?[²³]?)"
)
FORMULA_OPERATOR = r"(?:!=|>=|<=|≥|≤|≈|≠|±|[+*×÷/=><-])"
FORMULA_GROUP = rf"(?:\({FORMULA_ATOM}(?:\s*{FORMULA_OPERATOR}\s*{FORMULA_ATOM})*\))"
FORMULA_OPERAND = rf"(?:{FORMULA_GROUP}|{FORMULA_ATOM})"
FORMULA_RE = re.compile(
    rf"(?<![A-Za-z0-9_/])(?P<formula>{FORMULA_OPERAND}(?:\s*{FORMULA_OPERATOR}\s*{FORMULA_OPERAND})+)(?![A-Za-z0-9_/])"
)

GENERAL_NUMBER_RE = re.compile(rf"(?<![A-Za-z0-9_.])(?P<number>{SIGNED_NUMBER})(?![A-Za-z0-9_.])")
RESIDUAL_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")

INLINE_WHITESPACE_RE = re.compile(r"[\t \f\v]+")
CHINESE_INNER_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
CHINESE_BEFORE_PUNCTUATION_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[，。！？；：、])")
PUNCTUATION_BEFORE_CHINESE_RE = re.compile(r"(?<=[，。！？；：、])\s+(?=[\u4e00-\u9fff])")


FULLWIDTH_TRANSLATION = str.maketrans(
    {
        **{chr(ord("０") + index): str(index) for index in range(10)},
        "％": "%",
        "：": ":",
        "～": "~",
        "－": "-",
        "＋": "+",
        "＊": "*",
        "／": "/",
        "＝": "=",
        "＞": ">",
        "＜": "<",
        "．": ".",
    }
)
