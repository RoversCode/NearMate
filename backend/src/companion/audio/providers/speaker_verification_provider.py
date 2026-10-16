from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from companion.audio.pcm16_utils import pcm16_to_normalized_float32
from companion.audio.providers.base import SpeakerVerificationResult


@dataclass(frozen=True)
class SpeakerVerificationProviderConfig:
    """真实说话人确认 provider 的轻量配置。"""

    # name -> 嵌入向量的白名单字典，构造期由 runtime 预算。
    whitelist: dict[str, np.ndarray]
    # sherpa-onnx extractor；测试可通过 compute_embedding 注入绕过。
    extractor: Any = None
    # 嵌入模型采样率，固定 16kHz。
    sample_rate: int = 16000


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2 归一化到单位向量，零向量原样返回。"""

    norm = np.linalg.norm(vec)
    if norm < 1e-8:
        return vec.astype(np.float32, copy=False)
    return (vec / norm).astype(np.float32, copy=False)


class RealSpeakerVerificationProvider:
    """基于 cam++ 嵌入的说话人白名单确认 provider。"""

    def __init__(
        self,
        *,
        config: SpeakerVerificationProviderConfig,
        compute_embedding: Callable[[np.ndarray], np.ndarray] | None = None,
    ) -> None:
        # 真实路径需要 extractor；测试可注入 compute_embedding 绕过模型加载。
        if compute_embedding is None and config.extractor is None:
            raise ValueError("缺少 extractor 且未注入 compute_embedding，无法计算嵌入")

        self.config = config
        self._compute_embedding_override = compute_embedding
        # 白名单嵌入构造期预归一化，verify 热路径只需归一化 query 一次。
        self._whitelist_unit: dict[str, np.ndarray] = {
            name: _l2_normalize(np.asarray(vec, dtype=np.float32))
            for name, vec in config.whitelist.items()
        }

    def verify(self, pcm16: bytes) -> SpeakerVerificationResult:
        """判断 PCM16 音频是否属于白名单说话人。"""

        samples = pcm16_to_normalized_float32(pcm16)
        embedding = self._compute_embedding(samples)
        query_unit = _l2_normalize(embedding)

        # 对预归一化的白名单逐一做点积，取最高相似度。
        best_name: str | None = None
        best_similarity = -1.0
        for name, vec in self._whitelist_unit.items():
            similarity = float(np.dot(query_unit, vec))
            if similarity > best_similarity:
                best_name = name
                best_similarity = similarity

        return SpeakerVerificationResult(
            matched_name=best_name,
            best_similarity=best_similarity,
        )

    def _compute_embedding(self, samples: np.ndarray) -> np.ndarray:
        """计算一段 float32 波形的说话人嵌入。"""

        # 测试替身优先，避免单测加载真实 cam++ 模型。
        if self._compute_embedding_override is not None:
            return np.asarray(
                self._compute_embedding_override(samples), dtype=np.float32
            )

        extractor = self.config.extractor
        if extractor is None:
            raise RuntimeError("说话人确认 provider 未初始化 extractor，无法计算嵌入")

        stream = extractor.create_stream()
        stream.accept_waveform(
            sample_rate=self.config.sample_rate, waveform=samples
        )
        stream.input_finished()
        return np.array(extractor.compute(stream), dtype=np.float32)
