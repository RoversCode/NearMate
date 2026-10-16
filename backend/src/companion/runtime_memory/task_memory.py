"""把任务终态投影为自包含的 Foreground 短记忆。"""

from __future__ import annotations

from companion.prompt.xml_markup import render_xml_block, render_xml_cdata_leaf, render_xml_text_leaf
from companion.runtime_memory.models import TaskMemoryRecord, TaskRecoveryOutcome


# 用户能听懂、且与代码事实一致的取消原因大白话。裸枚举本身不承担业务语义，
# LLM 只应消费投影后的自然语言（Prompt 语义自包含）。
_CANCELLATION_REASON_MEANINGS = {
    "user_cancelled": "用户明确要求停止这项任务。",
    "user_correction": (
        "用户纠正了这项任务的目标，它因此被停止；接替它的新任务以本任务为父任务。"
    ),
    "permission_denied": "用户拒绝了这项任务必需的操作授权，它因此被停止。",
}

# 未列入上表的停止原因（助手运行时关闭、Backend 退出等）都来自系统侧。
# 默认文案必须保证不把系统停止误归到用户头上。
_SYSTEM_CANCELLATION_MEANING = "这次停止由系统侧触发，不是用户主动要求。"

_CORRECTION_RELATIONSHIP_MEANING = (
    "本任务是对父任务的纠正替代：用户纠正了父任务的目标，父任务因此停止，由本任务接替。"
)

# 普通派生没有 based_on_relationship 字段，必须显式说明，避免被误读成纠正替代。
_ORDINARY_DERIVATION_MEANING = (
    "本任务以已经结束的父任务为参考创建；父任务的结束不是本任务造成的。"
)


def task_cancellation_meaning(cancellation_reason: str) -> str:
    """把取消原因代码投影成角色能直接转述的大白话。

    Args:
        cancellation_reason: 执行器冻结的稳定取消原因代码。

    Returns:
        自包含的一句中文说明；未识别的系统侧原因返回不归咎用户的安全文案。
    """

    return _CANCELLATION_REASON_MEANINGS.get(
        cancellation_reason,
        _SYSTEM_CANCELLATION_MEANING,
    )


def task_based_on_meaning(based_on_relationship: str | None) -> str:
    """把任务与父任务的关系投影成角色能直接转述的大白话。

    Args:
        based_on_relationship: 写入时已校验的父子关系类型；普通派生为空。

    Returns:
        自包含的一句中文说明；无法识别的关系返回空串。
    """

    if based_on_relationship is None:
        return _ORDINARY_DERIVATION_MEANING
    if based_on_relationship == "activity_correction":
        return _CORRECTION_RELATIONSHIP_MEANING
    return ""


def render_task_memory(record: TaskMemoryRecord) -> str:
    """渲染一条可以独立理解的任务记忆 XML。

    Args:
        record: 已完成基础校验的任务记忆记录。

    Returns:
        不依赖后续 Notice 文本也能理解的 system 消息正文。
    """

    origin_tool_call_ids = ",".join(record.origin_tool_call_ids)
    children = [
        render_xml_text_leaf("activity_id", record.activity_id),
        render_xml_text_leaf("origin_tool_call_ids", origin_tool_call_ids),
        render_xml_text_leaf("source", record.source),
        render_xml_cdata_leaf("goal", record.goal),
        render_xml_text_leaf("terminal_state", record.terminal_state),
    ]
    if record.cancellation_reason:
        children.append(
            render_xml_text_leaf(
                "cancellation_reason",
                record.cancellation_reason,
            )
        )
        children.append(
            render_xml_cdata_leaf(
                "cancellation_meaning",
                task_cancellation_meaning(record.cancellation_reason),
            )
        )
    children.append(render_xml_cdata_leaf("outcome_brief", record.outcome_brief))
    if record.based_on_activity_id:
        children.append(
            render_xml_text_leaf(
                "based_on_activity_id",
                record.based_on_activity_id,
            )
        )
        if record.based_on_relationship:
            children.append(
                render_xml_text_leaf(
                    "based_on_relationship",
                    record.based_on_relationship,
                )
            )
        based_on_meaning = task_based_on_meaning(record.based_on_relationship)
        if based_on_meaning:
            children.append(
                render_xml_cdata_leaf("based_on_meaning", based_on_meaning)
            )
    if record.recovery_of_activity_id:
        children.append(
            render_xml_text_leaf(
                "recovery_of_activity_id",
                record.recovery_of_activity_id,
            )
        )
    if record.recovered_by_activity_id:
        children.append(
            render_xml_text_leaf(
                "recovered_by_activity_id",
                record.recovered_by_activity_id,
            )
        )
    if record.recovery_outcome is not None:
        children.append(_render_recovery_outcome(record.recovery_outcome))
    return render_xml_block("task_memory", children)


def render_task_recovery(
    *,
    source_activity_id: str,
    recovered_by_activity_id: str | None,
    outcome: TaskRecoveryOutcome,
) -> str:
    """渲染一项失败任务后来如何收口的独立恢复事实。

    Args:
        source_activity_id: 最初失败的 Cognitive Activity 编号。
        recovered_by_activity_id: 后来真实开始的 Activity 编号。
        outcome: 已经过本地提交边界验证的结构化结果。

    Returns:
        可以在原任务短记忆之后单独追加的恢复 XML。
    """

    children = [
        render_xml_text_leaf("source_activity_id", source_activity_id),
        _render_recovery_outcome(outcome),
    ]
    if recovered_by_activity_id:
        children.append(
            render_xml_text_leaf(
                "recovered_by_activity_id",
                recovered_by_activity_id,
            )
        )
    return render_xml_block("task_recovery", children)


def _render_recovery_outcome(outcome: TaskRecoveryOutcome) -> str:
    """把结构化恢复结果渲染成自包含的 XML 子块。

    Args:
        outcome: 已完成组合校验的恢复结果。

    Returns:
        同时保留机器事实和自然语言含义的 XML 块。
    """

    children = [
        render_xml_text_leaf("self_reflection_id", outcome.self_reflection_id),
        render_xml_text_leaf("kind", outcome.kind),
        render_xml_cdata_leaf("safe_summary", outcome.safe_summary),
    ]
    if outcome.tool_name:
        children.append(render_xml_text_leaf("tool_name", outcome.tool_name))
    if outcome.tool_status:
        children.append(render_xml_text_leaf("tool_status", outcome.tool_status))
    if outcome.reason_code:
        children.append(render_xml_text_leaf("reason_code", outcome.reason_code))
    return render_xml_block("recovery_outcome", children)
