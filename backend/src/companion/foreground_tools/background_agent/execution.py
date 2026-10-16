"""执行 Foreground 后台工作工具，不让对话编排器理解后台领域参数。"""

from __future__ import annotations

from threading import Event
from companion.background_agent.contracts import BackgroundExecutionLane
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.foreground_tools.background_agent.submission_broker import (
    WorkInteractionSubmissionBroker,
)
from companion.foreground_tools.background_agent.materials import (
    MAX_RECENT_MATERIAL_PATHS,
    materials_from_foreground_context,
    recent_materials_from_paths,
)
from companion.materials.models import normalize_material_path
from companion.realtime.assistant_events import (
    RECENT_MATERIAL_OBSERVATION_FIELD,
    RecentMaterialObservation,
)
from companion.foreground_tools.background_agent.submission_service import (
    BackgroundAgentActivityExecutorProtocol,
    BackgroundWorkSubmission,
    BackgroundWorkSubmissionService,
    PreparedBackgroundWork,
)
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.foreground_tools.task_memory.broker import TaskMemoryLookupBroker
from companion.llm.foreground_models import ForegroundToolCall


class BackgroundAgentToolExecutor:
    """校验后台工具参数，并把当前轮事实交给后台 Activity 执行器。

    Args:
        activity_executor: 后台工作的单槽位执行器；运行时未装配时可以为空。
        submission_broker: 等待主进程和后台执行器权威回执的进程内 Broker。
        commit_barrier: Background 提交与执行器关闭共用的排水屏障。
    """

    def __init__(
        self,
        activity_executor: BackgroundAgentActivityExecutorProtocol | None,
        submission_broker: WorkInteractionSubmissionBroker | None = None,
        task_memory_lookup_broker: TaskMemoryLookupBroker | None = None,
        commit_barrier: BackgroundCommitDrainBarrier | None = None,
    ) -> None:
        """保存后台工具唯一需要的执行器依赖。

        Args:
            activity_executor: 负责配置快照、槽位裁决和任务准备的执行器。
            submission_broker: 自然语言工作回答的权威提交等待器。
            task_memory_lookup_broker: 按编号读取父任务详情的等待器。
            commit_barrier: 可选的 Background 提交关闭排水屏障。
        """

        self._activity_executor = activity_executor
        self._submission_service = BackgroundWorkSubmissionService(
            activity_executor,
            commit_barrier=commit_barrier,
        )
        self._prepared_background_work: dict[str, PreparedBackgroundWork] = {}
        self._submission_broker = submission_broker
        self._task_memory_lookup_broker = task_memory_lookup_broker

    def background_available(self) -> bool:
        """返回当前 Background 提交边界是否真的可以接单。

        Returns:
            canonical submission service 的实时只读结果。
        """

        return self._submission_service.background_available()

    def execute_start_background_work(
        self,
        *,
        execution_lane: BackgroundExecutionLane,
        tool_call: ForegroundToolCall,
        interaction_session_id: str,
        foreground_context: dict[str, object],
        environment_snapshot: FrozenEnvironmentSnapshot | None,
        protocol_transaction_id: str,
        cancellation_event: Event | None = None,
    ) -> dict[str, object]:
        """校验启动参数，并冻结本轮材料和可选现场截图。

        Args:
            execution_lane: 可信调用方指定的 ordinary 或 recovery；不读取模型参数。
            tool_call: Foreground 模型产生的结构化工具调用。
            interaction_session_id: 当前产品交互会话编号。
            foreground_context: 当前轮冻结的材料与工作交互事实。
            environment_snapshot: 当前轮冻结的现场快照。
            cancellation_event: 当前 generation 的取消信号。
            protocol_transaction_id: 工具批次进入编排时分配的稳定事务编号。

        Returns:
            可交给工具事务层的 accepted、already_running 或 rejected 结果。
        """

        arguments = tool_call.arguments
        goal = arguments.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return _rejected("missing_goal", "需要先明确后台工作要完成什么。")

        deliverable = arguments.get("deliverable")
        if not isinstance(deliverable, bool):
            return _rejected(
                "invalid_deliverable",
                "后台任务缺少有效的交付规则。",
            )

        requirements = arguments.get("requirements", "")
        if not isinstance(requirements, str):
            return _rejected(
                "invalid_requirements",
                "后台任务的成果要求必须是文字。",
            )

        include_current_scene = arguments.get("include_current_scene", False)
        if not isinstance(include_current_scene, bool):
            return _rejected(
                "invalid_include_current_scene",
                "后台任务的现场输入规则无效。",
            )
        if include_current_scene and not _has_usable_scene(environment_snapshot):
            return _rejected(
                "current_scene_unavailable",
                "当前屏幕截图不可用，请重新截取现场或提供原文件。",
            )

        material_paths = arguments.get("material_paths", [])
        if (
            not isinstance(material_paths, list)
            or len(material_paths) > MAX_RECENT_MATERIAL_PATHS
            or not all(isinstance(item, str) and item.strip() for item in material_paths)
        ):
            return _rejected(
                "invalid_material_paths",
                f"近期材料路径必须是不超过 {MAX_RECENT_MATERIAL_PATHS} 条的非空文字列表。",
            )

        current_materials = materials_from_foreground_context(foreground_context)
        current_material_paths = {
            normalize_material_path(material.local_path)
            for material in current_materials
            if material.local_path
        }
        requested_recent_paths = [
            path
            for path in material_paths
            if normalize_material_path(path) not in current_material_paths
        ]
        recent_materials, material_failure = recent_materials_from_paths(
            foreground_context,
            requested_recent_paths,
        )
        if material_failure is not None:
            return {
                "state": "rejected",
                "safe_message": material_failure.detail,
                "material_failure": material_failure.to_tool_payload(),
                RECENT_MATERIAL_OBSERVATION_FIELD: RecentMaterialObservation(
                    outcome="validation_failed",
                ).to_payload(),
            }

        based_on_activity_id = arguments.get("based_on_activity_id")
        if based_on_activity_id is not None and not (
            isinstance(based_on_activity_id, str) and based_on_activity_id.strip()
        ):
            return _rejected(
                "invalid_based_on_activity_id",
                "旧任务编号无效，不能基于它继续。",
            )
        normalized_parent_id = (
            based_on_activity_id.strip()
            if isinstance(based_on_activity_id, str)
            else None
        )
        parent_task_context: dict[str, object] | None = None
        if normalized_parent_id is not None:
            if self._task_memory_lookup_broker is None:
                return _rejected(
                    "task_memory_unavailable",
                    "旧任务详情当前无法读取，不能基于它继续。",
                )
            parent_result = self._task_memory_lookup_broker.lookup_and_wait(
                activity_id=normalized_parent_id,
                purpose="task_derivation",
                cancellation_event=cancellation_event,
            )
            if parent_result.get("state") != "completed":
                safe_message = parent_result.get("safe_message")
                return _rejected(
                    str(parent_result.get("reason") or "parent_task_not_available"),
                    (
                        safe_message.strip()
                        if isinstance(safe_message, str) and safe_message.strip()
                        else "旧任务详情已经不可用，不能基于它继续。"
                    ),
                )
            parent_task_context = _parent_task_context(parent_result)

        normalized_goal = goal.strip()
        merged_materials = current_materials + recent_materials
        result = self._submission_service.prepare(
            BackgroundWorkSubmission(
                operation_id=(
                    f"{execution_lane}-background-{tool_call.tool_call_id}"
                ),
                execution_lane=execution_lane,
                tool_call_id=tool_call.tool_call_id,
                protocol_transaction_id=protocol_transaction_id,
                interaction_session_id=interaction_session_id,
                goal=normalized_goal,
                deliverable=deliverable,
                requirements=requirements,
                materials=merged_materials,
                current_scene_bytes=(
                    environment_snapshot.screenshot_bytes
                    if include_current_scene and environment_snapshot is not None
                    else None
                ),
                based_on_activity_id=normalized_parent_id,
                parent_task_context=parent_task_context,
            ),
            cancellation_event=cancellation_event,
        )
        if not isinstance(result, PreparedBackgroundWork):
            return dict(result.payload)
        self._prepared_background_work[result.activity_id] = result
        acceptance_payload = dict(result.acceptance_payload)
        reattached_paths = tuple(
            material.local_path
            for material in recent_materials
            if material.local_path is not None
        )
        if reattached_paths:
            acceptance_payload[RECENT_MATERIAL_OBSERVATION_FIELD] = (
                RecentMaterialObservation(
                    outcome="accepted",
                    paths=reattached_paths,
                ).to_payload()
            )
        return acceptance_payload

    def take_prepared_background_work(
        self,
        activity_id: str,
    ) -> PreparedBackgroundWork | None:
        """按 Activity 编号取走 canonical Prepared 结果。

        Args:
            activity_id: 原生工具 prepare 返回的 Background Activity 编号。

        Returns:
            首次领取时返回 PreparedBackgroundWork；不存在或已领取时返回空。
        """

        return self._prepared_background_work.pop(activity_id.strip(), None)

    def rollback_prepared_background_work(self, activity_id: str) -> bool:
        """在 generation 提交前取消时回滚 canonical Background 占位。

        Args:
            activity_id: 需要回滚的 Background Activity 编号。

        Returns:
            实际释放占位时返回真。
        """

        prepared = self._prepared_background_work.pop(activity_id.strip(), None)
        if prepared is None:
            return False
        return prepared.rollback_unclaimed()

    def execute_work_interaction(
        self,
        *,
        tool_call: ForegroundToolCall,
        current_user_text: str,
        foreground_context: dict[str, object],
        cancellation_event: Event | None,
    ) -> dict[str, object]:
        """校验自然语言工作回答，并等待后台执行器的真实接收结果。

        Args:
            tool_call: Foreground 模型产生的工作交互调用。
            current_user_text: 用户本轮原始话语。
            foreground_context: 当前轮冻结的待答请求和材料事实。
            cancellation_event: 当前 generation 的取消信号。

        Returns:
            主进程和后台执行器已经共同确认的结果，或用户安全拒绝。
        """

        if self._submission_broker is None:
            return _rejected(
                "background_unavailable",
                "后台当前无法接收这次回应，本次回应没有提交成功。",
            )

        request_id = tool_call.arguments.get("request_id")
        outcome = tool_call.arguments.get("outcome")
        answers = tool_call.arguments.get("answers")
        if not isinstance(request_id, str) or not request_id.strip():
            return _rejected(
                "invalid_agent_question_request_id",
                "这条后台资料问题已经失效。",
            )
        if outcome not in {"allow", "deny", "answer", "cancel"}:
            return _rejected(
                "invalid_work_interaction_outcome",
                "需要先明确允许、拒绝、取消或资料答案。",
            )

        normalized_request_id = request_id.strip()
        pending = _find_pending_interaction(
            foreground_context,
            normalized_request_id,
        )
        if pending is None:
            return _rejected(
                "work_interaction_not_found",
                "这条工作请求已经失效或被处理。",
            )

        kind = pending.get("kind")
        if kind == "permission" and outcome not in {"allow", "deny", "cancel"}:
            return _rejected(
                "invalid_permission_outcome",
                "这项权限请求需要明确允许、拒绝或取消。",
            )
        if kind == "question" and outcome not in {"answer", "cancel"}:
            return _rejected(
                "invalid_question_outcome",
                "这项资料请求需要完整答案或明确取消。",
            )
        if outcome == "answer" and not isinstance(answers, dict):
            return _rejected(
                "invalid_work_interaction_answers",
                "需要完整回答当前工作提出的资料问题。",
            )

        normalized_answers = dict(answers) if isinstance(answers, dict) else {}
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in normalized_answers.items()
        ):
            return _rejected(
                "invalid_work_interaction_answers",
                "资料答案必须是逐问题填写的文字。",
            )

        activity_id = pending.get("activity_id")
        if not isinstance(activity_id, str) or not activity_id.strip():
            return _rejected(
                "work_interaction_activity_missing",
                "这条工作请求已经失效。",
            )
        materials = materials_from_foreground_context(foreground_context)
        return self._submission_broker.submit_and_wait(
            request_id=normalized_request_id,
            activity_id=activity_id.strip(),
            outcome=outcome,
            answers=normalized_answers,
            user_text=current_user_text,
            materials=materials,
            turn_material_context_id=_turn_material_context_id(
                foreground_context
            ),
            cancellation_event=cancellation_event,
        )

