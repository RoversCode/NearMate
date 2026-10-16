from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from companion.background_agent.contracts import AgentMaterial
from companion.character_cards.models import CharacterCardDefinition
from companion.core.application.assistant_request_models import ForegroundContext
from companion.foreground_self_reflection.contracts import (
    SelfReflectionEntryContext,
    SelfReflectionInterruptionReplyContext,
    SelfReflectionRecoveryContext,
    SelfReflectionToolCommitDecisionCommand,
)
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.materials.foreground_images import FrozenVisualInput
from companion.runtime_memory.models import (
    RuntimeMemoryCompactionPlan,
    RuntimeMemorySnapshot,
    TaskMemoryLookupPurpose,
)
from companion.realtime.assistant_output_contracts import (
    ActivityProgressReference,
    AssistantHistoryPolicy,
)


# 助手请求来源：区分用户输入是从哪里进来的。
AssistantRequestSource = Literal[
    "text_input",
    "final_asr",
    "system_notice",
    "timed_reminder_notice",
    "self_reflection_entry",
    "self_reflection_recovery",
    "self_reflection_interruption_reply",
]
# - text_input: 桌面端/控制台打字输入。
# - final_asr: 语音转写结束后的最终文本。
# - system_notice: 系统主动插话提醒。
# - timed_reminder_notice: 到点提醒专用的强打断请求。

# 运行时事件来源：标识事件由哪个子系统产生。
RuntimeEventSource = Literal[
    "realtime_input",
    "assistant_runtime",
    "assistant_process",
    "assistant_output_lanes",
    "main_process",
]
# - realtime_input: 实时音频输入侧（VAD/ASR）。
# - assistant_runtime: 助手运行时。
# - assistant_process: 助手子进程。
# - assistant_output_lanes: 多 lane 并行输出系统。

# StreamVox 管理操作：控制 TTS 角色与配置。
StreamVoxManagementKind = Literal[
    "runtime_state",      # 查询运行状态。
    "delete_role",        # 删除角色。
    "switch_config",      # 切换配置。
    "create_role",        # 创建角色。
    "preview_role",       # 预览角色音色。
    "get_prompt_data",    # 获取提示词数据。
]

# 助手进程命令类型：告诉助手子进程要执行什么任务。
AssistantProcessCommandKind = Literal[
    "assistant_request",      # 普通助手生成请求。
    "assistant_control",      # 控制命令，如取消生成。
    "streamvox_management",   # StreamVox 管理操作。
    "shutdown",               # 关闭助手进程。
    "runner_event_tick",      # 运行器心跳/事件 tick。
]


