from __future__ import annotations

import unicodedata
from collections.abc import Callable

from companion.observability.logging_config import get_backend_logger
from companion.realtime.stream_text_semantics import project_speakable_text
from companion.realtime.text_tokenizer import count_text_tokens
from companion.text.format import format_text
from companion.text.http_urls import (
    find_http_url_spans,
    find_trailing_http_url_candidate_span,
)


logger = get_backend_logger(__name__)


SENTENCE_END_MARKS = frozenset("。！？!?；;.")
COMMA_SPLIT_MARKS = frozenset("，,：:、")
PUNCTUATION_CATEGORY_PREFIX = "P"


def is_punctuation_character(character: str) -> bool:
    """判断单个字符是否属于 Unicode 标点。

    Args:
        character: 待检查的单个字符。

    Returns:
        如果该字符属于 Unicode 标点类别，则返回 `True`。
    """

    # Unicode category 以 P 开头的字符都属于标点，集中使用标准分类可覆盖中英文逗号、省略号、顿号等变体。
    return bool(character) and unicodedata.category(character).startswith(PUNCTUATION_CATEGORY_PREFIX)


class SentenceQueue:
    """维护按先进先出顺序消费的句子队列。

    这个队列本身不负责分句，只负责保存已经确认可播报的完整句子，
    供输出侧按顺序取走。
    """

    def __init__(self) -> None:
        """初始化空句子队列。"""

        self._items: list[str] = []

    def push(self, sentence: str) -> None:
        """追加一句待播报文本。

        Args:
            sentence: 已经完成切分的句子文本。
        """

        self._items.append(sentence)

    def pop_next(self) -> str | None:
        """按先进先出顺序取下一句。

        Returns:
            下一句待播报文本；队列为空时返回 `None`。
        """

        if not self._items:
            return None
        return self._items.pop(0)

    def drain(self) -> list[str]:
        """一次性取出所有剩余句子。

        Returns:
            当前队列中的全部句子，顺序保持不变。
        """

        items = self._items[:]
        self._items.clear()
        return items


