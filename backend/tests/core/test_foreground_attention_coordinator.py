"""验证前台注意力协调器的单向状态与输入线性化规则。"""

from __future__ import annotations


from companion.foreground_self_reflection.models import (
    DivertFinalInputCommand,
    SelfReflectionAdmissionCommand,
    SelfReflectionOutcomeFact,
    SelfReflectionOutcomeKind,
    SelfReflectionState,
    SelfReflectionToolCommitIdentity,
    UnattendedMaterialReference,
)
from companion.foreground_self_reflection.registry import (
    ForegroundAttentionCoordinator,
)


def _coordinator(now: list[float] | None = None) -> ForegroundAttentionCoordinator:
    """构造身份和时钟均可预测的注意力协调器。

    Args:
        now: 测试可以推进的单调时钟容器。

    Returns:
        当前世代为 7 的协调器。
    """

    clock = now if now is not None else [100.0]
    counters: dict[str, int] = {}

    def identity_factory(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return ForegroundAttentionCoordinator(
        runtime_epoch=7,
        monotonic_now=lambda: clock[0],
        identity_factory=identity_factory,
    )


def _admission(
    *,
    source_activity_id: str = "cognitive-activity-1",
    runtime_epoch: int = 7,
) -> SelfReflectionAdmissionCommand:
    """构造恢复准入命令。

    Args:
        source_activity_id: 来源 Cognitive Activity 编号。
        runtime_epoch: 来源运行时世代。

    Returns:
        完整且稳定的准入命令。
    """

    return SelfReflectionAdmissionCommand(
        runtime_epoch=runtime_epoch,
        source_activity_id=source_activity_id,
        source_request_id="foreground-request-1",
        source_protocol_transaction_id="tool-transaction-1",
    )


def _outcome(self_reflection_id: str) -> SelfReflectionOutcomeFact:
    """构造无需新工具的成功恢复事实。

    Args:
        self_reflection_id: 当前自我反思编号。

    Returns:
        可用于关闭状态的冻结结果事实。
    """

    return SelfReflectionOutcomeFact.direct_response(
        self_reflection_id=self_reflection_id,
        source_activity_id="cognitive-activity-1",
    )


def _commit_identity(
    self_reflection_id: str,
    *,
    commit_request_id: str = "commit-request-1",
    tool_call_id: str = "tool-call-1",
) -> SelfReflectionToolCommitIdentity:
    """构造需要在 Attention 锁内完整冻结的工具提交身份。

    Args:
        self_reflection_id: 当前自我反思编号。
        commit_request_id: 子进程本次同步提交请求编号。
        tool_call_id: 模型产生的唯一工具调用编号。

    Returns:
        包含来源、世代、工具和 canonical 指纹的完整身份。
    """

    return SelfReflectionToolCommitIdentity(
        commit_request_id=commit_request_id,
        self_reflection_id=self_reflection_id,
        source_activity_id="cognitive-activity-1",
        runtime_epoch=7,
        tool_call_id=tool_call_id,
        tool_name="start_background_work",
        execution_channel="background",
        canonical_payload_fingerprint="canonical-fingerprint-1",
    )


def test_admission_owns_attention_and_state_only_moves_forward() -> None:
    """一次准入取得唯一注意力，状态按 acquiring 到 closed 单向前进。"""

    coordinator = _coordinator()

    admitted = coordinator.admit(_admission())
    assert admitted.status == "admitted"
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    assert admitted.snapshot.state is SelfReflectionState.ACQUIRING
    assert admitted.snapshot.deadline == 145.0
    assert coordinator.reserve_assistant_request(
        request_id="notice-during-reflection",
        runtime_epoch=7,
        request_source="system_notice",
    ) is None
    assert coordinator.can_submit_self_reflection_expression(
        runtime_epoch=7,
        request_source="self_reflection_entry",
    ) is True

    reflecting = coordinator.transition_to_reflecting(
        reflection_id,
        runtime_epoch=7,
    )
    committing = coordinator.authorize_tool_commit(
        _commit_identity(reflection_id),
    )
    backwards = coordinator.transition_to_reflecting(
        reflection_id,
        runtime_epoch=7,
    )
    closing = coordinator.begin_close(
        reflection_id,
        runtime_epoch=7,
        terminal_state=SelfReflectionState.CLOSED,
        reason="reflection_completed",
        outcome=_outcome(reflection_id),
    )
    for blocked_source in (
        "self_reflection_entry",
        "self_reflection_recovery",
    ):
        assert coordinator.can_submit_self_reflection_expression(
            runtime_epoch=7,
            request_source=blocked_source,
        ) is False
    frozen = coordinator.freeze_inputs_for_close(
        reflection_id,
        runtime_epoch=7,
    )
    closed = coordinator.complete_close(reflection_id, runtime_epoch=7)

    assert reflecting.status == "applied"
    assert committing.status == "applied"
    assert backwards.status == "rejected"
    assert backwards.reason == "invalid_state_transition"
    assert closing.snapshot is not None
    assert closing.snapshot.state is SelfReflectionState.FINALIZING
    assert frozen.snapshot is not None
    assert frozen.snapshot.state is SelfReflectionState.CLOSING_INPUTS
    assert closed.snapshot is not None
    assert closed.snapshot.state is SelfReflectionState.CLOSED
    notice_lease = coordinator.reserve_assistant_request(
        request_id="notice-after-close",
        runtime_epoch=7,
        request_source="system_notice",
    )
    assert notice_lease is not None
    assert notice_lease.rollback() is True


def test_tool_commit_authorization_freezes_full_identity() -> None:
    """同一提交可幂等重试，不同请求或工具身份不能冒充已批准动作。"""

    coordinator = _coordinator()
    admitted = coordinator.admit(_admission())
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    coordinator.transition_to_reflecting(reflection_id, runtime_epoch=7)
    identity = _commit_identity(reflection_id)

    first = coordinator.authorize_tool_commit(identity)
    duplicate = coordinator.authorize_tool_commit(identity)
    conflict = coordinator.authorize_tool_commit(
        _commit_identity(
            reflection_id,
            commit_request_id="commit-request-2",
            tool_call_id="tool-call-2",
        )
    )

    assert first.status == "applied"
    assert first.snapshot is not None
    assert first.snapshot.tool_commit_identity == identity
    assert duplicate.status == "duplicate"
    assert duplicate.snapshot is not None
    assert duplicate.snapshot.tool_commit_identity == identity
    assert conflict.status == "rejected"
    assert conflict.reason == "tool_commit_identity_conflict"


def test_canonical_background_commit_cannot_be_rewritten_by_deadline() -> None:
    """canonical 已生效后，deadline 只能等待同一工具结果正常收口。"""

    clock = [100.0]
    coordinator = _coordinator(clock)
    admitted = coordinator.admit(_admission())
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    coordinator.transition_to_reflecting(reflection_id, runtime_epoch=7)
    identity = _commit_identity(reflection_id)
    coordinator.authorize_tool_commit(identity)

    # canonical 应用可能刚好跨过 deadline；它已经成为主进程事实后，
    # reaper 不能再把同一恢复改写成“什么都没有提交”。
    clock[0] = 145.0
    committed = coordinator.complete_background_tool_commit(identity)
    expired = coordinator.reap_expired()
    closing = coordinator.begin_close(
        reflection_id,
        runtime_epoch=7,
        terminal_state=SelfReflectionState.CLOSED,
        reason="reflection_completed",
        outcome=_outcome(reflection_id),
    )

    assert committed.status == "applied"
    assert committed.snapshot is not None
    assert committed.snapshot.state is SelfReflectionState.COMMITTED
    assert expired == ()
    assert closing.status == "applied"


def test_deadline_and_runtime_replacement_win_before_tool_commit_authorization() -> None:
    """批准前到达的 deadline 或热替换必须阻止旧工具越过提交边界。"""

    clock = [100.0]
    deadline_coordinator = _coordinator(clock)
    admitted = deadline_coordinator.admit(_admission())
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    deadline_coordinator.transition_to_reflecting(reflection_id, runtime_epoch=7)
    clock[0] = 145.0

    deadline_result = deadline_coordinator.authorize_tool_commit(
        _commit_identity(reflection_id)
    )

    assert deadline_result.status == "rejected"
    assert deadline_result.reason == "reflection_deadline_exceeded"
    assert deadline_result.snapshot is not None
    assert deadline_result.snapshot.state is SelfReflectionState.REFLECTING

    replaced_coordinator = _coordinator()
    admitted = replaced_coordinator.admit(_admission())
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    replaced_coordinator.transition_to_reflecting(reflection_id, runtime_epoch=7)
    replaced_coordinator.replace_runtime_epoch(8)

    replacement_result = replaced_coordinator.authorize_tool_commit(
        _commit_identity(reflection_id)
    )

    assert replacement_result.status == "rejected"
    assert replacement_result.reason == "runtime_epoch_mismatch"


def test_duplicate_source_busy_attention_and_old_epoch_are_absorbed() -> None:
    """重复终态、第二项失败和旧世代事件都不能创建排队反思。"""

    coordinator = _coordinator()
    first = coordinator.admit(_admission())
    duplicate = coordinator.admit(_admission())
    busy = coordinator.admit(
        _admission(source_activity_id="cognitive-activity-2")
    )
    busy_duplicate = coordinator.admit(
        _admission(source_activity_id="cognitive-activity-2")
    )
    old_epoch = coordinator.admit(
        _admission(source_activity_id="cognitive-activity-old", runtime_epoch=6)
    )

    assert first.status == "admitted"
    assert duplicate.status == "duplicate"
    assert busy.status == "rejected"
    assert busy.reason == "attention_already_owned"
    assert busy_duplicate.status == "duplicate"
    assert old_epoch.status == "rejected"
    assert old_epoch.reason == "runtime_epoch_mismatch"


def test_pending_normal_request_reservation_wins_attention_admission() -> None:
    """普通输入先取得提交资格时，恢复不能从检查与入队之间插队。"""

    coordinator = _coordinator()

    ordinary_request = coordinator.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="text-submission-1",
            assistant_request_id="text-request-1",
            source="text",
            text="已经公开、正在准备提交的正文",
        )
    )
    admission = coordinator.admit(_admission())

    assert ordinary_request.status == "not_diverted"
    assert ordinary_request.reason == "assistant_request_reserved"
    assert admission.status == "rejected"
    assert admission.reason == "assistant_request_commit_in_progress"


