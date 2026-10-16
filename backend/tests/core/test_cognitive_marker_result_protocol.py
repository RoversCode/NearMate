from __future__ import annotations

import pytest

from companion.foreground_tools.cognitive.marker_result_protocol import (
    FAILURE_TAG,
    SUMMARY_TAG,
    parse_marked_cognitive_result,
)
from companion.foreground_tools.cognitive.result_protocol import (
    CognitiveFailureResult,
    CognitiveResultProtocolError,
    CognitiveSuccessResult,
    MAX_COGNITIVE_RESULT_CHARS,
)


def test_marker_protocol_accepts_success_with_markdown_and_summary() -> None:
    """成功标记允许完整 Markdown 结果，并只把标签后的正文作为摘要。"""

    result = parse_marked_cognitive_result(
        "建议使用以下配置：\n```json\n{\"timeout\":30}\n```\n"
        f"{SUMMARY_TAG}\n建议把 timeout 设置为 30 秒。"
    )

    assert isinstance(result, CognitiveSuccessResult)
    assert result.result.startswith("建议使用以下配置")
    assert result.summary == "建议把 timeout 设置为 30 秒。"


def test_marker_protocol_accepts_inline_success_tag() -> None:
    """标签不必独占一行，只要精确且成功两段都非空即可。"""

    result = parse_marked_cognitive_result(
        f"核对结果为 42。{SUMMARY_TAG}结果是 42。"
    )

    assert isinstance(result, CognitiveSuccessResult)
    assert result.result == "核对结果为 42。"
    assert result.summary == "结果是 42。"


def test_marker_protocol_accepts_failure_only_after_whitespace_prefix() -> None:
    """失败标签前只能有空白，原因正文必须非空。"""

    result = parse_marked_cognitive_result(
        f" \n\t{FAILURE_TAG}\n必要网页来源无法读取。"
    )

    assert isinstance(result, CognitiveFailureResult)
    assert result.failure_reason == "必要网页来源无法读取。"


def test_marker_protocol_accepts_inline_failure_tag_after_whitespace() -> None:
    """失败标签可以和原因同行，但标签前仍然只能出现空白。"""

    result = parse_marked_cognitive_result(
        f"  {FAILURE_TAG}必要网页来源无法读取。"
    )

    assert isinstance(result, CognitiveFailureResult)
    assert result.failure_reason == "必要网页来源无法读取。"


@pytest.mark.parametrize(
    "raw_output",
    [
        "完整结果",
        f"结果\n{SUMMARY_TAG}\n摘要\n{SUMMARY_TAG}\n重复摘要",
        f"{SUMMARY_TAG}\n摘要\n{FAILURE_TAG}\n失败",
        f"{FAILURE_TAG}\n失败\n{SUMMARY_TAG}\n摘要",
        f"前缀\n{FAILURE_TAG}\n失败",
        f"\n{SUMMARY_TAG}\n摘要",
        f"结果{SUMMARY_TAG}\n",
        f"{SUMMARY_TAG}\n",
        f"{FAILURE_TAG}\n",
        "<SUMMARY_TAG>\n摘要",
    ],
)
def test_marker_protocol_routes_malformed_markers_to_flash(raw_output: str) -> None:
    """缺失、重复、混合、前缀和空分段都允许一次 Flash 收口。"""

    with pytest.raises(CognitiveResultProtocolError) as captured:
        parse_marked_cognitive_result(raw_output)

    assert captured.value.repairable is True


def test_marker_protocol_rejects_oversized_plus_body_without_repair() -> None:
    """完整 Plus 原文超出总容量时不能复制给 Flash。"""

    with pytest.raises(CognitiveResultProtocolError) as captured:
        parse_marked_cognitive_result("x" * (MAX_COGNITIVE_RESULT_CHARS + 1))

    assert captured.value.repairable is False


def test_marker_protocol_rejects_unsafe_failure_reason_for_flash_repair() -> None:
    """失败说明触发敏感信息门禁时仍可交给 Flash 清理。"""

    with pytest.raises(CognitiveResultProtocolError) as captured:
        parse_marked_cognitive_result(
            f"{FAILURE_TAG}\n读取 C:\\Users\\secret\\token.txt 失败"
        )

    assert captured.value.repairable is True