class StreamingSentenceBuffer:
    """按流式文本增量切出适合送入 TTS 的句子片段。

    该缓冲区先等待最小 token 门槛，再优先按句末标点输出完整语义段；
    只有缓冲过长且迟迟没有句末标点时，才用逗号级标点兜底。
    """

    def __init__(
        self,
        *,
        min_segment_tokens: int = 10,
        max_segment_tokens: int = 80,
        token_counter: Callable[[str], int] = count_text_tokens,
        speakable_projector: Callable[[str], str] = project_speakable_text,
    ) -> None:
        """初始化流式句子缓冲。

        Args:
            min_segment_tokens: 普通流式切分允许送入 TTS 的最小 token 数。
            max_segment_tokens: 超过该 token 数后才启用逗号级兜底切分。
            token_counter: 文本 token 计数函数，生产默认使用本地 tokenizer，测试可注入确定性计数器。
            speakable_projector: 将原始流式文本投影为可朗读文本的函数，控制标签不参与门槛判断。
        """

        # 保存尚未形成完整句的文本片段，作用域限定在当前 generation。
        self._buffer = ""
        # 保留最近一个已完成句段，给 generation 结束时的短尾句提供自然合并锚点。
        self._pending_last_segment: str | None = None
        # 第一段优先降低首包延迟，后续句段才启用“保留最近一段”的收口策略。
        self._first_segment_released = False
        # 最小 token 门槛保护 TTS 不收到“嗯。”这类缺少上下文的短片段。
        self.min_segment_tokens = max(1, min_segment_tokens)
        # 最大 token 门槛只负责触发逗号级兜底，不做硬字符截断，避免把半句话强行切碎。
        self.max_segment_tokens = max(self.min_segment_tokens, max_segment_tokens)
        self._token_counter = token_counter
        self._speakable_projector = speakable_projector

    def push_text_delta(self, delta: str) -> list[str]:
        """追加一段 LLM 文本增量，并返回新完成的句子。

        Args:
            delta: 本次流式到达的文本增量。

        Returns:
            这一轮新增完成的句子列表，顺序与文本出现顺序一致。
        """

        # 空增量通常来自模型心跳或过滤后的内容，直接忽略可避免生成空音频。
        if not delta:
            return []

        # 将新 delta 接到历史半句后面，再从左到右寻找切分点，保证跨 delta 的句子也能被切出。
        self._buffer += delta
        if not self._has_min_tokens(self._buffer):
            return []

        completed_sentences = self._drain_by_sentence_end()
        if completed_sentences:
            return self._accept_completed_segments(completed_sentences)

        comma_segments = self._drain_by_comma_when_too_long()
        if comma_segments:
            return self._accept_completed_segments(comma_segments)

        return []

    def _accept_completed_segments(self, segments: list[str]) -> list[str]:
        """接收新完成句段，并释放不再需要等待短尾合并的旧句段。

        Args:
            segments: 本轮刚切出的可播句段。

        Returns:
            已确认不会再参与短尾合并、可以立即送入 TTS 的句段。
        """

        ready_segments: list[str] = []
        for segment in segments:
            if not self._first_segment_released:
                ready_segments.append(segment)
                self._first_segment_released = True
                continue

            # 最新句段需要暂留到下一次切分或 flush，避免收尾短句失去可合并对象。
            if self._pending_last_segment is not None:
                ready_segments.append(self._pending_last_segment)
            self._pending_last_segment = segment
        return ready_segments

    def _drain_by_sentence_end(self) -> list[str]:
        """按句末标点切出已满足最低 token 门槛的语义段。

        Returns:
            新完成的句段列表。
        """

        completed_sentences: list[str] = []
        sentence_start_index = 0
        http_url_spans = _protected_http_url_spans(self._buffer)
        for index, character in enumerate(self._buffer):
            if character not in SENTENCE_END_MARKS:
                continue
            if _index_in_spans(index, http_url_spans):
                continue
            if not self._is_complete_sentence_end(index):
                continue

            sentence = self._buffer[sentence_start_index : index + 1].strip()
            if not self._is_ready_segment(sentence):
                continue

            completed_sentences.append(format_text(sentence))
            sentence_start_index = index + 1

        self._buffer = self._buffer[sentence_start_index:]
        return completed_sentences

    def _drain_by_comma_when_too_long(self) -> list[str]:
        """缓冲过长时按逗号级标点兜底切出一个语义段。

        Returns:
            兜底切出的句段列表；没有合法切点时返回空列表。
        """

        if self._token_count(self._buffer) <= self.max_segment_tokens:
            return []

        split_index = self._find_comma_fallback_split_index()
        if split_index is None:
            return []

        segment = self._buffer[: split_index + 1].strip()
        self._buffer = self._buffer[split_index + 1 :]

        return [format_text(segment)]

    def _find_comma_fallback_split_index(self) -> int | None:
        """寻找满足最低 token 门槛的最后一个逗号级兜底切点。

        Returns:
            可切分标点的下标；没有合法切点时返回 `None`。
        """

        selected_index: int | None = None
        http_url_spans = _protected_http_url_spans(self._buffer)
        for index, character in enumerate(self._buffer):
            if character not in COMMA_SPLIT_MARKS:
                continue
            if _index_in_spans(index, http_url_spans):
                continue

            candidate = self._buffer[: index + 1].strip()
            if self._is_ready_segment(candidate):
                selected_index = index
        return selected_index

    def _is_complete_sentence_end(self, index: int) -> bool:
        """判断当前句末标点是否已经形成完整切点。

        Args:
            index: 当前标点在缓冲区中的下标。

        Returns:
            该位置可以作为句末切点时返回 `True`。
        """

        if self._buffer[index] == "." and self._is_inline_ascii_token_dot(index):
            return False

        next_index = index + 1
        if next_index < len(self._buffer) and self._buffer[next_index] in SENTENCE_END_MARKS:
            return False

        # 当前标点位于缓冲末尾时，后续 delta 可能继续补“？！”这类语气标点，先等下一轮或最终 flush 确认。
        return next_index < len(self._buffer)

    def _is_inline_ascii_token_dot(self, index: int) -> bool:
        """判断点号是否属于小数、版本、域名、隐藏文件或标识符内部。

        Args:
            index: ASCII 点号在当前缓冲区中的下标。

        Returns:
            点号能够确定属于 ASCII token 时返回 `True`。
        """

        if index + 1 >= len(self._buffer):
            return False
        following = self._buffer[index + 1]
        following_is_token = following.isascii() and (
            following.isalnum() or following == "_"
        )
        if not following_is_token:
            return False
        if index == 0:
            return True

        previous = self._buffer[index - 1]
        if previous.isascii() and (previous.isalnum() or previous == "_"):
            return True

        # 点号后直接跟数字时优先视为省略整数部分的小数，避免在语义正则化之前切成两句。
        if following.isdigit():
            return True

        # 隐藏文件名常出现在空白、引号、括号或路径分隔符之后；小写开头可覆盖“文件.env”写法。
        return (
            previous.isspace()
            or previous in "([{/'\"\\"
            or following == "_"
            or following.islower()
        )

    def flush(self) -> list[str]:
        """取出流结束后仍需播报的句段。

        Returns:
            剩余待播句段列表；没有可播内容时返回空列表。
        """

        # LLM 结束时即使没有句末标点，也要播出剩余文本，避免助手回复丢尾巴。
        tail = self._buffer.strip()
        self._buffer = ""
        pending_last_segment = self._pending_last_segment
        self._pending_last_segment = None

        # 空白尾句没有可播内容，只需要释放此前暂留的完整句段。
        if not self._has_semantic_text(tail):
            if pending_last_segment is None:
                return []
            return [pending_last_segment]

        if pending_last_segment is None:
            return [tail]

        # 短尾句单独播报会显得突兀，收口时优先并回最近一个完整句段改善 TTS 节奏。
        if not self._has_min_tokens(tail):
            return [format_text(f"{pending_last_segment}{tail}")]

        return [pending_last_segment, tail]

    def _is_ready_segment(self, segment: str) -> bool:
        """判断候选句段是否可以在流式阶段送入 TTS。

        Args:
            segment: 待检查的候选句段。

        Returns:
            同时具备语义文本和最低 token 数时返回 `True`。
        """

        return self._has_semantic_text(segment) and self._has_min_tokens(segment)

    def _has_min_tokens(self, text: str) -> bool:
        """判断文本是否达到最低流式推理 token 数。

        Args:
            text: 待检查文本。

        Returns:
            token 数达到最低门槛时返回 `True`。
        """

        return self._token_count(self._project_speakable_text(text)) >= self.min_segment_tokens

    def _token_count(self, text: str) -> int:
        """计算文本 token 数，空白文本直接视为零。

        Args:
            text: 待计数文本。

        Returns:
            token 数量。
        """

        normalized_text = text.strip()
        if not normalized_text:
            return 0
        return self._token_counter(normalized_text)

    def _has_semantic_text(self, segment: str) -> bool:
        """判断句段是否包含真实可读文本。

        Args:
            segment: 待检查的句段文本。

        Returns:
            只要句段中存在至少一个非空白、非标点字符，就返回 `True`。
        """

        speakable_segment = self._project_speakable_text(segment)

        # 逐字符检查可以覆盖“ ！ ”这类空白包裹标点的情况，确保过滤规则集中且不依赖调用方补丁。
        for character in speakable_segment:
            # 只有包含非空白、非标点字符的片段才算可送 TTS 的语义文本，避免逗号、省略号等纯标点污染播报和历史。
            if not character.isspace() and not is_punctuation_character(character):
                return True

        # 候选文本只有空白或标点时，没有独立业务语义，必须丢弃。
        return False

    def _project_speakable_text(self, text: str) -> str:
        """把原始句段投影为可参与语义和长度判断的可朗读文本。

        Args:
            text: 原始流式文本或候选句段。

        Returns:
            去除控制标签后的可朗读文本。
        """

        return self._speakable_projector(text)


def _index_in_spans(index: int, spans: tuple[tuple[int, int], ...]) -> bool:
    """判断字符下标是否落在任一 HTTP(S) URL 区间内。

    Args:
        index: 当前待判断字符的代码点下标。
        spans: 按文本顺序排列的 URL 左闭右开区间。

    Returns:
        字符属于某个 URL token 时返回真。
    """

    return any(start <= index < end for start, end in spans)


def _protected_http_url_spans(text: str) -> tuple[tuple[int, int], ...]:
    """合并正式 URL 与流式尾部候选，供分句阶段跳过内部标点。

    Args:
        text: 当前流式句子缓冲。

    Returns:
        不允许作为句末或逗号兜底切点的 HTTP 区间。
    """

    spans = list(find_http_url_spans(text))
    trailing_candidate = find_trailing_http_url_candidate_span(text)
    if trailing_candidate is None:
        return tuple(spans)
    if not any(
        start <= trailing_candidate[0] and trailing_candidate[1] <= end
        for start, end in spans
    ):
        spans.append(trailing_candidate)
    return tuple(spans)
