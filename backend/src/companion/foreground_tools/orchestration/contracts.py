from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias

from companion.llm.foreground_models import ForegroundToolCall
from companion.materials.foreground_images import FrozenVisualInput


ToolExecutionChannel = Literal["background", "deferred", "inline"]
BackgroundActivityDisposition = Literal["accepted", "merged"]
ToolRejectionState = Literal["rejected", "already_running", "ignored_duplicate"]
ToolFollowupFactKind = Literal[
    "activity_started",
    "activity_start_failed",
    "tool_rejected",
    "activity_replaced",
    "activity_replacement_failed",
    "activity_cancellation_requested",
    "activity_cancellation_failed",
    "timed_reminder_created",
    "timed_reminder_updated",
    "timed_reminder_cancelled",
    "timed_reminders_dismissed",
    "timed_reminder_command_failed",
]


@dataclass(frozen=True, slots=True)
class ToolFollowupFact:
    """保存 after_commit 后才能对用户表达的工具事实。

    Args:
        kind: Activity 启动、拒绝、纠正替换或取消请求的最终事实类型。
        activity_id: 该事实关联的 Activity 编号，用于和工具调用配对。
        goal: 普通任务或新任务目标。
        old_goal: 纠正时被取消的旧任务目标。
        new_goal: 纠正时准备启动的新任务目标。
        safe_message: 失败时可以交给 Foreground 转述的安全原因。
        already_running: 是否只是并入已经运行的同一 Activity。
        command_id: 定时提醒写命令编号，用于和原始工具调用配对。
        reminder_id: 主进程返回的提醒编号。
        reminder_content: 主进程返回的提醒事项。
        trigger_at: 主进程返回的绝对触发时刻。
        crosses_local_day: 提醒时刻是否跨越当前本地自然日。
        runtime_only: 提醒是否只在当前 Backend 运行期有效。
        reminder_contents: 批次关闭实际影响的提醒事项。
    """

    kind: ToolFollowupFactKind
    activity_id: str = ""
    goal: str = ""
    old_goal: str = ""
    new_goal: str = ""
    safe_message: str = ""
    already_running: bool = False
    command_id: str = ""
    reminder_id: str = ""
    reminder_content: str = ""
    trigger_at: str = ""
    crosses_local_day: bool = False
    runtime_only: bool = False
    reminder_contents: tuple[str, ...] = ()


class ToolReplyRequirement(StrEnum):
    """说明一个工具结果要不要进入本轮 Foreground 回复。

    后台任务刚被接下时通常只需确认；Inline 工具拿到具体内容时需要展示；
    拒绝和失败需要解释；重复调用等内部结果则不应打扰用户。
    """

    ACKNOWLEDGE = "acknowledge"  # 任务接下了，确认一下
    PRESENT_RESULT = "present_result"  # 工具已经拿到具体结果，要展示
    EXPLAIN = "explain"  # 工具没正常完成，要解释原因
    SILENT = "silent"  # 这条结果是内部信息，不要打扰用户


