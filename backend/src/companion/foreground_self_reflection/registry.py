"""在主进程单锁内管理前台注意力、输入分流与单向终态。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock, RLock
from time import monotonic
from uuid import uuid4

from companion.foreground_self_reflection.models import (
    SELF_REFLECTION_DECISION_BUDGET_SECONDS,
    DivertFinalInputCommand,
    InputDiversionResult,
    SelfReflectionAdmissionCommand,
    SelfReflectionAdmissionResult,
    SelfReflectionOutcomeFact,
    SelfReflectionOutcomeKind,
    SelfReflectionSnapshot,
    SelfReflectionState,
    SelfReflectionToolCommitIdentity,
    SelfReflectionTransitionResult,
    UnattendedEpisodeSnapshot,
    UnattendedInputRecord,
    validate_monotonic_time,
)


_DEADLINE_STATES = frozenset(
    {
        SelfReflectionState.ACQUIRING,
        SelfReflectionState.REFLECTING,
    }
)
_DECISION_STATES = _DEADLINE_STATES | {
    SelfReflectionState.COMMITTING,
    SelfReflectionState.COMMITTED,
    SelfReflectionState.FINALIZING,
}
_ATTENTION_STATES = _DECISION_STATES | {SelfReflectionState.CLOSING_INPUTS}
_TERMINAL_STATES = frozenset(
    {
        SelfReflectionState.CLOSED,
        SelfReflectionState.CLOSED_FAILED,
        SelfReflectionState.TERMINATED,
    }
)
_SPECIAL_REQUEST_SOURCES = frozenset(
    {
        "self_reflection_entry",
        "self_reflection_recovery",
        "self_reflection_interruption_reply",
    }
)
_ORDINARY_REQUEST_SOURCES = frozenset(
    {
        "text_input",
        "final_asr",
        "system_notice",
        "timed_reminder_notice",
    }
)


@dataclass(slots=True)
class AssistantRequestAdmissionLease:
    """把普通助手请求的 Attention 资格结算成已入队或已回滚。

    Args:
        request_id: 即将进入 Assistant Bridge 的稳定请求编号。
        runtime_epoch: 请求所属 Assistant Runtime 世代。
        request_source: Text、Final ASR 或 System Notice 来源。
        coordinator: 持有 Attention 单锁账本的协调器。
    """

    request_id: str
    runtime_epoch: int
    request_source: str
    coordinator: ForegroundAttentionCoordinator
    _state: str = field(default="prepared", init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def state(self) -> str:
        """返回该资格仍待提交、已经入队或已经回滚。"""

        with self._lock:
            return self._state

    def commit_enqueued(self) -> bool:
        """请求成功交给 Bridge 后释放 Admission 竞争窗口。

        Returns:
            本次首次提交并消费 Registry reservation 时返回真。
        """

        with self._lock:
            if self._state != "prepared":
                return False
            if not self.coordinator._settle_assistant_request_admission(
                self,
                outcome="enqueued",
            ):
                return False
            self._state = "enqueued"
            return True

    def rollback(self) -> bool:
        """请求尚未进入 Bridge 时归还 Admission 竞争窗口。

        Returns:
            本次首次回滚并释放 Registry reservation 时返回真。
        """

        with self._lock:
            if self._state != "prepared":
                return False
            if not self.coordinator._settle_assistant_request_admission(
                self,
                outcome="rolled_back",
            ):
                return False
            self._state = "rolled_back"
            return True


@dataclass(slots=True)
class _SelfReflectionRecord:
    """保存协调器锁内可变的一轮自我反思事实。

    Args:
        self_reflection_id: 本次自我反思编号。
        episode_id: 本轮未处理插话片段编号。
        command: 首次准入时冻结的来源身份。
        admitted_at: Attention Admission 的单调时间。
        deadline: 决策必须完成的绝对单调时间。
        state: 当前只允许向前推进的状态。
        reason: 最近一次转换的稳定原因。
        inputs: 按锁顺序保存的未处理输入。
        inputs_by_identity: 用于吸收提交重试的身份索引。
        outcome: 决策完成后冻结的结构化结果。
        pending_terminal_state: episode 提交完成后要进入的终态。
        tool_commit_identity: 已经在主进程冻结的唯一工具提交身份。
    """

    self_reflection_id: str
    episode_id: str
    command: SelfReflectionAdmissionCommand
    admitted_at: float
    deadline: float
    state: SelfReflectionState = SelfReflectionState.ACQUIRING
    reason: str = "attention_admitted"
    inputs: list[UnattendedInputRecord] = field(default_factory=list)
    inputs_by_identity: dict[str, UnattendedInputRecord] = field(default_factory=dict)
    reply_admission_open: bool = True
    # 首段 Recovery 正文开始前，决策预算仍有效；一旦正文交接开始，deadline 只保留为诊断事实。
    decision_completed: bool = False
    reply_reservations_by_request_id: dict[str, tuple[str, int]] = field(
        default_factory=dict
    )
    outcome: SelfReflectionOutcomeFact | None = None
    pending_terminal_state: SelfReflectionState | None = None
    tool_commit_identity: SelfReflectionToolCommitIdentity | None = None

    def snapshot(self) -> SelfReflectionSnapshot:
        """冻结当前状态供锁外读取。

        Returns:
            后续状态变化不会反向修改的快照。
        """

        return SelfReflectionSnapshot(
            self_reflection_id=self.self_reflection_id,
            episode_id=self.episode_id,
            runtime_epoch=self.command.runtime_epoch,
            source_activity_id=self.command.source_activity_id,
            source_request_id=self.command.source_request_id,
            source_protocol_transaction_id=(
                self.command.source_protocol_transaction_id
            ),
            state=self.state,
            reason=self.reason,
            admitted_at=self.admitted_at,
            deadline=self.deadline,
            interruption_count=len(self.inputs),
            input_count=len(self.inputs),
            outcome=self.outcome,
            tool_commit_identity=self.tool_commit_identity,
        )

    def episode_snapshot(self) -> UnattendedEpisodeSnapshot:
        """冻结当前按主进程顺序保存的未处理片段。

        Returns:
            明确标记为不可执行的闭合片段事实。
        """

        return UnattendedEpisodeSnapshot(
            episode_id=self.episode_id,
            self_reflection_id=self.self_reflection_id,
            runtime_epoch=self.command.runtime_epoch,
            inputs=tuple(self.inputs),
        )


class ForegroundAttentionCoordinator:
    """线程安全地裁决唯一前台注意力和 Final 输入归属。

    Args:
        runtime_epoch: 当前 Assistant Runtime 世代。
        monotonic_now: 可替换的单调时钟函数。
        identity_factory: 按业务前缀生成唯一身份的函数。

    所有会改变普通请求准入或 episode 归属的操作都使用同一把锁。调用方不能在锁外
    先检查 ``is_attention_active`` 再自行决定归属，而应直接调用这里提供的准入或
    ``divert_final_input``，否则关闭与输入之间会重新出现竞态窗口。
    """

    def __init__(
        self,
        *,
        runtime_epoch: int,
        monotonic_now: Callable[[], float] = monotonic,
        identity_factory: Callable[[str], str] | None = None,
    ) -> None:
        """初始化当前世代的注意力协调器。

        Args:
            runtime_epoch: 当前 Assistant Runtime 世代。
            monotonic_now: 可替换的单调时钟函数。
            identity_factory: 可替换的稳定身份生成函数。
        """

        if type(runtime_epoch) is not int or runtime_epoch <= 0:
            raise ValueError("runtime_epoch 必须是大于零的整数")
        self._lock = RLock()
        self._runtime_epoch = runtime_epoch
        self._terminated_runtime_epochs: set[int] = set()
        self._monotonic_now = monotonic_now
        self._identity_factory = identity_factory or (
            lambda prefix: f"{prefix}-{uuid4().hex}"
        )
        self._records: dict[str, _SelfReflectionRecord] = {}
        self._source_tombstones: dict[str, str] = {}
        self._active_self_reflection_id: str | None = None
        self._episode_sequence = 0
        self._assistant_request_admissions: dict[
            str,
            AssistantRequestAdmissionLease,
        ] = {}

    @property
    def runtime_epoch(self) -> int:
        """返回当前允许创建新状态的运行时世代。

        Returns:
            当前运行时世代。
        """

        with self._lock:
            return self._runtime_epoch

    def admit(
        self,
        command: SelfReflectionAdmissionCommand,
    ) -> SelfReflectionAdmissionResult:
        """原子裁决来源去重、世代校验和唯一 Attention Admission。

        Args:
            command: 已经通过恢复资格策略的来源身份。

        Returns:
            首次准入、幂等重复或拒绝结果。
        """

        with self._lock:
            existing_id = self._source_tombstones.get(command.source_activity_id)
            if existing_id is not None:
                existing = self._records.get(existing_id)
                return SelfReflectionAdmissionResult(
                    status="duplicate",
                    reason="source_activity_already_processed",
                    snapshot=existing.snapshot() if existing is not None else None,
                )
            if command.runtime_epoch != self._runtime_epoch:
                return SelfReflectionAdmissionResult(
                    status="rejected",
                    reason="runtime_epoch_mismatch",
                )
            if command.runtime_epoch in self._terminated_runtime_epochs:
                return SelfReflectionAdmissionResult(
                    status="rejected",
                    reason="runtime_epoch_terminated",
                )

            active = self._active_record_locked()
            if active is not None:
                # 这项失败已经选择普通失败 Notice 收口，必须留下永久墓碑，避免重放事件
                # 在当前状态结束后偷偷排成第二轮自我反思。
                self._source_tombstones[command.source_activity_id] = (
                    "attention_already_owned"
                )
                return SelfReflectionAdmissionResult(
                    status="rejected",
                    reason="attention_already_owned",
                )
            if self._assistant_request_admissions:
                # 普通请求已经在同一把锁内先取得 Bridge 提交资格。Recovery 不能
                # 从“输入未改道”与真实 queue put 之间插队，否则两条 generation
                # 会同时争夺前台可见输出所有权。
                self._source_tombstones[command.source_activity_id] = (
                    "assistant_request_commit_in_progress"
                )
                return SelfReflectionAdmissionResult(
                    status="rejected",
                    reason="assistant_request_commit_in_progress",
                )

            admitted_at = validate_monotonic_time(
                self._monotonic_now(),
                field_name="admitted_at",
            )
            self_reflection_id = self._new_identity_locked("self-reflection")
            episode_id = self._new_identity_locked("unattended-episode")
            record = _SelfReflectionRecord(
                self_reflection_id=self_reflection_id,
                episode_id=episode_id,
                command=command,
                admitted_at=admitted_at,
                deadline=(
                    admitted_at + SELF_REFLECTION_DECISION_BUDGET_SECONDS
                ),
            )
            self._records[self_reflection_id] = record
            self._source_tombstones[command.source_activity_id] = self_reflection_id
            self._active_self_reflection_id = self_reflection_id
            return SelfReflectionAdmissionResult(
                status="admitted",
                reason="attention_admitted",
                snapshot=record.snapshot(),
            )

    def transition_to_reflecting(
        self,
        self_reflection_id: str,
        *,
        runtime_epoch: int,
    ) -> SelfReflectionTransitionResult:
        """把已取得资源的状态推进到正式重新判断。

        Args:
            self_reflection_id: 要推进的自我反思编号。
            runtime_epoch: 命令所属运行时世代。

        Returns:
            状态转换结果；超时会先进入失败关闭。
        """

        return self._transition_decision_state(
            self_reflection_id,
            runtime_epoch=runtime_epoch,
            expected_state=SelfReflectionState.ACQUIRING,
            target_state=SelfReflectionState.REFLECTING,
            reason="reflection_started",
        )

    def authorize_tool_commit(
        self,
        identity: SelfReflectionToolCommitIdentity,
    ) -> SelfReflectionTransitionResult:
        """原子核对完整工具身份，并批准唯一请求进入提交阶段。

        Args:
            identity: 子进程提交请求、来源、世代、工具和 canonical 指纹。

        Returns:
            首次授权、同身份重试或稳定拒绝结果。
        """

        if not isinstance(identity, SelfReflectionToolCommitIdentity):
            raise ValueError("identity 必须是 SelfReflectionToolCommitIdentity")
        with self._lock:
            record, rejected = self._record_for_command_locked(
                identity.self_reflection_id,
                runtime_epoch=identity.runtime_epoch,
            )
            if rejected is not None:
                return rejected
            assert record is not None
            if identity.source_activity_id != record.command.source_activity_id:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="source_activity_mismatch",
                    snapshot=record.snapshot(),
                )
            if record.tool_commit_identity is not None:
                if record.tool_commit_identity == identity:
                    return SelfReflectionTransitionResult(
                        status="duplicate",
                        reason="tool_commit_already_authorized",
                        snapshot=record.snapshot(),
                    )
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="tool_commit_identity_conflict",
                    snapshot=record.snapshot(),
                )
            if record.state is not SelfReflectionState.REFLECTING:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="invalid_state_transition",
                    snapshot=record.snapshot(),
                )
            if self._deadline_reached_locked(record):
                self._begin_deadline_close_locked(record)
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="reflection_deadline_exceeded",
                    snapshot=record.snapshot(),
                    episode=record.episode_snapshot(),
                )
            record.tool_commit_identity = identity
            record.state = SelfReflectionState.COMMITTING
            record.reason = "tool_commit_authorized"
            return SelfReflectionTransitionResult(
                status="applied",
                reason="tool_commit_authorized",
                snapshot=record.snapshot(),
            )

    def complete_background_tool_commit(
        self,
        identity: SelfReflectionToolCommitIdentity,
    ) -> SelfReflectionTransitionResult:
        """把已经登记到主进程的 Background canonical 事实冻结为提交完成。

        Args:
            identity: 先前在同一 Attention 记录中授权的完整工具身份。

        Returns:
            首次完成、同身份重试或稳定拒绝结果。

        Raises:
            ValueError: 身份类型错误，或调用方把 Inline 工具误传到该入口时抛出。

        canonical 回调与 deadline reaper 在主进程排水循环中串行执行。回调已经
        成功返回时，canonical 事实先成立；这里不能再用刚好越过的 deadline
        把它改写成“没有提交”。Inline 没有预先登记的 canonical 事实，不能调用
        这个入口，仍由原 deadline 约束工具执行和结果收口。
        """

        if not isinstance(identity, SelfReflectionToolCommitIdentity):
            raise ValueError("identity 必须是 SelfReflectionToolCommitIdentity")
        if identity.execution_channel != "background":
            raise ValueError("只有 Background 工具能完成 canonical 提交")
        with self._lock:
            record, rejected = self._record_for_command_locked(
                identity.self_reflection_id,
                runtime_epoch=identity.runtime_epoch,
            )
            if rejected is not None:
                return rejected
            assert record is not None
            if record.tool_commit_identity != identity:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="tool_commit_identity_conflict",
                    snapshot=record.snapshot(),
                )
            if record.state is SelfReflectionState.COMMITTED:
                return SelfReflectionTransitionResult(
                    status="duplicate",
                    reason="canonical_tool_commit_already_completed",
                    snapshot=record.snapshot(),
                )
            if record.state is not SelfReflectionState.COMMITTING:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="invalid_state_transition",
                    snapshot=record.snapshot(),
                )
            record.state = SelfReflectionState.COMMITTED
            record.reason = "canonical_tool_commit_completed"
            return SelfReflectionTransitionResult(
                status="applied",
                reason="canonical_tool_commit_completed",
                snapshot=record.snapshot(),
            )

    def abort_admission(
        self,
        self_reflection_id: str,
        *,
        runtime_epoch: int,
        reason: str,
    ) -> SelfReflectionTransitionResult:
        """回滚尚未开始执行的准入，释放唯一 Attention 所有权。

        只有还停留在 ``acquiring`` 的记录可以回滚。进入反思、工具提交或输入
        收口后，状态已经对外产生事实，调用方必须走对应的失败关闭流程，不能
        通过这个入口把已经发生的状态伪装成从未准入。

        Args:
            self_reflection_id: 要回滚的自我反思编号。
            runtime_epoch: 调用方看到的 Assistant Runtime 世代。
            reason: 记录在回滚快照中的稳定原因。

        Returns:
            首次回滚、重复回滚或拒绝结果。
        """

        normalized_reason = reason.strip() if isinstance(reason, str) else ""
        if not normalized_reason:
            raise ValueError("reason 不能为空")
        with self._lock:
            record, rejected = self._record_for_command_locked(
                self_reflection_id,
                runtime_epoch=runtime_epoch,
            )
            if rejected is not None:
                return rejected
            assert record is not None
            if record.state is SelfReflectionState.ACQUIRING:
                record.state = SelfReflectionState.TERMINATED
                record.reason = normalized_reason
                if self._active_self_reflection_id == record.self_reflection_id:
                    self._active_self_reflection_id = None
                if self._source_tombstones.get(record.command.source_activity_id) == (
                    record.self_reflection_id
                ):
                    del self._source_tombstones[record.command.source_activity_id]
                return SelfReflectionTransitionResult(
                    status="applied",
                    reason=normalized_reason,
                    snapshot=record.snapshot(),
                )
            if record.state is SelfReflectionState.TERMINATED:
                return SelfReflectionTransitionResult(
                    status="duplicate",
                    reason="admission_already_aborted",
                    snapshot=record.snapshot(),
                )
            return SelfReflectionTransitionResult(
                status="rejected",
                reason="admission_already_started",
                snapshot=record.snapshot(),
            )

    def rollback_diverted_input(
        self,
        self_reflection_id: str,
        *,
        runtime_epoch: int,
        input_identity: str,
    ) -> SelfReflectionTransitionResult:
        """撤销一项尚未写入 Runtime Memory 的输入接纳。

        这个窄入口只用于 Memory append 在提交前失败的补偿。已经成功写入
        episode 的输入不能被删除；否则重试会同时留下两条无法解释的历史。

        Args:
            self_reflection_id: 当前自我反思编号。
            runtime_epoch: 输入所属 Assistant Runtime 世代。
            input_identity: 需要撤销的 Text 或 Final ASR 身份。

        Returns:
            首次撤销、重复撤销或拒绝结果。
        """

        normalized_input_identity = input_identity.strip()
        if not normalized_input_identity:
            raise ValueError("input_identity 不能为空")
        with self._lock:
            record, rejected = self._record_for_command_locked(
                self_reflection_id,
                runtime_epoch=runtime_epoch,
            )
            if rejected is not None:
                return rejected
            assert record is not None
            input_record = record.inputs_by_identity.pop(normalized_input_identity, None)
            if input_record is None:
                return SelfReflectionTransitionResult(
                    status="duplicate",
                    reason="input_not_present",
                    snapshot=record.snapshot(),
                )
            record.inputs = [
                item
                for item in record.inputs
                if item.input_identity != normalized_input_identity
            ]
            return SelfReflectionTransitionResult(
                status="applied",
                reason="input_append_rolled_back",
                snapshot=record.snapshot(),
            )

    def divert_final_input(
        self,
        command: DivertFinalInputCommand,
    ) -> InputDiversionResult:
        """在线性化点把 Final 输入归入当前 episode 或留给普通 Turn。

        Args:
            command: 已经越过 Text 或 Final ASR 提交边界的真实输入。

        Returns:
            首次接纳、幂等重复或不改道结果。
        """

        with self._lock:
            if command.runtime_epoch != self._runtime_epoch:
                return InputDiversionResult(
                    status="not_diverted",
                    reason="runtime_epoch_mismatch",
                    runtime_epoch=command.runtime_epoch,
                )
            record = self._active_record_locked()
            if record is None or record.state not in _DECISION_STATES:
                if record is None or record.state not in _ATTENTION_STATES:
                    request_source = (
                        "text_input" if command.source == "text" else "final_asr"
                    )
                    lease = self._reserve_assistant_request_locked(
                        request_id=command.assistant_request_id,
                        runtime_epoch=command.runtime_epoch,
                        request_source=request_source,
                    )
                    if lease is not None:
                        return InputDiversionResult(
                            status="not_diverted",
                            reason="assistant_request_reserved",
                            snapshot=(
                                record.snapshot() if record is not None else None
                            ),
                            runtime_epoch=command.runtime_epoch,
                            assistant_request_lease=lease,
                        )
                if record is not None and record.state in _ATTENTION_STATES:
                    return InputDiversionResult(
                        status="not_diverted",
                        reason="attention_not_accepting_inputs",
                        snapshot=record.snapshot(),
                        episode_id=record.episode_id,
                        history_disposition="unattended_episode",
                        runtime_epoch=command.runtime_epoch,
                    )
                return InputDiversionResult(
                    status="not_diverted",
                    reason="attention_not_accepting_inputs",
                    snapshot=record.snapshot() if record is not None else None,
                    runtime_epoch=command.runtime_epoch,
                )

            existing = record.inputs_by_identity.get(command.input_identity)
            if existing is not None:
                return InputDiversionResult(
                    status="duplicate",
                    reason="input_already_accepted",
                    accepted_input=existing,
                    snapshot=record.snapshot(),
                    episode_id=record.episode_id,
                    history_disposition="unattended_episode",
                    interruption_reply_eligible=record.reply_admission_open,
                    runtime_epoch=command.runtime_epoch,
                )

            # ASR 与 Text 只有在这里拿到序号。时间戳不能覆盖这把锁已经决定的顺序。
            self._episode_sequence += 1
            accepted = UnattendedInputRecord(
                input_identity=command.input_identity,
                source=command.source,
                text=command.text,
                materials=command.materials,
                episode_sequence=self._episode_sequence,
            )
            record.inputs.append(accepted)
            record.inputs_by_identity[accepted.input_identity] = accepted
            return InputDiversionResult(
                status="accepted",
                reason="input_diverted_to_episode",
                accepted_input=accepted,
                snapshot=record.snapshot(),
                episode_id=record.episode_id,
                history_disposition="unattended_episode",
                interruption_reply_eligible=record.reply_admission_open,
                runtime_epoch=command.runtime_epoch,
            )

    def reserve_interruption_reply_sequence(
        self,
        *,
        self_reflection_id: str,
        runtime_epoch: int,
        request_id: str,
        trigger_input_identity: str,
    ) -> int | None:
        """为一条辅助回应预留与用户输入共用的 episode 顺序号。

        Args:
            self_reflection_id: 当前自我反思编号。
            runtime_epoch: 当前 Assistant Runtime 世代。
            request_id: 辅助回应的稳定请求编号。
            trigger_input_identity: 触发回应的 episode 输入身份。

        Returns:
            准入仍开放时返回稳定顺序号，否则返回空。
        """

        normalized_request_id = request_id.strip()
        normalized_input_identity = trigger_input_identity.strip()
        if not normalized_request_id or not normalized_input_identity:
            raise ValueError("request_id 和 trigger_input_identity 不能为空")
        with self._lock:
            record, rejected = self._record_for_command_locked(
                self_reflection_id,
                runtime_epoch=runtime_epoch,
            )
            if rejected is not None or record is None:
                return None
            if (
                record.state not in _DECISION_STATES
                or not record.reply_admission_open
                or normalized_input_identity not in record.inputs_by_identity
            ):
                return None
            existing = record.reply_reservations_by_request_id.get(
                normalized_request_id
            )
            if existing is not None:
                existing_input_identity, existing_sequence = existing
                if existing_input_identity != normalized_input_identity:
                    raise ValueError("同一辅助请求不能改绑其它输入")
                return existing_sequence
            self._episode_sequence += 1
            record.reply_reservations_by_request_id[normalized_request_id] = (
                normalized_input_identity,
                self._episode_sequence,
            )
            return self._episode_sequence

    def close_interruption_reply_admission(
        self,
        *,
        self_reflection_id: str,
        runtime_epoch: int,
        episode_id: str,
    ) -> bool:
        """永久关闭当前 episode 的新打扰回应资格。

        Args:
            self_reflection_id: 当前自我反思编号。
            runtime_epoch: 当前 Assistant Runtime 世代。
            episode_id: 必须匹配的 episode 编号。

        Returns:
            首次关闭或已经关闭时返回真，身份不匹配时返回假。
        """

        normalized_episode_id = episode_id.strip()
        with self._lock:
            record, rejected = self._record_for_command_locked(
                self_reflection_id,
                runtime_epoch=runtime_epoch,
            )
            if (
                rejected is not None
                or record is None
                or record.episode_id != normalized_episode_id
            ):
                return False
            record.reply_admission_open = False
            record.decision_completed = True
            return True

    def reserve_assistant_request(
        self,
        *,
        request_id: str,
        runtime_epoch: int,
        request_source: str,
    ) -> AssistantRequestAdmissionLease | None:
        """为没有输入 diversion 的普通请求预留 Attention 提交资格。

        Args:
            request_id: 即将进入 Assistant Bridge 的稳定请求编号。
            runtime_epoch: 请求所属 Assistant Runtime 世代。
            request_source: 当前只允许 System Notice 调用该独立入口。

        Returns:
            没有 Attention 屏障时返回可结算 lease，否则返回空。

        Raises:
            ValueError: 请求身份为空或来源不是 System Notice。
        """

        normalized_source = request_source.strip()
        if normalized_source not in {"system_notice", "timed_reminder_notice"}:
            raise ValueError("独立 reservation 只允许 Notice 请求")
        with self._lock:
            return self._reserve_assistant_request_locked(
                request_id=request_id,
                runtime_epoch=runtime_epoch,
                request_source=normalized_source,
            )

    def begin_close(
        self,
        self_reflection_id: str,
        *,
        runtime_epoch: int,
        terminal_state: SelfReflectionState,
        reason: str,
        outcome: SelfReflectionOutcomeFact,
    ) -> SelfReflectionTransitionResult:
        """冻结最终结果，但继续接纳尚未越过关闭线性化点的输入。

        调用方应先完成可能失败的结果账本和材料清理，再在 episode 事务锁内调用
        ``freeze_inputs_for_close``。这样重试期间输入仍进入同一 episode，不会落入
        既不能追加片段、又不能创建普通 Turn 的半关闭窗口。

        Args:
            self_reflection_id: 要关闭的自我反思编号。
            runtime_epoch: 命令所属运行时世代。
            terminal_state: episode 提交成功后进入 closed 或 closed_failed。
            reason: 本地稳定收口原因。
            outcome: 最终恢复表达可以读取的冻结结构化事实。

        Returns:
            状态转换结果和一次性冻结的 episode。
        """

        normalized_reason = reason.strip() if isinstance(reason, str) else ""
        if not normalized_reason:
            raise ValueError("reason 不能为空")
        if terminal_state not in {
            SelfReflectionState.CLOSED,
            SelfReflectionState.CLOSED_FAILED,
        }:
            raise ValueError("terminal_state 只允许 closed 或 closed_failed")
        with self._lock:
            record, rejected = self._record_for_command_locked(
                self_reflection_id,
                runtime_epoch=runtime_epoch,
            )
            if rejected is not None:
                return rejected
            assert record is not None
            if record.state in {
                SelfReflectionState.FINALIZING,
                SelfReflectionState.CLOSING_INPUTS,
            }:
                return SelfReflectionTransitionResult(
                    status="duplicate",
                    reason=(
                        "outcome_already_finalizing"
                        if record.state is SelfReflectionState.FINALIZING
                        else "inputs_already_closing"
                    ),
                    snapshot=record.snapshot(),
                    episode=record.episode_snapshot(),
                )
            if record.state in _TERMINAL_STATES:
                return SelfReflectionTransitionResult(
                    status="duplicate",
                    reason="self_reflection_already_terminal",
                    snapshot=record.snapshot(),
                )
            if record.state not in _DECISION_STATES:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="invalid_state_transition",
                    snapshot=record.snapshot(),
                )
            if outcome.self_reflection_id != record.self_reflection_id:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="outcome_self_reflection_mismatch",
                    snapshot=record.snapshot(),
                )
            if outcome.source_activity_id != record.command.source_activity_id:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="outcome_source_activity_mismatch",
                    snapshot=record.snapshot(),
                )
            if (
                record.state in _DEADLINE_STATES
                and record.outcome is None
                and not record.decision_completed
                and self._deadline_reached_locked(record)
            ):
                self._begin_deadline_close_locked(record)
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="reflection_deadline_exceeded",
                    snapshot=record.snapshot(),
                    episode=record.episode_snapshot(),
                )
            # 超时或 canonical 工具事实一旦冻结，后来的可见 settlement 只能触发
            # 收口，不能把权威失败/工具事实改写成模型自行推断的结果。
            if record.outcome is None:
                record.outcome = outcome
            else:
                outcome = record.outcome
            record.pending_terminal_state = (
                SelfReflectionState.CLOSED_FAILED
                if outcome.kind is SelfReflectionOutcomeKind.REFLECTION_FAILED
                else terminal_state
            )
            record.state = SelfReflectionState.FINALIZING
            record.reply_admission_open = False
            record.reason = normalized_reason
            return SelfReflectionTransitionResult(
                status="applied",
                reason=normalized_reason,
                snapshot=record.snapshot(),
                episode=record.episode_snapshot(),
            )

    def freeze_inputs_for_close(
        self,
        self_reflection_id: str,
        *,
        runtime_epoch: int,
    ) -> SelfReflectionTransitionResult:
        """在 episode 事务锁内关闭输入接纳并冻结最后一份片段快照。

        Args:
            self_reflection_id: 要关闭输入归属的一轮自我反思编号。
            runtime_epoch: 命令所属运行时世代。

        Returns:
            首次关闭、幂等重复或身份不匹配的结构化结果。
        """

        with self._lock:
            record, rejected = self._record_for_command_locked(
                self_reflection_id,
                runtime_epoch=runtime_epoch,
            )
            if rejected is not None:
                return rejected
            assert record is not None
            if record.state is SelfReflectionState.CLOSING_INPUTS:
                return SelfReflectionTransitionResult(
                    status="duplicate",
                    reason="inputs_already_closing",
                    snapshot=record.snapshot(),
                    episode=record.episode_snapshot(),
                )
            if record.state is not SelfReflectionState.FINALIZING:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="outcome_not_finalizing",
                    snapshot=record.snapshot(),
                )
            record.state = SelfReflectionState.CLOSING_INPUTS
            return SelfReflectionTransitionResult(
                status="applied",
                reason="episode_inputs_frozen",
                snapshot=record.snapshot(),
                episode=record.episode_snapshot(),
            )

    def complete_close(
        self,
        self_reflection_id: str,
        *,
        runtime_epoch: int,
    ) -> SelfReflectionTransitionResult:
        """在 episode 已经可靠提交后释放普通请求屏障。

        Args:
            self_reflection_id: 要完成关闭的自我反思编号。
            runtime_epoch: 命令所属运行时世代。

        Returns:
            首次完成、幂等重复或拒绝结果。
        """

        with self._lock:
            record, rejected = self._record_for_command_locked(
                self_reflection_id,
                runtime_epoch=runtime_epoch,
            )
            if rejected is not None:
                return rejected
            assert record is not None
            if record.state in {
                SelfReflectionState.CLOSED,
                SelfReflectionState.CLOSED_FAILED,
            }:
                return SelfReflectionTransitionResult(
                    status="duplicate",
                    reason="self_reflection_already_closed",
                    snapshot=record.snapshot(),
                )
            if record.state is not SelfReflectionState.CLOSING_INPUTS:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="inputs_not_closing",
                    snapshot=record.snapshot(),
                )
            if record.pending_terminal_state not in {
                SelfReflectionState.CLOSED,
                SelfReflectionState.CLOSED_FAILED,
            }:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="terminal_state_missing",
                    snapshot=record.snapshot(),
                )
            record.state = record.pending_terminal_state
            record.pending_terminal_state = None
            if self._active_self_reflection_id == record.self_reflection_id:
                self._active_self_reflection_id = None
            return SelfReflectionTransitionResult(
                status="applied",
                reason=record.reason,
                snapshot=record.snapshot(),
            )

    def reap_expired(self) -> tuple[SelfReflectionTransitionResult, ...]:
        """标记尚未形成决策的 Recovery 超时，并等待正常正文兜底收口。

        Returns:
            本次新请求兜底的状态快照；没有到期时为空。
        """

        with self._lock:
            record = self._active_record_locked()
            if (
                record is None
                or record.state not in _DEADLINE_STATES
                or record.decision_completed
                or record.outcome is not None
                or not self._deadline_reached_locked(record)
            ):
                return ()
            self._begin_deadline_close_locked(record)
            return (
                SelfReflectionTransitionResult(
                    status="applied",
                    reason="reflection_deadline_exceeded",
                    snapshot=record.snapshot(),
                ),
            )

    def replace_runtime_epoch(
        self,
        new_runtime_epoch: int,
    ) -> tuple[SelfReflectionTransitionResult, ...]:
        """终止旧世代状态并切换到不恢复旧任务的新世代。

        Args:
            new_runtime_epoch: 新 Assistant Runtime 世代，必须严格递增。

        Returns:
            被终止状态及其最后一次 episode 快照；没有 active 状态时为空。
        """

        if type(new_runtime_epoch) is not int or new_runtime_epoch <= 0:
            raise ValueError("new_runtime_epoch 必须是大于零的整数")
        with self._lock:
            if new_runtime_epoch <= self._runtime_epoch:
                raise ValueError("new_runtime_epoch 必须严格大于当前世代")
            old_runtime_epoch = self._runtime_epoch
            self._terminated_runtime_epochs.add(old_runtime_epoch)
            record = self._active_record_locked()
            result: tuple[SelfReflectionTransitionResult, ...] = ()
            if record is not None and record.state in _ATTENTION_STATES:
                record.state = SelfReflectionState.TERMINATED
                record.reason = "runtime_reconfigured"
                record.pending_terminal_state = None
                result = (
                    SelfReflectionTransitionResult(
                        status="applied",
                        reason="runtime_reconfigured",
                        snapshot=record.snapshot(),
                        episode=record.episode_snapshot(),
                    ),
                )
            self._active_self_reflection_id = None
            self._assistant_request_admissions.clear()
            self._runtime_epoch = new_runtime_epoch
            return result

    def _reserve_assistant_request_locked(
        self,
        *,
        request_id: str,
        runtime_epoch: int,
        request_source: str,
    ) -> AssistantRequestAdmissionLease | None:
        """在 Attention 单锁内创建或复用一项普通请求 reservation。

        Args:
            request_id: 即将进入 Assistant Bridge 的稳定请求编号。
            runtime_epoch: 请求所属 Assistant Runtime 世代。
            request_source: Text、Final ASR 或 System Notice 来源。

        Returns:
            资格成立时返回同一 lease；Attention 已占用或世代失效时返回空。
        """

        normalized_request_id = request_id.strip()
        normalized_source = request_source.strip()
        if not normalized_request_id:
            raise ValueError("request_id 不能为空")
        if normalized_source not in _ORDINARY_REQUEST_SOURCES:
            raise ValueError("request_source 不是允许的普通请求来源")
        if runtime_epoch != self._runtime_epoch:
            return None
        if runtime_epoch in self._terminated_runtime_epochs:
            return None
        record = self._active_record_locked()
        if record is not None and record.state in _ATTENTION_STATES:
            return None
        existing = self._assistant_request_admissions.get(normalized_request_id)
        if existing is not None:
            if (
                existing.runtime_epoch != runtime_epoch
                or existing.request_source != normalized_source
            ):
                raise ValueError("同一 request_id 的 Attention reservation 身份冲突")
            return existing
        lease = AssistantRequestAdmissionLease(
            request_id=normalized_request_id,
            runtime_epoch=runtime_epoch,
            request_source=normalized_source,
            coordinator=self,
        )
        self._assistant_request_admissions[normalized_request_id] = lease
        return lease

    def _settle_assistant_request_admission(
        self,
        lease: AssistantRequestAdmissionLease,
        *,
        outcome: str,
    ) -> bool:
        """只允许创建 reservation 的同一 lease 释放提交窗口。

        Args:
            lease: 正在提交或回滚的普通请求资格。
            outcome: 已入队或已回滚；只用于校验调用意图。

        Returns:
            Registry 首次删除同一 reservation 时返回真。
        """

        if outcome not in {"enqueued", "rolled_back"}:
            raise ValueError("outcome 只允许 enqueued 或 rolled_back")
        with self._lock:
            current = self._assistant_request_admissions.get(lease.request_id)
            if current is not lease:
                return False
            del self._assistant_request_admissions[lease.request_id]
            return True

    def snapshot(
        self,
        self_reflection_id: str,
    ) -> SelfReflectionSnapshot | None:
        """读取指定状态的不可变快照。

        Args:
            self_reflection_id: 要查询的自我反思编号。

        Returns:
            记录存在时返回快照，否则返回空。
        """

        normalized_id = self_reflection_id.strip()
        if not normalized_id:
            raise ValueError("self_reflection_id 不能为空")
        with self._lock:
            record = self._records.get(normalized_id)
            return record.snapshot() if record is not None else None

    def active_snapshot(self) -> SelfReflectionSnapshot | None:
        """返回当前 Attention 所有者的快照，供运行时替换按旧世代收口。

        Returns:
            当前仍占有 Attention 的记录快照；没有 active 记录时返回空。
        """

        with self._lock:
            record = self._active_record_locked()
            return record.snapshot() if record is not None else None

    def is_attention_active(self, *, runtime_epoch: int) -> bool:
        """判断当前世代是否仍由自我反思持有前台注意力。

        Args:
            runtime_epoch: 查询方所属运行时世代。

        Returns:
            当前世代处于决策或输入关闭阶段时返回真。
        """

        with self._lock:
            if runtime_epoch != self._runtime_epoch:
                return False
            record = self._active_record_locked()
            return record is not None and record.state in _ATTENTION_STATES

    def can_submit_self_reflection_expression(
        self,
        *,
        runtime_epoch: int,
        request_source: str,
    ) -> bool:
        """判断一项 Self-Reflection 可见表达能否越过当前屏障。

        Args:
            runtime_epoch: 请求所属运行时世代。
            request_source: 入场或 Recovery 请求来源。

        Returns:
            本类表达在当前状态仍有提交资格时返回真。

        Raises:
            ValueError: 请求来源不是 Self-Reflection 可见表达。
        """

        normalized_source = request_source.strip()
        if normalized_source not in _SPECIAL_REQUEST_SOURCES:
            raise ValueError("request_source 不是 Self-Reflection 可见表达")
        with self._lock:
            if runtime_epoch != self._runtime_epoch:
                return False
            record = self._active_record_locked()
            if record is None or record.state not in _ATTENTION_STATES:
                return True
            if record.state in {
                SelfReflectionState.FINALIZING,
                SelfReflectionState.CLOSING_INPUTS,
            }:
                # Recovery 已经完成可见输出才会进入 closing_inputs；此后不允许再提交
                # 任何 Self-Reflection 请求，避免关闭期间重新占用 Foreground。
                return False
            if normalized_source == "self_reflection_interruption_reply":
                return record.reply_admission_open
            return True

    def _transition_decision_state(
        self,
        self_reflection_id: str,
        *,
        runtime_epoch: int,
        expected_state: SelfReflectionState,
        target_state: SelfReflectionState,
        reason: str,
    ) -> SelfReflectionTransitionResult:
        """在同一套 deadline 和世代门禁下推进决策状态。

        Args:
            self_reflection_id: 要推进的自我反思编号。
            runtime_epoch: 命令所属运行时世代。
            expected_state: 唯一允许的当前状态。
            target_state: 本次要进入的后继状态。
            reason: 记录在快照中的稳定原因。

        Returns:
            首次应用、幂等重复或拒绝结果。
        """

        with self._lock:
            record, rejected = self._record_for_command_locked(
                self_reflection_id,
                runtime_epoch=runtime_epoch,
            )
            if rejected is not None:
                return rejected
            assert record is not None
            if record.state is target_state:
                return SelfReflectionTransitionResult(
                    status="duplicate",
                    reason="state_already_applied",
                    snapshot=record.snapshot(),
                )
            if record.state is not expected_state:
                return SelfReflectionTransitionResult(
                    status="rejected",
                    reason="invalid_state_transition",
                    snapshot=record.snapshot(),
                )
            if self._deadline_reached_locked(record):
                self._begin_deadline_close_locked(record)
                record.state = target_state
                return SelfReflectionTransitionResult(
                    status="applied",
                    reason="reflection_deadline_exceeded",
                    snapshot=record.snapshot(),
                    episode=record.episode_snapshot(),
                )
            record.state = target_state
            record.reason = reason
            return SelfReflectionTransitionResult(
                status="applied",
                reason=reason,
                snapshot=record.snapshot(),
            )

    def _record_for_command_locked(
        self,
        self_reflection_id: str,
        *,
        runtime_epoch: int,
    ) -> tuple[
        _SelfReflectionRecord | None,
        SelfReflectionTransitionResult | None,
    ]:
        """在锁内核对命令身份并返回统一拒绝结果。

        Args:
            self_reflection_id: 命令目标编号。
            runtime_epoch: 命令所属运行时世代。

        Returns:
            有效记录与空拒绝，或空记录与明确拒绝。
        """

        normalized_id = self_reflection_id.strip()
        if not normalized_id:
            raise ValueError("self_reflection_id 不能为空")
        record = self._records.get(normalized_id)
        if record is None:
            return None, SelfReflectionTransitionResult(
                status="missing",
                reason="self_reflection_missing",
            )
        if runtime_epoch != self._runtime_epoch or runtime_epoch != record.command.runtime_epoch:
            return None, SelfReflectionTransitionResult(
                status="rejected",
                reason="runtime_epoch_mismatch",
                snapshot=record.snapshot(),
            )
        return record, None

    def _active_record_locked(self) -> _SelfReflectionRecord | None:
        """返回当前注意力所有者的锁内记录。

        Returns:
            当前 active 记录；没有所有者时返回空。
        """

        if self._active_self_reflection_id is None:
            return None
        return self._records.get(self._active_self_reflection_id)

    def _deadline_reached_locked(self, record: _SelfReflectionRecord) -> bool:
        """判断当前单调时间是否已经达到决策截止点。

        Args:
            record: 锁内自我反思记录。

        Returns:
            当前时间大于或等于绝对 deadline 时返回真。
        """

        now = validate_monotonic_time(
            self._monotonic_now(),
            field_name="monotonic_now",
        )
        return now >= record.deadline

    def _begin_deadline_close_locked(self, record: _SelfReflectionRecord) -> None:
        """在锁内冻结到期事实，但保留输出和 episode 到正常结算。

        Args:
            record: 已确认处于决策阶段的锁内记录。
        """

        record.outcome = SelfReflectionOutcomeFact.failed(
            self_reflection_id=record.self_reflection_id,
            source_activity_id=record.command.source_activity_id,
            reason="reflection_deadline_exceeded",
        )
        # 这里不能直接进入 closing_inputs：用户仍可能需要看到 Recovery fallback，
        # 打扰回应也必须继续开放到首段正文真正公开。
        record.pending_terminal_state = None
        record.reason = "reflection_deadline_exceeded"

    def _new_identity_locked(self, prefix: str) -> str:
        """生成非空且在当前记录中未使用的业务身份。

        Args:
            prefix: 身份用途前缀。

        Returns:
            经过裁剪的唯一身份。

        Raises:
            RuntimeError: 注入的工厂连续返回空值或重复值。
        """

        for _attempt in range(8):
            value = self._identity_factory(prefix)
            normalized = value.strip() if isinstance(value, str) else ""
            if not normalized:
                continue
            if normalized not in self._records:
                return normalized
        raise RuntimeError("identity_factory 未能生成唯一非空身份")
