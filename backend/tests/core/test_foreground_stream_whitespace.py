from __future__ import annotations

import pytest

from backend.tests.foreground_processor_factory import (
    build_foreground_processor_for_test,
)


def _collect_chunks(parts: list[str]) -> tuple[list[str], list[str]]:
    """按生产聚合规则收集完整正文与公共 chunk。

    Args:
        parts: Provider 依次返回的原始文本分片。

    Returns:
        `visible_parts` 与实际产生的公共 chunk 正文。
    """

    processor = build_foreground_processor_for_test()
    visible_parts: list[str] = []
    public_chunks: list[str] = []
    for part in parts:
        events = processor._push_visible_text_chunk(
            chunk=part,
            visible_parts=visible_parts,
        )
        public_chunks.extend(str(event.payload["text"]) for event in events)
    return visible_parts, public_chunks


@pytest.mark.parametrize(
    ("parts", "expected_visible_parts", "expected_public_chunks"),
    [
        (["[Shh]", " ", "正文"], ["[Shh]", " ", "正文"], ["[Shh]", " 正文"]),
        (["hello", " ", "world"], ["hello", " ", "world"], ["hello", " world"]),
        ([" ", "\n", "\t", "正文"], [" ", "\n", "\t", "正文"], [" \n\t正文"]),
        (["正文", "  "], ["正文", "  "], ["正文"]),
        ([" ", "\n"], [" ", "\n"], []),
        (["", "正文"], ["正文"], ["正文"]),
    ],
)
def test_text_chunk_events_keep_whitespace_without_publishing_blank_chunks(
    parts: list[str],
    expected_visible_parts: list[str],
    expected_public_chunks: list[str],
) -> None:
    """空白 delta 应保留在权威正文中，并并入下一条可见 chunk。"""

    visible_parts, public_chunks = _collect_chunks(parts)

    assert visible_parts == expected_visible_parts
    assert public_chunks == expected_public_chunks
    assert all(chunk.strip() for chunk in public_chunks)


def test_text_chunk_events_are_invariant_across_stream_split_positions() -> None:
    """同一正文无论怎样切分，都不能改变最终正文或产生纯空白公共 chunk。"""

    full_text = "[Question-oh] Did you find it?"
    partitions = [[full_text], list(full_text)] + [
        [full_text[:index], full_text[index:]]
        for index in range(len(full_text) + 1)
    ]

    for parts in partitions:
        visible_parts, public_chunks = _collect_chunks(parts)

        assert "".join(visible_parts) == full_text
        assert "".join(public_chunks) == full_text
        assert all(chunk.strip() for chunk in public_chunks)


def test_retired_route_marker_is_visible_across_every_stream_split() -> None:
    """旧 route 字面量及其后正文必须像普通文本一样完整公开。"""

    full_text = "先说明。<route:action>后续正文不能再被截断。"
    partitions = [list(full_text)] + [
        [full_text[:index], full_text[index:]]
        for index in range(len(full_text) + 1)
    ]

    for parts in partitions:
        visible_parts, public_chunks = _collect_chunks(parts)

        assert "".join(visible_parts) == full_text
        assert "".join(public_chunks) == full_text