@dataclass(frozen=True, slots=True)
class InlineToolCompleted:
    """表示 Inline 工具已在当前轮同步完成，结果可以立刻交给 Foreground。

    Inline 工具不会创建 Activity，也不需要等待后台提交事务。

    Args:
        payload: 工具返回的完整完成载荷；保留给模型协议和事件边界使用。
        mounted_visual_input: 仅当前请求链使用的临时图片，不写入工具载荷或历史。
    """

    payload: dict[str, object]
    mounted_visual_input: FrozenVisualInput = FrozenVisualInput()

    @property
    def state(self) -> Literal["completed"]:
        """返回 Inline 工具唯一可能表示成功的稳定状态。

        Returns:
            固定为 completed。
        """

        return "completed"

    def to_payload(self) -> dict[str, object]:
        """返回可写入工具协议消息的独立结果副本。

        Returns:
            不与结果对象共享字典本身的完成载荷副本。
        """

        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class BackgroundActivityPrepared:
    """表示后台 Activity 已占位，正等待 Foreground 提交启动。

    accepted 表示本批第一条调用建立了 Activity；merged 表示当前调用已并入
    同一个 Activity。两者都还没有真正运行，只有 accepted 会绑定启动事务。

    Args:
        activity_id: prepare 阶段分配的 Activity 编号，用于后续启动、回滚和跟踪。
        disposition: accepted 表示创建占位，merged 表示复用已有占位。
        payload: 工具返回的完整准备载荷；保留给模型协议和事件边界使用。
    """

    activity_id: str
    disposition: BackgroundActivityDisposition
    payload: dict[str, object]

    @property
    def state(self) -> BackgroundActivityDisposition:
        """返回当前调用建立占位还是合并到已有占位。

        Returns:
            accepted 或 merged。
        """

        return self.disposition

    def to_payload(self) -> dict[str, object]:
        """返回可写入工具协议消息的独立准备载荷副本。

        Returns:
            不与结果对象共享字典本身的准备载荷副本。
        """

        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class DeferredActionPrepared:
    """表示已有资源上的动作已准备，正等待 Foreground 提交。

    这类动作不会创建新的 Activity。例如回答一个正在等待的后台资料问题时，
    prepare 阶段只占住这次回答权；工具完成事件成功入队后才真正恢复原任务。

    Args:
        resource_id: 被本次动作修改的稳定资源编号，例如待答请求编号。
        payload: 工具返回的完整准备载荷。
    """

    resource_id: str
    payload: dict[str, object]

    @property
    def state(self) -> Literal["accepted"]:
        """返回延迟动作唯一的准备成功状态。

        Returns:
            固定为 accepted。
        """

        return "accepted"

    def to_payload(self) -> dict[str, object]:
        """返回可写入工具协议消息的独立载荷副本。

        Returns:
            不与结果对象共享字典本身的准备载荷副本。
        """

        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class ToolRejected:
    """表示工具调用没有执行，但这不是执行过程出错。

    例如已有同类 Activity 在运行、调用重复，或当前条件不允许这项能力。调用方
    可以按 reason 决定解释、静默处理或提示用户，而不应把它当成系统故障。

    Args:
        state: rejected、already_running 或 ignored_duplicate。
        reason: 供代码分支和日志使用的稳定拒绝原因。
        safe_message: 可直接交给 Foreground 转述的安全说明；允许为空。
        payload: 工具返回的完整拒绝载荷；保留给模型协议和事件边界使用。
    """

    state: ToolRejectionState
    reason: str
    safe_message: str
    payload: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        """返回可写入工具协议消息的独立拒绝载荷副本。

        Returns:
            不与结果对象共享字典本身的拒绝载荷副本。
        """

        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class ToolFailed:
    """表示工具执行失败，或返回了无法继续处理的协议结果。

    与 ToolRejected 不同，失败意味着调用方无法把这次调用视为已被正常处理。

    Args:
        reason: 供代码分支和日志使用的稳定失败原因。
        safe_message: 可直接交给 Foreground 转述的安全说明。
        payload: 不含内部异常正文的失败载荷。
    """

    reason: str
    safe_message: str
    payload: dict[str, object]

    @property
    def state(self) -> Literal["failed"]:
        """返回失败结果唯一使用的稳定状态。

        Returns:
            固定为 failed。
        """

        return "failed"

    def to_payload(self) -> dict[str, object]:
        """返回可写入工具协议消息的独立失败载荷副本。

        Returns:
            不与结果对象共享字典本身的失败载荷副本。
        """

        return dict(self.payload)


# Foreground 后续流程只能面对这五种结果，不能继续读取工具特有字典猜测状态。
ToolOutcome: TypeAlias = (
    InlineToolCompleted
    | BackgroundActivityPrepared
    | DeferredActionPrepared
    | ToolRejected
    | ToolFailed
)


def tool_outcome_from_payload(
    payload: dict[str, object],
    *,
    channel: ToolExecutionChannel,
    mounted_visual_input: FrozenVisualInput = FrozenVisualInput(),
) -> ToolOutcome:
    """把工具返回的字典收窄为 Foreground 可直接分支处理的结果类型。

    不同工具的原始载荷字段可能不完全一致，但 Foreground 后续流程只应面对
    五种明确结果：Inline 已完成、后台 Activity 已准备、延迟动作已准备、调用被拒绝或执行失败。
    这里统一清洗状态字段，并把缺少关键身份或无法识别的载荷转换成失败结果，
    避免调用方继续猜测字典内容。

    Args:
        payload: 具体工具执行器返回的原始协议载荷；函数不会修改原字典。
        channel: 工具执行通道。Inline 只接受 completed，后台通道只接受
            accepted 或 merged，延迟动作只接受带请求编号的 accepted。

    Returns:
        可以通过类型判断直接处理的 ToolOutcome 联合类型实例。
    """

    # 先复制载荷，保证结果对象携带的是稳定快照，不会被执行器后续修改。
    normalized = dict(payload)
    state_value = normalized.get("state")
    state = state_value.strip() if isinstance(state_value, str) else ""

    # Inline 工具没有 Activity，只有明确完成状态才算成功返回。
    if channel == "inline" and state == "completed":
        return InlineToolCompleted(
            payload=normalized,
            mounted_visual_input=mounted_visual_input,
        )

    # 后台工具必须带回 Activity 编号；没有编号就无法跟踪、取消或交付结果。
    if channel == "background" and state in {"accepted", "merged"}:
        activity_id_value = normalized.get("activity_id")
        activity_id = activity_id_value.strip() if isinstance(activity_id_value, str) else ""
        if activity_id:
            return BackgroundActivityPrepared(
                activity_id=activity_id,
                disposition=state,
                payload=normalized,
            )
        return ToolFailed(
            reason="background_activity_identity_missing",
            safe_message="后台任务没有成功建立。",
            payload={
                "state": "failed",
                "reason": "background_activity_identity_missing",
                "safe_message": "后台任务没有成功建立。",
            },
        )

    # 延迟动作修改的是已经存在的资源，不能强行要求它创建新的 Activity。
    if channel == "deferred" and state == "accepted":
        resource_id_value = normalized.get("resource_id")
        resource_id = (
            resource_id_value.strip()
            if isinstance(resource_id_value, str)
            else ""
        )
        if not resource_id:
            request_id_value = normalized.get("request_id")
            resource_id = (
                request_id_value.strip()
                if isinstance(request_id_value, str)
                else ""
            )
        if resource_id:
            return DeferredActionPrepared(
                resource_id=resource_id,
                payload=normalized,
            )
        return ToolFailed(
            reason="deferred_action_identity_missing",
            safe_message="这次补充信息没有成功提交。",
            payload={
                "state": "failed",
                "reason": "deferred_action_identity_missing",
                "safe_message": "这次补充信息没有成功提交。",
            },
        )

    # 失败和拒绝都保留原始原因，便于 Foreground 按“解释失败”或“说明未执行”分流。
    reason_value = normalized.get("reason")
    safe_message_value = normalized.get("safe_message")
    reason = reason_value.strip() if isinstance(reason_value, str) else ""
    safe_message = (
        safe_message_value.strip()
        if isinstance(safe_message_value, str)
        else ""
    )
    if state == "failed":
        return ToolFailed(
            reason=reason or "tool_execution_failed",
            safe_message=safe_message or "这项工具调用失败了。",
            payload=normalized,
        )
    if state in {"rejected", "already_running", "ignored_duplicate"}:
        return ToolRejected(
            state=state,
            reason=reason or "tool_rejected",
            safe_message=safe_message,
            payload=normalized,
        )

    # 任何未纳入协议的状态都按失败处理，防止未知载荷被误当成成功。
    return ToolFailed(
        reason="invalid_tool_outcome",
        safe_message=safe_message or "这项工具调用没有返回有效结果。",
        payload=normalized,
    )


