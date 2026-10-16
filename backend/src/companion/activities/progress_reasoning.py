"""维护异步活动尚未播报的 reasoning 冻结边界。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _FrozenProgressSegment:
    """保存一次 progress 对应的冻结文本与严格递增序号。"""

    sequence: int
    text: str


class ProgressReasoningBuffer:
    """累积流式 reasoning，并按 progress 序号精确冻结与确认。

    Args:
        max_text_chars: 尚未播报的冻结文本与实时尾部允许保留的总字符数。
    """

    def __init__(self, max_text_chars: int = 4000) -> None:
        """初始化空缓冲。

        Args:
            max_text_chars: 尚未播报原始 reasoning 的异常容量上限。

        Raises:
            ValueError: 容量上限不是正整数时抛出。
        """

        if max_text_chars <= 0:
            raise ValueError("max_text_chars 必须大于零")
        self._max_text_chars = max_text_chars
        self._accumulating_text = ""
        self._segments: list[_FrozenProgressSegment] = []
        self._latest_sequence = 0

    @property
    def latest_sequence(self) -> int:
        """返回当前任务已经产生的最新冻结序号。

        Returns:
            最近一次冻结分配的单调序号。
        """

        return self._latest_sequence

    def append_delta(self, text: str) -> None:
        """追加尚未到达 progress 边界的实时 reasoning。

        Args:
            text: 模型本次新增的 reasoning 文本。
        """

        if not text:
            return
        self._accumulating_text += text
        self._trim_oldest_text()

    def has_accumulating_text(self) -> bool:
        """判断实时尾部是否包含可冻结正文。

        Returns:
            实时尾部包含非空正文时返回真。
        """

        return bool(self._accumulating_text.strip())

    def freeze(self) -> int | None:
        """冻结完整实时尾部，并返回本次 progress 序号。

        Returns:
            新冻结片段的严格递增序号；没有可冻结正文时返回空。
        """

        text = self._accumulating_text
        self._accumulating_text = ""
        if not text.strip():
            return None
        self._latest_sequence += 1
        self._segments.append(
            _FrozenProgressSegment(sequence=self._latest_sequence, text=text)
        )
        self._trim_oldest_text()  # 删除过长的reason text
        return self._latest_sequence

    def frozen_text(self) -> str:
        """返回全部尚未确认冻结片段的有序拼接正文。

        Returns:
            按冻结顺序拼接的未确认进度文本。
        """

        return "".join(segment.text for segment in self._segments)

    def acknowledge_through(self, sequence: int) -> bool:
        """确认指定序号覆盖的未播报前缀。

        Args:
            sequence: 已经产生可见播报的 progress 序号。

        Returns:
            找到尚未确认且不晚于该序号的冻结边界时返回真。
        """

        matched = any(segment.sequence <= sequence for segment in self._segments)
        if not matched:
            return False
        self._segments = [
            segment for segment in self._segments if segment.sequence > sequence
        ]
        return True

    def clear_pending(self) -> None:
        """清空全部未播报文本，同时保留任务内 sequence 单调性。"""

        self._accumulating_text = ""
        self._segments.clear()

    def _trim_oldest_text(self) -> None:
        """从最旧正文开始裁剪超出安全上限的字符。"""

        total_chars = len(self._accumulating_text) + sum(
            len(segment.text) for segment in self._segments
        )
        overflow = total_chars - self._max_text_chars
        if overflow <= 0:
            return

        # 即使片段正文被容量保护裁空，也保留 sequence 标记供迟到确认精确闭环。
        for segment in self._segments:
            if overflow <= 0:
                break
            removed = min(len(segment.text), overflow)
            segment.text = segment.text[removed:]
            overflow -= removed
        if overflow > 0:
            self._accumulating_text = self._accumulating_text[overflow:]
