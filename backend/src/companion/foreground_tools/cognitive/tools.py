from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Any, Protocol, cast

from companion.foreground_tools.cognitive.contracts import (
    CognitiveExecutionPlan,
    CognitiveGoal,
    CognitiveGoalKind,
)
from companion.foreground_tools.cognitive.planner import CognitivePlanner
from companion.foreground_tools.cognitive.schemas import (
    COGNITIVE_TOOL_NAMES,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.materials.foreground_images import (
    ForegroundImageReference,
    VisualEvidence,
    load_foreground_visual_input,
)
from companion.observability.logging_config import get_backend_logger
from companion.text.http_urls import normalize_http_urls


logger = get_backend_logger(__name__)


class CognitiveTaskSnapshotProtocol(Protocol):
    """定义认知工具判断跨轮占用所需的快照接口。"""

    state: str

    def to_payload(self) -> dict[str, object]:
        """返回不含内部流状态的公开任务载荷。"""


class CognitiveActivityExecutorProtocol(Protocol):
    """定义认知工具提交后台计划所需的最小 Activity 执行接口。"""

    def snapshot(self, interaction_session_id: str) -> CognitiveTaskSnapshotProtocol:
        """读取当前会话的任务占用快照。"""

    def prepare(
        self,
        *,
        interaction_session_id: str,
        plan: CognitiveExecutionPlan,
        source_user_text: str,
    ) -> tuple[bool, list[dict[str, object]]]:
        """登记一份等待 generation 接受的后台认知计划。"""


@dataclass(frozen=True)
class CognitiveToolContext:
    """承载同一 turn 认知工具规划所需的运行时上下文。

    Args:
        interaction_session_id: 当前交互会话编号。
        source_user_message_id: 触发本轮回复的用户消息编号。
        source_user_text: 触发本轮回复的真实用户正文。
        foreground_context: 当前产品态势，包含正式材料和活动状态。
        images: 由代码冻结并编码好的本轮图片证据，来源标签随像素一起传递。
        evidence_limitations: 由代码冻结的视觉证据限制说明。
        image_materials: 本轮图片材料的安全引用。
        cancellation_event: 当前 generation 的协作式取消信号。
        task_memory_lookup: 按父任务编号读取主进程权威详情的函数。
    """

    interaction_session_id: str
    source_user_message_id: int | None
    source_user_text: str
    foreground_context: dict[str, object]
    images: tuple[VisualEvidence, ...] = ()
    evidence_limitations: tuple[str, ...] = ()
    image_materials: tuple[ForegroundImageReference, ...] = ()
    cancellation_event: Event | None = None
    task_memory_lookup: Any | None = None


class CognitiveToolExecutor:
    """把同一轮认知工具调用合并为一个执行计划。"""

    def __init__(
        self,
        *,
        activity_executor: CognitiveActivityExecutorProtocol,
        planner: CognitivePlanner | None = None,
    ) -> None:
        """初始化认知工具执行器。

        Args:
            activity_executor: 负责后台认知 Activity 生命周期的执行器。
            planner: 可替换的纯规则认知计划器。
        """

        self._activity_executor = activity_executor
        self._planner = planner or CognitivePlanner()

    def execute_many(
        self,
        calls: tuple[ForegroundToolCall, ...],
        *,
        context: CognitiveToolContext,
    ) -> tuple[dict[str, object], ...]:
        """合并执行同一 turn 的全部认知工具调用。

        Args:
            calls: 按模型原始顺序排列的认知工具调用。
            context: 当前 turn 的用户、材料和环境快照上下文。

        Returns:
            与原始调用数量一致的 accepted、merged 或 rejected 结果。
        """

        if not calls:
            return ()

        # 如果同一会话里已有认知任务在跑，直接返回占用状态，避免重复启动。
        running_snapshot = self._activity_executor.snapshot(context.interaction_session_id)
        if running_snapshot.state in {"prepared", "starting", "running"}:
            snapshot_payload = running_snapshot.to_payload()
            return tuple(
                _already_running_result(call, snapshot_payload)
                for call in calls
            )

        parent_activity_ids = _parent_activity_ids(calls)
        if len(parent_activity_ids) > 1:
            return tuple(
                _parent_task_rejection_result(
                    call,
                    reason="multiple_parent_tasks",
                    safe_message="同一项认知任务只能基于一条旧任务记录继续。",
                )
                for call in calls
            )
        parent_activity_id = next(iter(parent_activity_ids), None)
        parent_task_context: dict[str, object] | None = None
        inherited_images: list[VisualEvidence] = []
        image_materials = list(context.image_materials)
        inherited_limitations: list[str] = []
        inherited_source_urls: list[str] = []
        if parent_activity_id is not None:
            if context.task_memory_lookup is None:
                return tuple(
                    _parent_task_rejection_result(
                        call,
                        reason="task_memory_unavailable",
                        safe_message="旧任务详情当前无法读取，不能基于它继续。",
                    )
                    for call in calls
                )
            parent_result = context.task_memory_lookup(
                parent_activity_id,
                context.cancellation_event,
            )
            if parent_result.get("state") != "completed":
                safe_message = parent_result.get("safe_message")
                return tuple(
                    _parent_task_rejection_result(
                        call,
                        reason=str(
                            parent_result.get("reason")
                            or "parent_task_not_available"
                        ),
                        safe_message=(
                            safe_message.strip()
                            if isinstance(safe_message, str) and safe_message.strip()
                            else "旧任务详情已经不可用，不能基于它继续。"
                        ),
                    )
                    for call in calls
                )
            parent_task_context = _parent_task_context(parent_result)
            input_snapshot = parent_result.get("input_snapshot")
            if isinstance(input_snapshot, dict):
                parent_images = input_snapshot.get("image_materials")
                if isinstance(parent_images, list) and parent_images:
                    reloaded = load_foreground_visual_input({"attachments": parent_images}, None, report_missing_screen=False)
                    inherited_images.extend(reloaded.images)
                    image_materials.extend(reloaded.image_materials)
                    inherited_limitations.extend(reloaded.limitations)
                if input_snapshot.get("encoded_image_count"):
                    inherited_limitations.append("父任务像素不保存在任务记忆中；重读的文件仅代表现在的材料，本轮桌面图不能代替父任务当时的现场。")
                inherited_source_urls.extend(
                    _string_list(input_snapshot.get("source_urls"))
                )
                inherited_limitations.extend(_string_list(input_snapshot.get("evidence_limitations")))

        # 为每个调用预分配结果槽位，保证最终输出顺序与输入顺序一致。
        results: list[dict[str, object] | None] = [None] * len(calls)
        # 只把通过校验的调用收集成本轮统一规划的目标。
        goals: list[CognitiveGoal] = []
        # 整轮共享的图片和网页来源会在这里汇总；去重和上限由计划器统一处理。
        images: list[VisualEvidence] = [*context.images, *inherited_images]
        evidence_limitations: list[str] = [*inherited_limitations, *context.evidence_limitations]
        selected_source_urls: list[str] = list(inherited_source_urls)
        # 记录通过校验的调用索引，后续取消或被 Activity 执行器拒绝时统一回填结果。
        valid_indexes: list[int] = []
        for index, call in enumerate(calls):
            if call.name not in COGNITIVE_TOOL_NAMES:
                results[index] = _unsupported_tool_result(call.name)
                continue

            allowed_arguments = {"brief", "response_goal", "based_on_activity_id"}
            if call.name == "lookup_current_fact":
                allowed_arguments.add("source_urls")
            if set(call.arguments) - allowed_arguments:
                results[index] = _rejected_result(
                    kind=_tool_name_to_kind(call.name), brief=_brief_from_arguments(call.arguments),
                    reason="invalid_cognitive_arguments", safe_message="认知请求包含不支持的参数。",
                )
                continue

            # 如果当前生成已被取消，不再登记新任务，直接按取消返回。
            if context.cancellation_event is not None and context.cancellation_event.is_set():
                results[index] = _rejected_result(
                    kind=_tool_name_to_kind(call.name),
                    brief=_brief_from_arguments(call.arguments),
                    reason="generation_cancelled",
                    safe_message="当前回复已经取消，认知任务没有启动。",
                )
                continue

            brief = _brief_from_arguments(call.arguments)
            if not brief:
                brief = context.source_user_text.strip() or "完成用户刚才提出的认知请求"
            response_goal = _response_goal_from_arguments(call.arguments)
            kind = _tool_name_to_kind(call.name)
            current_source_urls: list[str] = []
            if kind == "lookup_current_fact":
                # URL 只是可选来源，这里只做结构校验；内容读取和判断由 Knowledge 负责。
                requested_source_urls = _string_list_from_arguments(
                    call.arguments,
                    "source_urls",
                )
                current_source_urls = normalize_http_urls(requested_source_urls)
                ignored_url_count = len(requested_source_urls) - len(current_source_urls)
                if ignored_url_count:
                    # 网页来源不是任务成立的必要条件，坏 URL 只会降级能力；日志里不记录原始地址。
                    logger.info(
                        "认知查询忽略无效网页来源 tool_call_id=%s requested_count=%s "
                        "accepted_count=%s ignored_count=%s",
                        call.tool_call_id,
                        len(requested_source_urls),
                        len(current_source_urls),
                        ignored_url_count,
                    )

            goals.append(CognitiveGoal(
                tool_call_id=call.tool_call_id,
                kind=kind,
                brief=brief,
                response_goal=response_goal,
            ))
            selected_source_urls.extend(current_source_urls)
            valid_indexes.append(index)

        if not goals:
            return _complete_results(calls, results)

        # 至少有一个目标通过校验，接下来生成统一的后台认知计划。
        plan = self._planner.plan(
            goals=goals,
            images=tuple(images),
            source_urls=tuple(dict.fromkeys(selected_source_urls)),
            evidence_limitations=tuple(dict.fromkeys(evidence_limitations)),
            image_materials=image_materials,
            based_on_activity_id=parent_activity_id,
            parent_task_context=parent_task_context,
        )

        # 生成计划后再检查一次取消状态，避免把刚生成的计划塞进已取消的 Activity。
        if context.cancellation_event is not None and context.cancellation_event.is_set():
            for index in valid_indexes:
                call = calls[index]
                results[index] = _rejected_result(
                    kind=_tool_name_to_kind(call.name),
                    brief=_brief_from_arguments(call.arguments),
                    reason="generation_cancelled",
                    safe_message="当前回复已经取消，认知任务没有启动。",
                )
            return _complete_results(calls, results)

        # 这里只登记待启动的 Activity；真正执行要等到前台事件被接收。
        accepted, events = self._activity_executor.prepare(
            interaction_session_id=context.interaction_session_id,
            plan=plan,
            source_user_text=context.source_user_text,
        )
        if not accepted:
            for index in valid_indexes:
                call = calls[index]
                results[index] = _activity_executor_rejection_result(
                    events=events,
                    kind=_tool_name_to_kind(call.name),
                    brief=_brief_from_arguments(call.arguments),
                )
            return _complete_results(calls, results)

        prepared_payload = _first_event_payload(events, "cognitive_task.prepared")
        activity_id = str(prepared_payload.get("activity_id") or "").strip()
        for position, index in enumerate(valid_indexes):
            call = calls[index]
            # 同批目标里第一条对外显示为 accepted，其余合并到同一个 activity_id 上，标记为 merged。
            accepted_result: dict[str, object] = {
                "state": "accepted" if position == 0 else "merged",
                "activity_id": activity_id,
                "kind": _tool_name_to_kind(call.name),
                "brief": _brief_from_arguments(call.arguments),
            }
            if parent_activity_id is not None:
                accepted_result["based_on_activity_id"] = parent_activity_id
            results[index] = accepted_result
        return _complete_results(calls, results)


def _tool_name_to_kind(name: str) -> CognitiveGoalKind:
    """把合法工具名转换为认知目标类型。

    Args:
        name: 认知工具名称。

    Returns:
        对应认知目标类型。
    """

    if name not in COGNITIVE_TOOL_NAMES:
        raise ValueError(f"不支持的认知工具：{name}")
    return cast(CognitiveGoalKind, name)


def _brief_from_arguments(arguments: dict[str, Any]) -> str:
    """从工具参数中读取并清洗 brief。"""

    brief = arguments.get("brief")
    return brief.strip() if isinstance(brief, str) else ""


def _response_goal_from_arguments(arguments: dict[str, Any]) -> str:
    """从工具参数中读取并清洗可选回答目的。

    Args:
        arguments: Foreground 为当前认知工具提供的参数。

    Returns:
        去除首尾空白后的回答目的；未提供时返回空字符串。
    """

    response_goal = arguments.get("response_goal")
    return response_goal.strip() if isinstance(response_goal, str) else ""


def _parent_activity_ids(calls: tuple[ForegroundToolCall, ...]) -> set[str]:
    """读取同一批认知调用明确引用的父任务编号。

    Args:
        calls: 本轮全部认知工具调用。

    Returns:
        去空白后的父任务编号集合。
    """

    return {
        value.strip()
        for call in calls
        for value in [call.arguments.get("based_on_activity_id")]
        if isinstance(value, str) and value.strip()
    }


def _parent_task_context(result: dict[str, object]) -> dict[str, object]:
    """截取执行新任务真正需要的父任务事实。

    Args:
        result: 主进程返回的完整任务详情。

    Returns:
        只保留旧任务来源、目标、终态、结果和有用输入线索的字典；内部编号、
        召回协议计数、旧任务的执行参数和空值字段一律剔除。
    """

    candidates = {
        "source": result.get("source"),
        "goal": result.get("goal"),
        "terminal_state": result.get("terminal_state"),
        "outcome_brief": result.get("outcome_brief"),
        "result_text": result.get("result_text"),
        "output_paths": result.get("output_paths"),
        "safe_interactions": result.get("safe_interactions"),
    }
    # 空值字段对新任务是纯噪声，一律不出现在最终字典里。
    context = {key: value for key, value in candidates.items() if value}
    # 结果截断标记只在为真时有意义；prompt 会据此单独追加警告行。
    if result.get("truncated") is True:
        context["truncated"] = True
    # 只保留对理解旧结果有参考价值的输入线索。旧任务的执行参数（推理档位、
    # 联网开关、派生编号等）对新任务是噪声，还会暗示旧结论已经过核查；
    # 图片编码本身走多模态继承，不进入这份文本上下文。
    input_snapshot = result.get("input_snapshot")
    if isinstance(input_snapshot, dict):
        for key in ("source_user_text", "source_urls", "evidence_limitations"):
            value = input_snapshot.get(key)
            if value:
                context[key] = value
    return context


def _parent_task_rejection_result(
    call: ForegroundToolCall,
    *,
    reason: str,
    safe_message: str,
) -> dict[str, object]:
    """构造父任务无法使用时的认知工具拒绝结果。

    Args:
        call: 当前认知工具调用。
        reason: 稳定拒绝原因。
        safe_message: Foreground 可以自然转述的事实。

    Returns:
        不创建 Activity 的拒绝载荷。
    """

    return _rejected_result(
        kind=_tool_name_to_kind(call.name),
        brief=_brief_from_arguments(call.arguments),
        reason=reason,
        safe_message=safe_message,
    )


def _string_list(value: object) -> list[str]:
    """从未知值读取非空字符串列表。"""

    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _string_list_from_arguments(arguments: dict[str, Any], field_name: str) -> list[str]:
    """读取并保序去重工具提交的字符串列表。

    Args:
        arguments: 模型提交的工具参数。
        field_name: 待读取字段名。

    Returns:
        去除空项并保留首次出现顺序的字符串列表。
    """

    raw_values = arguments.get(field_name)
    if not isinstance(raw_values, list):
        return []
    return list(dict.fromkeys(
        item.strip()
        for item in raw_values
        if isinstance(item, str) and item.strip()
    ))


def _rejected_result(
    *,
    kind: CognitiveGoalKind,
    brief: str,
    reason: str,
    safe_message: str,
) -> dict[str, object]:
    """构造单个认知目标的本地拒绝结果。

    Args:
        kind: 认知目标类型。
        brief: 目标摘要。
        reason: 稳定拒绝原因。
        safe_message: Foreground 可转述的声明式用户安全消息。

    Returns:
        可直接写入 tool message 的拒绝载荷。
    """

    return {
        "state": "rejected",
        "kind": kind,
        "brief": brief,
        "reason": reason,
        "safe_message": safe_message,
    }


def _already_running_result(
    call: ForegroundToolCall,
    snapshot_payload: dict[str, object],
) -> dict[str, object]:
    """构造跨 turn 已有认知任务占用结果。

    Args:
        call: 当前被拒绝的认知调用。
        snapshot_payload: 正在运行的任务公开快照。

    Returns:
        可供 Foreground 解释占用状态的工具结果。
    """

    return {
        "state": "already_running",
        "kind": _tool_name_to_kind(call.name),
        "brief": _brief_from_arguments(call.arguments),
        "activity_id": snapshot_payload.get("activity_id"),
        "reason": "cognitive_task_running",
        "running_task": _running_task_payload(snapshot_payload),
        "safe_message": "已有认知任务正在处理，当前无法开启新任务。",
    }


def _running_task_payload(snapshot_payload: dict[str, object]) -> dict[str, object]:
    """截取可交给 Foreground 的运行任务摘要。"""

    return {
        "activity_id": snapshot_payload.get("activity_id"),
        "kind": snapshot_payload.get("kind"),
        "brief": snapshot_payload.get("brief", ""),
        "source_user_text": snapshot_payload.get("source_user_text", ""),
        "state": snapshot_payload.get("state", "running"),
    }


def _activity_executor_rejection_result(
    *,
    events: list[dict[str, object]],
    kind: CognitiveGoalKind,
    brief: str,
) -> dict[str, object]:
    """把 Activity 执行器的拒绝事件转换为工具结果。

    Args:
        events: Activity 执行器同步返回的生命周期事件。
        kind: 当前认知目标类型。
        brief: 当前认知目标摘要。

    Returns:
        可直接写入 tool message 的拒绝结果。
    """

    payload = _first_event_payload(events, "cognitive_task.rejected")
    reason = str(payload.get("reason") or "cognitive_task_rejected").strip()
    return {
        "state": "already_running" if reason == "cognitive_task_running" else "rejected",
        "kind": kind,
        "brief": brief,
        "activity_id": payload.get("activity_id"),
        "reason": reason,
        "safe_message": "这项认知任务没有接下。",
    }


def _first_event_payload(
    events: list[dict[str, object]],
    event_type: str,
) -> dict[str, object]:
    """返回指定事件类型的第一个载荷。"""

    for event in events:
        if event.get("type") != event_type:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _unsupported_tool_result(name: str) -> dict[str, object]:
    """返回不支持认知工具的统一结果。"""

    return {
        "state": "rejected",
        "kind": name,
        "reason": "unsupported_cognitive_tool",
        "safe_message": "这项认知能力当前不可用。",
    }


def _complete_results(
    calls: tuple[ForegroundToolCall, ...],
    results: list[dict[str, object] | None],
) -> tuple[dict[str, object], ...]:
    """确保每个原始调用都有且仅有一个结果。

    Args:
        calls: 原始认知工具调用。
        results: 与调用索引对齐的可选结果。

    Returns:
        不含空位的结果序列。
    """

    return tuple(
        result or _unsupported_tool_result(calls[index].name)
        for index, result in enumerate(results)
    )