def test_settled_normal_request_reservation_releases_attention_competition() -> None:
    """普通请求入队或失败后都必须释放后续 Recovery 的准入窗口。"""

    for outcome in ("enqueued", "rolled_back"):
        coordinator = _coordinator()
        ordinary_request = coordinator.divert_final_input(
            DivertFinalInputCommand(
                runtime_epoch=7,
                input_identity=f"text-submission-{outcome}",
                assistant_request_id=f"text-request-{outcome}",
                source="text",
                text="已经完成普通请求提交裁决",
            )
        )
        lease = ordinary_request.assistant_request_lease
        assert lease is not None

        settled = (
            lease.commit_enqueued()
            if outcome == "enqueued"
            else lease.rollback()
        )
        admission = coordinator.admit(_admission())

        assert settled is True
        assert admission.status == "admitted"


def test_deadline_is_absolute_and_marks_uncommitted_decision_for_fallback() -> None:
    """进入表达和打扰都不能把 45 秒决策截止时间向后延长。"""

    now = [10.0]
    coordinator = _coordinator(now)
    admitted = coordinator.admit(_admission())
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    coordinator.transition_to_reflecting(reflection_id, runtime_epoch=7)
    coordinator.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="text-submission-1",
            assistant_request_id="text-request-1",
            source="text",
            text="第一句话",
        )
    )
    now[0] = 55.0

    expired = coordinator.reap_expired()

    assert len(expired) == 1
    assert expired[0].snapshot is not None
    assert expired[0].snapshot.deadline == 55.0
    assert expired[0].snapshot.state is SelfReflectionState.REFLECTING
    assert expired[0].snapshot.reason == "reflection_deadline_exceeded"
    assert expired[0].snapshot.outcome is not None
    assert expired[0].snapshot.outcome.kind is SelfReflectionOutcomeKind.REFLECTION_FAILED


