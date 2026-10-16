"""校验两个 Activity 纠正工具，并调用共享纠正服务。"""

from __future__ import annotations

from threading import Event
from typing import cast

from companion.environment.models import FrozenEnvironmentSnapshot
from companion.materials.foreground_images import FrozenVisualInput
from companion.foreground_tools.activity_correction.policy import (
    COGNITIVE_TOOL_TYPES,
    CorrectableActivityContext,
)
from companion.foreground_tools.activity_correction.contracts import (
    CorrectionReservation,
)
from companion.foreground_tools.activity_correction.service import ActivityCorrectionService
from companion.foreground_tools.background_agent.materials import (
    materials_from_foreground_context,
)
from companion.foreground_tools.cognitive.contracts import (
    CognitiveGoal,
    CognitiveGoalKind,
)
from companion.foreground_tools.cognitive.planner import CognitivePlanner
from companion.foreground_tools.cognitive.tools import (
    _string_list_from_arguments,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.foreground_tools.orchestration.transactions import (
    BackgroundActivityReservation,
)
from companion.text.http_urls import normalize_http_urls


class ActivityCorrectionToolExecutor:
    """把纠正工具参数转换成新的 Cognitive 或 Background Activity。"""

    def __init__(
        self,
        *,
        correction_service: ActivityCorrectionService,
        planner: CognitivePlanner | None = None,
    ) -> None:
        """初始化纠正工具执行器。

        Args:
            correction_service: 负责取消旧 Activity 并准备新 Activity 的服务。
            planner: 可选的 Cognitive 计划生成器。
        """

        self._correction_service = correction_service
        self._planner = planner or CognitivePlanner()

    def take_reservation(self, activity_id: str) -> BackgroundActivityReservation | None:
        """把已准备的新 Activity 纠正事务移交给 Foreground 提交链路。

        Args:
            activity_id: 纠正工具刚分配的新 Activity 编号。

        Returns:
            尚未移交的提交事务；编号无效、已移交或已回滚时返回空。
        """

        return self._correction_service.take_reservation(activity_id)

    def rollback_prepared(self, activity_id: str) -> bool:
        """释放尚未进入 Foreground 提交链路的纠正预留。

        Args:
            activity_id: 纠正工具刚分配的新 Activity 编号。

        Returns:
            本次确实释放预留时返回真。
        """

        return self._correction_service.rollback_prepared(activity_id)

    def execute_cognitive_correction(
        self,
        *,
        tool_call: ForegroundToolCall,
        interaction_session_id: str,
        current_user_text: str,
        foreground_context: dict[str, object],
        correctable_activities: tuple[CorrectableActivityContext, ...],
        frozen_visual_input: FrozenVisualInput,
        cancellation_event: Event | None,
    ) -> dict[str, object]:
        """执行 Cognitive 纠正，并为新任务分配新的 Activity 编号。

        Args:
            tool_call: Foreground 模型产生的纠正工具调用。
            interaction_session_id: 当前 Foreground 交互会话编号。
            current_user_text: 用户本轮真实纠正文。
            foreground_context: 当前轮冻结的材料和 Activity 态势。
            correctable_activities: 与 Prompt 和 schema 共用的冻结纠正允许列表。
            frozen_visual_input: 首轮冻结的当前截图、材料图片和读取限制。
            cancellation_event: 当前 generation 的取消信号。

        Returns:
            新 Activity 的接受事实，或参数、材料、任务归属对应的安全拒绝。
        """

        if cancellation_event is not None and cancellation_event.is_set():
            return _rejected("generation_cancelled", "当前回复已经取消，纠正没有提交。")
        arguments = tool_call.arguments
        if set(arguments) - {"activity_id", "correction_goal", "additional_cognitive_types", "source_urls"}:
            return _rejected("invalid_correction_arguments", "认知纠正包含不支持的参数。")
        activity_id = _required_text(arguments.get("activity_id"))
        correction_goal = _required_text(arguments.get("correction_goal"))
        if not activity_id or not correction_goal:
            return _rejected("invalid_correction_arguments", "纠正需要旧 Activity 编号和完整的新综合目标。")

        additional_types, invalid_types = _cognitive_types_from_arguments(
            arguments.get("additional_cognitive_types")
        )
        if invalid_types:
            return _rejected("invalid_additional_cognitive_types", "纠正后的新增认知能力不可用。")

        parent_context, context_rejection = self._resolve_activity_context(
            correctable_activities=correctable_activities,
            interaction_session_id=interaction_session_id,
            activity_id=activity_id,
            expected_source="cognitive",
        )
        if parent_context is None:
            return context_rejection or _rejected(
                "correction_activity_not_found",
                "旧 Activity 已经不可用，无法纠正。",
            )

        # 本轮证据由请求入口冻结，不再让模型挑选路径或选择是否附带截图。
        inherited_evidence = self._correction_service.correction_evidence(
            activity_id=activity_id,
            parent_task_context=parent_context,
        )
        inherited_types = _cognitive_types(inherited_evidence.cognitive_tool_types)
        duplicate_types = set(additional_types) & set(inherited_types)
        if duplicate_types:
            return _rejected(
                "cognitive_type_already_inherited",
                "纠正后的新增认知能力已经包含在原任务中。",
            )
        effective_types = _ordered_cognitive_types(
            (*inherited_types, *additional_types)
        )
        if not effective_types:
            return _rejected(
                "missing_cognitive_type",
                "无法判断纠正后的认知能力，请明确新增的 Cognitive 类型。",
            )

        # 合并本轮冻结证据和旧任务继承证据；去重和上限由计划器统一处理。
        images = (*frozen_visual_input.images, *inherited_evidence.images)
        goal = CognitiveGoal(
            tool_call_id=tool_call.tool_call_id,
            kind=effective_types[0],
            brief=correction_goal,
        )
        plan = self._planner.plan(
            goals=[goal],
            images=images,
            image_materials=(
                *frozen_visual_input.image_materials,
                *inherited_evidence.image_materials,
            ),
            source_urls=normalize_http_urls(
                [
                    *inherited_evidence.source_urls,
                    *_string_list_from_arguments(arguments, "source_urls"),
                ]
            ),
            based_on_activity_id=activity_id,
            # Activity Correction 的新目标是唯一权威输入。父现场只用于服务端复核归属，
            # 不能作为旧目标参考再注入 B 的 Cognitive Prompt。
            parent_task_context=None,
            cognitive_tool_types=effective_types,
            evidence_limitations=frozen_visual_input.limitations,
        )
        reservation = self._correction_service.prepare_cognitive_correction(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id,
            plan=plan,
            source_user_text=current_user_text,
            parent_task_context=parent_context,
        )
        return _accepted_or_rejected(
            reservation,
            activity_id,
            "认知纠正没有启动。",
            goal=correction_goal,
            cognitive_tool_types=list(effective_types),
        )

    def execute_background_correction(
        self,
        *,
        tool_call: ForegroundToolCall,
        interaction_session_id: str,
        foreground_context: dict[str, object],
        correctable_activities: tuple[CorrectableActivityContext, ...],
        environment_snapshot: FrozenEnvironmentSnapshot | None,
        cancellation_event: Event | None,
    ) -> dict[str, object]:
        """执行 Background Work 纠正，并保留同一 AgentSession 的父子关系。

        Args:
            tool_call: Foreground 模型产生的纠正工具调用。
            interaction_session_id: 当前 Foreground 交互会话编号。
            foreground_context: 当前轮冻结的材料和 Activity 态势。
            correctable_activities: 与 Prompt 和 schema 共用的冻结纠正允许列表。
            environment_snapshot: 可选的本轮现场截图。
            cancellation_event: 当前 generation 的取消信号。

        Returns:
            新 Activity 的接受事实，或参数、现场、任务归属对应的安全拒绝。
        """

        if cancellation_event is not None and cancellation_event.is_set():
            return _rejected("generation_cancelled", "当前回复已经取消，纠正没有提交。")
        arguments = tool_call.arguments
        activity_id = _required_text(arguments.get("activity_id"))
        goal = _required_text(arguments.get("goal"))
        deliverable = arguments.get("deliverable")
        if not activity_id or not goal or not isinstance(deliverable, bool):
            return _rejected("invalid_correction_arguments", "后台纠正需要旧 Activity 编号、目标和交付规则。")
        requirements = arguments.get("requirements", "")
        if not isinstance(requirements, str):
            return _rejected("invalid_requirements", "纠正后的成果要求必须是文字。")
        include_scene = arguments.get("include_current_scene", False)
        if not isinstance(include_scene, bool):
            return _rejected("invalid_include_current_scene", "纠正后的现场输入规则无效。")
        if include_scene and (
            environment_snapshot is None or not environment_snapshot.screenshot_bytes
        ):
            return _rejected("current_scene_unavailable", "纠正需要的当前屏幕截图不可用。")
        parent_context, context_rejection = self._resolve_activity_context(
            correctable_activities=correctable_activities,
            interaction_session_id=interaction_session_id,
            activity_id=activity_id,
            expected_source="background_agent",
        )
        if parent_context is None:
            return context_rejection or _rejected(
                "correction_activity_not_found",
                "旧 Activity 已经不可用，无法纠正。",
            )
        reservation = self._correction_service.prepare_background_correction(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id,
            goal=goal,
            deliverable=deliverable,
            requirements=requirements,
            materials=materials_from_foreground_context(foreground_context),
            current_scene_bytes=(
                environment_snapshot.screenshot_bytes
                if include_scene and environment_snapshot is not None
                else None
            ),
            parent_task_context=parent_context,
        )
        return _accepted_or_rejected(
            reservation,
            activity_id,
            "后台纠正没有启动。",
            goal=goal,
            deliverable=deliverable,
        )

    def _resolve_activity_context(
        self,
        *,
        correctable_activities: tuple[CorrectableActivityContext, ...],
        interaction_session_id: str,
        activity_id: str,
        expected_source: str,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        """只从本轮可纠正活跃快照读取旧任务事实。

        Args:
            correctable_activities: Prompt、schema 与执行共用的冻结纠正允许列表。
            interaction_session_id: 当前 Foreground 交互会话编号。
            activity_id: 用户明确要纠正的旧 Activity 编号。
            expected_source: 当前工具允许纠正的执行来源。

        Returns:
            可交给新任务的旧 Activity 事实，以及目标不再活跃时的安全拒绝结果。
        """

        active_context = _activity_context(
            correctable_activities,
            interaction_session_id=interaction_session_id,
            activity_id=activity_id,
            expected_source=expected_source,
        )
        if active_context is None:
            return None, _rejected(
                "correction_activity_not_found",
                "这项任务已经不在可纠正阶段；如果它已经结束，请使用重做入口。",
            )
        return active_context, None


def _required_text(value: object) -> str:
    """把未知值收窄为去空白的必填文字。

    Args:
        value: 尚未信任的工具参数或任务记忆字段。

    Returns:
        去除首尾空白后的文字；类型不符时返回空串。
    """

    return value.strip() if isinstance(value, str) else ""


def _cognitive_types_from_arguments(
    value: object,
) -> tuple[tuple[CognitiveGoalKind, ...], bool]:
    """读取并校验纠正轮新增的 Cognitive 类型。"""

    if value is None:
        return (), False
    if not isinstance(value, (list, tuple)):
        return (), True
    result: list[CognitiveGoalKind] = []
    seen: set[str] = set()
    invalid = False
    for item in value:
        if not isinstance(item, str):
            invalid = True
            continue
        normalized = item.strip()
        if normalized not in COGNITIVE_TOOL_TYPES or normalized in seen:
            invalid = True
            continue
        seen.add(normalized)
        result.append(cast(CognitiveGoalKind, normalized))
    return tuple(result), invalid


def _cognitive_types(value: object) -> tuple[CognitiveGoalKind, ...]:
    """从父 Activity 证据中读取合法的 Cognitive 类型。"""

    if not isinstance(value, (list, tuple)):
        return ()
    return _ordered_cognitive_types(value)


def _ordered_cognitive_types(value: object) -> tuple[CognitiveGoalKind, ...]:
    """按首次出现顺序清洗并去重 Cognitive 类型。"""

    if not isinstance(value, (list, tuple)):
        return ()
    result: list[CognitiveGoalKind] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if normalized not in COGNITIVE_TOOL_TYPES or normalized in seen:
            continue
        seen.add(normalized)
        result.append(cast(CognitiveGoalKind, normalized))
    return tuple(result)


def _activity_context(
    correctable_activities: tuple[CorrectableActivityContext, ...],
    *,
    interaction_session_id: str,
    activity_id: str,
    expected_source: str,
) -> dict[str, object] | None:
    """从本轮冻结允许列表提取旧任务的完整只读现场。

    Args:
        correctable_activities: Prompt、schema 与执行共用的不可变现场。
        interaction_session_id: 当前 Foreground 交互会话编号。
        activity_id: 用户明确指定的旧 Activity 编号。
        expected_source: 当前工具允许纠正的执行来源。

    Returns:
        匹配的来源、目标和状态；活跃快照中不存在时返回空。
    """

    normalized_activity_id = activity_id.strip()
    normalized_session_id = interaction_session_id.strip()
    for activity_context in correctable_activities:
        if (
            activity_context.activity_id == normalized_activity_id
            and activity_context.interaction_session_id == normalized_session_id
            and activity_context.source == expected_source
        ):
            return {
                "activity_id": activity_context.activity_id,
                "source": activity_context.source,
                "interaction_session_id": activity_context.interaction_session_id,
                "state": activity_context.state,
                "phase": activity_context.phase,
                "goal": activity_context.goal,
                "cognitive_tool_types": activity_context.cognitive_tool_types,
            }
    return None


def _accepted_or_rejected(
    reservation: CorrectionReservation,
    old_activity_id: str,
    fallback: str,
    **extra: object,
) -> dict[str, object]:
    """把纠正服务事件收窄成 Foreground 工具结果。

    Args:
        reservation: 共享服务返回的结构化预留结果。
        old_activity_id: 被纠正的旧 Activity 编号。
        fallback: 拒绝事件没有安全说明时使用的固定文案。
        extra: 需要随接受结果返回的目标类型字段。

    Returns:
        只包含稳定身份和安全原因的 Foreground 工具结果。
    """

    event = next((item for item in reservation.events if isinstance(item, dict)), {})
    payload = event.get("payload") if isinstance(event, dict) else None
    payload_dict = dict(payload) if isinstance(payload, dict) else {}
    if reservation.accepted:
        result: dict[str, object] = {
            "state": "accepted",
            "activity_id": reservation.activity_id,
            "based_on_activity_id": old_activity_id,
            "based_on_relationship": "activity_correction",
        }
        result.update(extra)
        return result
    return {
        "state": "rejected",
        "activity_id": old_activity_id,
        "reason": payload_dict.get("reason") or "activity_correction_rejected",
        "safe_message": payload_dict.get("safe_message") or fallback,
    }


def _rejected(reason: str, safe_message: str) -> dict[str, object]:
    """构造不会泄露执行器内部细节的纠正拒绝结果。

    Args:
        reason: 供协议和日志判断的稳定原因码。
        safe_message: 允许 Foreground 向用户自然解释的固定事实。

    Returns:
        状态固定为 rejected 的工具结果。
    """

    return {"state": "rejected", "reason": reason, "safe_message": safe_message}
