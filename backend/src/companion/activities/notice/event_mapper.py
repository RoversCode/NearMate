"""把活动执行器的来源事件翻译为 Notice 协调器可理解的领域事实。

本模块是无状态的输入适配器：只校验事件身份、提取安全的公开字段，并输出
生命周期事实、弱进度提醒意图或弱提醒失效事实。它不决定是否真正形成提醒，
也不管理候选替换、批次预留、请求提交或取消；这些决策统一由
``NoticeDeliveryCoordinator`` 在后续步骤处理。
"""

from __future__ import annotations

from companion.activities.notice.models import (
    ActivityInteractionNoticeInvalidation,
    ActivityLifecycleEvent,
    ActivityProgressNoticeIntent,
    ActivityProgressNoticeInvalidation,
    ActivityTerminalState,
    MappedActivityEvent,
)
from companion.background_agent.question_contract import normalize_agent_questions
from companion.activities.runtime.failure_reporting import (
    failure_notice_text,
    parse_activity_failure,
)


_COGNITIVE_TERMINAL_STATES: dict[str, ActivityTerminalState] = {
    "cognitive_task.completed": "completed",
    "cognitive_task.failed": "failed",
    "cognitive_task.rejected": "rejected",
    "cognitive_task.cancelled": "cancelled",
}


def map_activity_event(
    event_type: str,
    payload: dict[str, object],
) -> MappedActivityEvent | None:
    """把来源事件翻译成 Notice 协调器可消费的标准事实。

    生产链路中，``AssistantRequestContextPreparer.handle_activity_event()`` 调用
    此函数，再把返回值交给 ``NoticeDeliveryCoordinator.apply_event()``。因此这里
    只回答“这个来源事件意味着什么”，不回答“现在是否要向用户发提醒”。

    Args:
        event_type: 来源事件名称。
        payload: 来源事件载荷。

    Returns:
        标准活动事件；来源未知或必要标识不完整时返回空。
    """

    if event_type.startswith("cognitive_task."):
        return _map_cognitive_event(event_type, payload)
    if event_type.startswith("background_agent."):
        return _map_background_agent_event(event_type, payload)
    return None


def _map_background_agent_event(
    event_type: str,
    payload: dict[str, object],
) -> MappedActivityEvent | None:
    """把后台智能体事件映射为统一的生命周期提醒事实。"""

    identity = _event_identity(payload)
    if identity is None:
        return None
    activity_id, interaction_session_id = identity
    created_at = _float_or_default(payload.get("created_at") or payload.get("updated_at"))
    common = {
        "activity_id": activity_id,
        "interaction_session_id": interaction_session_id,
        "source": "background_agent",
        "category": "后台工作",
        "goal": _clean_text(payload.get("goal")),
        "activity_kind": "background_work",
        "created_at": created_at,
    }
    if event_type == "background_agent.progress":
        return _progress_intent(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id,
            source="background_agent",
            sequence=payload.get("notice_seq"),
            created_at=created_at,
        )
    if event_type == "background_agent.progress_invalidated":
        return ActivityProgressNoticeInvalidation(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id,
            source="background_agent",
            created_at=created_at,
        )
    if event_type == "background_agent.interaction_requested":
        kind = _clean_text(payload.get("kind"))
        request_id = _clean_text(payload.get("request_id"))
        if not request_id:
            return None
        if kind == "permission":
            action_summary = _clean_text(payload.get("action_summary"))
            if not action_summary or payload.get("grant_scope") != "activity":
                return None
            return ActivityLifecycleEvent(
                lifecycle="waiting_user",
                intent="permission_request",
                interaction_request_id=request_id,
                action_summary=action_summary,
                grant_scope="activity",
                **common,
            )
        if kind == "question":
            questions = normalize_agent_questions(payload.get("questions"))
            if questions is None:
                return None
            return ActivityLifecycleEvent(
                lifecycle="waiting_user",
                intent="information_request",
                interaction_request_id=request_id,
                questions=tuple(dict(question) for question in questions),
                **common,
            )
        return None
    if event_type == "background_agent.interaction_resolved":
        request_id = _clean_text(payload.get("request_id"))
        if not request_id:
            return None
        return ActivityInteractionNoticeInvalidation(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id,
            source="background_agent",
            interaction_request_id=request_id,
            created_at=created_at,
        )
    if event_type == "background_agent.started":
        return ActivityLifecycleEvent(lifecycle="started", **common)
    terminal_state_by_event: dict[str, ActivityTerminalState] = {
        "background_agent.completed": "completed",
        "background_agent.failed": "failed",
        "background_agent.cancelled": "cancelled",
    }
    terminal_state = terminal_state_by_event.get(event_type)
    if terminal_state is None:
        return None
    return ActivityLifecycleEvent(
        lifecycle="terminal",
        terminal_state=terminal_state,
        intent=("work_result" if terminal_state == "completed" else "terminal_status"),
        status_context=(
            _failure_notice_text(payload) if terminal_state == "failed" else ""
        ),
        suppress_notice=(
            payload.get("suppress_notice") is True
            or (
                terminal_state == "cancelled"
                and payload.get("cancellation_reason") == "user_correction"
            )
        ),
        **common,
    )