def test_text_and_asr_share_lock_order_and_duplicate_identity_is_idempotent() -> None:
    """文本与 Final ASR 按越过同一把锁的顺序编号，重试不重复写入。"""

    coordinator = _coordinator()
    admitted = coordinator.admit(_admission())
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    coordinator.transition_to_reflecting(reflection_id, runtime_epoch=7)

    first = coordinator.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="asr-turn-1",
            assistant_request_id="asr-request-1",
            source="final_asr",
            text="第一句话",
        )
    )
    duplicate = coordinator.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="asr-turn-1",
            assistant_request_id="asr-request-1",
            source="final_asr",
            text="不会覆盖原文",
        )
    )
    second = coordinator.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="text-submission-2",
            assistant_request_id="text-request-2",
            source="text",
            text="第二句话",
            materials=(
                UnattendedMaterialReference(
                    file_name="notes.txt",
                    media_type="text/plain",
                ),
            ),
        )
    )

    assert first.status == "accepted"
    assert first.accepted_input is not None
    assert first.accepted_input.episode_sequence == 1
    assert duplicate.status == "duplicate"
    assert duplicate.accepted_input == first.accepted_input
    assert second.accepted_input is not None
    assert second.accepted_input.episode_sequence == 2

    closing = coordinator.begin_close(
        reflection_id,
        runtime_epoch=7,
        terminal_state=SelfReflectionState.CLOSED,
        reason="reflection_completed",
        outcome=_outcome(reflection_id),
    )
    assert closing.episode is not None
    assert [item.text for item in closing.episode.inputs] == [
        "第一句话",
        "第二句话",
    ]
    assert closing.episode.inputs[1].materials[0].file_name == "notes.txt"
    assert closing.episode.actionable is False


