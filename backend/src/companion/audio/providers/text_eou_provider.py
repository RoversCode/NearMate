from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np
from streamvox_runtime import ensure_single_onnxruntime_distribution

from companion.audio.bootstrap.asset_registry import (
    TEXT_EOU_MODEL_FILE_NAMES,
    TEXT_EOU_TOKENIZER_RELATIVE_PATH,
)

ensure_single_onnxruntime_distribution()
import onnxruntime as ort  # type: ignore
from tokenizers import Tokenizer  # type: ignore


MAX_HISTORY_TOKENS = 128
_PAD_TOKEN = "[PAD]"
_PUNCT_RE = re.compile(r"[，。？！,. ?!]")


@dataclass(frozen=True)
class TextEouProviderConfig:
    """文本 EOU provider 配置。

    Args:
        model_dir: FireRedChat turn detector 模型根目录。
        lang: 使用中文或多语言模型文件。
        tokenizer_path: 可选 tokenizer 路径，既可指向目录也可指向 tokenizer.json。
        model_path: 可选 ONNX 模型文件路径；为空时按 lang 从 model_dir 推导。
        providers: ONNX Runtime provider 列表。

    Returns:
        TextEouProviderConfig: 不可变配置快照。
    """

    model_dir: str
    lang: Literal["chinese", "multilingual"] = "chinese"
    tokenizer_path: str | None = None
    model_path: str | None = None
    providers: tuple[str, ...] = ("CPUExecutionProvider",)


class RealTextEouProvider:
    """基于 FireRedChat turn detector 的文本轮次结束判断 provider。"""

    _LANG_TO_FILE = TEXT_EOU_MODEL_FILE_NAMES

    def __init__(
        self,
        *,
        config: TextEouProviderConfig,
        predict_probability: Callable[[str], float] | None = None,
    ) -> None:
        """初始化文本 EOU provider。

        Args:
            config: 文本 EOU provider 配置。
            predict_probability: 测试或替代实现注入的已清洗文本概率预测函数。

        Returns:
            None: 构造函数完成依赖加载或保存注入预测函数。
        """

        if config.lang not in self._LANG_TO_FILE:
            raise ValueError("text_eou lang 只能是 chinese 或 multilingual")

        self.config = config
        self._predict_probability = predict_probability
        self._session: ort.InferenceSession | None = None
        self._tokenizer: Tokenizer | None = None

        if self._predict_probability is not None:
            return

        self._initialize_model()

    def predict_end_probability(self, text: str) -> float:
        """预测文本是否已经表达完毕。

        Args:
            text: ASR 产出的 final transcript 原文。

        Returns:
            float: 句子结束概率，范围通常在 0 到 1 之间。
        """

        cleaned_text = clean_text_for_eou(text)
        if not cleaned_text:
            return 0.0

        if self._predict_probability is not None:
            return float(self._predict_probability(cleaned_text))

        return self._predict_cleaned_text(cleaned_text)

    def _initialize_model(self) -> None:
        """加载 tokenizer 与 ONNX session。"""

        model_dir = Path(self.config.model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"text EOU model_dir not found: {model_dir}")

        model_path = Path(
            self.config.model_path
            or model_dir / self._LANG_TO_FILE[self.config.lang]
        )
        if not model_path.is_file():
            raise FileNotFoundError(f"text EOU onnx model not found: {model_path}")

        tokenizer_file = self._resolve_tokenizer_file(model_dir)
        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))

        # 参考模型按固定长度左截断、定长 padding 训练，运行期必须复现相同输入语义。
        pad_id = self._tokenizer.token_to_id(_PAD_TOKEN)
        self._tokenizer.enable_truncation(max_length=MAX_HISTORY_TOKENS, direction="left")
        self._tokenizer.enable_padding(
            length=MAX_HISTORY_TOKENS,
            pad_id=pad_id,
            pad_token=_PAD_TOKEN,
        )
        if "CUDAExecutionProvider" in self.config.providers:
            from streamvox_runtime import initialize_nvidia_runtime

            initialize_nvidia_runtime(consumer="onnx_cuda", required=True)
        self._session = ort.InferenceSession(str(model_path), providers=list(self.config.providers))

    def _resolve_tokenizer_file(self, model_dir: Path) -> Path:
        """解析 tokenizer.json 的真实文件路径。"""

        tokenizer_path = Path(
            self.config.tokenizer_path
            or model_dir / TEXT_EOU_TOKENIZER_RELATIVE_PATH
        )
        if tokenizer_path.is_file():
            return tokenizer_path

        candidate = tokenizer_path / TEXT_EOU_TOKENIZER_RELATIVE_PATH.name
        if candidate.is_file():
            return candidate

        raise FileNotFoundError(f"text EOU tokenizer.json not found at or under: {tokenizer_path}")

    def _predict_cleaned_text(self, text: str) -> float:
        """对已清洗文本执行 ONNX 推理。"""

        if self._session is None or self._tokenizer is None:
            raise RuntimeError("文本 EOU provider 未初始化模型依赖")

        encoding = self._tokenizer.encode(text, add_special_tokens=False)
        inputs = {
            "input_ids": np.array([encoding.ids], dtype=np.int64),
            "attention_mask": np.array([encoding.attention_mask], dtype=np.int64),
        }
        outputs = self._session.run(None, inputs)
        return float(_softmax(outputs[0]).flatten()[-1])


def clean_text_for_eou(text: str) -> str:
    """按参考模型规则清洗 ASR 文本。"""

    return _PUNCT_RE.sub("", text)


def _softmax(value: np.ndarray) -> np.ndarray:
    """计算数值稳定的 batch softmax。"""

    array = np.asarray(value, dtype=np.float64)
    exp_value = np.exp(array - np.max(array, axis=1, keepdims=True))
    return exp_value / np.sum(exp_value, axis=1, keepdims=True)
