"""把清理后的最终 LLM 请求渲染成人工可读 Markdown。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from companion.observability.prompt_audit_sanitizer import (
    sanitize_chat_messages,
    sanitize_prompt_value,
)


_PROMPT_FIELDS = frozenset({"messages"})
_MODEL_RESULT_START = "<!-- prompt-audit-model-result:start -->"
_MODEL_RESULT_END = "<!-- prompt-audit-model-result:end -->"


def render_prompt_audit_markdown(
    *,
    captured_at: datetime,
    process_role: str,
    run_id: str,
    sequence: int,
    role: str,
    source: str,
    protocol: str,
    model: str,
    operation: str,
    request: Mapping[str, Any],
    trace_fields: Mapping[str, object],
) -> str:
    """把最终 SDK 请求渲染成面向人工审查的 Markdown。

    Args:
        captured_at: 本次审查记录时间。
        process_role: 产生请求的进程角色。
        run_id: 当前进程生命周期对应的运行编号。
        sequence: 本次运行内的调用序号。
        role: LLM 职责或内部用途。
        source: 触发本次调用的业务来源。
        protocol: 厂商 API 协议。
        model: 本次请求使用的真实模型名。
        operation: 当前调用阶段。
        request: 最终 SDK 请求参数。
        trace_fields: 请求或 Activity 关联身份。

    Returns:
        完整 Markdown 文本。
    """

    metadata_rows = [
        ("captured_at", captured_at.isoformat().replace("+00:00", "Z")),
        ("process_role", process_role),
        ("run_id", run_id),
        ("sequence", str(sequence)),
        ("role", role),
        ("source", source),
        ("protocol", protocol),
        ("model", model),
        ("operation", operation),
    ]
    preferred_trace_keys = (
        "request_id",
        "generation_id",
        "request_source",
        "round",
        "activity_id",
        "activity_kind",
        "llm_call_id",
    )
    metadata_rows.extend(
        (key, _clean_metadata_text(trace_fields.get(key)))
        for key in preferred_trace_keys
        if _clean_metadata_text(trace_fields.get(key))
    )
    remaining_trace = {
        key: value
        for key, value in trace_fields.items()
        if key not in preferred_trace_keys and key not in {"protocol", "role"}
    }

    messages = request.get("messages")
    tools = request.get("tools")
    request_options = {
        key: value
        for key, value in request.items()
        if key not in _PROMPT_FIELDS and key != "tools"
    }
    safe_messages = sanitize_chat_messages(messages)
    # 工具 Schema 的属性名可能恰好叫 api_key。这里只保留字段名，真实请求值仍会屏蔽。
    safe_tools = sanitize_prompt_value(
        tools,
        redact_credentials=True,
        preserve_schema_property_names=True,
    )
    safe_options = sanitize_prompt_value(request_options, redact_credentials=True)
    safe_remaining_trace = sanitize_prompt_value(
        remaining_trace,
        redact_credentials=True,
    )

    sections = [
        "# LLM Prompt 审查记录",
        "",
        "> 警告：本文件可能包含完整用户正文、会话历史与材料内容，仅用于本地开发排查。",
        "",
        "## 调用信息",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
    ]
    sections.extend(
        f"| `{_escape_table_text(key)}` | {_escape_table_text(value)} |"
        for key, value in metadata_rows
    )
    if safe_remaining_trace:
        sections.extend(
            [
                "",
                "### 其它关联信息",
                "",
                _fenced_json(safe_remaining_trace),
            ]
        )

    sections.extend(
        [
            "",
            "## 请求参数",
            "",
            _fenced_json(safe_options),
            "",
            "## Prompt 消息",
            "",
            _render_messages(safe_messages),
            "",
            "## 工具声明",
            "",
            _render_tools(safe_tools),
            "",
        ]
    )
    return "\n".join(sections)


def render_prompt_audit_model_result(
    *,
    content_text: str,
    tool_calls: object,
    error: Mapping[str, object] | None,
) -> str:
    """把一次模型调用的正文、工具调用和错误渲染为可替换章节。

    Args:
        content_text: 模型在本次调用中产生的普通正文；错误时可以是不完整正文。
        tool_calls: 模型产生的原始结构化工具调用。
        error: 调用或流消费错误；正常完成时为空。

    Returns:
        带稳定边界标记的 Markdown 结果章节。
    """

    sanitized_message = sanitize_chat_messages(
        [
            {
                "role": "assistant",
                "content": content_text,
                "tool_calls": tool_calls,
            }
        ]
    )[0]
    safe_content = ""
    safe_tool_calls: object = []
    if isinstance(sanitized_message, dict):
        raw_content = sanitized_message.get("content")
        if isinstance(raw_content, str):
            safe_content = raw_content
        safe_tool_calls = sanitized_message.get("tool_calls", [])
    safe_error = sanitize_prompt_value(error, redact_credentials=True)

    sections = [
        _MODEL_RESULT_START,
        "## 模型结果",
        "",
        f"状态：`{'error' if error is not None else 'completed'}`",
        "",
        "### 普通正文",
        "",
        _fenced_text(safe_content) if safe_content else "_模型没有产生普通正文。_",
        "",
        "### 结构化工具调用",
        "",
        _fenced_json(safe_tool_calls) if safe_tool_calls else "_模型没有产生结构化工具调用。_",
        "",
        "### 调用或流消费错误",
        "",
        _fenced_json(safe_error) if safe_error else "_本次调用正常完成。_",
        _MODEL_RESULT_END,
    ]
    return "\n".join(sections)


def replace_prompt_audit_model_result(
    markdown: str,
    result_markdown: str,
) -> str:
    """在现有请求记录中新增或替换模型结果章节。

    Args:
        markdown: 已经落盘的完整请求审查 Markdown。
        result_markdown: 新渲染的模型结果章节。

    Returns:
        仍然只包含一份模型结果章节的完整 Markdown。
    """

    start_index = markdown.find(_MODEL_RESULT_START)
    end_index = markdown.find(_MODEL_RESULT_END)
    if start_index < 0 or end_index < start_index:
        return f"{markdown.rstrip()}\n\n{result_markdown}\n"
    result_end_index = end_index + len(_MODEL_RESULT_END)
    return (
        f"{markdown[:start_index].rstrip()}\n\n"
        f"{result_markdown}\n"
        f"{markdown[result_end_index:].lstrip()}"
    )


def _render_messages(messages: object) -> str:
    """按请求顺序展开 Chat messages。

    Args:
        messages: 已折叠二进制内容的消息序列。

    Returns:
        面向人工阅读的消息 Markdown。
    """

    if not isinstance(messages, list):
        return "_本次请求没有消息序列。_" if messages is None else _fenced_json(messages)
    if not messages:
        return "_本次请求的消息序列为空。_"

    rendered_messages: list[str] = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            rendered_messages.extend(
                [f"### {index}. 未知消息", "", _fenced_json(message), ""]
            )
            continue

        label = _clean_metadata_text(message.get("role")) or "未知消息"
        rendered_messages.extend([f"### {index}. `{label}`", ""])
        content = message.get("content")
        if isinstance(content, str):
            rendered_messages.extend([_fenced_text(content), ""])
        elif isinstance(content, list):
            rendered_messages.extend([_render_content_blocks(content), ""])
        elif content is not None:
            rendered_messages.extend([_fenced_json(content), ""])

        extra_fields = {
            key: value
            for key, value in message.items()
            if key not in {"role", "type", "content"}
        }
        if extra_fields:
            rendered_messages.extend(
                ["#### 附加字段", "", _fenced_json(extra_fields), ""]
            )
    return "\n".join(rendered_messages).rstrip()


def _render_content_blocks(content: list[object]) -> str:
    """展开一条多模态消息中的文本、图片和其它内容块。

    Args:
        content: 按请求顺序排列的内容块。

    Returns:
        多模态内容块 Markdown。
    """

    rendered_blocks: list[str] = []
    for index, block in enumerate(content, start=1):
        if not isinstance(block, dict):
            rendered_blocks.extend(
                [f"#### 内容块 {index}", "", _fenced_json(block), ""]
            )
            continue
        block_type = _clean_metadata_text(block.get("type")) or "未知类型"
        rendered_blocks.extend([f"#### 内容块 {index}: `{block_type}`", ""])
        text = block.get("text")
        if isinstance(text, str):
            rendered_blocks.extend([_fenced_text(text), ""])
            remaining_block = {
                key: value
                for key, value in block.items()
                if key not in {"type", "text"}
            }
            if remaining_block:
                rendered_blocks.extend([_fenced_json(remaining_block), ""])
            continue
        rendered_blocks.extend([_fenced_json(block), ""])
    return "\n".join(rendered_blocks).rstrip()


def _render_tools(tools: object) -> str:
    """按顺序展开本次请求携带的完整工具声明。

    Args:
        tools: Chat 最终函数工具数组。

    Returns:
        完整工具 Markdown。
    """

    if tools is None:
        return "_本次请求没有工具声明。_"
    if not isinstance(tools, list):
        return _fenced_json(tools)
    if not tools:
        return "_本次请求的工具声明为空。_"

    rendered_tools: list[str] = []
    for index, tool in enumerate(tools, start=1):
        rendered_tools.extend(
            [
                f"### {index}. `{_tool_display_name(tool)}`",
                "",
                _fenced_json(tool),
                "",
            ]
        )
    return "\n".join(rendered_tools).rstrip()


def _tool_display_name(tool: object) -> str:
    """返回工具声明中最适合人看的名称。

    Args:
        tool: 单个 Chat 函数工具声明。

    Returns:
        工具函数名、工具类型或未知工具。
    """

    if not isinstance(tool, dict):
        return "未知工具"
    function = tool.get("function")
    if isinstance(function, dict):
        name = _clean_metadata_text(function.get("name"))
        if name:
            return name
    return _clean_metadata_text(tool.get("type")) or "未知工具"


def _fenced_text(value: str) -> str:
    """使用不会被正文反引号提前关闭的 Markdown 围栏包裹文本。

    Args:
        value: 需要完整展示的 Prompt 文本。

    Returns:
        带 text 类型标记的 Markdown 代码块。
    """

    return _fenced_block(value, language="text")


def _fenced_json(value: object) -> str:
    """把任意请求值格式化成稳定 JSON 代码块。

    Args:
        value: 需要展示的请求值。

    Returns:
        带 json 类型标记的 Markdown 代码块。
    """

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        default=_json_fallback,
    )
    return _fenced_block(serialized, language="json")


def _fenced_block(value: str, *, language: str) -> str:
    """选择足够长的代码围栏，保证正文内容不会破坏 Markdown。

    Args:
        value: 代码块正文。
        language: Markdown 语法高亮类型。

    Returns:
        安全的 Markdown 代码块。
    """

    longest_run = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{language}\n{value}\n{fence}"


def _json_fallback(value: object) -> object:
    """把少量非 JSON SDK 参数转换成可读文本。

    Args:
        value: JSON 编码器无法直接处理的值。

    Returns:
        Pydantic 字典、文件路径或可读字符串。
    """

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def _clean_metadata_text(value: object) -> str:
    """把元数据值转换成无首尾空白的字符串。

    Args:
        value: 任意元数据值。

    Returns:
        空值对应空串，其余值对应裁剪后的字符串。
    """

    if value is None:
        return ""
    return str(value).strip()


def _escape_table_text(value: object) -> str:
    """转义 Markdown 表格中的换行和竖线。

    Args:
        value: 表格单元格内容。

    Returns:
        不会破坏表格结构的文本。
    """

    return str(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")
