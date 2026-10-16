"""定义异步活动提醒领域使用的显式表达意图。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypeAlias, TypedDict


ActivityLifecycle = Literal["started", "terminal", "waiting_user"]
ActivityTerminalState = Literal[
    "completed",
    "failed",
    "rejected",
    "cancelled",
    "interrupted",
]
TerminalStatusState = Literal["failed", "rejected", "cancelled", "interrupted"]
NoticeIntent = Literal[
    "progress_report",
    "cognitive_result",
    "work_result",
    "terminal_status",
    "permission_request",
    "information_request",
]
ActivityNoticeInterruptionStrategy = Literal["silent", "notify"]
NoticeDeliveryOutcome = Literal[
    "request_enqueued",
    "request_enqueue_failed",
    "llm_output_completed",
    "delivery_cancelled",
    "output_unavailable",
    "provider_protocol_failed",
    "superseded",
]


class ProgressReportNoticeSnapshot(TypedDict):
    """定义需要与实时 Activity 快照对齐的弱进度提醒。"""

    notice_id: str
    activity_id: str
    source: str
    intent: Literal["progress_report"]
    sequence: int


class CognitiveResultNoticeSnapshot(TypedDict):
    """定义由 Foreground 自然总结回答的 Cognitive 成果。"""

    notice_id: str
    activity_id: str
    source: str
    intent: Literal["cognitive_result"]
    category: str
    goal: str
    activity_kind: NotRequired[str]
    result_context: str


class WorkResultNoticeSnapshot(TypedDict):
    """定义后台工作成功后的短成果汇报。"""

    notice_id: str
    activity_id: str
    source: str
    intent: Literal["work_result"]
    category: str
    goal: str
    activity_kind: NotRequired[str]
    result_context: NotRequired[str]
    # 结果卡登记事实由 Notice 结果投影器从权威完成结果补写；缺失一律按未登记处理。
    deliverable: NotRequired[bool]


class TerminalStatusNoticeSnapshot(TypedDict):
    """定义失败、拒绝、取消或中断的终态汇报。"""

    notice_id: str
    activity_id: str
    source: str
    intent: Literal["terminal_status"]
    category: str
    goal: str
    activity_kind: NotRequired[str]
    terminal_state: TerminalStatusState
    status_context: NotRequired[str]


class PermissionRequestNoticeSnapshot(TypedDict):
    """定义需要用户明确允许或拒绝的安全操作摘要。"""

    notice_id: str
    activity_id: str
    source: str
    intent: Literal["permission_request"]
    category: str
    goal: str
    action_summary: str
    grant_scope: Literal["activity"]


class InformationRequestNoticeSnapshot(TypedDict):
    """定义需要用户补充的真实问题列表。"""

    notice_id: str
    activity_id: str
    source: str
    intent: Literal["information_request"]
    category: str
    goal: str
    questions: list[dict[str, object]]


NoticeSnapshot: TypeAlias = (
    ProgressReportNoticeSnapshot
    | CognitiveResultNoticeSnapshot
    | WorkResultNoticeSnapshot
    | TerminalStatusNoticeSnapshot
    | PermissionRequestNoticeSnapshot
    | InformationRequestNoticeSnapshot
)


class NoticeContext(TypedDict):
    """定义主进程冻结并交给 Foreground 的提醒批次。"""

    notice_batch: list[NoticeSnapshot]


@dataclass(frozen=True)
class NoticeDeliveryCommand:
    """命令 Notice 账本提交或补偿一次明确的请求交付结果。

    Args:
        request_id: 本次交付结果对应的助手请求编号。
        outcome: 已由事件边界归一化的交付结果。
    """

    request_id: str
    outcome: NoticeDeliveryOutcome


@dataclass(frozen=True)
class NoticeDeliveryResult:
    """描述 Notice 账本是否接受一条交付命令。

    Args:
        accepted: 请求身份有效且命令已交给对应状态转换时为真。
        request_id: 清洗后的助手请求编号。
        outcome: 本次尝试应用的交付结果。
        released_terminal_activity_keys: 已完成表达或明确放弃、可以删除 frozen Progress 的 Activity。
    """

    accepted: bool
    request_id: str
    outcome: NoticeDeliveryOutcome
    released_terminal_activity_keys: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ActivityLifecycleEvent:
    """描述活动开始、终止或需要用户介入的结构化事实。

    Args:
        lifecycle: 当前生命周期事件类型。
        activity_id: 来源系统内稳定的活动编号。
        interaction_session_id: 活动所属的交互会话编号。
        source: 活动来源类别。
        category: 可供 Foreground 理解的活动类别。
        goal: 用户希望这项活动完成的目标。
        activity_kind: 选择 Notice 表达规则所需的稳定任务类别。
        intent: 这条事实真正要表达的语义；started 为空。
        result_context: Cognitive 实际结果，只用于 cognitive_result。
        status_context: 可安全转述的终态原因。
        terminal_state: 活动终态。
        interaction_request_id: 用于精确失效的内部交互请求编号。
        action_summary: 权限请求的安全中文操作摘要。
        grant_scope: 权限请求覆盖的固定 Activity 范围。
        questions: 已经统一契约清洗的资料问题。
        suppress_notice: 终态是否只做清理而不追加用户可见提醒。
        created_at: 来源事件的单调时间或时间戳。
    """

    lifecycle: ActivityLifecycle
    activity_id: str
    interaction_session_id: str
    source: str
    category: str
    goal: str
    activity_kind: str = ""
    intent: NoticeIntent | None = None
    result_context: str = ""
    status_context: str = ""
    terminal_state: ActivityTerminalState | None = None
    interaction_request_id: str = ""
    action_summary: str = ""
    grant_scope: Literal["activity"] | None = None
    questions: tuple[dict[str, object], ...] = ()
    suppress_notice: bool = False
    created_at: float = 0.0


@dataclass(frozen=True)
class ActivityProgressNoticeIntent:
    """表示某项活动现在具备一次主动进度表达资格。

    Args:
        activity_id: 产生进度的 Activity 编号。
        interaction_session_id: Activity 所属产品交互会话。
        source: 进度来源执行器。
        sequence: 同一 Activity 内严格递增的进度序号。
        created_at: 来源事件创建时间。
        intent: 固定为 progress_report，避免下游猜测语义。
    """

    activity_id: str
    interaction_session_id: str
    source: str
    sequence: int
    created_at: float = 0.0
    intent: Literal["progress_report"] = "progress_report"


@dataclass(frozen=True)
class ActivityProgressNoticeInvalidation:
    """表示某项活动之前产生的弱进度已经失效。

    Args:
        activity_id: 需要废止进度的 Activity 编号。
        interaction_session_id: Activity 所属产品交互会话。
        source: 进度来源执行器。
        created_at: 失效事实创建时间。
    """

    activity_id: str
    interaction_session_id: str
    source: str
    created_at: float = 0.0


@dataclass(frozen=True)
class ActivityInteractionNoticeInvalidation:
    """表示一条权限或资料请求已被精确处理。

    Args:
        activity_id: 交互请求所属 Activity 编号。
        interaction_session_id: Activity 所属产品交互会话。
        source: 交互请求来源执行器。
        interaction_request_id: 只在进程内用于精确失效的请求编号。
        created_at: 解决事实创建时间。
    """

    activity_id: str
    interaction_session_id: str
    source: str
    interaction_request_id: str
    created_at: float = 0.0


@dataclass(frozen=True)
class ActivityEventApplyResult:
    """描述事件应用后建议取消的过期 System Notice 请求。

    Args:
        superseded_request_ids: 只包含独立 System Notice 的过期请求编号。
        released_terminal_activity_keys: 已无需表达、可以删除 frozen Progress 的 Activity。
    """

    superseded_request_ids: tuple[str, ...] = ()
    released_terminal_activity_keys: tuple[tuple[str, str], ...] = ()


MappedActivityEvent: TypeAlias = (
    ActivityLifecycleEvent
    | ActivityProgressNoticeIntent
    | ActivityProgressNoticeInvalidation
    | ActivityInteractionNoticeInvalidation
)