@dataclass(frozen=True)
class AssistantRequest:
    """用户输入进入助手生成链路的请求。

    Args:
        request_id: 请求幂等标识，通常对应文本输入请求或语音 turn。
        source_session_id: 文本或媒体输入来源的会话标识。
        interaction_session_id: 主进程产品交互会话标识，只用于请求归属和迟到事件隔离。
        character_card: 本轮请求使用的角色卡快照。
        user_call_name: 本轮请求使用的用户称呼。
        user_self_intro: 本轮请求使用的用户自我介绍。
        user_extra_notes: 本轮请求使用的用户补充备注。  
        user_text: 用户最终输入文本。
        source: 请求来源，用于区分文本输入和 final ASR。
        source_turn_id: 语音输入来源的 turn 标识；文本输入时为空。
        turn_material_context_id: 本轮材料上下文编号；系统通知时为空。
        requested_at: 请求创建时的单调时间。
        foreground_context: 本轮用于粗路由判断的产品态势面板。
        environment_snapshot: 本轮冻结的环境截图与元数据。
        frozen_visual_input: 请求准备时编码的瞬态图片；首轮、续写与恢复共用，不进入历史。
        history_policy: 本轮可见正文进入稳定对话、Activity Progress 临时账本，
            还是未处理插话片段账本。
        activity_progress_references: 纯 Progress 请求关联的 Activity 冻结进度。
        derived_from_activity_ids: 本轮表达依赖的 Cognitive Result Activity 编号。
        self_reflection_context: 自我反思可见表达的冻结指令；普通请求为空。
        direct_output_text: 已由主进程确定、无需调用 Foreground LLM 的可见正文。
    """

    request_id: str
    source_session_id: str
    interaction_session_id: str
    character_card: CharacterCardDefinition
    user_call_name: str
    user_self_intro: str
    user_extra_notes: str
    user_text: str
    source: AssistantRequestSource
    source_turn_id: str | None
    requested_at: float
    turn_material_context_id: str | None = None
    foreground_context: ForegroundContext = field(default_factory=ForegroundContext)
    environment_snapshot: FrozenEnvironmentSnapshot | None = None
    frozen_visual_input: FrozenVisualInput | None = None
    runtime_memory_snapshot: RuntimeMemorySnapshot = RuntimeMemorySnapshot()
    history_policy: AssistantHistoryPolicy = "stable_dialogue"
    activity_progress_references: tuple[ActivityProgressReference, ...] = ()
    derived_from_activity_ids: tuple[str, ...] = ()
    self_reflection_context: (
        SelfReflectionEntryContext
        | SelfReflectionRecoveryContext
        | SelfReflectionInterruptionReplyContext
        | None
    ) = None
    # Self-Reflection 入场表达需要明确主动抢占原因，避免旧回答被误标为用户打断。
    generation_preemption_reason: str | None = None
    direct_output_text: str | None = None


@dataclass(frozen=True)
class CancelCurrentGenerationCommand:
    """取消当前输出，或只取消编号匹配的请求及 generation。

    Args:
        generation_id: 可选的目标生成编号。
        reason: 取消输出的业务原因。
        issued_at: 取消意图形成时的单调时间。
        request_id: 可选的目标请求编号；用于保护随后开始的新回复。
    """

    generation_id: str | None
    reason: str
    issued_at: float
    request_id: str | None = None
    kind: Literal["cancel_current_generation"] = field(
        default="cancel_current_generation",
        init=False,
    )


@dataclass(frozen=True)
class RequestSelfReflectionFallbackCommand:
    """请求指定 Recovery 停止模型思考并公开确定性兜底正文。"""

    self_reflection_id: str
    runtime_epoch: int
    reason: str
    issued_at: float
    kind: Literal["request_self_reflection_fallback"] = field(
        default="request_self_reflection_fallback",
        init=False,
    )


@dataclass(frozen=True)
class CancelSupersededSystemNoticeCommand:
    """取消一条已被更新事实取代的独立 System Notice。

    Args:
        request_id: 需要严格匹配的独立 System Notice 请求编号。
        reason: 触发本次取消的业务原因。
        issued_at: 主进程创建命令的单调时间。
    """

    request_id: str
    reason: str
    issued_at: float
    kind: Literal["cancel_superseded_system_notice"] = field(
        default="cancel_superseded_system_notice",
        init=False,
    )


@dataclass(frozen=True)
class CancelTimedReminderNoticeCommand:
    """按请求编号停止一条正在生成或播放的 Reminder Notice。"""

    request_id: str
    reason: str
    issued_at: float
    kind: Literal["cancel_timed_reminder_notice"] = field(
        default="cancel_timed_reminder_notice",
        init=False,
    )


@dataclass(frozen=True)
class WarmupForegroundCommand:
    """请求助手子进程预热 Foreground 模型。"""

    reason: str
    issued_at: float
    kind: Literal["warmup_foreground"] = field(
        default="warmup_foreground",
        init=False,
    )


@dataclass(frozen=True)
class CancelActivityCommand:
    """按内部来源和 Activity 编号取消后台活动。"""

    activity_id: str
    source: str
    reason: str
    issued_at: float
    kind: Literal["cancel_activity"] = field(default="cancel_activity", init=False)


