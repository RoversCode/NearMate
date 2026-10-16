"""判断哪些当前 Activity 仍允许用户纠正目标。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CorrectableActivitySource = Literal["cognitive", "background_agent"]
_CORRECTABLE_STATES = frozenset({"running", "waiting_user", "attention"})
COGNITIVE_TOOL_TYPES = (
    "lookup_current_fact",
    "analyze_deeply",
)
COGNITIVE_TOOL_TYPE_LABELS = {
    "lookup_current_fact": "当前事实核查",
    "analyze_deeply": "深度分析",
}
COGNITIVE_ACTIVITY_CORRECTION_CONTEXT_TEXT = (
    "这是当前尚未结束的认知任务。只有用户明确指出原目标错误、"
    "给出可执行的替代目标并要求停止、替换或改做时，才使用 "
    "correct_cognitive_activity。correction_goal 必须写成纠正后要完整执行的新综合目标。"
)
BACKGROUND_ACTIVITY_CORRECTION_CONTEXT_TEXT = (
    "这是当前尚未结束的后台工作。只有用户明确指出原目标错误、"
    "给出可执行的替代目标并要求停止、替换或改做时，才使用 "
    "correct_background_work。goal 必须写成纠正后要完整执行的唯一后台工作目标。"
)


@dataclass(frozen=True, slots=True)
class CorrectableActivityContext:
    """保存本轮允许纠正的一项不可变现场事实。

    Args:
        activity_id: 当前仍可中断的 Activity 编号。
        source: Activity 当前由 Cognitive 或后台工作执行。
        interaction_session_id: Activity 所属的产品交互会话编号。
        state: 冻结时的 Activity 状态。
        phase: 冻结时的 Activity 阶段。
        goal: 当前正在执行、将被新目标替换的旧目标。
        cognitive_tool_types: Cognitive Activity 当前计划已经包含的认知工具类型。
    """

    activity_id: str
    source: CorrectableActivitySource
    interaction_session_id: str
    state: str
    phase: str
    goal: str
    cognitive_tool_types: tuple[str, ...] = ()


def select_correctable_activity_contexts(
    value: object,
    *,
    interaction_session_id: str,
) -> tuple[CorrectableActivityContext, ...]:
    """从实时活动快照中筛出属于当前交互会话的纠正现场。

    Args:
        value: `foreground_context.active_activities` 的未知载荷。
        interaction_session_id: 当前 Foreground 交互会话编号。

    Returns:
        按快照顺序排列且编号去重的不可变纠正现场。
    """

    normalized_session_id = _clean_text(interaction_session_id)
    if not normalized_session_id or not isinstance(value, list):
        return ()
    contexts: list[CorrectableActivityContext] = []
    seen_activity_ids: set[str] = set()
    for item in value:
        context = correctable_activity_context(
            item,
            interaction_session_id=normalized_session_id,
        )
        if context is None or context.activity_id in seen_activity_ids:
            continue
        seen_activity_ids.add(context.activity_id)
        contexts.append(context)
    return tuple(contexts)


def correctable_activity_context(
    value: object,
    *,
    interaction_session_id: str,
) -> CorrectableActivityContext | None:
    """把单条活动快照收窄为当前会话可纠正的不可变现场。

    Args:
        value: 一条尚未信任的 Activity 快照。
        interaction_session_id: 当前 Foreground 交互会话编号。

    Returns:
        同会话、状态仍可中断且尚未进入最终交付时返回现场；否则返回空。
    """

    if not isinstance(value, dict):
        return None
    activity_id = _clean_text(value.get("activity_id"))
    source = _clean_text(value.get("source"))
    activity_session_id = _clean_text(value.get("interaction_session_id"))
    expected_session_id = _clean_text(interaction_session_id)
    state = _clean_text(value.get("state"))
    phase = _clean_text(value.get("phase")) or "reasoning"
    if (
        not activity_id
        or not expected_session_id
        or activity_session_id != expected_session_id
        or source not in {"cognitive", "background_agent"}
        or state not in _CORRECTABLE_STATES
        or phase == "finalizing"
    ):
        return None
    cognitive_tool_types = _clean_tool_types(value.get("cognitive_tool_types"))
    if source == "cognitive" and not cognitive_tool_types:
        activity_kind = _clean_text(value.get("activity_kind"))
        if activity_kind in COGNITIVE_TOOL_TYPES:
            cognitive_tool_types = (activity_kind,)
    return CorrectableActivityContext(
        activity_id=activity_id,
        source=source,  # type: ignore[arg-type]
        interaction_session_id=activity_session_id,
        state=state,
        phase=phase,
        goal=_clean_text(value.get("goal")),
        cognitive_tool_types=cognitive_tool_types,
    )


def _clean_text(value: object) -> str:
    """把未知值收窄为去除首尾空白的文字。"""

    return value.strip() if isinstance(value, str) else ""


def _clean_tool_types(value: object) -> tuple[str, ...]:
    """只保留已知 Cognitive 类型并按首次出现顺序去重。"""

    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if normalized not in COGNITIVE_TOOL_TYPES or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)
