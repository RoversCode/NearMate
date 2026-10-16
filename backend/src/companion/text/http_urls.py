from __future__ import annotations

import re
from urllib.parse import urlsplit


HTTP_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'`，。；！？、）】“”‘’《》〈〉「」『』]+",
    re.IGNORECASE,
)
TRAILING_URL_PUNCTUATION = ".,;:!?，。；：！？、）】`”’》〉」』"
URL_BRACKET_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"))
SPEECH_TRAILING_CANDIDATE_SUFFIX = (
    TRAILING_URL_PUNCTUATION.replace(":", "") + ")]}'\" \t\r\n"
)
_STREAMING_HTTP_SCHEME_PREFIXES = (
    "https://",
    "https:/",
    "https:",
    "https",
    "http://",
    "http:/",
    "http:",
    "http",
    "htt",
    "ht",
    "h",
)


def is_http_url(value: str) -> bool:
    """判断整串文本是否为结构完整的 HTTP(S) URL。

    Args:
        value: 待校验文本。

    Returns:
        文本非空、不含空白并具有 HTTP(S) scheme 与网络位置时返回真。
    """

    normalized = value.strip()
    if not normalized or any(character.isspace() for character in normalized):
        return False
    try:
        parsed = urlsplit(normalized)
        # 访问 hostname 会触发端口、方括号等结构校验，避免仅检查 netloc 放过坏 URL。
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(hostname)


def normalize_http_urls(values: list[str]) -> list[str]:
    """校验并保序去重一组 HTTP(S) URL。

    Args:
        values: 待校验的原始字符串列表。

    Returns:
        去除首尾空白、过滤非法项并保留首次出现顺序的 URL 列表。
    """

    normalized_urls: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized in seen or not is_http_url(normalized):
            continue
        seen.add(normalized)
        normalized_urls.append(normalized)
    return normalized_urls


def find_http_url_spans(text: str) -> tuple[tuple[int, int], ...]:
    """定位文本中可独立替换的 HTTP(S) URL 区间。

    Args:
        text: 可能包含一个或多个 URL 的普通文本。

    Returns:
        使用 Python 代码点下标表示的左闭右开区间。
    """

    spans: list[tuple[int, int]] = []
    for match in HTTP_URL_PATTERN.finditer(text):
        candidate = _trim_external_url_punctuation(match.group(0))
        if not candidate or not is_http_url(candidate):
            continue
        spans.append((match.start(), match.start() + len(candidate)))
    return tuple(spans)


def find_trailing_http_url_candidate_span(text: str) -> tuple[int, int] | None:
    """定位可能被后续流式增量继续补全的缓冲尾部 HTTP 区间。

    Args:
        text: 当前尚未完全分句的 LLM 文本缓冲。

    Returns:
        延伸到缓冲末尾的候选左闭右开区间；不存在时返回空。
    """

    if not text:
        return None

    # 已具有完整 scheme 的尾部 token 仍可能继续追加路径、查询参数或标点，
    # 因此这里保留词法原始区间，不执行面向最终替换的尾部标点裁剪。
    for match in HTTP_URL_PATTERN.finditer(text):
        if match.end() == len(text):
            return match.start(), match.end()

    folded_text = text.casefold()
    for prefix in _STREAMING_HTTP_SCHEME_PREFIXES:
        if not folded_text.endswith(prefix):
            continue
        start = len(text) - len(prefix)
        if start == 0 or _is_streaming_scheme_boundary(text[start - 1]):
            return start, len(text)
    return None


def _is_streaming_scheme_boundary(character: str) -> bool:
    """判断字符后方是否可以开始一个流式 HTTP scheme。

    Args:
        character: 候选 scheme 前一个字符。

    Returns:
        前一个字符不属于 ASCII 标识符时返回真。
    """

    return not (
        character.isascii()
        and (character.isalnum() or character == "_")
    )


def _trim_external_url_punctuation(value: str) -> str:
    """剥离链接外部标点，同时保留 URL 自身配对的括号。

    Args:
        value: 词法匹配得到的 URL 候选文本。

    Returns:
        可继续执行整串结构校验的 URL 候选。
    """

    candidate = value.rstrip(TRAILING_URL_PUNCTUATION)
    changed = True
    while candidate and changed:
        changed = False
        for opening, closing in URL_BRACKET_PAIRS:
            if (
                candidate.endswith(closing)
                and candidate.count(closing) > candidate.count(opening)
            ):
                candidate = candidate[:-1]
                changed = True
                break
    return candidate


def replace_http_urls_for_speech(text: str, *, replacement: str = "链接") -> str:
    """把普通文本中的 HTTP(S) URL 与未完成尾部候选替换为短语。

    Args:
        text: 准备送入语音合成的文本。
        replacement: 每个 URL 对应的播报替代文本。

    Returns:
        保留 URL 外部文字与标点的播报文本。
    """

    spans = list(find_http_url_spans(text))
    # 分句后句末标点已经属于当前 TTS 工作项；先临时剥离这些外部字符再识别候选，替换区间仍使用原文下标。
    candidate_source = text.rstrip(SPEECH_TRAILING_CANDIDATE_SUFFIX)
    trailing_candidate = find_trailing_http_url_candidate_span(candidate_source)
    if trailing_candidate is not None:
        candidate_start, candidate_end = trailing_candidate
        overlaps_complete_url = any(
            candidate_start < span_end and span_start < candidate_end
            for span_start, span_end in spans
        )
        # 完整 URL 由严格区间负责，以保留链接外句末标点；只有结构尚未补全的尾部候选才追加替换。
        # 单个 h/ht/htt 只服务流式切分等待，生成已经结束时不足以判定为链接，避免误改普通英文尾字母。
        if not overlaps_complete_url and candidate_end - candidate_start >= len("http"):
            spans.append(trailing_candidate)
    if not spans:
        return text
    spans.sort()
    parts: list[str] = []
    previous_end = 0
    for start, end in spans:
        parts.append(text[previous_end:start])
        parts.append(replacement)
        previous_end = end
    parts.append(text[previous_end:])
    return "".join(parts)