def _has_usable_scene(snapshot: FrozenEnvironmentSnapshot | None) -> bool:
    """判断当前请求是否带有可原样交接的截图字节。

    Args:
        snapshot: 当前轮冻结的桌面现场快照。

    Returns:
        图片类型和图片字节都存在时返回真。
    """

    return bool(
        snapshot is not None
        and snapshot.screen_available
        and snapshot.screenshot_bytes is not None
    )


def _turn_material_context_id(
    foreground_context: dict[str, object],
) -> str | None:
    """读取本轮新增材料的生命周期上下文编号。

    Args:
        foreground_context: 当前轮冻结的产品态势。

    Returns:
        非空上下文编号；本轮没有正式材料上下文时返回空。
    """

    material_context = foreground_context.get("active_turn_material_context")
    if not isinstance(material_context, dict):
        return None
    context_id = material_context.get("context_id")
    if not isinstance(context_id, str) or not context_id.strip():
        return None
    return context_id.strip()


def _find_pending_interaction(
    foreground_context: dict[str, object],
    request_id: str,
) -> dict[str, object] | None:
    """按请求编号读取本轮权威待答事项。

    Args:
        foreground_context: 当前轮冻结的产品态势。
        request_id: Foreground 准备回答的工作交互请求编号。

    Returns:
        匹配的待答事项；不存在时返回空。
    """

    pending_items = foreground_context.get("pending_work_interactions")
    if not isinstance(pending_items, list):
        return None
    return next(
        (
            item
            for item in pending_items
            if isinstance(item, dict) and item.get("request_id") == request_id
        ),
        None,
    )