@dataclass(frozen=True)
class BackgroundAgentInteractionAnswerCommand:
    """主进程送回当前后台工作的一次权限或资料回答。

    Args:
        request_id: 已经由主进程 Registry claim 的请求编号。
        outcome: 用户明确给出的允许、拒绝、回答或取消。
        answers: 资料问题逐问题答案；权限请求为空。
        reason: 用户填写的拒绝或取消原因。
        materials: 本轮新增、需要扩展到当前工作的正式材料。
        user_text: 触发自然语言回答工具的原始用户话语。
        submission_id: 自然语言回答等待主进程权威回执时使用的提交编号；鼠标权限回答为空。
        issued_at: 主进程创建命令的单调时间。
    """

    request_id: str
    outcome: Literal["allow", "deny", "answer", "cancel"]
    answers: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    materials: tuple[AgentMaterial, ...] = ()
    user_text: str = ""
    submission_id: str | None = None
    issued_at: float = 0.0
    kind: Literal["background_agent_interaction_answer"] = field(
        default="background_agent_interaction_answer", init=False
    )


@dataclass(frozen=True)
class BackgroundAgentInteractionSubmissionRejectedCommand:
    """告诉助手子进程一次自然语言交互回答没有抢到提交权。

    Args:
        submission_id: 子进程正在等待的提交编号。
        request_id: 后台交互请求编号。
        reason: 主进程拒绝本次回答的稳定原因。
        issued_at: 主进程创建命令时的单调时间。
    """

    submission_id: str
    request_id: str
    reason: str
    issued_at: float = 0.0
    kind: Literal["background_agent_interaction_submission_rejected"] = field(
        default="background_agent_interaction_submission_rejected", init=False
    )


@dataclass(frozen=True)
class TaskMemoryLookupResultCommand:
    """把主进程读取到的任务详情送回当前助手子进程。

    Args:
        lookup_id: 子进程为本次同步召回生成的编号。
        activity_id: 本次明确读取的 Activity 编号。
        purpose: 本次读取用于前台回答还是派生任务执行。
        result: 主进程权威记忆返回的完成、不可用或失败事实。
        issued_at: 主进程创建回执时的单调时间。
    """

    lookup_id: str
    activity_id: str
    purpose: TaskMemoryLookupPurpose
    result: dict[str, object]
    issued_at: float = 0.0
    kind: Literal["task_memory_lookup_result"] = field(
        default="task_memory_lookup_result",
        init=False,
    )


@dataclass(frozen=True)
class ActivityCancellationResultCommand:
    """把主进程的 Activity 取消接收结果送回助手子进程。

    Args:
        cancellation_id: 子进程为本次取消生成的请求编号。
        activity_id: 本次精确请求取消的 Activity 编号。
        state: 主进程已接收取消请求，或明确拒绝本次请求。
        reason: 拒绝时供程序判断的稳定原因；成功时为 `accepted`。
        safe_message: Foreground 可以照实转述的安全事实。
        issued_at: 主进程创建回执时的单调时间。
    """

    cancellation_id: str
    activity_id: str
    state: Literal["cancellation_requested", "rejected"]
    reason: str
    safe_message: str
    issued_at: float = 0.0
    kind: Literal["activity_cancellation_result"] = field(
        default="activity_cancellation_result",
        init=False,
    )


@dataclass(frozen=True)
class TimedReminderCommandResultCommand:
    """把主进程完成的定时提醒写结果送回助手子进程。

    Args:
        command_id: 子进程为本次写操作生成的唯一编号。
        operation: 创建、修改、取消或关闭动作。
        result: 主进程返回的 canonical 提醒或安全失败事实。
        issued_at: 主进程创建回执时的单调时间。
    """

    command_id: str
    operation: Literal["create", "update", "cancel", "dismiss"]
    result: dict[str, object]
    issued_at: float = 0.0
    kind: Literal["timed_reminder_command_result"] = field(
        default="timed_reminder_command_result",
        init=False,
    )


