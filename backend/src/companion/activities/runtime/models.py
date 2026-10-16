"""定义 Activity 运行时对内流转、对外展示所共用的稳定数据模型。

本模块只描述 Activity 事实的形状，不负责推进生命周期、生成故障文案或广播快照。
生命周期变化由 Registry 处理，故障文案由 Failure Reporter 生成，各消费端只读取这里定义的公开载荷。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from companion.activities.work_plan import AgentWorkPlan


# 生命周期状态回答“活动现在处于什么状态”，不能与下方的执行阶段混为一谈。
ActivityStatus = Literal[
    "running",
    "waiting_user",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
# 终态一旦写入便不会重新变回运行态，也不会在产品层复活或重试。
ActivityTerminalStatus = Literal["completed", "failed", "cancelled", "interrupted"]
# 取消入口用明确结果区分“已标记”“已经结束”和“目标不存在”。
ActivityCancellationMarkResult = Literal["marked", "already_terminal", "missing"]
# 来源事件经过应用边界后，必须明确说明是改变事实、重复事件，还是被边界吸收。
ActivityApplyDisposition = Literal["changed", "duplicate", "absorbed"]
# 这是来源适配器允许提交给 Registry 的变化类型，不包含仅供公共展示的状态。
ActivityChangeKind = Literal[
    "started",
    "stage_changed",
    "work_plan_changed",
    "turn_output",
    "waiting_user_entered",
    "waiting_user_resumed",
    "completed",
    "failed",
    "cancelled",
]

# 执行阶段回答“活动正在做什么”；阶段只会向前推进，但允许按实际流程跳过中间阶段。
ActivityStageCode = Literal[
    "preparing",
    "loading_materials",
    "processing",
    "generating_result",
    "delivering_result",
]

# 错误码只表达需要不同用户说明或行动建议的产品故障，不暴露第三方异常类型。
ActivityFailureCode = Literal[
    "configuration_invalid",
    "authentication_failed",
    "rate_limited",
    "provider_unavailable",
    "network_unavailable",
    "request_timed_out",
    "provider_stream_interrupted",
    "invalid_provider_response",
    "material_unavailable",
    "agent_max_turns",
    "agent_execution_error",
    "agent_cli_crash",
    "agent_transport_overflow",
    "runtime_reconfigured",
    "runtime_interrupted",
    "cognitive_task_failed",
    "cognitive_result_protocol_failed",
    "unclassified_error",
]
# 严格解析跨进程载荷时使用该集合，避免接受领域定义以外的任意字符串。
ACTIVITY_FAILURE_CODES: frozenset[str] = frozenset(get_args(ActivityFailureCode))


@dataclass(frozen=True, slots=True)
class ActivityStagePlanEntry:
    """保存一个稳定执行阶段的代码和用户可见名称。

    阶段计划供 Backend、Console 与 Desktop 共用，避免各端自行翻译同一个阶段。

    Args:
        stage_code: 五个稳定阶段之一的代码。
        stage_label: 面向用户的阶段名称。
    """

    stage_code: str
    stage_label: str

    def to_payload(self) -> dict[str, object]:
        """把阶段定义转换成可直接序列化的公共载荷。

        Returns:
            只包含阶段代码与文案的字典。
        """

        return {"stage_code": self.stage_code, "stage_label": self.stage_label}


# 这里是五个稳定执行阶段、顺序和中文名称的唯一权威来源，消费端不得复制第二套定义。
ACTIVITY_STAGE_PLAN: tuple[ActivityStagePlanEntry, ...] = (
    ActivityStagePlanEntry("preparing", "准备任务"),
    ActivityStagePlanEntry("loading_materials", "读取材料"),
    ActivityStagePlanEntry("processing", "分析与处理"),
    ActivityStagePlanEntry("generating_result", "生成结果"),
    ActivityStagePlanEntry("delivering_result", "交付结果"),
)

# 集合用于快速校验输入，映射分别服务于用户文案查询和阶段前进判断。
ACTIVITY_STAGE_CODES: frozenset[str] = frozenset(
    entry.stage_code for entry in ACTIVITY_STAGE_PLAN
)
_ACTIVITY_STAGE_LABELS: dict[str, str] = {
    entry.stage_code: entry.stage_label for entry in ACTIVITY_STAGE_PLAN
}
ACTIVITY_STAGE_ORDER: dict[str, int] = {
    entry.stage_code: index for index, entry in enumerate(ACTIVITY_STAGE_PLAN)
}


def activity_stage_label(stage_code: str) -> str:
    """根据稳定阶段代码查出统一的用户可见名称。

    Args:
        stage_code: 五个稳定阶段之一的代码。

    Returns:
        对应的面向用户阶段名称。

    Raises:
        ValueError: 阶段代码不在稳定阶段计划内。
    """

    label = _ACTIVITY_STAGE_LABELS.get(stage_code)
    if label is None:
        raise ValueError(f"未知 Activity 阶段代码：{stage_code!r}")
    return label


@dataclass(frozen=True, slots=True)
class ActivityStagePeriod:
    """记录活动实际进入过的一个执行阶段及其起止时间。

    阶段仍在进行时没有结束时间；活动前进或进入终态时，Registry 会关闭最后一个开放阶段。

    Args:
        stage_code: 阶段代码。
        stage_label: 阶段用户文案。
        started_at: 带时区的 ISO-8601 阶段开始时间。
        finished_at: 阶段关闭时间；仍在执行时为空。
    """

    stage_code: str
    stage_label: str
    started_at: str
    finished_at: str | None

    def to_payload(self) -> dict[str, object]:
        """把阶段区间转换成可直接序列化的公共载荷。

        Returns:
            阶段时间线元素字典。
        """

        return {
            "stage_code": self.stage_code,
            "stage_label": self.stage_label,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class ActivityTurnOutput:
    """保存后台智能体一轮可见的纯文本回复。

    Args:
        turn_index: 在同一 Activity 内递增的 AssistantMessage 序号。
        content_text: 只由 SDK TextBlock.text 按原顺序连接出的正文。
        created_at: Backend 记录该轮输出的 ISO-8601 时间。
    """

    turn_index: int
    content_text: str
    created_at: str

    def to_payload(self) -> dict[str, object]:
        """转换为 Console 详情接口使用的字典。"""

        return {
            "turn_index": self.turn_index,
            "content_text": self.content_text,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ActivityFailure:
    """保存一次失败或中断可公开展示的结构化故障报告。

    这里只允许出现已经分类和脱敏的信息。原始异常、请求内容、Prompt 与完整堆栈不能进入该模型。

    Args:
        stage_code: 失败发生时所在阶段。
        stage_label: 阶段用户文案。
        code: 稳定产品错误码。
        summary: 普通用户可理解的原因。
        technical_detail: 由白名单字段组装并完成脱敏的技术线索。
        suggested_action: 用户下一步建议。
    """

    stage_code: str
    stage_label: str
    code: ActivityFailureCode
    summary: str
    technical_detail: str
    suggested_action: str

    def to_payload(self) -> dict[str, object]:
        """把故障报告转换成可直接返回给消费端的公共载荷。

        Returns:
            故障报告完整公开字段字典。
        """

        return {
            "stage_code": self.stage_code,
            "stage_label": self.stage_label,
            "code": self.code,
            "summary": self.summary,
            "technical_detail": self.technical_detail,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True, slots=True)
class ActivityCompletionResult:
    """保存成功 Activity 的完整成果、交付摘要和真实文件事实。

    Args:
        display_content: 结果卡和 Activity 详情展示的完整正文。
        delivery_brief: Foreground 自然汇报时读取的中立成果摘要；没有独立摘要时为空。
        output_paths: Backend 扫描并确认存在的交付文件绝对路径。
        deliverable: 本次任务是否向聊天窗口登记了结果卡；未显式声明时按未登记处理，
            Cognitive 结果恒为假。
    """

    display_content: str
    delivery_brief: str
    output_paths: tuple[str, ...]
    deliverable: bool = False


@dataclass(frozen=True, slots=True)
class ActivityCompact:
    """保存 Desktop 与 Console 列表视图共用的活动摘要。

    摘要只包含任务中心展示所需的信息，不暴露内部来源、运行时世代或完整执行结果。

    Args:
        activity_id: 全局唯一活动编号。
        kind: 可扩展的稳定活动种类。
        type_label: 面向用户的活动类型名称。
        status: 当前产品状态。
        goal_summary: 经过长度限制的任务目标摘要。
        failure_summary: 失败或中断的公开摘要；其他状态为空。
        stage_code: 当前或最后执行阶段的稳定代码；无阶段记录时为空。
        stage_label: 当前或最后执行阶段的用户文案；无阶段记录时为空。
        started_at: 带时区的 ISO-8601 开始时间。
        updated_at: 带时区的 ISO-8601 最后更新时间。
        finished_at: 终态时间；活动中为空。
        can_cancel: 当前是否允许提交取消。
    """

    activity_id: str
    kind: str
    type_label: str
    status: ActivityStatus
    goal_summary: str
    failure_summary: str | None
    stage_code: str | None
    stage_label: str | None
    started_at: str | None
    updated_at: str
    finished_at: str | None
    can_cancel: bool

    def __post_init__(self) -> None:
        """确保只有失败和中断状态携带非空失败摘要。

        Returns:
            无返回值。

        Raises:
            ValueError: 失败摘要缺失，或不应携带失败摘要的状态带有该字段。
        """

        requires_summary = self.status in {"failed", "interrupted"}
        has_summary = (
            isinstance(self.failure_summary, str)
            and bool(self.failure_summary.strip())
        )
        if requires_summary and not has_summary:
            raise ValueError("失败或中断 Activity 必须携带失败摘要")
        if not requires_summary and self.failure_summary is not None:
            raise ValueError("Activity 失败摘要与状态不一致")

    def to_payload(self) -> dict[str, object]:
        """把活动摘要转换成可直接提供给消费端的公共载荷。

        Returns:
            不包含内部来源和执行细节的字典。
        """

        return {
            "activity_id": self.activity_id,
            "kind": self.kind,
            "type_label": self.type_label,
            "status": self.status,
            "goal_summary": self.goal_summary,
            "failure_summary": self.failure_summary,
            "stage_code": self.stage_code,
            "stage_label": self.stage_label,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "can_cancel": self.can_cancel,
        }


@dataclass(frozen=True, slots=True)
class ActivityDetail:
    """保存 Console 查询单项活动时需要展示的完整公开详情。

    详情在摘要之外补充完整目标、阶段时间线、结果正文或故障报告，但仍不包含内部路由身份。

    Args:
        compact: 公共活动摘要。
        goal: 用户提出的完整公开目标。
        stage_plan: 五个稳定阶段的有序计划，用于渲染未进入或被跳过的阶段。
        stage_timeline: 实际进入过的阶段时间线。
        result_text: 成功时可公开展示的结果正文；没有结果时为空字符串。
        failure: 失败或中断时的结构化故障报告；其他情况为空。
        failure_reason: Cognitive 自主判定未完成时的安全具体原因；其他情况为空。
        work_plan: Background Agent 自己维护的真实执行计划；没有使用 Task 工具时为空。
        turn_outputs: 后台 Agent 已经产生的纯文本 Turn，按序保留到 Activity 被淘汰。
    """

    compact: ActivityCompact
    goal: str
    stage_plan: tuple[ActivityStagePlanEntry, ...]
    stage_timeline: tuple[ActivityStagePeriod, ...]
    result_text: str
    failure: ActivityFailure | None
    failure_reason: str | None
    work_plan: AgentWorkPlan | None = None
    turn_outputs: tuple[ActivityTurnOutput, ...] = ()

    def __post_init__(self) -> None:
        """确保详情、摘要和故障报告表达的是同一项公共事实。

        Returns:
            无返回值。

        Raises:
            ValueError: 故障报告、具体原因、摘要或失败阶段与活动摘要不一致。
        """

        requires_failure = self.compact.status in {"failed", "interrupted"}
        if requires_failure and self.failure is None:
            raise ValueError("失败或中断 Activity 详情必须携带故障报告")
        if not requires_failure and self.failure is not None:
            raise ValueError("Activity 故障报告与状态不一致")
        requires_failure_reason = (
            self.failure is not None
            and self.failure.code == "cognitive_task_failed"
        )
        has_failure_reason = (
            isinstance(self.failure_reason, str)
            and bool(self.failure_reason.strip())
        )
        if requires_failure_reason != has_failure_reason:
            raise ValueError("Activity 具体失败原因与故障类型不一致")
        if self.failure is None:
            return
        if self.compact.failure_summary != self.failure.summary:
            raise ValueError("Activity 失败摘要与故障报告不一致")
        if (
            self.compact.stage_code != self.failure.stage_code
            or self.compact.stage_label != self.failure.stage_label
        ):
            raise ValueError("Activity 最后阶段与故障报告不一致")

    def to_payload(self) -> dict[str, object]:
        """把摘要与详情字段合并成 Console 详情接口载荷。

        Returns:
            摘要字段与详情正文合并后的字典。
        """

        return {
            **self.compact.to_payload(),
            "goal": self.goal,
            "stage_plan": [entry.to_payload() for entry in self.stage_plan],
            "stage_timeline": [period.to_payload() for period in self.stage_timeline],
            "result_text": self.result_text,
            "failure": self.failure.to_payload() if self.failure is not None else None,
            "failure_reason": self.failure_reason,
            "work_plan": (
                self.work_plan.to_payload() if self.work_plan is not None else None
            ),
            "turn_outputs": [output.to_payload() for output in self.turn_outputs],
        }


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    """保存某个公开版本下完整且自洽的活动列表快照。

    消费端先用 ``runtime_id`` 识别 Backend 是否重启，再用 ``revision`` 丢弃同一进程中的旧快照。

    Args:
        revision: 进程内严格单调递增的公开版本号。
        runtime_id: 本次 Backend 进程的运行编号，用于识别服务重启。
        active: 当前尚未结束的全部活动。
        recent: 按结束时间倒序排列、数量已经过接口裁剪的终态活动。
    """

    revision: int
    runtime_id: str
    active: tuple[ActivityCompact, ...]
    recent: tuple[ActivityCompact, ...]

    def to_payload(self) -> dict[str, object]:
        """把完整快照转换成 HTTP 或 WebSocket 使用的载荷。

        Returns:
            JSON 友好的完整快照。
        """

        return {
            "revision": self.revision,
            "runtime_id": self.runtime_id,
            "active": [activity.to_payload() for activity in self.active],
            "recent": [activity.to_payload() for activity in self.recent],
        }


@dataclass(frozen=True, slots=True)
class ActivityProjection:
    """表示已经写入 Registry、仍等待广播的一次 Activity 变化。

    广播方必须携带提交后的版本号读取快照，避免较早的异步广播覆盖较新的 Activity 事实。

    Args:
        revision: 来源变化提交后的 Registry 版本，用于拒绝回退广播。
    """

    revision: int


@dataclass(frozen=True, slots=True)
class ActivityApplyResult:
    """说明一条来源事件经过 Activity 应用边界后的处理结果。

    ``changed`` 表示公共事实发生变化，``duplicate`` 表示同一事实的幂等重放，
    ``absorbed`` 表示无效、过期或已经由终态墓碑接管的事件。

    Args:
        disposition: 事实已改变、重复吸收或因无效/过期被隔离。
        projection: 事实改变时生成的广播凭据；其他结果为空。
    """

    disposition: ActivityApplyDisposition
    projection: ActivityProjection | None = None

    def __post_init__(self) -> None:
        """确保只有真正改变公共事实的结果携带广播凭据。

        Returns:
            无返回值。

        Raises:
            ValueError: 处理结果与广播凭据的有无不匹配。
        """

        has_projection = self.projection is not None
        if (self.disposition == "changed") != has_projection:
            raise ValueError("Activity 应用结果与投影凭据不一致")


@dataclass(frozen=True, slots=True)
class ActivitySourceChange:
    """保存来源适配器交给 Registry 的标准化 Activity 变化。

    Cognitive 与后台工作的原始事件先在适配器中收窄成该模型，Registry 因而无需理解各来源协议。

    Args:
        change: 活动开始、阶段前进或进入终态的变化类型。
        activity_id: 标准 UUID 活动编号。
        kind: 公共活动种类。
        type_label: 面向用户的类型名称。
        control_source: 把取消命令送回正确子进程执行器的内部来源。
        runtime_epoch: 产生该事实的助手运行时世代，用于隔离热替换后的迟到事件。
        goal: 经过长度限制的用户目标。
        stage: 阶段前进事件的目标阶段；其他事件为空。
        completion_result: 成功终态经过来源边界清洗后的完整结果；其他变化为空。
        failure: 失败或中断终态的公开故障报告；其他情况为空。
        failure_reason: Cognitive 自主判定未完成时的安全具体原因；其他变化为空。
        interaction_request_id: 等待用户变化对应的请求编号；其他变化为空。
        work_plan: 后台智能体确认后的全量计划快照；其他变化为空。
        turn_index: 后台 Agent Turn 的递增编号；其他变化为空。
        content_text: 该 Turn 的纯文本正文；其他变化为空。
        created_at: 该 Turn 的 ISO-8601 时间；其他变化为空。
    """

    change: ActivityChangeKind
    activity_id: str
    kind: str
    type_label: str
    control_source: str
    runtime_epoch: int
    goal: str
    stage: ActivityStageCode | None = None
    completion_result: ActivityCompletionResult | None = None
    failure: ActivityFailure | None = None
    failure_reason: str | None = None
    interaction_request_id: str = ""
    work_plan: AgentWorkPlan | None = None
    turn_index: int | None = None
    content_text: str = ""
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class ActivityCancelTarget:
    """保存已经通过 Registry 校验、可以下发给执行器的取消目标。

    取消命令必须同时匹配活动编号、内部来源和运行时世代，防止热替换后误取消新执行器中的任务。

    Args:
        activity_id: 准备取消的活动编号。
        control_source: 接收取消命令的子进程内部路由来源。
        runtime_epoch: 活动所属助手运行时世代。
    """

    activity_id: str
    control_source: str
    runtime_epoch: int
