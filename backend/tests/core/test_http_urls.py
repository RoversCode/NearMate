import pytest

from companion.text.http_urls import (
    find_http_url_spans,
    find_trailing_http_url_candidate_span,
    is_http_url,
    normalize_http_urls,
    replace_http_urls_for_speech,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("地址 h", (3, 4)),
        ("地址 htt", (3, 6)),
        ("地址 https:", (3, 9)),
        ("地址 https:/", (3, 10)),
        ("地址 https://", (3, 11)),
        ("地址 https://example.com/private?token=secret", (3, 43)),
        ("with", None),
    ],
)
def test_trailing_http_candidate_only_protects_streaming_buffer_tail(
    text: str,
    expected: tuple[int, int] | None,
) -> None:
    """流式候选应覆盖未完成 scheme 和延伸到缓冲末尾的 URL token。"""

    assert find_trailing_http_url_candidate_span(text) == expected


def test_http_url_validation_accepts_only_complete_http_values() -> None:
    """整串校验只接受没有空白且结构完整的 HTTP(S) URL。"""

    assert is_http_url("https://user:pass@127.0.0.1:8080/path?q=1") is True
    assert is_http_url("http://localhost/test") is True
    assert is_http_url("ftp://example.com/file") is False
    assert is_http_url("https://example.com/a b") is False
    assert is_http_url("https://[broken") is False


def test_http_url_normalization_preserves_order_and_deduplicates() -> None:
    """工具参数 URL 应保序去重并忽略非法项。"""

    assert normalize_http_urls([
        " https://example.com/a ",
        "bad-url",
        "https://example.com/a",
        "http://example.com/b",
    ]) == ["https://example.com/a", "http://example.com/b"]


def test_speech_projection_replaces_each_url_and_keeps_external_punctuation() -> None:
    """播报投影应替换所有 URL，同时保留链接外的括号和中英文标点。"""

    text = (
        "看（https://example.com/report?q=a;b.c&lang=zh），"
        "再看 https://example.org/a)."
    )

    assert find_http_url_spans(text)
    assert replace_http_urls_for_speech(text) == "看（链接），再看 链接)."


def test_speech_projection_ignores_plain_domains() -> None:
    """没有 HTTP(S) scheme 的普通域名不属于语音替换范围。"""

    assert replace_http_urls_for_speech("打开 example.com 看看") == "打开 example.com 看看"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("请看 h", "请看 h"),
        ("请看 htt", "请看 htt"),
        ("请看 http", "请看 链接"),
        ("请看 https:", "请看 链接"),
        ("请看 https:/", "请看 链接"),
        ("请看 https://", "请看 链接"),
        ("请看 https:。", "请看 链接。"),
        ("请看（https:/）。", "请看（链接）。"),
    ],
)
def test_speech_projection_replaces_unambiguous_trailing_http_candidate(
    text: str,
    expected: str,
) -> None:
    """流结束时残留的 HTTP 候选也不能进入语音合成。"""

    assert replace_http_urls_for_speech(text) == expected


def test_speech_projection_preserves_balanced_url_brackets() -> None:
    """URL 自身配对括号应整体替换，外层未配对括号仍保留为正文标点。"""

    text = (
        "条目 https://en.wikipedia.org/wiki/Foo_(bar)，"
        "本机 https://[::1]/status；外层（https://example.com/report）。"
        "另见 https://example.net/a、结束"
    )

    assert replace_http_urls_for_speech(text) == (
        "条目 链接，本机 链接；外层（链接）。另见 链接、结束"
    )