@dataclass(frozen=True)
class RuntimeMemoryCompactionPlanCommand:
    """要求助手子进程执行一笔主进程授权的滚动摘要。

    Args:
        plan: 包含校验身份和协议完整消息前缀的不可变计划。
        issued_at: 主进程提交计划时的单调时间。
    """

    plan: RuntimeMemoryCompactionPlan
    issued_at: float = 0.0
    kind: Literal["runtime_memory_compaction_plan"] = field(
        default="runtime_memory_compaction_plan",
        init=False,
    )


@dataclass(frozen=True)
class AcknowledgeActivityProgressCommand:
    """确认一段 Progress 正文已经由主进程成功广播并登记。

    Args:
        request_id: 产生这段公开正文的助手请求编号。
        references: 本次公开正文覆盖的 Activity 冻结进度。
        issued_at: 主进程确认广播与账本写入完成的单调时间。
    """

    request_id: str
    references: tuple[ActivityProgressReference, ...]
    issued_at: float = 0.0
    kind: Literal["acknowledge_activity_progress"] = field(
        default="acknowledge_activity_progress",
        init=False,
    )


AssistantControlCommand: TypeAlias = (
    CancelCurrentGenerationCommand
    | RequestSelfReflectionFallbackCommand
    | CancelSupersededSystemNoticeCommand
    | CancelTimedReminderNoticeCommand
    | WarmupForegroundCommand
    | CancelActivityCommand
    | BackgroundAgentInteractionAnswerCommand
    | BackgroundAgentInteractionSubmissionRejectedCommand
    | TaskMemoryLookupResultCommand
    | ActivityCancellationResultCommand
    | TimedReminderCommandResultCommand
    | RuntimeMemoryCompactionPlanCommand
    | AcknowledgeActivityProgressCommand
    | SelfReflectionToolCommitDecisionCommand
)


RECENT_MATERIAL_OBSERVATION_FIELD = "_recent_material_observation"
RecentMaterialObservationOutcome = Literal["accepted", "validation_failed"]


@dataclass(frozen=True, slots=True)
class RecentMaterialObservation:
    """描述助手子进程对近期材料进行的一次私有观察。

    这份事实只在助手子进程和主进程 Router 之间短暂流转。Router 消费后必须删除，
    不能进入模型工具结果、Runtime Memory、公共事件或运行日志。

    Args:
        outcome: 路径通过校验并被工具接受，或校验失败需要刷新 Desktop 状态。
        paths: 成功接受时实际重挂的权威路径；校验失败时为空。
    """

    outcome: RecentMaterialObservationOutcome
    paths: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        """转换成跨进程可序列化的私有载荷。

        Returns:
            只包含稳定结果和路径列表的 JSON 友好字典。
        """

        return {
            "outcome": self.outcome,
            "paths": list(self.paths),
        }

    @classmethod
    def from_payload(cls, value: object) -> "RecentMaterialObservation":
        """把未知内部载荷收窄成正式观察事实。

        Args:
            value: 助手子进程送回的私有字段值。

        Returns:
            已完成类型校验的近期材料观察。

        Raises:
            ValueError: 结果类型、路径结构或成功观察内容不符合内部契约。
        """

        if not isinstance(value, dict):
            raise ValueError("近期材料观察载荷必须是字典")
        outcome = value.get("outcome")
        raw_paths = value.get("paths")
        if outcome not in {"accepted", "validation_failed"}:
            raise ValueError("近期材料观察结果无效")
        if not isinstance(raw_paths, list) or not all(
            isinstance(path, str) and path.strip()
            for path in raw_paths
        ):
            raise ValueError("近期材料观察路径无效")
        if outcome == "accepted" and not raw_paths:
            raise ValueError("成功的近期材料观察必须包含路径")
        if outcome == "validation_failed" and raw_paths:
            raise ValueError("失败的近期材料观察不能携带路径")
        return cls(
            outcome=outcome,
            paths=tuple(raw_paths),
        )


