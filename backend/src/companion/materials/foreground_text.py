from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from companion.materials.bounded_text import (
    DEFAULT_TEXT_READ_CHUNK_BYTES,
    read_complete_utf8_text,
)
from companion.materials.models import TurnMaterialContext
from companion.materials.text_paste_policy import (
    MAX_INLINE_TEXT_BYTES,
    MAX_INLINE_TEXT_TOKENS,
)
from companion.realtime.text_tokenizer import count_text_tokens


FOREGROUND_TEXT_READ_CHUNK_BYTES = DEFAULT_TEXT_READ_CHUNK_BYTES
ForegroundTextLoadReason = Literal[
    "ready",
    "no_material",
    "non_text_material",
    "invalid_path",
    "byte_limit_exceeded",
    "token_limit_exceeded",
    "unreadable_text",
]


@dataclass(frozen=True)
class ForegroundTextMaterial:
    """保存当前 turn 可直接交给 Foreground 的单项完整文本。

    Args:
        material_id: 正式材料编号。
        display_name: 用户看到的材料名称。
        text: 完整 UTF-8 正文。
    """

    material_id: str
    display_name: str
    text: str

    def to_prompt_payload(self) -> dict[str, str]:
        """转换为仅供当前 Prompt 使用的文本载荷。

        Returns:
            不包含路径和授权字段的完整小文本对象。
        """

        return {
            "material_id": self.material_id,
            "display_name": self.display_name,
            "text": self.text,
        }


@dataclass(frozen=True)
class ForegroundTextBatch:
    """描述一次全有或全无的小型纯文本装载结果。

    Args:
        reason: 整批成功或固定失败原因。
        materials: 成功时的全部完整文本材料。
        token_count: 全部正文与分隔换行的总 token 数。
    """

    reason: ForegroundTextLoadReason
    materials: tuple[ForegroundTextMaterial, ...] = ()
    token_count: int = 0


@dataclass(frozen=True)
class _TextReference:
    """保存装载器所需的最小材料引用。

    Args:
        material_id: 正式材料编号。
        display_name: 面向用户的名称。
        path: 本地材料路径。
        media_class: 准入阶段冻结的媒体类别。
    """

    material_id: str
    display_name: str
    path: str
    media_class: str


def load_foreground_text_batch(
    turn: TurnMaterialContext | dict[str, object] | None,
    *,
    token_counter: Callable[[str], int] = count_text_tokens,
    max_tokens: int = MAX_INLINE_TEXT_TOKENS,
    max_bytes: int = MAX_INLINE_TEXT_BYTES,
    chunk_bytes: int = FOREGROUND_TEXT_READ_CHUNK_BYTES,
) -> ForegroundTextBatch:
    """读取本轮要交给 Foreground LLM 的纯文本材料。

    这是一个全有或全无的加载器：只要有一个材料不合格，整批都只返回失败原因。
    底层读取与 Background 短文本展示共用同一套普通文件、UTF-8 和硬字节边界。

    Args:
        turn: 当前冻结的本轮材料上下文。
        token_counter: 项目统一 tokenizer 计数函数。
        max_tokens: 整批正文允许进入 Foreground 的最大 token 数。
        max_bytes: 整批读取允许消耗的最大字节数。
        chunk_bytes: 单次读取文件的最大字节数。

    Returns:
        成功时返回所有完整文本材料和总 token 数；失败时不带任何正文。
    """

    references = _text_references(turn)
    if references is None:
        return ForegroundTextBatch("invalid_path")
    if not references:
        return ForegroundTextBatch("no_material")

    materials: list[ForegroundTextMaterial] = []
    consumed_bytes = 0
    for reference in references:
        if reference.media_class != "text":
            return ForegroundTextBatch("non_text_material")
        remaining_bytes = max_bytes - consumed_bytes
        if remaining_bytes <= 0:
            return ForegroundTextBatch("byte_limit_exceeded")
        read_result = read_complete_utf8_text(
            Path(reference.path),
            max_bytes=remaining_bytes,
            chunk_bytes=chunk_bytes,
        )
        if read_result.reason != "ready" or read_result.text is None:
            return ForegroundTextBatch(read_result.reason)
        candidate_texts = [*(item.text for item in materials), read_result.text]
        candidate_batch = "\n".join(candidate_texts)
        token_count = token_counter(candidate_batch)
        if token_count > max_tokens:
            return ForegroundTextBatch("token_limit_exceeded")
        consumed_bytes += read_result.byte_count
        materials.append(
            ForegroundTextMaterial(
                material_id=reference.material_id,
                display_name=reference.display_name,
                text=read_result.text,
            )
        )

    batch_text = "\n".join(item.text for item in materials)
    return ForegroundTextBatch(
        "ready",
        tuple(materials),
        token_counter(batch_text),
    )


def _text_references(
    turn: TurnMaterialContext | dict[str, object] | None,
) -> tuple[_TextReference, ...] | None:
    """从本轮材料上下文里提取文本文件引用。

    Args:
        turn: 类型化材料上下文、跨进程字典或空值。

    Returns:
        字段完整的引用；协议结构不合法时返回空值。
    """

    if turn is None:
        return ()
    if isinstance(turn, TurnMaterialContext):
        return tuple(
            _TextReference(item.material_id, item.display_name, item.path, item.media_class)
            for item in turn.attachments
        )
    raw_attachments = turn.get("attachments")
    if not isinstance(raw_attachments, list):
        return None

    references: list[_TextReference] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            return None
        material_id = str(item.get("material_id") or "").strip()
        display_name = str(item.get("display_name") or "").strip()
        path = str(item.get("path") or "").strip()
        media_class = str(item.get("media_class") or "").strip()
        if not material_id or not display_name or not path or not media_class:
            return None
        references.append(_TextReference(material_id, display_name, path, media_class))
    return tuple(references)
