"""判断哪些当前 Activity 可以由 Foreground 精确取消。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast


CancellableActivitySource = Literal["cognitive", "background_agent"]
_CANCELLABLE_STATES = frozenset({"running", "waiting_user"})


@dataclass(frozen=True, slots=True)
class CancellableActivityContext:
    """保存本轮允许取消的一项不可变 Activity 现场。

    Args:
        activity_id: 当前仍可取消的 Activity 编号。
        source: Activity 当前由 Cognitive 或后台工作执行。
        interaction_session_id: Activity 所属的产品交互会话编号。
        state: 冻结时的 Activity 状态。
        phase: 冻结时的 Activity 阶段。
        goal: 当前正在执行、取消后将停止继续推进的目标。
    """

    activity_id: str
    source: CancellableActivitySource
    interaction_session_id: str
    state: str
    phase: str
    goal: str


def select_cancellable_activity_contexts(
    value: object,
    *,
    interaction_session_id: str,
) -> tuple[CancellableActivityContext, ...]:
    """从实时 Activity 快照中筛出当前会话可取消的候选项。

    Args:
        value: `foreground_context.active_activities` 的未知载荷。
        interaction_session_id: 当前 Foreground 交互会话编号。

    Returns:
        按快照顺序排列且编号去重的不可变取消现场。
    """

    normalized_session_id = _clean_text(interaction_session_id)
    if not normalized_session_id or not isinstance(value, list):
        return ()

    contexts: list[CancellableActivityContext] = []
    seen_activity_ids: set[str] = set()
    for item in value:
        context = cancellable_activity_context(
            item,
            interaction_session_id=normalized_session_id,
        )
        if context is None or context.activity_id in seen_activity_ids:
            continue
        seen_activity_ids.add(context.activity_id)
        contexts.append(context)
    return tuple(contexts)


def cancellable_activity_context(
    value: object,
    *,
    interaction_session_id: str,
) -> CancellableActivityContext | None:
    """把单条 Activity 快照收窄为当前会话可取消的现场。

    Args:
        value: 一条尚未信任的 Activity 快照。
        interaction_session_id: 当前 Foreground 交互会话编号。

    Returns:
        同会话且权威状态仍允许取消时返回现场，否则返回空。
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
        or state not in _CANCELLABLE_STATES
    ):
        return None
    return CancellableActivityContext(
        activity_id=activity_id,
        source=cast(CancellableActivitySource, source),
        interaction_session_id=activity_session_id,
        state=state,
        phase=phase,
        goal=_clean_text(value.get("goal")),
    )


def _clean_text(value: object) -> str:
    """把未知值收窄为去除首尾空白的文字。

    Args:
        value: 尚未确认类型的输入值。

    Returns:
        字符串输入清理后的内容；其它类型返回空字符串。
    """

    return value.strip() if isinstance(value, str) else ""