@dataclass(frozen=True)
class RuntimeEvent:
    """实时链路内部流转的运行时事件。

    Args:
        type: 控制面事件类型。
        source: 事件来源组件，用于日志、测试和跨线程排查。
        payload: 控制面载荷，只存放 JSON 友好数据。
        produced_at: 事件生成时的单调时间。
        runtime_epoch: 事件所属助手运行时世代；非助手运行时事件为空。
        retired_runtime_event: 是否是助手世代退出时取回的退役 backlog 事件；该标记只供主进程收口使用。
    """

    type: str
    source: RuntimeEventSource
    payload: dict[str, Any] = field(default_factory=dict)
    produced_at: float = 0.0
    runtime_epoch: int | None = None
    # 退役事件只能进入主进程的幂等清理分支，不能重新走普通 Activity、Notice 或输出流程。
    retired_runtime_event: bool = False

    def to_control_event(self) -> dict[str, Any]:
        """转换为控制 WebSocket 使用的事件字典。

        Returns:
            dict[str, Any]: 只包含 `type` 和 `payload` 的控制面事件。
        """

        # 广播层只理解产品协议字段，source 和 produced_at 保留在运行时内部用于观测。
        return {"type": self.type, "payload": self.payload}

    def as_dict(self) -> dict[str, Any]:
        """转换为控制 WebSocket 使用的事件字典。

        Returns:
            dict[str, Any]: 与 `to_control_event` 完全一致的控制面事件。
        """

        return self.to_control_event()


@dataclass(frozen=True)
class StreamVoxManagementRequest:
    """主进程发往助手子进程的 StreamVox 管理请求。

    Args:
        request_id: 请求与响应的匹配标识。
        kind: 管理动作类型。
        payload: 管理动作参数，只允许 JSON 友好数据。
        issued_at: 请求创建时的单调时间。
    """

    # request_id 是同步等待响应时的唯一匹配键，避免迟到响应污染当前请求。
    request_id: str
    # kind 表示本次管理动作，保持窄集合便于子进程显式分支处理。
    kind: StreamVoxManagementKind
    # payload 保存模型、设备、角色名等 JSON 友好参数。
    payload: dict[str, Any] = field(default_factory=dict)
    # issued_at 记录主进程发起管理请求的时间，用于日志和测试断言。
    issued_at: float = 0.0


@dataclass(frozen=True)
class StreamVoxManagementResponse:
    """助手子进程返回的 StreamVox 管理响应。

    Args:
        request_id: 对应管理请求的匹配标识。
        ok: 管理动作是否执行成功。
        payload: 成功时返回的状态快照或动作结果。
        error: 失败时返回的错误摘要。
        produced_at: 响应生成时的单调时间。
    """

    # request_id 必须与请求一致，主进程会据此跳过其它迟到响应。
    request_id: str
    # ok 为 True 时 payload 可用；False 时 error 应包含失败摘要。
    ok: bool
    # payload 保存 JSON 友好的 StreamVox 状态或删除结果。
    payload: dict[str, Any] = field(default_factory=dict)
    # error 保存失败文本，不携带完整异常对象，避免跨进程序列化失败。
    error: str | None = None
    # produced_at 记录子进程生成响应时间。
    produced_at: float = 0.0


@dataclass(frozen=True)
class AssistantProcessCommand:
    """主进程发往助手子进程的统一命令。

    Args:
        kind: 命令类型，用于区分生成请求、控制命令、管理命令和生命周期命令。
        payload: 命令载荷；不同 kind 对应不同 dataclass，空命令可为 None。
    """

    kind: AssistantProcessCommandKind
    payload: AssistantRequest | AssistantControlCommand | StreamVoxManagementRequest | None = None
