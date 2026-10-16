# coding=utf-8
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Protocol, Sequence

import numpy as np


class HotwordTokenizer(Protocol):
    """热词雷达所需的最小 tokenizer 协议。"""

    def tokenize(
        self, text: str, add_special: bool = False, parse_special: bool = True
    ) -> List[int]:
        """将热词文本转换为 token id 序列。"""

    def detokenize(self, tokens: List[int]) -> str:
        """将 token id 序列还原为文本。"""


@dataclass(frozen=True)
class HotwordEntry:
    """单个热词的 token 化缓存。

    text 是用户维护的原始热词，用于最终替换展示。
    token_ids 是基于 Qwen3 tokenizer 得到的精确匹配序列，若为空则不会参与扫描。
    """

    text: str
    token_ids: List[int]


@dataclass(frozen=True)
class HotwordHit:
    """热词雷达命中结果。

    start_index 和 end_index 是稳定输出 token 序列中的闭区间下标。
    score 是命中 token 在各自 topK 中的平均相对概率，用于重叠候选择优。
    token_indices 保存热词各 token 实际落到的生成位置，便于后续排查跳跃匹配。
    """

    text: str
    start_index: int
    end_index: int
    score: float
    token_indices: List[int]

    def as_dict(self) -> Dict[str, Any]:
        """将内部命中对象转换为上层易消费的 dict。

        返回结构只包含稳定、可序列化字段；后续 WebSocket 若要展示热词命中，可直接复用。
        """
        return {
            "text": self.text,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "score": round(self.score, 6),
            "token_indices": list(self.token_indices),
        }


def load_hotwords(source: Any) -> List[str]:
    """加载并去重热词列表。"""
    if source is None:
        return []

    if isinstance(source, (list, tuple, set)):
        # 用户直接传列表时，每个元素都按单个热词处理。
        # 这里仍然统一走 _dedupe_words，保证空字符串和重复项被过滤。
        return _dedupe_words(str(item) for item in source)

    if isinstance(source, Path):
        # Path 对象语义明确，读取失败时直接返回空列表，让 ASR 主流程保持可用。
        return _load_hotwords_from_path(source)

    if isinstance(source, str):
        text = source.strip()
        if not text:
            return []

        candidate_path = Path(text)
        if candidate_path.exists():
            # 字符串刚好指向现有文件时，按一行一个热词读取。
            return _load_hotwords_from_path(candidate_path)

        if "\n" in text:
            # 多行字符串通常来自配置中心或接口入参，按文件内容等价处理。
            return _dedupe_words(text.splitlines())

        if "/" in text or "\\" in text or candidate_path.suffix:
            # 看起来像路径但不存在时，保守返回空列表，避免误召回文件名。
            return []

        return _dedupe_words([text])

    return []


