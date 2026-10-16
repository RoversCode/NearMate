"""把助手子进程来源事件转换为 Activity Registry 可接受的标准变化。

本模块是 Activity 领域的输入边界。它只负责识别后台工作和认知活动的公开生命周期事实、
校验 Activity 编号与执行阶段，并清洗允许进入任务中心的字段。它不写入 Registry、不广播
快照，也不处理进度 Notice。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError

from companion.activities.runtime.identity import canonical_activity_uuid
from companion.activities.runtime.failure_reporting import parse_activity_failure
from companion.activities.runtime.models import (
    ACTIVITY_STAGE_CODES,
    ActivityChangeKind,
    ActivityCompletionResult,
    ActivityFailure,
    ActivitySourceChange,
    ActivityStageCode,
)
from companion.activities.runtime.public_text import (
    normalize_activity_completion_content,
    sanitize_activity_goal,
    sanitize_activity_result,
)
from companion.activities.work_plan import parse_agent_work_plan
from companion.foreground_tools.cognitive.result_protocol import CognitiveFailureResult


# 只有这些来源事件会改变 Activity 生命周期；进度事件只进入 Notice 链路，不能写入 Registry。
_COGNITIVE_LIFECYCLE_CHANGES: dict[str, ActivityChangeKind] = {
    "cognitive_task.started": "started",
    "cognitive_task.completed": "completed",
    "cognitive_task.failed": "failed",
    "cognitive_task.cancelled": "cancelled",
}

# 已知认知工具使用稳定中文类型；新工具仍可投影为通用认知任务，不能因展示文案缺失而丢事件。
_COGNITIVE_TYPE_LABELS: dict[str, str] = {
    "lookup_current_fact": "事实查询",
    "analyze_deeply": "深度分析",
}


def adapt_activity_source_event(
    event_type: str,
    payload: dict[str, object],
    runtime_epoch: int,
) -> ActivitySourceChange | None:
    """把一条助手来源事件转换为 Activity Registry 的标准变化。

    只有带合法 Activity 编号且属于两类公开生命周期事件的输入才会被转换。进度、内部控制
    事件和旧格式编号都返回空，由调用方吸收，避免它们污染任务中心的权威状态。

    Args:
        event_type: 子进程来源事件名称。
        payload: 来源事件的 JSON 友好载荷。
        runtime_epoch: 事件所属助手运行时世代。

    Returns:
        可进入运行时 Registry 的标准变化；非公开事件返回空。

    Raises:
        ValueError: 阶段代码或公开故障报告结构非法。
    """

    # Registry 用 Activity 编号保证生命周期连续；无效编号无法安全归属，必须在分流前丢弃。
    activity_id = canonical_activity_uuid(payload.get("activity_id"))
    if activity_id is None:
        return None
    if event_type.startswith("cognitive_task."):
        return _adapt_cognitive(event_type, payload, activity_id, runtime_epoch)
    if event_type.startswith("background_agent."):
        return _adapt_background_agent(event_type, payload, activity_id, runtime_epoch)
    return None


def validate_activity_terminal_contract(
    event_type: str,
    payload: dict[str, object],
    *,
    require_canonical_activity_id: bool = False,
) -> None:
    """在 Progress 与 Runtime Memory 结算前校验来源终态合同。

    Args:
        event_type: Cognitive 或 Background Work 终态事件名。
        payload: 尚未写入任何主进程账本的来源载荷。
        require_canonical_activity_id: 生产运行时是否要求 Activity 编号为标准 UUID。

    Raises:
        ValueError: 终态身份、成功结果或失败报告不符合公开合同。
    """

    if event_type not in {
        "cognitive_task.completed",
        "cognitive_task.failed",
        "cognitive_task.cancelled",
    }:
        return
    if require_canonical_activity_id and canonical_activity_uuid(
        payload.get("activity_id")
    ) is None:
        raise ValueError("Activity 终态缺少合法 Activity 编号")
    if event_type == "cognitive_task.completed":
        display_content = sanitize_activity_result(payload.get("result_text"))
        delivery_brief = sanitize_activity_result(payload.get("summary"))
        if not display_content or not delivery_brief:
            raise ValueError("Cognitive 成功终态缺少完整结果或摘要")
        return
    # 取消表示执行器已经按请求退出，不是执行故障，因此不会携带故障报告。
    if event_type == "cognitive_task.failed":
        _parse_cognitive_failure(payload)


def _parse_cognitive_failure(
    payload: dict[str, object],
) -> tuple[ActivityFailure, str | None]:
    """校验 Cognitive 失败报告，并提取允许公开的自主失败原因。

    Args:
        payload: Cognitive 失败终态的来源载荷。

    Returns:
        结构化故障报告，以及仅供自主失败使用的安全具体原因。

    Raises:
        ValueError: 故障报告非法，或自主失败缺少可安全公开的具体原因。
    """

    failure = parse_activity_failure(payload.get("failure"))
    if failure.code != "cognitive_task_failed":
        return failure, None

    # 这个原因会进入任务记忆和 Console，必须在任何主进程账本写入前复用
    # Cognitive 原有的凭据、堆栈和路径门禁。
    try:
        failure_reason = CognitiveFailureResult.model_validate(
            {"failure_reason": payload.get("failure_reason")},
            strict=True,
        ).failure_reason
    except ValidationError:
        raise ValueError("Cognitive 自主失败缺少安全的具体原因") from None
    return failure, failure_reason


def _adapt_background_agent(
    event_type: str,
    payload: dict[str, object],
    activity_id: str,
    runtime_epoch: int,
) -> ActivitySourceChange | None:
    """转换后台智能体的生命周期和执行阶段事件。"""

    if event_type == "background_agent.work_plan_changed":
        return ActivitySourceChange(
            change="work_plan_changed",
            activity_id=activity_id,
            kind="background_agent",
            type_label="后台工作",
            control_source="background_agent",
            runtime_epoch=runtime_epoch,
            goal="",
            work_plan=parse_agent_work_plan(payload.get("work_plan")),
        )
    if event_type == "background_agent.turn_output":
        turn_index = payload.get("turn_index")
        content_text = payload.get("content_text")
        created_at = _normalize_turn_timestamp(payload.get("created_at"))
        if (
            isinstance(turn_index, bool)
            or not isinstance(turn_index, int)
            or turn_index <= 0
            or not isinstance(content_text, str)
            or not content_text.strip()
            or not created_at
        ):
            return None
        return ActivitySourceChange(
            change="turn_output",
            activity_id=activity_id,
            kind="background_agent",
            type_label="后台工作",
            control_source="background_agent",
            runtime_epoch=runtime_epoch,
            goal="",
            turn_index=turn_index,
            content_text=content_text.strip(),
            created_at=created_at,
        )

    lifecycle = {
        "background_agent.started": "started",
        "background_agent.completed": "completed",
        "background_agent.failed": "failed",
        "background_agent.cancelled": "cancelled",
    }.get(event_type)
    waiting_change = {
        "background_agent.interaction_requested": "waiting_user_entered",
        "background_agent.interaction_resolved": "waiting_user_resumed",
    }.get(event_type)
    if waiting_change is not None:
        return ActivitySourceChange(
            change=waiting_change,  # type: ignore[arg-type]
            activity_id=activity_id,
            kind="background_agent",
            type_label="后台工作",
            control_source="background_agent",
            runtime_epoch=runtime_epoch,
            goal="",
            interaction_request_id=_limited_text(payload.get("request_id"), 200),
        )
    if event_type == "background_agent.stage_changed":
        return ActivitySourceChange(
            change="stage_changed",
            activity_id=activity_id,
            kind="background_agent",
            type_label="后台工作",
            control_source="background_agent",
            runtime_epoch=runtime_epoch,
            goal="",
            stage=_require_stage(payload.get("stage")),
        )
    if lifecycle is None:
        return None
    completion_result = None
    if lifecycle == "completed":
        display_content = normalize_activity_completion_content(
            payload.get("display_content")
        )
        # 这里只做 Activity 公共结果的总长度护栏，不能提前截成 Notice 恰好允许的
        # 2000 字符。Notice 投影器必须看见真实超限事实，才能整段省略而不是播报残句。
        delivery_brief = sanitize_activity_result(payload.get("delivery_brief"))
        if not display_content or not delivery_brief:
            raise ValueError("后台工作成功终态缺少完整成果或交付摘要")
        completion_result = ActivityCompletionResult(
            display_content=display_content,
            delivery_brief=delivery_brief,
            output_paths=_clean_output_paths(payload.get("output_paths")),
            # 结果卡登记事实只认权威布尔 True；字段缺失或形状异常时按未登记处理，
            # 退化为旧版行为：完成提醒只播报结果，不指引聊天窗口结果卡。
            deliverable=payload.get("deliverable") is True,
        )
    return ActivitySourceChange(
        change=lifecycle,  # type: ignore[arg-type]
        activity_id=activity_id,
        kind="background_agent",
        type_label="后台工作",
        control_source="background_agent",
        runtime_epoch=runtime_epoch,
        goal=sanitize_activity_goal(payload.get("goal")),
        completion_result=completion_result,
        failure=(parse_activity_failure(payload.get("failure")) if lifecycle == "failed" else None),
    )


def _adapt_cognitive(
    event_type: str,
    payload: dict[str, object],
    activity_id: str,
    runtime_epoch: int,
) -> ActivitySourceChange | None:
    """转换认知活动的开始、阶段前进和终态事件。

    Args:
        event_type: 已确认属于认知活动事件族的来源事件名称。
        payload: 助手子进程提供的 JSON 友好载荷。
        activity_id: 已通过标准 UUID 校验的 Activity 编号。
        runtime_epoch: 产生该事件的助手运行时世代。

    Returns:
        可写入 Registry 的标准变化；未声明为公开生命周期的事件返回空。

    Raises:
        ValueError: 阶段代码、失败终态的故障报告或具体原因不符合公开契约。
    """

    kind = _limited_text(payload.get("kind"), 64)
    type_label = _COGNITIVE_TYPE_LABELS.get(kind, "认知任务")
    if event_type == "cognitive_task.stage_changed":
        # 阶段事件只推进执行阶段；类型仍保留，方便任务中心显示这项活动在做什么。
        return ActivitySourceChange(
            change="stage_changed",
            activity_id=activity_id,
            kind="cognitive",
            type_label=type_label,
            control_source="cognitive",
            runtime_epoch=runtime_epoch,
            goal="",
            stage=_require_stage(payload.get("stage")),
        )
    change = _COGNITIVE_LIFECYCLE_CHANGES.get(event_type)
    if change is None:
        return None
    completion_result = None
    failure = None
    failure_reason = None
    if change == "completed":
        display_content = sanitize_activity_result(payload.get("result_text"))
        delivery_brief = sanitize_activity_result(payload.get("summary"))
        if not display_content or not delivery_brief:
            raise ValueError("Cognitive 成功终态缺少完整结果或摘要")
        completion_result = ActivityCompletionResult(
            display_content=display_content,
            delivery_brief=delivery_brief,
            output_paths=(),
            deliverable=False,
        )
    elif change == "failed":
        failure, failure_reason = _parse_cognitive_failure(payload)
    return ActivitySourceChange(
        change=change,
        activity_id=activity_id,
        kind="cognitive",
        type_label=type_label,
        control_source="cognitive",
        runtime_epoch=runtime_epoch,
        goal=sanitize_activity_goal(
            payload.get("brief") or payload.get("source_user_text"),
        ),
        # Cognitive 的 summary 专门供 Foreground 简短承接使用；完整 result
        # 仍保留在 Activity 详情和需要完整内容的内部路径。
        completion_result=completion_result,
        failure=failure,
        failure_reason=failure_reason,
    )


def _require_stage(value: object) -> ActivityStageCode:
    """校验来源事件携带的稳定执行阶段代码。

    Activity Registry 依赖固定阶段顺序来拒绝倒退事件，因此这里不能猜测、补默认值或接受
    执行器私有阶段名；不合法输入必须让调用方记录并隔离。

    Args:
        value: 来源事件中的 stage 字段。

    Returns:
        合法的五个稳定阶段代码之一。

    Raises:
        ValueError: 阶段代码缺失或不在稳定阶段计划内。
    """

    if not isinstance(value, str) or value not in ACTIVITY_STAGE_CODES:
        raise ValueError(f"非法 Activity 阶段代码：{value!r}")
    return cast(ActivityStageCode, value)


def _limited_text(value: object, limit: int) -> str:
    """清洗并截断允许进入公共 Activity 的短文本。

    Args:
        value: 来源事件中可能不是字符串的原始字段。
        limit: 保留的最大字符数，包含截断标记。

    Returns:
        去除首尾空白后的短文本；非字符串返回空字符串，超长文本以省略号收尾。
    """

    # 任务中心只接受短公共文本；绝不能把字典、异常对象或大段执行结果隐式转成字符串写入状态。
    text = value.strip() if isinstance(value, str) else ""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _clean_output_paths(value: object) -> tuple[str, ...]:
    """清洗 Backend 扫描后跨进程传回的交付文件路径。

    Args:
        value: 来源成功终态中的文件路径列表。

    Returns:
        最多一百个非空路径组成的不可变元组。
    """

    if not isinstance(value, list):
        return ()
    return tuple(
        path.strip()
        for path in value[:100]
        if isinstance(path, str) and path.strip()
    )


def _normalize_turn_timestamp(value: object) -> str:
    """把 Runner 的毫秒时间或 ISO 时间转换成公开 ISO-8601 文本。"""

    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return ""
