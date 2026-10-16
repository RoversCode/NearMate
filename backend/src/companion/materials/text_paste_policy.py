from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from companion.realtime.text_tokenizer import count_text_tokens


MAX_INLINE_TEXT_TOKENS = 500
MAX_INLINE_TEXT_BYTES = 16 * 1024


@dataclass(frozen=True)
class TextPasteClassification:
    """描述一次纯文本粘贴应进入消息正文还是材料区。"""

    disposition: Literal["inline", "material"]
    token_count: int

    def to_payload(self) -> dict[str, object]:
        """转换为 Desktop 可直接消费的分类结果。"""

        return {"disposition": self.disposition, "token_count": self.token_count}


def classify_text_paste(
    text: str,
    *,
    token_counter: Callable[[str], int] = count_text_tokens,
) -> TextPasteClassification:
    """使用项目统一 tokenizer 分类完整粘贴文本。

    Args:
        text: 本次粘贴的完整纯文本。
        token_counter: 项目统一 tokenizer，测试可注入确定性计数器。

    Returns:
        不截断正文的 inline 或 material 分类结果。
    """

    token_count = token_counter(text)
    byte_count = len(text.encode("utf-8"))
    disposition: Literal["inline", "material"] = (
        "inline"
        if token_count <= MAX_INLINE_TEXT_TOKENS and byte_count <= MAX_INLINE_TEXT_BYTES
        else "material"
    )
    return TextPasteClassification(disposition, token_count)