def _load_hotwords_from_path(path: Path) -> List[str]:
    """从文本文件读取热词。

    文件读取失败时返回空列表，业务意图是热词能力降级而不是打断实时识别。
    """
    try:
        return _dedupe_words(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return []


def _dedupe_words(words: Iterable[str]) -> List[str]:
    """按原始顺序过滤空热词和重复热词。

    seen 的作用域只在本次加载内，保证不同引擎实例之间不会共享可变状态。
    """
    result: List[str] = []
    seen = set()
    for raw_word in words:
        # 热词前后空白通常来自人工维护文件时的换行或缩进，去掉后再参与匹配。
        word = raw_word.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        result.append(word)
    return result


class HotwordRadar:
    """Qwen3 ASR 自回归解码热词雷达。

    它不改变采样过程，只读取每个生成位置的 Top-K token 候选。
    命中后在稳定输出 token 序列上做后处理替换，避免 KV Cache 与采样路径产生不一致。
    """

    def __init__(self, hotwords: Sequence[str], tokenizer: HotwordTokenizer):
        """初始化热词雷达并预构建首 token 索引。

        hotwords 是用户维护的热词文本列表。
        tokenizer 必须提供 Qwen3 GGUF 的 tokenize 和 detokenize 能力。
        无法 token 化的热词会被跳过，这是为了避免单个坏配置影响整个 ASR 引擎。
        """
        self.tokenizer = tokenizer
        self.entries: List[HotwordEntry] = []
        self.prefix_index: Dict[int, List[int]] = {}

        for text in hotwords:
            try:
                # 热词匹配必须和真实生成使用同一个 tokenizer，避免字符串级别误判。
                token_ids = tokenizer.tokenize(
                    text, add_special=False, parse_special=True
                )
            except Exception:
                token_ids = []

            if not token_ids:
                continue

            entry_index = len(self.entries)
            self.entries.append(HotwordEntry(text=text, token_ids=token_ids))
            first_token = token_ids[0] # 首token
            self.prefix_index.setdefault(first_token, []).append(entry_index)  # key是首token ，value是拥有同样首token的全部热词

    @property
    def enabled(self) -> bool:
        """返回当前雷达是否拥有可扫描热词。"""
        return bool(self.entries and self.prefix_index)

    def scan(
        self,
        topk_token_ids: Sequence[Sequence[int]],
        topk_token_probs: Sequence[Sequence[float]],
        max_lookahead: int,
        max_gap: int,
    ) -> List[HotwordHit]:
        """扫描 Top-K 轨迹并返回非重叠热词命中。

        topk_token_ids 和 topk_token_probs 必须与稳定输出 token 位置一一对应。
        max_lookahead 控制多 token 热词向后搜索范围，max_gap 控制允许跳过的普通生成位置数量。
        """
        if not self.enabled or not topk_token_ids:
            return []

        raw_hits: List[HotwordHit] = []
        for pos, candidates in enumerate(topk_token_ids):
            # 每个生成位置可能有多个候选 token 命中不同热词首 token。
            # seen_first_tokens 用来避免同一个 token 在同一位置重复触发同一批热词。
            seen_first_tokens = set()
            for rank, token_id in enumerate(candidates):
                token_id = int(token_id)
                if token_id in seen_first_tokens:
                    continue
                seen_first_tokens.add(token_id)

                if token_id not in self.prefix_index:
                    continue

                for entry_index in self.prefix_index[token_id]:
                    entry = self.entries[entry_index]
                    hit = self._try_match_entry(
                        entry=entry,
                        start_pos=pos,
                        start_rank=rank,
                        topk_token_ids=topk_token_ids,
                        topk_token_probs=topk_token_probs,
                        max_lookahead=max_lookahead,
                        max_gap=max_gap,
                    )
                    if hit is not None:
                        raw_hits.append(hit)

        return self._select_non_overlapping_hits(raw_hits)

    def apply(
        self,
        text: str,
        stable_tokens: Sequence[int],
        topk_token_ids: Sequence[Sequence[int]],
        topk_token_probs: Sequence[Sequence[float]],
        max_lookahead: int,
        max_gap: int,
    ) -> tuple[str, List[Dict[str, Any]]]:
        """对单个 chunk 的稳定文本应用热词替换。

        text 是原始识别文本；stable_tokens 是与它对应的稳定 token 序列。
        当没有命中或轨迹长度不一致时返回原文本，确保热词后处理不会破坏主识别结果。
        """
        if not stable_tokens or len(stable_tokens) != len(topk_token_ids):
            return text, []

        hits = self.scan(
            topk_token_ids=topk_token_ids,
            topk_token_probs=topk_token_probs,
            max_lookahead=max_lookahead,
            max_gap=max_gap,
        )
        if not hits:
            return text, []

        fused_text = self._fuse_text_with_hits(stable_tokens, hits)
        return fused_text, [hit.as_dict() for hit in hits]

    def _try_match_entry(
        self,
        entry: HotwordEntry,
        start_pos: int,
        start_rank: int,
        topk_token_ids: Sequence[Sequence[int]],
        topk_token_probs: Sequence[Sequence[float]],
        max_lookahead: int,
        max_gap: int,
    ) -> HotwordHit | None:
        """尝试从指定位置匹配一个热词。

        匹配规则是 token id 精确相等；允许在自回归生成位置上小范围跳跃，
        用于召回模型把热词拆散或插入过渡 token 的情况。
        """
        token_ids = entry.token_ids
        matched_positions = [start_pos]
        score_sum = self._candidate_prob(topk_token_probs, start_pos, start_rank)
        last_pos = start_pos

        for token_offset in range(1, len(token_ids)):
            target_token = token_ids[token_offset]
            next_match = self._find_next_token(
                target_token=target_token,
                last_pos=last_pos,
                topk_token_ids=topk_token_ids,
                topk_token_probs=topk_token_probs,
                max_lookahead=max_lookahead,
                max_gap=max_gap,
            )
            if next_match is None:
                return None

            found_pos, found_rank, found_prob = next_match
            matched_positions.append(found_pos)
            score_sum += found_prob
            last_pos = found_pos

        return HotwordHit(
            text=entry.text,
            start_index=start_pos,
            end_index=last_pos,
            score=score_sum / max(1, len(token_ids)),
            token_indices=matched_positions,
        )

    def _find_next_token(
        self,
        target_token: int,
        last_pos: int,
        topk_token_ids: Sequence[Sequence[int]],
        topk_token_probs: Sequence[Sequence[float]],
        max_lookahead: int,
        max_gap: int,
    ) -> tuple[int, int, float] | None:
        """在后续 Top-K 位置中寻找热词的下一个 token。

        如果多个位置都能命中，选择概率最高的位置，业务意图是优先保留模型最有把握的热词路径。
        """
        best_match: tuple[int, int, float] | None = None
        search_end = min(len(topk_token_ids), last_pos + max_lookahead + 1)

        for pos in range(last_pos + 1, search_end):
            # Qwen3 自回归输出没有 blank，因此 gap 直接表示两个热词 token 之间可跳过的生成位置数。
            if pos - last_pos - 1 > max_gap:
                continue

            for rank, token_id in enumerate(topk_token_ids[pos]):
                if int(token_id) != int(target_token):
                    continue

                prob = self._candidate_prob(topk_token_probs, pos, rank)
                if best_match is None or prob > best_match[2]:
                    best_match = (pos, rank, prob)

        return best_match

    def _candidate_prob(
        self, topk_token_probs: Sequence[Sequence[float]], pos: int, rank: int
    ) -> float:
        """安全读取某个 Top-K 候选的概率。

        当概率轨迹缺失或 rank 越界时返回 0，避免调试数据不完整时抛异常。
        """
        if pos >= len(topk_token_probs) or rank >= len(topk_token_probs[pos]):
            return 0.0
        return float(topk_token_probs[pos][rank])

    def _select_non_overlapping_hits(self, hits: List[HotwordHit]) -> List[HotwordHit]:
        """从原始命中中选择非重叠结果。

        排序策略先按分数降序，再按跨度降序和起点升序；这样重叠候选会优先选择置信度更高、
        覆盖更完整的热词，最后再按文本顺序输出。
        """
        if not hits:
            return []

        ranked_hits = sorted(
            hits,
            key=lambda hit: (
                -hit.score,
                -(hit.end_index - hit.start_index),
                hit.start_index,
            ),
        )
        selected: List[HotwordHit] = []
        covered_ranges: List[tuple[int, int]] = []

        for hit in ranked_hits:
            # 当前命中如果与已选中的更高分命中有交叉，就直接跳过，避免同一段文本重复替换。
            if any(
                hit.start_index <= end and hit.end_index >= start
                for start, end in covered_ranges
            ):
                continue
            selected.append(hit)
            covered_ranges.append((hit.start_index, hit.end_index))

        return sorted(selected, key=lambda hit: hit.start_index)

    def _fuse_text_with_hits(
        self, stable_tokens: Sequence[int], hits: Sequence[HotwordHit]
    ) -> str:
        """按 token span 将原始输出融合成热词增强文本。

        非热词区间仍然通过原 tokenizer 反解，热词区间直接使用用户维护的原始热词文本。
        """
        pieces: List[str] = []
        cursor = 0

        for hit in hits:
            if cursor < hit.start_index:
                # 热词之前的普通 token 保持模型原始输出。
                prefix_tokens = list(stable_tokens[cursor : hit.start_index])
                pieces.append(self.tokenizer.detokenize(prefix_tokens))

            # 命中的 span 直接替换为热词原文，确保专有名词大小写和中文写法可控。
            pieces.append(hit.text)
            cursor = hit.end_index + 1

        if cursor < len(stable_tokens):
            # 最后一个热词之后的尾部 token 继续保持原始输出。
            suffix_tokens = list(stable_tokens[cursor:])
            pieces.append(self.tokenizer.detokenize(suffix_tokens))

        return "".join(pieces)


def topk_from_logits(logits: np.ndarray, top_k: int) -> tuple[List[int], List[float]]:
    """从一维 logits 中提取 Top-K token id 和相对概率。

    该函数只用于观察，不回写 logits，也不会影响 llama.cpp 采样器的选择。
    概率在 Top-K 内部归一化，足够用于热词候选之间比较，同时避免每步对全词表做完整 softmax。
    """
    if top_k <= 0 or logits.size == 0:
        return [], []

    k = min(int(top_k), int(logits.size))
    candidate_indices = np.argpartition(logits, -k)[-k:]  # 找到数组中最大的 $k$ 个值
    ordered_indices = candidate_indices[np.argsort(logits[candidate_indices])[::-1]]

    # 为了数值稳定，先减去 Top-K 最大 logit 再指数化。
    ordered_logits = logits[ordered_indices].astype(np.float64)
    shifted_logits = ordered_logits - float(np.max(ordered_logits))
    weights = np.exp(shifted_logits)
    denom = float(np.sum(weights))
    probs = weights / denom if denom > 0 else np.zeros_like(weights)

    return ordered_indices.astype(np.int64).tolist(), probs.astype(np.float64).tolist()