def _rejected(reason: str, safe_message: str) -> dict[str, object]:
    """构造不泄漏内部执行细节的统一拒绝结果。

    Args:
        reason: 供内部诊断和分支判断使用的稳定原因。
        safe_message: 可以交给 Foreground 自然转述的安全事实。

    Returns:
        统一的拒绝结果字典。
    """

    return {
        "state": "rejected",
        "reason": reason,
        "safe_message": safe_message,
    }


def _parent_task_context(result: dict[str, object]) -> dict[str, object]:
    """截取后台执行内核可以读取的父任务事实。

    Args:
        result: 主进程返回的完整任务详情。

    Returns:
        旧目标、终态、结果、输入和安全交互事实。
    """

    input_snapshot = result.get("input_snapshot")
    return {
        "activity_id": result.get("activity_id"),
        "source": result.get("source"),
        "goal": result.get("goal"),
        "terminal_state": result.get("terminal_state"),
        "outcome_brief": result.get("outcome_brief"),
        "result_text": result.get("result_text"),
        "output_paths": result.get("output_paths"),
        "safe_interactions": result.get("safe_interactions"),
        "input_snapshot": _background_parent_input_snapshot(input_snapshot),
        "original_character_count": result.get("original_character_count"),
        "returned_character_count": result.get("returned_character_count"),
        "truncated": result.get("truncated", False),
    }


def _background_parent_input_snapshot(value: object) -> dict[str, object]:
    """把旧输入投影成后台文本执行内核真正能消费的事实。

    Cognitive 图片编码不能作为普通 JSON 文本交给 Claude。跨来源派生仍会说明旧任务
    使用过多少张图片；如果当前目标确实需要原图，执行内核应向用户索取正式材料。

    Args:
        value: 主进程返回的父任务原始输入快照。

    Returns:
        不含 data URL 的输入事实字典。
    """

    if not isinstance(value, dict):
        return {}
    snapshot = dict(value)
    # 兼容两种历史形态：元素可能是裸 data_url 字符串，也可能是带标签的证据字典；
    # 像素一律不交给文本执行内核，只保留数量事实。
    encoded_images = snapshot.pop("encoded_images", None)
    if isinstance(encoded_images, (list, tuple)):
        snapshot["encoded_image_count"] = len([
            item for item in encoded_images
            if (isinstance(item, str) and item)
            or (isinstance(item, dict) and item.get("data_url"))
        ])
        snapshot["encoded_images_available"] = False
    return snapshot