def test_finalizing_keeps_input_in_episode_until_freeze_boundary() -> None:
    """结果副作用重试期间的新输入仍应进入最终冻结的同一个 episode。"""

    coordinator = _coordinator()
    admitted = coordinator.admit(_admission())
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    coordinator.transition_to_reflecting(reflection_id, runtime_epoch=7)
    finalizing = coordinator.begin_close(
        reflection_id,
        runtime_epoch=7,
        terminal_state=SelfReflectionState.CLOSED,
        reason="reflection_completed",
        outcome=_outcome(reflection_id),
    )

    diverted = coordinator.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="input-during-finalizing",
            assistant_request_id="request-during-finalizing",
            source="text",
            text="这句话不能在收口重试时丢失",
        )
    )
    frozen = coordinator.freeze_inputs_for_close(
        reflection_id,
        runtime_epoch=7,
    )

    assert finalizing.snapshot is not None
    assert finalizing.snapshot.state is SelfReflectionState.FINALIZING
    assert diverted.status == "accepted"
    assert diverted.interruption_reply_eligible is False
    assert frozen.episode is not None
    assert [item.input_identity for item in frozen.episode.inputs] == [
        "input-during-finalizing"
    ]


def test_runtime_replacement_terminates_old_epoch_and_rejects_late_work() -> None:
    """热替换终止旧状态后，迟到输入、决定和重复终态都不能复活。"""

    coordinator = _coordinator()
    admitted = coordinator.admit(_admission())
    assert admitted.snapshot is not None
    reflection_id = admitted.snapshot.self_reflection_id
    coordinator.transition_to_reflecting(reflection_id, runtime_epoch=7)
    coordinator.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="old-input",
            assistant_request_id="old-request",
            source="text",
            text="旧世代正文",
        )
    )

    terminated = coordinator.replace_runtime_epoch(8)
    late_input = coordinator.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="late-input",
            assistant_request_id="late-request",
            source="text",
            text="迟到正文",
        )
    )
    late_close = coordinator.begin_close(
        reflection_id,
        runtime_epoch=7,
        terminal_state=SelfReflectionState.CLOSED_FAILED,
        reason="late_failure",
        outcome=SelfReflectionOutcomeFact.failed(
            self_reflection_id=reflection_id,
            source_activity_id="cognitive-activity-1",
            reason="late_failure",
        ),
    )
    late_admission = coordinator.admit(
        _admission(source_activity_id="late-cognitive", runtime_epoch=7)
    )

    assert len(terminated) == 1
    assert terminated[0].snapshot is not None
    assert terminated[0].snapshot.state is SelfReflectionState.TERMINATED
    assert terminated[0].episode is not None
    assert len(terminated[0].episode.inputs) == 1
    assert late_input.status == "not_diverted"
    assert late_close.status == "rejected"
    assert late_admission.status == "rejected"
    assert late_admission.reason == "runtime_epoch_mismatch"


def test_material_reference_rejects_paths_and_only_keeps_safe_names() -> None:
    """未处理片段不能长期保存用户材料的本地路径。"""

    try:
        UnattendedMaterialReference(
            file_name=r"C:\private\secret.wav",
            media_type="audio/wav",
        )
    except ValueError as exception:
        assert "file_name" in str(exception)
    else:
        raise AssertionError("材料绝对路径必须被拒绝")
