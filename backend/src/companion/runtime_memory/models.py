"""定义跨主进程与助手子进程传递的运行期记忆契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal


TaskMemorySource = Literal["cognitive", "background_agent"]
TaskTerminalState = Literal["completed", "failed", "cancelled", "interrupted"]
TaskMemoryBasedOnRelationship = Literal["activity_correction"]
TaskMemoryLookupPurpose = Literal["foreground_recall", "task_derivation"]
TaskRecoveryOutcomeKind = Literal[
    "direct_response",
    "inline_result_available",
    "inline_tool_failed",
    "activity_started",
    "activity_start_failed",
    "tool_rejected",
    "reflection_failed",
]
TaskRecoveryToolStatus = Literal[
    "completed",
    "failed",
    "started",
    "start_failed",
    "rejected",
]


_RECOVERY_TOOL_STATUS_BY_OUTCOME: dict[
    TaskRecoveryOutcomeKind,
    TaskRecoveryToolStatus,
] = {
    "inline_result_available": "completed",
    "inline_tool_failed": "failed",
    "activity_started": "started",
    "activity_start_failed": "start_failed",
    "tool_rejected": "rejected",
}


@dataclass(frozen=True, slots=True)
class TaskRecoveryOutcome:
    """保存一次前台重新判断后经过系统确认的结构化结果。

    Args:
        self_reflection_id: 产生这项结果的前台自我反思编号。
        kind: 恢复最终落到哪一种真实结果。
        safe_summary: 可以进入任务记忆和摘要的自然语言事实。
        tool_name: 真正调用过工具时的工具名。
        tool_status: 工具经过本地提交边界确认后的状态。
        reason_code: 只用于稳定诊断的本地原因码。
    """

    self_reflection_id: str
    kind: TaskRecoveryOutcomeKind
    safe_summary: str
    tool_name: str | None = None
    tool_status: TaskRecoveryToolStatus | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        """规范化安全文本，并拒绝互相矛盾的结果字段。"""

        normalized_reflection_id = (
            self.self_reflection_id.strip()
            if isinstance(self.self_reflection_id, str)
            else ""
        )
        normalized_summary = (
            self.safe_summary.strip() if isinstance(self.safe_summary, str) else ""
        )
        normalized_tool_name = (
            self.tool_name.strip()
            if isinstance(self.tool_name, str) and self.tool_name.strip()
            else None
        )
        normalized_reason_code = (
            self.reason_code.strip()
            if isinstance(self.reason_code, str) and self.reason_code.strip()
            else None
        )
        if not normalized_reflection_id:
            raise ValueError("self_reflection_id 不能为空")
        if not normalized_summary:
            raise ValueError("safe_summary 不能为空")
        if len(normalized_summary) > 2_000:
            raise ValueError("safe_summary 不能超过 2000 个字符")
        if not isinstance(self.kind, str) or self.kind not in {
            "direct_response",
            "inline_result_available",
            "inline_tool_failed",
            "activity_started",
            "activity_start_failed",
            "tool_rejected",
            "reflection_failed",
        }:
            raise ValueError("kind 不是允许的恢复结果")

        expected_tool_status = _RECOVERY_TOOL_STATUS_BY_OUTCOME.get(self.kind)
        if expected_tool_status is None:
            if normalized_tool_name is not None or self.tool_status is not None:
                raise ValueError("没有调用工具的恢复结果不能携带工具事实")
        elif (
            normalized_tool_name is None
            or self.tool_status != expected_tool_status
        ):
            raise ValueError("恢复结果与工具状态不匹配")
        if self.kind == "reflection_failed" and normalized_reason_code is None:
            raise ValueError("reflection_failed 必须提供 reason_code")

        object.__setattr__(self, "self_reflection_id", normalized_reflection_id)
        object.__setattr__(self, "safe_summary", normalized_summary)
        object.__setattr__(self, "tool_name", normalized_tool_name)
        object.__setattr__(self, "reason_code", normalized_reason_code)

    @property
    def establishes_activity(self) -> bool:
        """判断结果是否已经建立一项真实后台 Activity。

        Returns:
            结果为已开始时返回真。
        """

        return self.kind == "activity_started"

    def to_payload(self) -> dict[str, object]:
        """转换成不共享可变状态的结构化任务记忆载荷。

        Returns:
            可供召回工具和测试读取的恢复事实字典。
        """

        return {
            "self_reflection_id": self.self_reflection_id,
            "kind": self.kind,
            "safe_summary": self.safe_summary,
            "tool_name": self.tool_name,
            "tool_status": self.tool_status,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class RuntimeMemoryMessage:
    """保存一条由主进程编号的 Chat-like 历史消息。

    Args:
        message_id: Backend 进程内单调递增的消息编号。
        role: Chat-like 消息角色。
        content: 消息正文。
        token_count: 当前消息的估算 Token 数。
        created_at: 这条消息真实发生的本地时间。
        tool_calls: assistant 消息携带的工具调用。
        tool_call_id: tool 消息对应的工具调用编号。
        llm_context_note: 只给 Foreground 看的安全事实附注。
        metadata: 不进入 Prompt 的运行期归因字段。
    """

    message_id: int
    role: str
    content: str
    token_count: int
    created_at: datetime
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None
    llm_context_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_llm_message(self) -> dict[str, Any]:
        """转换为 Foreground 客户端使用的 Chat-like 消息。

        Returns:
            保留正文、附注和工具协议字段的独立字典。
        """

        note = self.llm_context_note.strip()
        content = f"{self.content}\n\n{note}" if note else self.content
        message: dict[str, Any] = {"role": self.role, "content": content}
        if self.tool_calls:
            message["tool_calls"] = [dict(item) for item in self.tool_calls]
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        return message

    def to_payload(self) -> dict[str, Any]:
        """转换成可跨进程序列化的载荷。

        Returns:
            不共享可变容器的消息字典。
        """

        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "token_count": self.token_count,
            "created_at": self.created_at.isoformat(),
            "tool_calls": [dict(item) for item in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "llm_context_note": self.llm_context_note,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimeMemorySnapshot:
    """冻结一次助手请求允许读取的运行期记忆。

    Args:
        memory_epoch: 主进程记忆在冻结时的版本号。
        rolling_summary: 已成功压缩的对话摘要。
        summary_covered_from: 当前摘要覆盖的首个本地自然日。
        summary_covered_to: 当前摘要覆盖的最后一个本地自然日。
        recent_messages: 尚未被摘要覆盖的协议完整消息。
        summary_source_message_id: 当前摘要覆盖到的最后消息编号。
        context_token_count: 摘要和近期消息的估算 Token 总数。
        latest_message_id: 冻结时已经分配的最大消息编号。
    """

    memory_epoch: int = 0
    rolling_summary: str = ""
    summary_covered_from: date | None = None
    summary_covered_to: date | None = None
    recent_messages: tuple[RuntimeMemoryMessage, ...] = ()
    summary_source_message_id: int | None = None
    context_token_count: int = 0
    latest_message_id: int = 0


@dataclass(frozen=True, slots=True)
class MemoryCommit:
    """描述助手 generation 希望写回主进程的历史增量。

    Args:
        commit_id: 本次提交的幂等编号。
        request_id: 产生这些消息的助手请求编号。
        base_memory_epoch: 子进程读取快照时的主进程版本。
        messages: 按真实协议顺序追加的无权威编号消息。
    """

    commit_id: str
    request_id: str
    base_memory_epoch: int
    messages: tuple[dict[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        """转换为 RuntimeEvent 使用的 JSON 友好字典。

        Returns:
            包含提交身份和消息副本的字典。
        """

        return {
            "commit_id": self.commit_id,
            "request_id": self.request_id,
            "base_memory_epoch": self.base_memory_epoch,
            "messages": [dict(message) for message in self.messages],
        }


@dataclass(frozen=True, slots=True)
class RuntimeMemoryCompactionPlan:
    """冻结一笔由主进程授权的滚动摘要计划。

    Args:
        memory_epoch: 主进程创建计划时的记忆版本。
        plan_id: 本次计划的幂等编号。
        source_message_id: 摘要允许覆盖到的最后消息编号。
        owner_runtime_epoch: 实际接管这笔计划的助手运行时世代。
        covered_from: 摘要覆盖范围的首个本地自然日。
        covered_to: 摘要覆盖范围的最后一个本地自然日。
        previous_summary: 上一轮已经生效的滚动摘要。
        messages_to_summarize: 本次需要送入 Summary 模型的协议完整前缀。
    """

    memory_epoch: int
    plan_id: str
    source_message_id: int
    owner_runtime_epoch: int
    covered_from: date
    covered_to: date
    previous_summary: str
    messages_to_summarize: tuple[RuntimeMemoryMessage, ...]


@dataclass(slots=True)
class TaskMemoryRecord:
    """保存一项已结束任务的短记忆和可按编号召回的详细事实。

    Args:
        activity_id: Activity 的稳定编号。
        origin_tool_call_ids: 首次创建任务的 Foreground 工具调用编号。
        source: 认知活动或后台工作。
        goal: 当时实际执行的目标。
        terminal_state: 首个权威终态。
        cancellation_reason: cancelled 终态的稳定原因；其它终态为空。
        outcome_brief: 可以长期放进短历史的结果概况。
        based_on_activity_id: 派生任务的父任务编号。
        based_on_relationship: 当前任务与父任务之间的特殊关系；普通派生为空。
        recovery_of_activity_id: 当前任务是为恢复哪项失败任务而建立的。
        recovered_by_activity_id: 当前失败任务后来由哪项新 Activity 恢复。
        recovery_outcome: 前台重新判断后经过系统确认的结构化结果。
        full_result: 完整结果或安全失败事实。
        output_paths: 后台工作实际确认的输出路径。
        safe_interactions: 影响任务结果的安全权限或资料事实。
        input_snapshot: 可供派生任务复用的实际输入快照。
        terminal_message_id: 对应 task_memory 系统消息编号。
        last_referenced_message_id: 最近一次召回或派生引用对应的消息编号。
    """

    activity_id: str
    origin_tool_call_ids: tuple[str, ...]
    source: TaskMemorySource
    goal: str
    terminal_state: TaskTerminalState
    outcome_brief: str
    cancellation_reason: str | None = None
    based_on_activity_id: str | None = None
    based_on_relationship: TaskMemoryBasedOnRelationship | None = None
    recovery_of_activity_id: str | None = None
    recovered_by_activity_id: str | None = None
    recovery_outcome: TaskRecoveryOutcome | None = None
    full_result: str = ""
    output_paths: tuple[str, ...] = ()
    safe_interactions: tuple[dict[str, Any], ...] = ()
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    terminal_message_id: int | None = None
    last_referenced_message_id: int | None = None
