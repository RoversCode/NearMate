"""定义前台自我反思跨线程传递的不可变业务事实。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING, Literal, TypeAlias

from companion.activities.runtime.models import ACTIVITY_FAILURE_CODES

if TYPE_CHECKING:
    from companion.foreground_self_reflection.contracts import (
        AssistantRequestAdmissionLeaseProtocol,
    )


SELF_REFLECTION_DECISION_BUDGET_SECONDS = 45.0

ActivityTerminalState: TypeAlias = Literal[
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "rejected",
]
FinalInputSource: TypeAlias = Literal["text", "final_asr"]
AdmissionStatus: TypeAlias = Literal["admitted", "duplicate", "rejected"]
TransitionStatus: TypeAlias = Literal[
    "applied",
    "duplicate",
    "rejected",
    "missing",
]
InputDiversionStatus: TypeAlias = Literal[
    "accepted",
    "duplicate",
    "not_diverted",
]
FinalTranscriptHistoryDisposition: TypeAlias = Literal[
    "ordinary_turn",
    "unattended_episode",
]
ExpressionSettlementDisposition: TypeAlias = Literal[
    "committed",
    "ignored",
    "retryable",
]
SelfReflectionToolExecutionChannel: TypeAlias = Literal["inline", "background"]
PendingRecoverySubmissionStatus: TypeAlias = Literal[
    "preparing",
    "entry_output_accepted",
    "recovery_output_accepted",
]


class SelfReflectionState(StrEnum):
    """列出单次自我反思只允许向前推进的状态。"""

    ACQUIRING = "acquiring"
    REFLECTING = "reflecting"
    COMMITTING = "committing"
    COMMITTED = "committed"
    FINALIZING = "finalizing"
    CLOSING_INPUTS = "closing_inputs"
    CLOSED = "closed"
    CLOSED_FAILED = "closed_failed"
    TERMINATED = "terminated"


class SelfReflectionOutcomeKind(StrEnum):
    """列出最终恢复表达可以依赖的冻结事实类型。"""

    DIRECT_RESPONSE = "direct_response"
    INLINE_RESULT_AVAILABLE = "inline_result_available"
    INLINE_TOOL_FAILED = "inline_tool_failed"
    ACTIVITY_STARTED = "activity_started"
    ACTIVITY_START_FAILED = "activity_start_failed"
    TOOL_REJECTED = "tool_rejected"
    REFLECTION_FAILED = "reflection_failed"


class SelfReflectionToolStatus(StrEnum):
    """列出恢复工具经过系统边界确认后的真实状态。"""

    COMPLETED = "completed"
    FAILED = "failed"
    STARTED = "started"
    START_FAILED = "start_failed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SelfReflectionToolCommitIdentity:
    """冻结一次工具提交在主进程授权时必须核对的完整身份。

    Args:
        commit_request_id: 子进程为同步授权生成的稳定请求编号。
        self_reflection_id: 本次自我反思编号。
        source_activity_id: 被恢复的 Cognitive Activity 编号。
        runtime_epoch: 请求所属 Assistant Runtime 世代。
        tool_call_id: 模型产生的唯一工具调用编号。
        tool_name: 本次实际选择的白名单工具名。
        execution_channel: 工具在子进程走 Inline 还是 Background 提交路径。
        canonical_payload_fingerprint: Background canonical 载荷的稳定指纹；
            Inline 尚无完成事实时固定为空。
    """

    commit_request_id: str
    self_reflection_id: str
    source_activity_id: str
    runtime_epoch: int
    tool_call_id: str
    tool_name: str
    execution_channel: SelfReflectionToolExecutionChannel
    canonical_payload_fingerprint: str

    def __post_init__(self) -> None:
        """规范化身份，并拒绝无法安全比较的半完整提交请求。"""

        _normalize_required_text(
            self,
            "commit_request_id",
            "self_reflection_id",
            "source_activity_id",
            "tool_call_id",
            "tool_name",
        )
        _validate_runtime_epoch(self.runtime_epoch)
        if self.execution_channel not in {"inline", "background"}:
            raise ValueError("execution_channel 不是允许的工具执行通道")
        fingerprint = self.canonical_payload_fingerprint.strip()
        if self.execution_channel == "background" and not fingerprint:
            raise ValueError("Background 提交必须携带 canonical 载荷指纹")
        if self.execution_channel == "inline" and fingerprint:
            raise ValueError("Inline 授权不能提前携带 canonical 载荷指纹")
        object.__setattr__(self, "canonical_payload_fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class CognitiveRecoverySignal:
    """保存恢复资格策略需要查看的最小 Cognitive 终态事实。

    Args:
        runtime_epoch: 来源 Assistant Runtime 的世代。
        source_activity_id: 来源 Cognitive Activity 编号。
        source_request_id: 创建来源 Activity 的 Foreground 请求编号。
        source_protocol_transaction_id: 来源 Cognitive 工具事务编号。
        activity_started: 是否已经发布真实的 Cognitive started 事实。
        terminal_state: Activity 的执行终态。
        failure_code: Activity 执行器归一化后的系统错误码。
    """

    runtime_epoch: int
    source_activity_id: str
    source_request_id: str
    source_protocol_transaction_id: str
    activity_started: bool
    terminal_state: ActivityTerminalState
    failure_code: str | None

    def __post_init__(self) -> None:
        """规范化身份并拒绝无法安全归属的终态事实。"""

        _normalize_required_text(
            self,
            "source_activity_id",
            "source_request_id",
            "source_protocol_transaction_id",
        )
        _validate_runtime_epoch(self.runtime_epoch)
        if not isinstance(self.activity_started, bool):
            raise ValueError("activity_started 必须是布尔值")
        if self.terminal_state not in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "rejected",
        }:
            raise ValueError("terminal_state 不是允许的 Activity 终态")
        if (
            self.failure_code is not None
            and self.failure_code not in ACTIVITY_FAILURE_CODES
        ):
            raise ValueError("failure_code 不是允许的 Activity 错误码")


@dataclass(frozen=True, slots=True)
class CognitiveRecoveryDecision:
    """描述一条 Cognitive 终态是否具有一次恢复资格。

    Args:
        eligible: 是否允许尝试取得前台注意力。
        reason: 本地日志和分支使用的稳定原因。
    """

    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PendingRecovery:
    """保存 Cognitive 业务失败到接管结果确定之间的短时事实。

    这不是新的 Attention 状态。它只让输入路由和接管事务共享同一份来源、
    前台输出身份和提交进度，接管成功或失败后立即删除。

    Args:
        source_activity_id: 触发恢复的 Cognitive Activity 编号。
        runtime_epoch: 失败与接管所属的 Assistant Runtime 世代。
        safe_task_target: 从 Cognitive 任务目标整理出的短标签。
        untrusted_failure_reason: 模型生成的失败说明，只能作为 Recovery 的引用数据。
        interrupted_request_id: 接管时仍在输出的普通 Foreground 请求编号。
        interrupted_generation_id: 接管时已知的普通 Foreground generation 编号。
        submission_status: 接管组合提交目前推进到的实现层阶段。
    """

    source_activity_id: str
    runtime_epoch: int
    safe_task_target: str
    untrusted_failure_reason: str
    interrupted_request_id: str | None = None
    interrupted_generation_id: str | None = None
    submission_status: PendingRecoverySubmissionStatus = "preparing"

    def __post_init__(self) -> None:
        """规范化接管事实并拒绝无法稳定归属的半完整记录。"""

        _normalize_required_text(self, "source_activity_id", "safe_task_target")
        _validate_runtime_epoch(self.runtime_epoch)
        object.__setattr__(
            self,
            "untrusted_failure_reason",
            self.untrusted_failure_reason.strip(),
        )
        object.__setattr__(
            self,
            "interrupted_request_id",
            _normalize_optional_text(self.interrupted_request_id),
        )
        object.__setattr__(
            self,
            "interrupted_generation_id",
            _normalize_optional_text(self.interrupted_generation_id),
        )
        if self.submission_status not in {
            "preparing",
            "entry_output_accepted",
            "recovery_output_accepted",
        }:
            raise ValueError("submission_status 不是允许的接管提交阶段")


@dataclass(frozen=True, slots=True)
class SelfReflectionAdmissionCommand:
    """保存主进程尝试取得唯一前台注意力所需的来源身份。

    Args:
        runtime_epoch: 来源 Assistant Runtime 的世代。
        source_activity_id: 失败 Cognitive Activity 编号。
        source_request_id: 创建来源 Activity 的 Foreground 请求编号。
        source_protocol_transaction_id: 来源 Cognitive 工具事务编号。
    """

    runtime_epoch: int
    source_activity_id: str
    source_request_id: str
    source_protocol_transaction_id: str

    def __post_init__(self) -> None:
        """规范化准入身份并拒绝无效世代。"""

        _normalize_required_text(
            self,
            "source_activity_id",
            "source_request_id",
            "source_protocol_transaction_id",
        )
        _validate_runtime_epoch(self.runtime_epoch)


@dataclass(frozen=True, slots=True)
class UnattendedMaterialReference:
    """保存未处理输入材料中允许长期回忆的安全名称和类型。

    Args:
        file_name: 不含目录的安全文件名。
        media_type: 材料的媒体类型，例如 ``audio/wav``。
    """

    file_name: str
    media_type: str

    def __post_init__(self) -> None:
        """拒绝路径和空类型，避免片段泄漏本机目录。"""

        _normalize_required_text(self, "file_name", "media_type")
        if "/" in self.file_name or "\\" in self.file_name:
            raise ValueError("file_name 只能包含文件名，不能包含路径")
        if self.file_name in {".", ".."}:
            raise ValueError("file_name 不是有效文件名")


@dataclass(frozen=True, slots=True)
class DivertFinalInputCommand:
    """保存 Attention active 时需要改道的一项 Final 用户输入。

    Args:
        runtime_epoch: 输入越过提交边界时所属的运行时世代。
        input_identity: 原 Text submission 或 Final ASR turn 的稳定身份。
        assistant_request_id: 普通请求若胜出时将进入 Assistant Bridge 的身份。
        source: 输入来自文本还是 Final ASR。
        text: Desktop 已确认显示的真实用户正文。
        materials: 只含安全文件名和媒体类型的材料引用。
    """

    runtime_epoch: int
    input_identity: str
    assistant_request_id: str
    source: FinalInputSource
    text: str
    materials: tuple[UnattendedMaterialReference, ...] = ()

    def __post_init__(self) -> None:
        """规范化稳定身份和正文，并拒绝未知输入来源。"""

        _normalize_required_text(
            self,
            "input_identity",
            "assistant_request_id",
            "text",
        )
        _validate_runtime_epoch(self.runtime_epoch)
        if self.source not in {"text", "final_asr"}:
            raise ValueError("source 只允许 text 或 final_asr")
        object.__setattr__(self, "materials", tuple(self.materials))
        if not all(
            isinstance(item, UnattendedMaterialReference)
            for item in self.materials
        ):
            raise ValueError("materials 必须是安全材料引用")


@dataclass(frozen=True, slots=True)
class UnattendedInputRecord:
    """保存一项已经由主进程线性接纳的未处理输入。

    Args:
        input_identity: 原输入稳定身份。
        source: 输入来自文本还是 Final ASR。
        text: 当时没有处理的真实正文。
        materials: 可供未来回忆的安全材料名称与类型。
        episode_sequence: 本进程按锁顺序分配的单调序号。
    """

    input_identity: str
    source: FinalInputSource
    text: str
    materials: tuple[UnattendedMaterialReference, ...]
    episode_sequence: int


@dataclass(frozen=True, slots=True)
class UnattendedEpisodeSnapshot:
    """冻结一轮状态关闭时需要原子写入 Runtime Memory 的片段。

    Args:
        episode_id: 未处理插话片段编号。
        self_reflection_id: 所属自我反思编号。
        runtime_epoch: 所属运行时世代。
        inputs: 按主进程锁顺序排列的输入。
        actionable: 固定为假，表示内容不是待办和动作授权。
    """

    episode_id: str
    self_reflection_id: str
    runtime_epoch: int
    inputs: tuple[UnattendedInputRecord, ...]
    actionable: Literal[False] = False


@dataclass(frozen=True, slots=True)
class SelfReflectionToolFact:
    """保存一次恢复工具经过系统校验后的真实结果。

    Args:
        tool_name: 实际执行的已注册工具名。
        status: Inline 结果或异步提交的 canonical 状态。
        safe_result: 可供最终表达读取的安全结果说明。
        activity_id: 后台任务已经建立时的 Activity 编号。
    """

    tool_name: str
    status: SelfReflectionToolStatus
    safe_result: str
    activity_id: str | None = None

    def __post_init__(self) -> None:
        """规范化真实工具事实并拒绝缺少后台身份的成功提交。"""

        _normalize_required_text(self, "tool_name", "safe_result")
        if not isinstance(self.status, SelfReflectionToolStatus):
            raise ValueError("status 不是允许的恢复工具状态")
        normalized_activity_id = _normalize_optional_text(self.activity_id)
        object.__setattr__(self, "activity_id", normalized_activity_id)
        if (
            self.status is SelfReflectionToolStatus.STARTED
            and normalized_activity_id is None
        ):
            raise ValueError("后台工具已接纳时必须提供 activity_id")

    def to_payload(self) -> dict[str, object]:
        """转换成不共享可变状态的事件载荷。

        Returns:
            包含工具名、真实状态、安全结果和可选 Activity 身份的字典。
        """

        return {
            "tool_name": self.tool_name,
            "status": self.status.value,
            "safe_result": self.safe_result,
            "activity_id": self.activity_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> SelfReflectionToolFact:
        """从运行时事件重建已经过子进程校验的工具事实。

        Args:
            payload: `foreground.self_reflection_outcome` 中的工具事实对象。

        Returns:
            通过同一数据类不变量校验的不可变工具事实。

        Raises:
            ValueError: 载荷不是完整的工具事实。
        """

        if not isinstance(payload, dict):
            raise ValueError("tool_fact 必须是对象")
        raw_status = payload.get("status")
        try:
            status = SelfReflectionToolStatus(raw_status)
        except ValueError as exception:
            raise ValueError("tool_fact.status 不是允许的状态") from exception
        activity_id = payload.get("activity_id")
        if activity_id is not None and not isinstance(activity_id, str):
            raise ValueError("tool_fact.activity_id 必须是字符串或空")
        return cls(
            tool_name=payload.get("tool_name", ""),
            status=status,
            safe_result=payload.get("safe_result", ""),
            activity_id=activity_id,
        )


_OUTCOME_TOOL_STATUS = {
    SelfReflectionOutcomeKind.INLINE_RESULT_AVAILABLE: SelfReflectionToolStatus.COMPLETED,
    SelfReflectionOutcomeKind.INLINE_TOOL_FAILED: SelfReflectionToolStatus.FAILED,
    SelfReflectionOutcomeKind.ACTIVITY_STARTED: SelfReflectionToolStatus.STARTED,
    SelfReflectionOutcomeKind.ACTIVITY_START_FAILED: SelfReflectionToolStatus.START_FAILED,
    SelfReflectionOutcomeKind.TOOL_REJECTED: SelfReflectionToolStatus.REJECTED,
}


@dataclass(frozen=True, slots=True)
class SelfReflectionOutcomeFact:
    """保存可见 Recovery 正文之外的机械执行结果。

    Args:
        self_reflection_id: 产生结果的自我反思编号。
        source_activity_id: 被恢复的 Cognitive Activity 编号。
        kind: 最终表达可以依赖的受限结果类型。
        safe_message: 恢复失败时可以进入任务记忆的安全自然语言事实。
        tool_fact: 只有真实调用工具时存在的系统结果。
        reason: 本地诊断使用的稳定收口原因。
    """

    self_reflection_id: str
    source_activity_id: str
    kind: SelfReflectionOutcomeKind
    safe_message: str = ""
    tool_fact: SelfReflectionToolFact | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        """验证结果类型与工具事实严格一一对应。"""

        _normalize_required_text(
            self,
            "self_reflection_id",
            "source_activity_id",
        )
        if not isinstance(self.kind, SelfReflectionOutcomeKind):
            raise ValueError("kind 不是允许的自我反思结果")
        object.__setattr__(self, "safe_message", self.safe_message.strip())
        object.__setattr__(self, "reason", self.reason.strip())
        expected_status = _OUTCOME_TOOL_STATUS.get(self.kind)
        if expected_status is not None:
            if self.tool_fact is None or self.tool_fact.status is not expected_status:
                raise ValueError("结果类型与真实工具状态不匹配")
            if self.safe_message:
                raise ValueError("工具结果不能再保存模型自由说明")
            return
        if self.tool_fact is not None:
            raise ValueError("直接回复或恢复失败结果不能携带工具事实")
        if self.kind is SelfReflectionOutcomeKind.DIRECT_RESPONSE:
            if self.safe_message or self.reason:
                raise ValueError("直接回复只记录机械完成事实")
            return
        if not self.safe_message:
            raise ValueError("恢复失败结果必须提供安全事实")

    @classmethod
    def direct_response(
        cls,
        *,
        self_reflection_id: str,
        source_activity_id: str,
    ) -> SelfReflectionOutcomeFact:
        """构造不复制模型正文的直接回复完成事实。

        Args:
            self_reflection_id: 当前自我反思编号。
            source_activity_id: 被恢复的 Cognitive Activity 编号。

        Returns:
            只表示可见 Recovery 已正常结算的机械事实。
        """

        return cls(
            self_reflection_id=self_reflection_id,
            source_activity_id=source_activity_id,
            kind=SelfReflectionOutcomeKind.DIRECT_RESPONSE,
        )

    @classmethod
    def failed(
        cls,
        *,
        self_reflection_id: str,
        source_activity_id: str,
        reason: str,
    ) -> SelfReflectionOutcomeFact:
        """构造不含异常正文的稳定恢复失败事实。

        Args:
            self_reflection_id: 当前自我反思编号。
            source_activity_id: 被恢复的 Cognitive Activity 编号。
            reason: 本地稳定失败码。

        Returns:
            只说明重新梳理未完成的失败事实。
        """

        normalized_reason = reason.strip() if isinstance(reason, str) else ""
        if not normalized_reason:
            raise ValueError("reason 不能为空")
        return cls(
            self_reflection_id=self_reflection_id,
            source_activity_id=source_activity_id,
            kind=SelfReflectionOutcomeKind.REFLECTION_FAILED,
            safe_message="重新梳理没有成功完成。",
            reason=normalized_reason,
        )

    @property
    def recovered_by_activity_id(self) -> str | None:
        """返回恢复后真实建立的后台 Activity 编号。

        Returns:
            后台任务已开始时返回 Activity 编号，其它结果返回空。
        """

        if self.tool_fact is None:
            return None
        if self.tool_fact.status is not SelfReflectionToolStatus.STARTED:
            return None
        return self.tool_fact.activity_id

    def to_payload(self) -> dict[str, object]:
        """转换成主进程可以严格重建的事件载荷。

        Returns:
            不包含 Thinking、原始模型正文或供应商诊断的结构化字典。
        """

        return {
            "self_reflection_id": self.self_reflection_id,
            "source_activity_id": self.source_activity_id,
            "kind": self.kind.value,
            "safe_message": self.safe_message,
            "tool_fact": (
                self.tool_fact.to_payload()
                if self.tool_fact is not None
                else None
            ),
            "reason": self.reason,
            "recovered_by_activity_id": self.recovered_by_activity_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> SelfReflectionOutcomeFact:
        """从主进程收到的事件载荷重建结构化恢复结果。

        Args:
            payload: `foreground.self_reflection_outcome` 事件载荷。

        Returns:
            通过结果与工具状态一致性校验的恢复事实。

        Raises:
            ValueError: 载荷缺字段、枚举非法或工具事实不匹配。
        """

        if not isinstance(payload, dict):
            raise ValueError("自我反思结果必须是对象")
        raw_kind = payload.get("kind")
        try:
            kind = SelfReflectionOutcomeKind(raw_kind)
        except ValueError as exception:
            raise ValueError("kind 不是允许的自我反思结果") from exception
        raw_tool_fact = payload.get("tool_fact")
        tool_fact = (
            SelfReflectionToolFact.from_payload(raw_tool_fact)
            if raw_tool_fact is not None
            else None
        )
        return cls(
            self_reflection_id=payload.get("self_reflection_id", ""),
            source_activity_id=payload.get("source_activity_id", ""),
            kind=kind,
            safe_message=payload.get("safe_message", ""),
            tool_fact=tool_fact,
            reason=payload.get("reason", ""),
        )


@dataclass(frozen=True, slots=True)
class SelfReflectionSnapshot:
    """提供一项自我反思在某个完整时刻的只读状态。

    Args:
        self_reflection_id: 本次自我反思编号。
        episode_id: 本轮未处理插话片段编号。
        runtime_epoch: 所属 Assistant Runtime 世代。
        source_activity_id: 失败 Cognitive Activity 编号。
        source_request_id: 创建来源 Activity 的 Foreground 请求编号。
        source_protocol_transaction_id: 来源工具事务编号。
        state: 当前单向状态。
        reason: 最近一次状态转换的稳定原因。
        admitted_at: Attention Admission 的单调时间。
        deadline: 从 Admission 起固定 45 秒的绝对截止时间。
        interruption_count: 本轮已经接纳的 Final 输入数。
        input_count: 当前片段已经保存的输入数。
        outcome: 决策完成后冻结的结构化恢复结果。
        tool_commit_identity: 已经原子授权的唯一工具提交身份；尚未选工具时为空。
    """

    self_reflection_id: str
    episode_id: str
    runtime_epoch: int
    source_activity_id: str
    source_request_id: str
    source_protocol_transaction_id: str
    state: SelfReflectionState
    reason: str
    admitted_at: float
    deadline: float
    interruption_count: int
    input_count: int
    outcome: SelfReflectionOutcomeFact | None
    tool_commit_identity: SelfReflectionToolCommitIdentity | None = None


@dataclass(frozen=True, slots=True)
class SelfReflectionAdmissionResult:
    """描述一次 Attention Admission 是否取得唯一所有权。

    Args:
        status: 首次准入、幂等重复或拒绝。
        reason: 本地稳定原因。
        snapshot: 成功或重复时的状态快照。
    """

    status: AdmissionStatus
    reason: str
    snapshot: SelfReflectionSnapshot | None = None


@dataclass(frozen=True, slots=True)
class SelfReflectionTransitionResult:
    """描述一次状态转换及其需要提交的 episode 快照。

    Args:
        status: 首次应用、幂等重复、拒绝或记录不存在。
        reason: 本地稳定原因。
        snapshot: 操作后的完整状态快照。
        episode: 开始关闭或热替换时冻结的未处理片段。
    """

    status: TransitionStatus
    reason: str
    snapshot: SelfReflectionSnapshot | None = None
    episode: UnattendedEpisodeSnapshot | None = None


@dataclass(frozen=True, slots=True)
class InputDiversionResult:
    """描述一项 Final 输入归入 episode 或继续普通提交的唯一裁决。

    Args:
        status: 首次接纳、幂等重复或不属于当前状态。
        reason: 本地稳定原因。
        accepted_input: 首次或重复接纳时的冻结输入。
        snapshot: 操作后的自我反思快照。
        episode_id: 输入归入的 episode 编号；普通 Turn 为空。
        history_disposition: 公开转写应该立即入稳定历史还是只做临时显示。
        interruption_reply_eligible: 当前输入能否触发一条辅助打扰回应。
        runtime_epoch: 做出本次归属裁决时观察到的 Assistant Runtime 世代。
        assistant_request_lease: 没有 Attention 时允许普通请求继续使用的提交资格。
    """

    status: InputDiversionStatus
    reason: str
    accepted_input: UnattendedInputRecord | None = None
    snapshot: SelfReflectionSnapshot | None = None
    episode_id: str | None = None
    history_disposition: FinalTranscriptHistoryDisposition = "ordinary_turn"
    interruption_reply_eligible: bool = False
    runtime_epoch: int | None = None
    assistant_request_lease: AssistantRequestAdmissionLeaseProtocol | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """校验历史归属和 episode 身份始终互相一致。"""

        if self.runtime_epoch is not None and (
            type(self.runtime_epoch) is not int or self.runtime_epoch <= 0
        ):
            raise ValueError("runtime_epoch 必须是大于零的整数")
        if self.history_disposition == "unattended_episode":
            if not isinstance(self.episode_id, str) or not self.episode_id.strip():
                raise ValueError("unattended_episode 必须携带 episode_id")
            object.__setattr__(self, "episode_id", self.episode_id.strip())
            return
        if self.episode_id is not None:
            raise ValueError("ordinary_turn 不能携带 episode_id")
        if self.interruption_reply_eligible:
            raise ValueError("ordinary_turn 不能允许打扰回应")

    @property
    def diverted(self) -> bool:
        """判断输入是否已经完整进入 Episode。

        Returns:
            首次接纳或幂等重复时返回真。
        """

        return self.status in {"accepted", "duplicate"}


def _normalize_required_text(instance: object, *field_names: str) -> None:
    """规范化冻结数据类中的必填字符串。

    Args:
        instance: 正在执行 ``__post_init__`` 的冻结数据类。
        field_names: 必须是非空字符串的字段名。
    """

    for field_name in field_names:
        value = getattr(instance, field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} 不能为空")
        object.__setattr__(instance, field_name, value.strip())


def _normalize_optional_text(value: str | None) -> str | None:
    """把可选字符串统一成裁剪后的正文或空。

    Args:
        value: 原始可选字符串。

    Returns:
        有正文时返回裁剪结果，否则返回空。
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("可选文本必须是字符串或空")
    return value.strip() or None


def _validate_runtime_epoch(runtime_epoch: int) -> None:
    """验证运行时世代能够稳定参与身份比较。

    Args:
        runtime_epoch: 待验证的运行时世代。

    Raises:
        ValueError: 世代不是大于零的整数。
    """

    if type(runtime_epoch) is not int or runtime_epoch <= 0:
        raise ValueError("runtime_epoch 必须是大于零的整数")


def validate_monotonic_time(value: float, *, field_name: str) -> float:
    """验证单调时钟值是有限数字。

    Args:
        value: 单调时钟返回值。
        field_name: 错误信息中使用的字段名。

    Returns:
        转换后的浮点数。

    Raises:
        ValueError: 值不是有限数字。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} 必须是有限数字")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError(f"{field_name} 必须是有限数字")
    return normalized
