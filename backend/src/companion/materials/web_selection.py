from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_WEB_SELECTION_BYTES = 5 * 1024 * 1024
MAX_WEB_SELECTION_PARTS = 200


class WebSelectionError(ValueError):
    """表示网页选区 JSON 不符合受支持协议。"""


@dataclass(frozen=True, slots=True)
class WebSelectionPart:
    """描述网页选区中一个保持原始位置的增强片段。

    Args:
        kind: 普通文本、网页链接或远程图片 URL。
        text: 文本正文或链接可见文字；图片和无文字链接为空。
        url: 未经 Backend 解释的链接目标或图片地址；普通文本为空。
    """

    kind: Literal["text", "link", "image_url"]
    text: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class WebSelection:
    """保存一份完成严格校验的网页选区 v2。

    Args:
        plain_text: 剪贴板 `text/plain` 中的权威用户原文；没有该格式时为空。
        parts: 按网页 DOM 顺序保存的文字、链接和图片增强信息。
    """

    plain_text: str | None
    parts: tuple[WebSelectionPart, ...]

    def text_byte_count(self) -> int:
        """计算预检需要使用的唯一文本预算。

        Returns:
            有权威纯文本时只计算它一次；否则计算各可见文字片段。
        """

        if self.plain_text is not None:
            return len(self.plain_text.encode("utf-8"))
        return sum(
            len((part.text or "").encode("utf-8"))
            for part in self.parts
            if part.kind in {"text", "link"}
        )


def read_web_selection(path: Path) -> WebSelection:
    """读取单文件网页选区，并严格验证 v2 的结构、字段和顺序。

    Args:
        path: Desktop 生成的网页选区 JSON 文件。

    Returns:
        权威纯文本与按网页 DOM 顺序排列的增强片段。

    Raises:
        WebSelectionError: 文件不可读或协议结构不合法。
    """

    try:
        # 先在文件层面卡住不存在、过大的输入，避免把异常或超大 JSON 带入协议解析阶段。
        if not path.is_file() or path.stat().st_size > MAX_WEB_SELECTION_BYTES:
            raise WebSelectionError("网页选区文件不存在或超过大小限制。")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except WebSelectionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise WebSelectionError("网页选区文件无法读取。") from exception

    # Desktop 与 Backend 随同一版本发布；旧协议和扩展字段都应立即暴露失配。
    if not isinstance(payload, dict) or set(payload) != {"version", "plain_text", "parts"}:
        raise WebSelectionError("网页选区顶层字段不合法。")
    if payload.get("version") != 2 or isinstance(payload.get("version"), bool):
        raise WebSelectionError("网页选区版本不受支持。")

    plain_text = _parse_plain_text(payload.get("plain_text"))
    raw_parts = payload.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise WebSelectionError("网页选区必须包含非空 parts。")
    if len(raw_parts) > MAX_WEB_SELECTION_PARTS:
        raise WebSelectionError("网页选区内容片段数量超过限制。")

    parts = tuple(_parse_part(raw_part) for raw_part in raw_parts)
    if not any(part.kind in {"link", "image_url"} for part in parts):
        raise WebSelectionError("网页选区必须包含链接或图片增强信息。")
    return WebSelection(plain_text=plain_text, parts=parts)


def _parse_plain_text(value: object) -> str | None:
    """验证并清理权威纯文本字段。

    Args:
        value: 反序列化后尚未校验的 `plain_text`。

    Returns:
        去除选区首尾空白后的完整原文；原字段为空时返回空。

    Raises:
        WebSelectionError: 字段不是空值或非空字符串。
    """

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WebSelectionError("网页选区 plain_text 不合法。")
    return value.strip()


def _parse_part(raw_part: object) -> WebSelectionPart:
    """验证并转换单个网页选区片段。

    Args:
        raw_part: 反序列化后尚未校验的单个 part 对象。

    Returns:
        通过协议校验后的网页选区片段领域对象。

    Raises:
        WebSelectionError: part 结构、字段或值不符合受支持协议时抛出。
    """

    if not isinstance(raw_part, dict):
        raise WebSelectionError("网页选区 part 必须是对象。")
    part_type = raw_part.get("type")
    if part_type == "text":
        text = raw_part.get("text")
        if set(raw_part) != {"type", "text"} or not isinstance(text, str) or not text.strip():
            raise WebSelectionError("网页选区文本 part 不合法。")
        return WebSelectionPart(kind="text", text=text.strip())
    if part_type == "link":
        text = raw_part.get("text")
        url = raw_part.get("url")
        if (
            set(raw_part) != {"type", "text", "url"}
            or (text is not None and (not isinstance(text, str) or not text.strip()))
            or not isinstance(url, str)
            or not url.strip()
        ):
            raise WebSelectionError("网页选区链接 URL 字段不合法。")
        return WebSelectionPart(
            kind="link",
            text=text.strip() if isinstance(text, str) else None,
            url=url.strip(),
        )
    if part_type == "image_url":
        url = raw_part.get("url")
        if set(raw_part) != {"type", "url"} or not isinstance(url, str) or not url.strip():
            raise WebSelectionError("网页选区图片 URL 字段不合法。")
        return WebSelectionPart(kind="image_url", url=url.strip())
    raise WebSelectionError("网页选区包含不支持的 part 类型。")