def _map_cognitive_event(
    event_type: str,
    payload: dict[str, object],
) -> MappedActivityEvent | None:
    """翻译认知活动事件中与 Notice 有关的那一部分事实。

    ``progress`` 只产生可被替换的弱进度意图，``progress_invalidated`` 撤销尚未
    交付的该类意图；``started`` 和各终态产生强生命周期事实。准备完成、执行
    阶段变化等事件虽然会更新活动快照，却不改变 Notice 候选，因此在这里忽略。

    Args:
        event_type: 认知任务来源事件名称。
        payload: 来源事件的原始载荷。

    Returns:
        标准活动事件；事件类型或身份无效时返回空。
    """

    # 没有稳定身份就无法与已有提醒、交互会话关联，宁可忽略也不能猜测归属。
    identity = _event_identity(payload)
    if identity is None:
        return None
    activity_id, interaction_session_id = identity
    created_at = _float_or_default(payload.get("updated_at"))

    if event_type == "cognitive_task.progress":
        return _progress_intent(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id,
            source="cognitive",
            sequence=payload.get("notice_seq"),
            created_at=created_at,
        )
    if event_type == "cognitive_task.progress_invalidated":
        return ActivityProgressNoticeInvalidation(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id,
            source="cognitive",
            created_at=created_at,
        )

    category = _clean_text(payload.get("category")) or "认知任务"
    common = {
        "activity_id": activity_id,
        "interaction_session_id": interaction_session_id,
        "source": "cognitive",
        "category": category,
        "goal": _clean_text(payload.get("brief"))
        or _clean_text(payload.get("source_user_text")),
        "activity_kind": _cognitive_activity_kind(payload),
        "created_at": created_at,
    }
    if event_type == "cognitive_task.started":
        return ActivityLifecycleEvent(lifecycle="started", **common)

    # 终态
    terminal_state = _COGNITIVE_TERMINAL_STATES.get(event_type)
    if terminal_state is None:
        return None
    return ActivityLifecycleEvent(
        lifecycle="terminal",
        terminal_state=terminal_state,
        intent=(
            "cognitive_result"
            if terminal_state == "completed"
            else "terminal_status"
        ),
        result_context=(
            _clean_text(payload.get("result_text"))
            if terminal_state == "completed"
            else ""
        ),
        status_context=_cognitive_terminal_status_context(payload, terminal_state),
        suppress_notice=(
            payload.get("suppress_notice") is True
            or (
                terminal_state == "cancelled"
                and payload.get("cancellation_reason") == "user_correction"
            )
        ),
        **common,
    )


