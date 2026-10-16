from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
import json
from typing import Any

from companion.llm.foreground_models import ForegroundToolCall


_BACKGROUND_WORK_TOOL_NAME = "start_background_work"


def project_tool_calls_for_runtime_memory(
    tool_calls: Sequence[object],
) -> list[dict[str, Any]]:
    """生成可以进入聊天历史和摘要的工具调用副本。

    `start_background_work.material_paths` 是执行期本地能力，不是聊天内容。这里保留
    工具协议配对和非路径参数，只删除本机路径。其它工具调用保持原样。

    Args:
        tool_calls: 执行阶段产生的原始 Chat-like 工具调用列表。

    Returns:
        与输入互不共享可变对象的安全工具调用列表；非法列表项会被忽略。
    """

    projected_calls: list[dict[str, Any]] = []
    for raw_tool_call in tool_calls:
        if not isinstance(raw_tool_call, dict):
            continue
        projected_call = deepcopy(raw_tool_call)
        function_payload = projected_call.get("function")
        if not isinstance(function_payload, dict):
            projected_calls.append(projected_call)
            continue
        if (
            str(function_payload.get("name") or "").strip()
            != _BACKGROUND_WORK_TOOL_NAME
        ):
            projected_calls.append(projected_call)
            continue

        raw_arguments = function_payload.get("arguments")
        try:
            parsed_arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else None
            )
        except ValueError:
            parsed_arguments = None
        if not isinstance(parsed_arguments, dict):
            function_payload["arguments"] = "{}"
            projected_calls.append(projected_call)
            continue

        parsed_arguments.pop("material_paths", None)
        function_payload["arguments"] = json.dumps(
            parsed_arguments,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        projected_calls.append(projected_call)
    return projected_calls


def build_assistant_tool_call_message(
    tool_calls: Sequence[ForegroundToolCall],
) -> dict[str, Any]:
    """构造可与原生工具结果配对的 assistant 消息。

    Args:
        tool_calls: 按模型原始顺序排列的工具调用。

    Returns:
        包含空正文和原生 tool_calls 的 assistant 消息。
    """

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_call.tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                },
            }
            for tool_call in tool_calls
        ],
    }


def validate_tool_protocol_messages(
    messages: Sequence[dict[str, Any]],
    *,
    require_final_assistant: bool = False,
) -> None:
    """校验 Chat-like 工具调用、结果和最终回复的原子顺序。

    Args:
        messages: 待发送或持久化的 Chat-like 消息序列。
        require_final_assistant: 是否要求工具结果后必须存在非空最终助手回复。

    Raises:
        ValueError: 调用编号缺失、结果孤立、顺序错误或事务不完整时抛出。
    """

    pending_tool_call_ids: list[str] = []
    awaiting_final_assistant = False

    for message in messages:
        role = str(message.get("role") or "").strip()
        tool_calls = message.get("tool_calls")
        normalized_tool_calls = tool_calls if isinstance(tool_calls, list) else []

        if pending_tool_call_ids:
            if role != "tool":
                raise ValueError("assistant tool_calls 后必须紧跟完整 tool 结果")
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            expected_tool_call_id = pending_tool_call_ids.pop(0)
            if tool_call_id != expected_tool_call_id:
                raise ValueError(
                    "tool 结果顺序或编号错误："
                    f"expected={expected_tool_call_id} actual={tool_call_id or 'empty'}"
                )
            if not pending_tool_call_ids:
                awaiting_final_assistant = True
            continue

        if awaiting_final_assistant:
            content = str(message.get("content") or "")
            if role != "assistant" or normalized_tool_calls or not content.strip():
                raise ValueError("完整工具事务必须以非空 assistant final 收尾")
            awaiting_final_assistant = False
            continue

        if role == "tool":
            raise ValueError("发现没有对应 assistant tool_calls 的孤立 tool 消息")
        if not normalized_tool_calls:
            continue
        if role != "assistant":
            raise ValueError("只有 assistant 消息可以声明 tool_calls")

        seen_ids: set[str] = set()
        for tool_call in normalized_tool_calls:
            tool_call_payload = tool_call if isinstance(tool_call, dict) else {}
            tool_call_id = str(tool_call_payload.get("id") or "").strip()
            if not tool_call_id:
                raise ValueError("assistant tool_calls 必须包含非空 id")
            if tool_call_id in seen_ids:
                raise ValueError(f"重复 tool_call_id: {tool_call_id}")
            seen_ids.add(tool_call_id)
            pending_tool_call_ids.append(tool_call_id)

    if pending_tool_call_ids:
        raise ValueError(
            "缺少 tool_call_id 响应: " + ", ".join(pending_tool_call_ids)
        )
    if require_final_assistant and awaiting_final_assistant:
        raise ValueError("持久工具事务缺少最终 assistant 回复")