@dataclass(frozen=True)
class ExecutedToolCall:
    """把一条原始工具调用和它的唯一结果放在一起。

    上游按模型的调用顺序保存这个对象；下游无需再次解析结果字典，即可决定是否
    发工具完成事件、是否需要第二轮 Foreground 回复，以及是否要创建后台提交事务。

    Args:
        call: Foreground 模型返回的原始工具调用。
        outcome: 已归一化的完成、后台准备、拒绝或失败结果。
        reply_requirement: Foreground 对这条结果应确认、展示、解释还是静默处理。
        channel: 工具所属的 background、deferred 或 inline 执行通道。
    """

    call: ForegroundToolCall
    outcome: ToolOutcome
    reply_requirement: ToolReplyRequirement
    channel: ToolExecutionChannel

    @property
    def state(self) -> str:
        """返回标准化工具状态。

        Returns:
            completed、accepted、merged、rejected、failed 等稳定状态。
        """

        return self.outcome.state

    @property
    def activity_id(self) -> str | None:
        """返回工具准备结果明确关联的 Activity 编号。

        Returns:
            后台调用或 Activity 延迟动作已 prepare 时返回编号；其它结果返回空。
        """

        if isinstance(self.outcome, BackgroundActivityPrepared):
            return self.outcome.activity_id
        if isinstance(self.outcome, DeferredActionPrepared):
            activity_id = self.outcome.payload.get("activity_id")
            if isinstance(activity_id, str) and activity_id.strip():
                return activity_id.strip()
        return None

    @property
    def resource_id(self) -> str | None:
        """返回延迟动作在 prepare 阶段冻结的资源编号。

        Returns:
            延迟动作已准备时返回编号，其它通道返回空。
        """

        if isinstance(self.outcome, DeferredActionPrepared):
            return self.outcome.resource_id
        return None

    @property
    def runs_in_background(self) -> bool:
        """判断当前调用是否归入后台 Activity。

        名称沿用既有调用点；返回真只表示已建立或合并到 Activity 占位，不表示
        Activity 已经执行到 running 状态。

        Returns:
            outcome 是 BackgroundActivityPrepared 时返回真。
        """

        return isinstance(self.outcome, BackgroundActivityPrepared)

    def result_payload(self) -> dict[str, object]:
        """返回用于模型协议和前台事件的独立结果载荷。

        Returns:
            outcome 的字典副本。
        """

        return self.outcome.to_payload()


@dataclass(frozen=True)
class ForegroundToolExecution:
    """保存一轮工具调用的完整结果，并维持模型最初的调用顺序。

    Cognitive、TaskResult 和 Inline 虽然会在不同时间完成，但 calls 始终按模型原始
    tool_calls 的顺序排列，方便回写工具协议消息和判断后续回复策略。

    Args:
        calls: 严格按模型原始 tool_calls 顺序排列的全部执行结果。
    """

    calls: tuple[ExecutedToolCall, ...]