def _cognitive_activity_kind(payload: dict[str, object]) -> str:
    """从认知事件中恢复用于 Notice 表达的稳定任务类别。

    混合认知任务可能先读图、再事实核查或深度分析。这里沿用进度策略的优先级，
    保证终态 Notice 不会因为主目标恰好是读图而失去真正的回答类型。

    Args:
        payload: Cognitive Executor 发布的生命周期事件载荷。

    Returns:
        analyze_deeply、lookup_current_fact 或空字符串。
    """

    raw_tool_types = payload.get("cognitive_tool_types")
    tool_types = {
        item.strip()
        for item in raw_tool_types
        if isinstance(item, str) and item.strip()
    } if isinstance(raw_tool_types, list) else set()
    if "analyze_deeply" in tool_types:
        return "analyze_deeply"
    if "lookup_current_fact" in tool_types:
        return "lookup_current_fact"
    kind = _clean_text(payload.get("kind"))
    return kind if kind in {
        "analyze_deeply",
        "lookup_current_fact",
    } else ""


def _failure_notice_text(payload: dict[str, object]) -> str:
    """从结构化故障报告取得可供前台回复转述的固定失败文案。

    来源载荷不能直接当作用户文案使用：其中可能含内部异常或不可信文本。只有
    ``ActivityFailure`` 的严格解析成功后，才按稳定错误码查出预先定义的公开文案。

    Args:
        payload: 携带 failure 对象的终态事件载荷。

    Returns:
        原因摘要与行动建议的联排文本；缺失或结构不完整时返回空串。
    """

    try:
        failure = parse_activity_failure(payload.get("failure"))
    except ValueError:
        return ""
    return failure_notice_text(failure.code)


def _progress_intent(
    *,
    activity_id: str,
    interaction_session_id: str,
    source: str,
    sequence: object,
    created_at: float,
) -> ActivityProgressNoticeIntent | None:
    """把带有效序号的进度事件转换为不含正文的弱提醒意图。

    ``sequence`` 是执行器在实际产生进度时冻结的版本边界，供协调器判断新旧候选；
    它不是推理正文的快照。本意图故意不携带 reasoning/content 文本，后续前台请求
    只根据活动身份和当前状态组织提醒，避免把内部推理跨边界传递。

    Args:
        activity_id: 产生进度的活动编号。
        interaction_session_id: 活动所属交互会话编号。
        source: 活动来源类型。
        sequence: Activity Executor 分配的冻结进度序号。
        created_at: 进度意图产生时间。

    Returns:
        可进入提醒协调器的进度意图；序号无效时返回空。
    """

    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        return None
    return ActivityProgressNoticeIntent(
        activity_id=activity_id,
        interaction_session_id=interaction_session_id,
        source=source,
        sequence=sequence,
        created_at=created_at,
    )


def _event_identity(payload: dict[str, object]) -> tuple[str, str] | None:
    """读取把来源事件归属到同一活动和交互会话所需的身份字段。

    Args:
        payload: 来源事件载荷。

    Returns:
        活动编号与会话编号；任一编号缺失时返回空。
    """

    activity_id = _clean_text(payload.get("activity_id"))
    interaction_session_id = _clean_text(payload.get("interaction_session_id"))
    if not activity_id or not interaction_session_id:
        return None
    return activity_id, interaction_session_id


def _cognitive_terminal_status_context(
    payload: dict[str, object],
    terminal_state: ActivityTerminalState,
) -> str:
    """选择认知非成功终态中允许交给前台回复的原因。

    完成事件可以提供已经产出的结果；失败只能使用结构化故障报告对应的固定文案；
    取消不附带解释。这样前台不会把执行器内部诊断文本误当作用户可见内容。

    Args:
        payload: 认知终态事件载荷。
        terminal_state: 标准化后的活动终态。

    Returns:
        失败或拒绝时的安全说明；成功与取消返回空串。
    """

    if terminal_state == "failed":
        return _failure_notice_text(payload)
    if terminal_state in {"completed", "cancelled"}:
        return ""
    return _clean_text(payload.get("reason"))


def _clean_text(value: object) -> str:
    """只接受来源载荷中的非空字符串字段。

    Args:
        value: 尚未校验的载荷字段。

    Returns:
        去除首尾空白后的字符串，非字符串返回空串。
    """

    return value.strip() if isinstance(value, str) else ""


def _float_or_default(value: object) -> float:
    """读取来源事件中的数值时间，隔离不可靠的载荷类型。

    Args:
        value: 尚未校验的时间字段。

    Returns:
        可用的浮点时间；无效值返回零。
    """

    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
