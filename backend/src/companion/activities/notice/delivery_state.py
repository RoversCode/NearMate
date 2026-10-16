"""定义 NoticeDeliveryCoordinator 在同一锁下维护的内部交付账本。

账本按三层关联：交互会话保存全部提醒与独立 System Notice 占用；助手请求记录一批
已预留提醒的提交阶段；活动键用于进度去重、终态去重和运行时中断。这里的类型只描述
进程内状态，不能直接作为跨进程或前台公共契约。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from companion.activities.notice.models import ActivityTerminalState, NoticeIntent


# 读取模式决定批次的领取范围和归属：预览不占用；用户请求只附着强提醒；独立
# System Notice 可领取强提醒和弱进度，并独占该交互会话的一条输出链路。
NoticeReadMode = Literal["preview", "user_request", "system_notice"]

NoticeStrength = Literal["weak", "strong"]

# NoticeRecord 的状态只描述提醒自身，不描述请求是否已入队：
# pending ─→ reserved（独立播报）/ attached_user（附着用户请求）─→ 删除
#   │                 │
#   └──────────────→ dropped；已被新事实替代的已领取项先标为 superseded，等待外层收口。
NoticeState = Literal[
    "pending",
    "reserved",
    "attached_user",
    "superseded",
    "dropped",
]
# 请求阶段补足 NoticeState：prepared 表示仅完成批次预留，enqueued 表示 Bridge 已接管。
NoticeRequestStage = Literal["prepared", "enqueued"]

# (运行时世代、交互会话、活动来源、活动编号) 唯一标识一次可接收的活动生命周期。
ActivityNoticeKey: TypeAlias = tuple[int, str, str, str]


@dataclass
class NoticeRecord:
    """保存一条从活动事实生成、但尚未完成交付收口的提醒。

    该记录同时保存前台可见的最小事实和内部交付归属。弱进度不保存推理或正文；
    强提醒才携带类别、目标、终态和已经安全筛选的转述文案。
    """

    # 来源身份把提醒关联回原活动，并隔离热替换前后的同名活动。
    notice_id: str
    activity_id: str
    interaction_session_id: str
    source: str
    runtime_epoch: int

    # 显式意图决定候选替换和批次优先级；sequence 只用于弱进度的版本比较。
    intent: NoticeIntent
    strength: NoticeStrength
    sequence: int
    created_at: float

    # 强提醒允许交给前台的事实；原始命令、路径和 SDK 描述不进入这里。
    category: str = ""
    goal: str = ""
    activity_kind: str = ""
    result_context: str = ""
    status_context: str = ""
    terminal_state: ActivityTerminalState | None = None
    interaction_request_id: str = ""
    action_summary: str = ""
    grant_scope: Literal["activity"] | None = None
    questions: tuple[dict[str, object], ...] = ()

    # 交付账本：记录协议失败预算、当前提醒状态以及领取它的助手请求（若已有）。
    provider_protocol_failure_count: int = 0
    state: NoticeState = "pending"
    request_id: str | None = None


@dataclass
class InteractionSessionNoticeState:
    """保存一个交互会话的提醒主账本和独立输出占用。

    强提醒按来源车道和创建顺序等待领取；弱进度还必须在来源候选槽中胜出，才有
    资格被批次选择。Notice 的 ``request_id`` 表示对应来源车道仍被请求占用，
    ``system_request_id`` 额外防止同一会话并发预留两条独立 System Notice。
    """

    # 这个会话所有尚未彻底收尾的 NoticeRecord
    notices_by_id: dict[str, NoticeRecord] = field(default_factory=dict)
    # 每个来源当前最值得说的一条待领取弱进度；强提醒直接保存在主账本中排队。
    pending_progress_notice_id_by_source: dict[str, str] = field(
        default_factory=dict
    )
    # 当前是否已有独立 System Notice 正在占用输出
    system_request_id: str | None = None


@dataclass
class NoticeRequestState:
    """保存一次助手请求领取的提醒批次及其是否已被 Bridge 接管。

    不单独保存提醒编号列表；协调器始终按 ``NoticeRecord.request_id`` 查询，避免维护
    容易在回滚时不同步的双向索引。
    """

    request_id: str
    interaction_session_id: str
    mode: NoticeReadMode
    # prepared 仍可回滚；enqueued 后只能由实际输出终态收口。
    stage: NoticeRequestStage = "prepared"


@dataclass
class ActiveActivityNoticeState:
    """保存活动仍在运行时，Notice 协调器收口所需的最小公开事实。

    意外运行时中断需要类别和目标来生成 ``interrupted`` 强终态；进度序号用于拒绝
    迟到或重复的弱进度。执行正文、材料和内部故障细节不在此保存。
    """
    # 意外中断时，要告诉用户是什么活动
    category: str = ""
    # 意外中断时，要告诉用户原目标是什么
    goal: str = ""
    # 已接受到第几号 progress
    latest_progress_sequence: int = 0


@dataclass
class NoticeDeliveryState:
    """聚合 NoticeDeliveryCoordinator 在同一锁边界内维护的全部账本。

    Coordinator 的每次事件应用、批次预留和请求收口都只能读写本对象，以保证会话候选、
    请求归属和活动去重在并发回调下保持一致。
    """

    # 每个交互会话自己的“提醒盒子”
    # 想找“request-7 领了什么”，Coordinator 会去 session-a 的 Notice 盒子里找：
    sessions_by_id: dict[str, InteractionSessionNoticeState] = field(
        default_factory=dict
    )
    # 某次助手请求的“领号单”
    # requests_by_id["request-7"]
    # 是“request-7 这次助手请求领了哪些提醒”的收据。
    requests_by_id: dict[str, NoticeRequestState] = field(default_factory=dict)
    # 正在运行的活动名单
    # key是(runtime_epoch, interaction_session_id, source, activity_id)
    # 它用来判断进度序号是否有效：latest 为 2 时接受 seq=3；latest 已为 3 时，
    # 重复或迟到的 seq=2 直接忽略。
    active_activities_by_key: dict[
        ActivityNoticeKey, ActiveActivityNoticeState
    ] = field(default_factory=dict)
    # “最近已经结束的活动名单”。
    #  key是(1, "session-a", "cognitive", "cognitive-42") → None
    # 意思是：这个活动在这个运行时世代已经结束了。
    # Activity 编号在一个 Backend 进程内不复用。永久 tombstone 防止极迟事件在近期记录
    # 被淘汰后重新创建 active Notice。
    terminal_activity_keys: set[ActivityNoticeKey] = field(default_factory=set)
    # 中断某世代后抬高此下界，旧子进程事件不得重新写入账本。
    minimum_open_runtime_epoch: int = 1
    # 只在进程内递增，既保证同一创建时刻的稳定排序，也不暴露为领域身份。
    next_notice_number: int = 1
