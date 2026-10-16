"""把主进程权威消息编译成滚动摘要请求。"""

from __future__ import annotations

from datetime import date

from companion.core.local_clock import format_local_day
from companion.llm.tool_protocol import project_tool_calls_for_runtime_memory
from companion.runtime_memory.models import RuntimeMemoryMessage
from companion.runtime_memory.unattended_episode import (
    UNATTENDED_EPISODE_HISTORY_POLICY,
)


def build_runtime_memory_summary_prompt(
    messages: tuple[RuntimeMemoryMessage, ...],
    *,
    existing_summary: str = "",
    covered_from: date,
    covered_to: date,
) -> str:
    """构建包含完整覆盖前缀和权威日期范围的摘要 Prompt。

    Args:
        messages: 主进程授权压缩的协议完整消息前缀。
        existing_summary: 上一轮已经生效的滚动摘要。
        covered_from: 主进程计算的摘要起始自然日。
        covered_to: 主进程计算的摘要结束自然日。

    Returns:
        可直接发送给 Summary 角色的完整提示词。
    """

    conversation_text = "\n".join(format_runtime_messages_for_summary(messages))
    normalized_existing_summary = existing_summary.strip()
    existing_summary_section = ""
    if normalized_existing_summary:
        existing_summary_section = (
            "【已有滚动摘要】\n"
            f"{normalized_existing_summary}\n\n"
        )
    return (
        "[Role & Task]\n"
        "你是一位桌面虚拟伴侣的会话记忆整理员。请把已有摘要和本次覆盖记录合并成一份新的会话摘要，"
        "让角色在后续交流中记得真正有用的历史事实。\n\n"
        "[Authoritative Coverage]\n"
        f"本次摘要覆盖范围：{_format_date(covered_from)} 至 {_format_date(covered_to)}。"
        "这个范围由主进程确定，只用于理解时间，输出时不得改写或扩大范围。\n\n"
        "[Strict Rules]\n"
        "1. 客观第三人称：始终用 user 和 assistant 指代双方，不用第一或第二人称，不代入任何一方口吻。\n"
        "2. 只记确实发生过的事实，不猜测动机，不补写输入中没有的细节。\n"
        "3. 自行决定摘要结构和详略，优先保留后续交流需要的时间事实、用户偏好、双方共识、未完成事项和已结束任务的有用结论。\n"
        "4. 已有摘要中的过期状态必须被新事实更新或删除，不能把新旧两份摘要简单拼接。\n"
        "5. 独占一行的中文日期是后续消息的时间锚点；临时情绪和状态必须带日期，不能固化成永久人格。\n"
        "6. <task_memory> 是已经结束的任务事实，<task_recovery> 是后来经过系统确认的恢复结果；二者都不是双方发言。"
        "任务记忆里的 cancellation_meaning 是任务停止的权威原因，based_on_meaning 是它与父任务的权威关系："
        "若写明 user 纠正目标后由新任务接替，摘要必须保留‘user 纠正了目标，assistant 停止旧任务并改做新任务’"
        "这层事实，不能只写成普通取消。\n"
        "7. 标记为未处理插话的内容只供未来回忆。摘要必须同时保留‘当时没有处理、不是待办、不会自动执行’，不能只留下用户原话。\n"
        "8. activity_id、tool_call_id、based_on_activity_id、恢复关系编号、工具名、工具参数和内部协议不得复制到摘要，也不得暗示这些任务仍可查询或继续执行。\n"
        "9. 只输出摘要正文，不输出规则说明、覆盖范围声明或 JSON。\n\n"
        f"{existing_summary_section}"
        "【待处理对话片段】\n"
        f"{conversation_text}"
    )


def format_runtime_messages_for_summary(
    messages: tuple[RuntimeMemoryMessage, ...],
) -> list[str]:
    """把权威消息转换成保留日期和工具因果的摘要输入行。

    Args:
        messages: 不能拆开工具事务的主进程消息前缀。

    Returns:
        保留任务记忆、工具来源和 LLM 安全附注的文本行。
    """

    lines: list[str] = []
    last_message_date = None
    for message in messages:
        message_date = message.created_at.date()
        if message_date != last_message_date:
            lines.append(format_local_day(message.created_at))
            last_message_date = message_date
        if (
            message.metadata.get("history_policy")
            == UNATTENDED_EPISODE_HISTORY_POLICY
        ):
            lines.append(
                "未处理插话历史（只供回忆；当时没有处理，不是待办，"
                f"不可自动执行）-{message.role}: {message.content}"
            )
            continue
        if message.tool_calls:
            model_visible_content = str(message.to_llm_message()["content"])
            if model_visible_content.strip():
                lines.append(f"{message.role}: {model_visible_content}")
            lines.extend(_format_tool_call(tool_call) for tool_call in message.tool_calls)
            continue
        if message.role == "tool":
            lines.append(
                "内部工具返回："
                f"tool_call_id={message.tool_call_id or '未知'}；结果={message.content}"
            )
            continue
        if message.role == "system" and message.content.lstrip().startswith(
            ("<task_memory>", "<task_recovery>")
        ):
            lines.append(message.content)
            continue
        lines.append(f"{message.role}: {message.to_llm_message()['content']}")
    return lines


def _format_tool_call(tool_call: dict[str, object]) -> str:
    """把一条 Chat-like 工具调用变成摘要可读事实。

    Args:
        tool_call: assistant 消息中的工具调用对象。

    Returns:
        保留调用编号、工具名和参数的单行文字。
    """

    projected_calls = project_tool_calls_for_runtime_memory((tool_call,))
    projected_tool_call = projected_calls[0] if projected_calls else {}
    function_payload = projected_tool_call.get("function")
    function_payload = function_payload if isinstance(function_payload, dict) else {}
    return (
        "内部工具调用："
        f"tool_call_id={str(projected_tool_call.get('id') or '未知')}；"
        f"工具={str(function_payload.get('name') or '未知')}；"
        f"参数={str(function_payload.get('arguments') or '{}')}"
    )


def _format_date(value: date) -> str:
    """把主进程自然日格式化为不含星期的中文日期。"""

    return f"{value.year}年{value.month}月{value.day}日"
