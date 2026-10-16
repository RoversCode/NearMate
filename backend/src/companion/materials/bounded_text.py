from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from companion.filesystem.safety import (
    FileSystemNodeInspectionError,
    inspect_filesystem_node,
)
from companion.materials.type_registry import MATERIAL_TYPES


DEFAULT_TEXT_READ_CHUNK_BYTES = 8 * 1024
CompleteTextReadReason = Literal[
    "ready",
    "non_text_material",
    "invalid_path",
    "byte_limit_exceeded",
    "unreadable_text",
]


@dataclass(frozen=True, slots=True)
class CompleteTextRead:
    """保存一次有界 UTF-8 文本读取的完整结果。

    Args:
        reason: 成功或固定失败原因。
        text: 仅在整份文件完整读取成功时存在的原文。
        byte_count: 成功读取的原始字节数；失败时为零。
    """

    reason: CompleteTextReadReason
    text: str | None = None
    byte_count: int = 0


def read_complete_utf8_text(
    path: Path,
    *,
    max_bytes: int,
    chunk_bytes: int = DEFAULT_TEXT_READ_CHUNK_BYTES,
) -> CompleteTextRead:
    """在硬字节上限内读取一整个安全普通 UTF-8 文本文件。

    只要文件类型、节点形态、编码、内容或预算有一项不合格，就只返回失败原因，
    绝不返回截断正文。

    Args:
        path: 需要读取的本地材料路径。
        max_bytes: 本次调用最多允许读取的完整文件字节数。
        chunk_bytes: 单次底层读取的最大字节数。

    Returns:
        完整正文及字节数，或不携带正文的固定失败结果。

    Raises:
        ValueError: 字节上限或读取块大小不是正整数。
    """

    if max_bytes <= 0 or chunk_bytes <= 0:
        raise ValueError("文本读取字节上限和分块大小必须为正数")
    if MATERIAL_TYPES.classify_path_media(path) != "text":
        return CompleteTextRead("non_text_material")

    try:
        snapshot = inspect_filesystem_node(path)
        if (
            snapshot.node_kind != "file"
            or snapshot.is_linked_or_reparse
            or snapshot.size <= 0
        ):
            return CompleteTextRead("invalid_path")
        if snapshot.size > max_bytes:
            return CompleteTextRead("byte_limit_exceeded")

        chunks: list[bytes] = []
        consumed_bytes = 0
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_bytes):
                consumed_bytes += len(chunk)
                if consumed_bytes > max_bytes:
                    return CompleteTextRead("byte_limit_exceeded")
                chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", errors="strict")
        if not text.strip():
            return CompleteTextRead("unreadable_text")
        return CompleteTextRead("ready", text=text, byte_count=consumed_bytes)
    except (
        FileNotFoundError,
        FileSystemNodeInspectionError,
        OSError,
        UnicodeError,
    ):
        return CompleteTextRead("unreadable_text")


__all__ = [
    "CompleteTextRead",
    "CompleteTextReadReason",
    "DEFAULT_TEXT_READ_CHUNK_BYTES",
    "read_complete_utf8_text",
]
