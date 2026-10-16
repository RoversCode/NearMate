"""统一计算 Chat-like 历史消息占用的协议文本。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def protocol_message_token_text(
    *,
    role: str,
    content: str,
    tool_calls: Sequence[Mapping[str, Any]] = (),
    tool_call_id: str | None = None,
    llm_context_note: str = "",
) -> str:
    """生成主进程和助手子进程共用的 Token 计数文本。

    Args:
        role: Chat-like 消息角色。
        content: 消息原始正文。
        tool_calls: assistant 声明的工具调用协议。
        tool_call_id: tool 消息对应的调用编号。
        llm_context_note: 只给 Foreground 阅读的安全附注。

    Returns:
        覆盖所有模型可见协议字段的稳定 JSON 文本。
    """

    return json.dumps(
        {
            "role": role,
            "content": content,
            "llm_context_note": llm_context_note.strip(),
            "tool_calls": [dict(item) for item in tool_calls],
            "tool_call_id": tool_call_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
