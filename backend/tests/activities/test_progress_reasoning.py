from __future__ import annotations

import pytest

from companion.activities.progress_reasoning import ProgressReasoningBuffer


def test_buffer_freezes_and_acknowledges_old_sequence_prefix() -> None:
    """迟到的旧播报确认只能消费自己的 reasoning 前缀。"""

    buffer = ProgressReasoningBuffer()
    buffer.append_delta("进展A")
    assert buffer.freeze() == 1
    buffer.append_delta("进展B")
    assert buffer.freeze() == 2

    assert buffer.frozen_text() == "进展A进展B"
    assert buffer.acknowledge_through(1) is True
    assert buffer.frozen_text() == "进展B"
    assert buffer.acknowledge_through(1) is False
    assert buffer.acknowledge_through(2) is True
    assert buffer.frozen_text() == ""


def test_buffer_never_exposes_accumulating_tail() -> None:
    """实时 reasoning 只有冻结后才进入 foreground 可读正文。"""

    buffer = ProgressReasoningBuffer()
    buffer.append_delta("尚未冻结")

    assert buffer.has_accumulating_text() is True
    assert buffer.frozen_text() == ""


def test_buffer_preserves_whitespace_across_freeze_boundaries() -> None:
    """冻结边界不得破坏原始流式文本中的必要空白。"""

    buffer = ProgressReasoningBuffer()
    buffer.append_delta("checking ")
    assert buffer.freeze() == 1
    buffer.append_delta("the second source")
    assert buffer.freeze() == 2

    assert buffer.frozen_text() == "checking the second source"


def test_buffer_clear_keeps_sequence_monotonic() -> None:
    """content 失效清空后，新提醒不得复用旧序号。"""

    buffer = ProgressReasoningBuffer()
    buffer.append_delta("普通思考")
    assert buffer.freeze() == 1
    buffer.clear_pending()
    buffer.append_delta("正在收尾")

    assert buffer.freeze() == 2
    assert buffer.frozen_text() == "正在收尾"


def test_buffer_trims_oldest_text_and_preserves_sequence_boundary() -> None:
    """容量保护应保留最新正文，同时允许被裁空的旧序号完成确认。"""

    buffer = ProgressReasoningBuffer(max_text_chars=5)
    buffer.append_delta("甲乙丙")
    assert buffer.freeze() == 1
    buffer.append_delta("丁戊己")

    assert buffer.frozen_text() == "乙丙"
    assert buffer.freeze() == 2
    assert buffer.frozen_text() == "乙丙丁戊己"
    assert buffer.acknowledge_through(1) is True
    assert buffer.frozen_text() == "丁戊己"


def test_buffer_rejects_non_positive_capacity() -> None:
    """非法容量必须在构造阶段失败。"""

    with pytest.raises(ValueError, match="max_text_chars"):
        ProgressReasoningBuffer(max_text_chars=0)
