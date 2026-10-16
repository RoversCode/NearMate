from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal


FinalTranscriptIgnoredReason = Literal["empty", "single_cjk_character", "single_english_word"]


@dataclass(frozen=True)
class FinalTranscriptDecision:
    """final ASR 文本是否足以触发助手回复的判定结果。

    Args:
        should_request_assistant: 是否应该把该 final ASR 投递给助手输出侧。
        ignored_reason: 被忽略时的原因；可回复文本固定为 None。
    """

    should_request_assistant: bool
    ignored_reason: FinalTranscriptIgnoredReason | None = None


def classify_final_transcript(text: str) -> FinalTranscriptDecision:
    """判断 final ASR 文本是否应该触发助手回复。

    Args:
        text: ASR provider 返回的最终转写文本。

    Returns:
        FinalTranscriptDecision: 包含是否入队助手请求和忽略原因的判定结果。
    """

    tokens = _content_tokens_without_punctuation(text)
    if not tokens:
        return FinalTranscriptDecision(should_request_assistant=False, ignored_reason="empty")

    if len(tokens) != 1:
        return FinalTranscriptDecision(should_request_assistant=True)

    token_kind, _ = tokens[0]
    if token_kind == "cjk":
        return FinalTranscriptDecision(should_request_assistant=False, ignored_reason="single_cjk_character")
    if token_kind == "english":
        return FinalTranscriptDecision(should_request_assistant=False, ignored_reason="single_english_word")

    return FinalTranscriptDecision(should_request_assistant=True)


def _content_tokens_without_punctuation(text: str) -> list[tuple[str, str]]:
    """提取去除空白和标点后的内容 token。

    Args:
        text: 原始 final ASR 文本。

    Returns:
        list[tuple[str, str]]: token 类型和文本内容组成的列表。
    """

    tokens: list[tuple[str, str]] = []
    english_buffer: list[str] = []

    for character in text:
        if _is_ignored_separator(character):
            _flush_english_buffer(tokens, english_buffer)
            continue

        if character.isascii() and character.isalpha():
            english_buffer.append(character)
            continue

        _flush_english_buffer(tokens, english_buffer)
        if _is_cjk_character(character):
            tokens.append(("cjk", character))
            continue

        tokens.append(("other", character))

    _flush_english_buffer(tokens, english_buffer)
    return tokens


def _flush_english_buffer(tokens: list[tuple[str, str]], english_buffer: list[str]) -> None:
    """把连续英文字符合并为一个英文词 token。

    Args:
        tokens: 正在构造的内容 token 列表。
        english_buffer: 尚未写入 tokens 的连续英文字符。

    Returns:
        None: 该函数会原地更新 tokens 和 english_buffer。
    """

    if not english_buffer:
        return

    tokens.append(("english", "".join(english_buffer)))
    english_buffer.clear()


def _is_ignored_separator(character: str) -> bool:
    """判断字符是否应在短文本判定中被忽略。

    Args:
        character: 单个 Unicode 字符。

    Returns:
        bool: 空白或标点返回 True，其它内容字符返回 False。
    """

    return character.isspace() or unicodedata.category(character).startswith("P")


def _is_cjk_character(character: str) -> bool:
    """判断字符是否属于常见 CJK 表意文字区间。

    Args:
        character: 单个 Unicode 字符。

    Returns:
        bool: 属于 CJK 统一表意文字相关区间时返回 True。
    """

    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x2CEB0 <= codepoint <= 0x2EBEF
        or 0x30000 <= codepoint <= 0x3134F
    )
