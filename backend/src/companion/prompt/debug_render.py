from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


_PROTOCOL_KEYS = {"role", "content", "tool_calls", "tool_call_id"}


def render_llm_messages_for_debug(
    messages: Sequence[Mapping[str, Any]],
) -> str:
    """把 Chat-like LLM messages 渲染为保留工具协议的人类可读文本。

    Args:
        messages: 即将发送给 LLM 的 Chat-like 消息列表。

    Returns:
        保留 role、content、tool_calls、tool_call_id 和额外字段的多行文本。
    """

    rendered: list[str] = []
    for index, message in enumerate(messages):
        role = str(message.get("role") or "unknown")
        content = _debug_text(message.get("content"))

        if role == "assistant" and message.get("tool_calls"):
            rendered.append(f"[{index}] assistant.tool_calls:")
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    rendered.append(f"  - {_debug_json(tool_call)}")
            else:
                rendered.append(f"  - {_debug_json(tool_calls)}")
            if content:
                rendered.append(f"  content: {content}")
            _append_extra_fields(rendered, message)
            continue

        if role == "tool":
            rendered.append(
                f"[{index}] tool: "
                f"tool_call_id={message.get('tool_call_id')!r} "
                f"content={content}"
            )
            _append_extra_fields(rendered, message)
            continue

        rendered.append(f"[{index}] {role}: {content}")
        _append_extra_fields(rendered, message)

    return "\n".join(rendered)


def _append_extra_fields(
    rendered: list[str],
    message: Mapping[str, Any],
) -> None:
    """追加标准协议字段之外的调试信息。

    Args:
        rendered: 正在累积的调试文本行。
        message: 当前 Chat-like 消息。
    """

    extra_fields = {
        key: value
        for key, value in message.items()
        if key not in _PROTOCOL_KEYS
    }
    if not extra_fields:
        return
    rendered.append(f"  extra: {_debug_json(extra_fields)}")


def _debug_text(value: object) -> str:
    """把消息正文安全转换为单行调试文本。

    Args:
        value: Chat-like 消息里的 content 字段。

    Returns:
        可安全拼进调试日志的文本。
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _debug_json(value)


def _debug_json(value: object) -> str:
    """把调试对象转换为稳定 JSON 文本。

    Args:
        value: 需要渲染的调试对象。

    Returns:
        保留中文且字段稳定排序的 JSON 文本。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
