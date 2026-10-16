from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer

from companion.core.runtime_paths import BACKEND_ROOT


DEFAULT_TOKENIZER_PATH = BACKEND_ROOT / "ckpt" / "tokenizer.json"


@lru_cache(maxsize=1)
def _load_default_tokenizer() -> Tokenizer:
    """加载实时文本切分使用的 tokenizer。

    Returns:
        从仓库模型资产中加载出的 tokenizer 实例。
    """

    return Tokenizer.from_file(str(DEFAULT_TOKENIZER_PATH))


def count_text_tokens(text: str, tokenizer_path: Path | str | None = None) -> int:
    """计算文本在当前 TTS/ASR 资产 tokenizer 下的 token 数量。

    Args:
        text: 待计数文本。
        tokenizer_path: 可选 tokenizer 文件路径，主要供诊断或测试替换。

    Returns:
        编码后的 token 数量。
    """

    if tokenizer_path is None:
        tokenizer = _load_default_tokenizer()
    else:
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
    return len(tokenizer.encode(text).ids)
