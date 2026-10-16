from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from companion.core.application.turn_admission import (
    TurnAdmissionSequencer,
    TurnSubmissionConflict,
    TurnSubmissionNotFound,
    TurnSubmissionQueueFull,
)
from companion.core.application.turn_submission import (
    TextTurnDispatch,
    TurnSubmissionOutcome,
)
from companion.foreground_self_reflection.models import (
    DivertFinalInputCommand,
    InputDiversionResult,
)
from companion.realtime.visible_output_journal import VisibleOutputSettlement


@pytest.mark.asyncio
async def test_runtime_transition_retries_each_material_without_losing_later_records() -> None:
    """首项释放失败不能丢掉后续材料，成功项也不能重复释放。"""
    sequencer, hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="a", message_text="第一条")
    await sequencer.reserve_text(submission_id="b", message_text="第二条")
    finish = hub.turn_materials.finish_turn
    failed = False

    def fail_first(context_id: str) -> list[str]:
        """让第一份材料首次释放失败，其余请求正常处理。"""
        nonlocal failed
        if context_id.endswith("_a") and not failed:
            failed = True
            raise RuntimeError("暂时无法释放")
        return finish(context_id)

    hub.turn_materials.finish_turn = Mock(side_effect=fail_first)
    with pytest.raises(RuntimeError):
        await sequencer.clear_pending_for_runtime_transition()
    assert sequencer.snapshot_payload()["items"] == []
    assert hub.turn_materials.finished_context_ids == ["context-text_submission_b"]
    assert not sequencer.allows_system_notice()
    assert not sequencer.reserve_timed_reminder("late-reminder")
    with pytest.raises(TurnSubmissionConflict):
        await sequencer.reserve_text(submission_id="late", message_text="迟到文本")
    await sequencer.clear_pending_for_runtime_transition()
    await sequencer.clear_pending_for_runtime_transition()
    assert hub.turn_materials.finished_context_ids == [
        "context-text_submission_b", "context-text_submission_a",
    ]
    assert not sequencer.allows_system_notice()
    sequencer.resume_after_runtime_transition()
    assert sequencer.allows_system_notice()


@pytest.mark.asyncio
async def test_runtime_transition_retries_release_broadcast_without_finishing_twice() -> None:
    """广播失败后仍保留资产编号，重试不能再次执行材料释放。"""
    sequencer, hub, lifecycle, _dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="a", message_text="第一条")
    await sequencer.reserve_text(submission_id="b", message_text="第二条")
    lifecycle.broadcast_changed = AsyncMock(side_effect=[RuntimeError("广播失败"), {}, {}, {}])
    with pytest.raises(RuntimeError):
        await sequencer.clear_pending_for_runtime_transition()
    with pytest.raises(TurnSubmissionConflict):
        sequencer.resume_after_runtime_transition()
    await sequencer.clear_pending_for_runtime_transition()
    assert hub.turn_materials.finished_context_ids == [
        "context-text_submission_a", "context-text_submission_b",
    ]
    assert [call.args[0] for call in lifecycle.broadcast_changed.await_args_list] == [
        ["asset-context-text_submission_a"], ["asset-context-text_submission_b"],
        ["asset-context-text_submission_a"],
    ]


class RecordingTurnMaterials:
    """记录 reservation 创建和材料上下文释放事实。"""

    def __init__(self) -> None:
        """初始化空记录。"""

        self.activated_turn_ids: list[str] = []
        self.finished_context_ids: list[str] = []

    def activate_turn(self, *, turn_id: str, created_at: str) -> SimpleNamespace:
        """为一条 reservation 创建稳定材料上下文。

        Args:
            turn_id: Backend 为文本提交生成的轮次编号。
            created_at: 创建轮次时的本地时间。

        Returns:
            带上下文编号和空附件集合的测试替身。
        """

        _ = created_at
        self.activated_turn_ids.append(turn_id)
        return SimpleNamespace(
            context_id=f"context-{turn_id}",
            attachments=[],
        )

    def finish_turn(self, context_id: str) -> list[str]:
        """释放一条不再派发的 reservation 材料。

        Args:
            context_id: reservation 创建的材料上下文编号。

        Returns:
            可由 Desktop 删除的受管资产编号。
        """

        self.finished_context_ids.append(context_id)
        return [f"asset-{context_id}"]


class RecordingMaterialLifecycle:
    """记录材料快照广播及释放批次。"""

    def __init__(self) -> None:
        """初始化空广播记录。"""

        self.changed_batches: list[list[str] | None] = []
        self.remembered_attachments: list[tuple[object, ...]] = []
        self.broadcast_started: asyncio.Event | None = None
        self.broadcast_gate: asyncio.Event | None = None

    def remember_recent_materials(self, attachments: object) -> bool:
        """记录正式成立输入交给近期注册表的附件集合。

        Args:
            attachments: Sequencer 在提交边界冻结的完整附件。

        Returns:
            测试附件为空，不需要额外广播，固定返回假。
        """

        self.remembered_attachments.append(tuple(attachments))
        return False

    async def broadcast_changed(
        self,
        released_managed_asset_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """记录一次材料变化广播。

        Args:
            released_managed_asset_ids: 本次状态变化后可释放的资产编号。

        Returns:
            测试不使用的空快照。
        """

        self.changed_batches.append(released_managed_asset_ids)
        if self.broadcast_started is not None:
            self.broadcast_started.set()
        if self.broadcast_gate is not None:
            await self.broadcast_gate.wait()
        return {}


class RecordingHub:
    """提供 Sequencer 需要的最小广播和材料端口。"""

    def __init__(self) -> None:
        """初始化空事件记录。"""

        self.turn_materials = RecordingTurnMaterials()
        self.events: list[dict[str, object]] = []
        self.published_domains: list[tuple[str, str]] = []
        self.publish_failures: dict[str, int] = {}
        self.broadcast_gate: asyncio.Event | None = None

    def publisher(self, domain: str) -> SimpleNamespace:
        """返回测试使用的 Turn Admission 领域 Publisher。

        Args:
            domain: Sequencer 必须选择的共享事件领域。

        Returns:
            只暴露 publish 端口的测试 Publisher。
        """

        assert domain in {"turn_admission", "assistant_output", "interaction"}
        return SimpleNamespace(
            publish=lambda event: self.publish(domain=domain, event=event)
        )

    async def publish(self, *, domain: str, event: dict[str, object]) -> None:
        """记录一条权威控制事件。

        Args:
            domain: 事件在共享目录登记的领域。
            event: Sequencer 产生的控制事件。
        """

        remaining_failures = self.publish_failures.get(domain, 0)
        if remaining_failures > 0:
            self.publish_failures[domain] = remaining_failures - 1
            raise RuntimeError(f"injected {domain} publish failure")
        self.events.append(event)
        self.published_domains.append((domain, str(event["type"])))
        if self.broadcast_gate is not None:
            await self.broadcast_gate.wait()


class RecordingDispatcher:
    """记录已越过顺序屏障的文本并返回可配置结果。"""

    def __init__(self) -> None:
        """初始化成功派发模式。"""

        self.dispatched: list[TextTurnDispatch] = []
        self.outcomes: list[bool | BaseException] = []
        self.before_return: Callable[[TurnSubmissionOutcome], None] | None = None

    async def __call__(self, dispatch: TextTurnDispatch) -> TurnSubmissionOutcome:
        """记录一条派发并返回助手启动结果。

        Args:
            dispatch: 已获准的文本提交。

        Returns:
            与当前 submission 对应的助手启动结果。

        Raises:
            BaseException: 测试预置派发异常时原样抛出。
        """

        self.dispatched.append(dispatch)
        configured_outcome = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(configured_outcome, BaseException):
            raise configured_outcome
        if isinstance(configured_outcome, TurnSubmissionOutcome):
            outcome = configured_outcome
            if self.before_return is not None:
                self.before_return(outcome)
            return outcome
        outcome = TurnSubmissionOutcome(
            request_id=dispatch.request_id,
            assistant_request_started=configured_outcome,
        )
        if self.before_return is not None:
            self.before_return(outcome)
        return outcome


def build_sequencer(
    *,
    max_pending_submissions: int = 10,
    try_divert_reserved_text: Callable[
        [DivertFinalInputCommand], InputDiversionResult
    ]
    | None = None,
) -> tuple[
    TurnAdmissionSequencer,
    RecordingHub,
    RecordingMaterialLifecycle,
    RecordingDispatcher,
    list[str],
]:
    """构造全部依赖都可观察的 Turn Sequencer。

    Args:
        max_pending_submissions: 测试使用的待发文本容量。
        try_divert_reserved_text: Reservation 阶段可选的 Self-Reflection 归属端口。

    Returns:
        Sequencer、广播中心、材料生命周期、派发器和 Notice 取消记录。
    """

    hub = RecordingHub()
    material_lifecycle = RecordingMaterialLifecycle()
    dispatcher = RecordingDispatcher()
    notice_cancellations: list[str] = []
    sequencer = TurnAdmissionSequencer(
        hub=hub,  # type: ignore[arg-type]
        material_lifecycle=material_lifecycle,  # type: ignore[arg-type]
        dispatch_text_turn=dispatcher,
        cancel_system_notice=notice_cancellations.append,
        try_divert_reserved_text=try_divert_reserved_text,
        current_runtime_epoch=lambda: 7,
        max_pending_submissions=max_pending_submissions,
    )
    return (
        sequencer,
        hub,
        material_lifecycle,
        dispatcher,
        notice_cancellations,
    )


@pytest.mark.asyncio
async def test_runtime_transition_clears_voice_hold_and_output_barrier() -> None:
    """语音中途到期后丢弃旧占位，重新激活能接纳新的提醒和文字。"""
    sequencer, *_ = build_sequencer()
    sequencer.hold_for_voice("expired-voice")
    assert sequencer.reserve_system_notice("new-notice") is False
    await sequencer.clear_pending_for_runtime_transition()
    assert sequencer.reserve_system_notice("new-notice") is False
    sequencer.resume_after_runtime_transition()
    assert sequencer.reserve_system_notice("new-notice") is True


def settled(request_id: str) -> VisibleOutputSettlement:
    """构造一条最小可见输出结算事实。

    Args:
        request_id: 需要释放顺序屏障的助手请求编号。

    Returns:
        稳定对话正常完成的结算事实。
    """

    return VisibleOutputSettlement(
        settlement_id=f"settlement-{request_id}",
        request_id=request_id,
        generation_id=f"generation-{request_id}",
        history_policy="stable_dialogue",
        outcome="completed",
        visible_text="回复",
        progress_references=(),
        derived_from_activity_ids=(),
        settled_at=1.0,
    )


async def wait_until(
    condition: Callable[[], bool],
    *,
    attempts: int = 20,
) -> None:
    """让同步回调调度的短任务获得执行机会。

    Args:
        condition: 满足时结束等待的条件。
        attempts: 最多让出事件循环的次数。

    Raises:
        AssertionError: 条件在限定轮数内仍未满足。
    """

    for _ in range(attempts):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("异步 Turn 状态没有在限定轮数内收敛")


@pytest.mark.asyncio
async def test_reservation_is_immediately_visible_and_idempotent() -> None:
    """重复 reservation 只能重读同一事实，不能再次冻结材料或推进 revision。"""

    sequencer, hub, material_lifecycle, _dispatcher, _cancellations = (
        build_sequencer()
    )

    first = await sequencer.reserve_text(
        submission_id="submission-a",
        message_text="第一条",
    )
    second = await sequencer.reserve_text(
        submission_id="submission-a",
        message_text="第一条",
    )

    assert first == second
    assert first == {
        "revision": 1,
        "items": [
            {
                "submission_id": "submission-a",
                "message_text": "第一条",
                "state": "preparing",
                "created_order": 1,
                "error_message": None,
            }
        ],
    }
    assert hub.turn_materials.activated_turn_ids == [
        "text_submission_submission-a"
    ]
    assert material_lifecycle.changed_batches == [None]


@pytest.mark.asyncio
async def test_reservation_diverts_active_self_reflection_before_context_preparation() -> None:
    """活跃 Self-Reflection 必须在 Reservation 阶段接管文本并延迟稳定历史。"""

    commands: list[DivertFinalInputCommand] = []

    def divert(command: DivertFinalInputCommand) -> InputDiversionResult:
        """记录早期归属命令并返回已经稳定写入 Episode 的事实。"""

        commands.append(command)
        return InputDiversionResult(
            status="accepted",
            reason="input_diverted_to_episode",
            episode_id="episode-a",
            history_disposition="unattended_episode",
            runtime_epoch=7,
        )

    sequencer, hub, lifecycle, dispatcher, _cancellations = build_sequencer(
        try_divert_reserved_text=divert,
    )

    snapshot = await sequencer.reserve_text(
        submission_id="submission-episode-a",
        message_text="反思期间的新输入",
    )

    assert snapshot["items"] == []
    assert len(commands) == 1
    assert commands[0].input_identity == "submission-episode-a"
    assert commands[0].assistant_request_id == "text-input-submission-episode-a"
    assert dispatcher.dispatched == []
    assert hub.turn_materials.finished_context_ids == [
        "context-text_submission_submission-episode-a"
    ]
    assert lifecycle.changed_batches[-1] == [
        "asset-context-text_submission_submission-episode-a"
    ]
    transcript = next(
        event for event in hub.events if event["type"] == "user.final_transcript"
    )
    assert transcript["payload"]["history_disposition"] == "unattended_episode"
    assert transcript["payload"]["episode_id"] == "episode-a"


@pytest.mark.asyncio
async def test_diverted_text_retries_transcript_and_always_releases_material() -> None:
    """Episode 已接纳后，transcript 首次失败也不能丢事件或泄漏材料。"""

    def divert(_command: DivertFinalInputCommand) -> InputDiversionResult:
        """把当前文本稳定归入测试 Episode。"""

        return InputDiversionResult(
            status="accepted",
            reason="input_diverted_to_episode",
            episode_id="episode-a",
            history_disposition="unattended_episode",
            runtime_epoch=7,
        )

    sequencer, hub, _lifecycle, dispatcher, _cancellations = build_sequencer(
        try_divert_reserved_text=divert,
    )
    hub.publish_failures["interaction"] = 1

    snapshot = await sequencer.reserve_text(
        submission_id="submission-retryable-transcript",
        message_text="这条话已经属于反思片段",
    )
    await wait_until(
        lambda: any(
            event.get("type") == "user.final_transcript"
            for event in hub.events
        )
    )

    assert snapshot["items"] == []
    assert dispatcher.dispatched == []
    assert hub.turn_materials.finished_context_ids == [
        "context-text_submission_submission-retryable-transcript"
    ]
    transcript = next(
        event for event in hub.events if event.get("type") == "user.final_transcript"
    )
    assert transcript["payload"]["episode_id"] == "episode-a"  # type: ignore[index]


@pytest.mark.asyncio
async def test_diverted_reservation_is_frozen_before_material_broadcast() -> None:
    """Episode 已接纳的文本不能在材料广播期间再被编辑或删除。"""

    commands: list[DivertFinalInputCommand] = []

    def divert(command: DivertFinalInputCommand) -> InputDiversionResult:
        """记录归属正文并返回已稳定写入 Episode 的事实。"""

        commands.append(command)
        return InputDiversionResult(
            status="accepted",
            reason="input_diverted_to_episode",
            episode_id="episode-a",
            history_disposition="unattended_episode",
            runtime_epoch=7,
        )

    sequencer, _hub, lifecycle, _dispatcher, _cancellations = build_sequencer(
        try_divert_reserved_text=divert,
    )
    lifecycle.broadcast_started = asyncio.Event()
    lifecycle.broadcast_gate = asyncio.Event()

    reservation = asyncio.create_task(
        sequencer.reserve_text(
            submission_id="submission-a",
            message_text="原始正文",
        )
    )
    await lifecycle.broadcast_started.wait()

    try:
        assert [command.text for command in commands] == ["原始正文"]
        with pytest.raises(TurnSubmissionNotFound):
            await sequencer.edit_text(
                submission_id="submission-a",
                message_text="不应写入的正文",
            )
        with pytest.raises(TurnSubmissionNotFound):
            await sequencer.delete_text("submission-a")
    finally:
        lifecycle.broadcast_gate.set()
        snapshot = await reservation
    assert snapshot["items"] == []


@pytest.mark.asyncio
async def test_ordinary_reservation_remains_editable_during_material_broadcast() -> None:
    """没有活跃 Self-Reflection 时，材料广播不能提前冻结普通待发文本。"""

    def do_not_divert(command: DivertFinalInputCommand) -> InputDiversionResult:
        """返回当前没有活跃 Self-Reflection 的权威裁决。"""

        return InputDiversionResult(
            status="not_diverted",
            reason="self_reflection_not_active",
            runtime_epoch=command.runtime_epoch,
        )

    sequencer, _hub, lifecycle, _dispatcher, _cancellations = build_sequencer(
        try_divert_reserved_text=do_not_divert,
    )
    lifecycle.broadcast_started = asyncio.Event()
    lifecycle.broadcast_gate = asyncio.Event()

    reservation = asyncio.create_task(
        sequencer.reserve_text(
            submission_id="submission-a",
            message_text="原始正文",
        )
    )
    await lifecycle.broadcast_started.wait()
    try:
        edited = await sequencer.edit_text(
            submission_id="submission-a",
            message_text="编辑后的正文",
        )
        assert edited["items"][0]["message_text"] == "编辑后的正文"  # type: ignore[index]
    finally:
        lifecycle.broadcast_gate.set()
        snapshot = await reservation
    assert snapshot["items"][0]["message_text"] == "编辑后的正文"  # type: ignore[index]


@pytest.mark.asyncio
async def test_context_ready_dispatches_and_settlement_releases_next_text() -> None:
    """第二条文本必须等第一条助手输出完成归档后才能正式派发。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="submission-a", message_text="第一条")
    await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id="environment-a",
    )
    await sequencer.reserve_text(submission_id="submission-b", message_text="第二条")
    queued_snapshot = await sequencer.commit_context(
        submission_id="submission-b",
        environment_snapshot_id="environment-b",
    )

    assert [item.submission_id for item in dispatcher.dispatched] == ["submission-a"]
    assert queued_snapshot["items"][0]["state"] == "queued"  # type: ignore[index]

    sequencer.handle_output_settled(settled("text-input-submission-a"))
    await wait_until(lambda: len(dispatcher.dispatched) == 2)

    assert [item.submission_id for item in dispatcher.dispatched] == [
        "submission-a",
        "submission-b",
    ]


@pytest.mark.asyncio
async def test_failed_preparation_drains_following_queued_text() -> None:
    """前条文本准备失败后，后条已经排队的文本必须自动继续派发。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="submission-a", message_text="第一条")
    await sequencer.reserve_text(submission_id="submission-b", message_text="第二条")
    await sequencer.commit_context(
        submission_id="submission-b",
        environment_snapshot_id="environment-b",
    )

    assert dispatcher.dispatched == []

    await sequencer.fail_preparation(submission_id="submission-a")
    await wait_until(lambda: len(dispatcher.dispatched) == 1)

    assert dispatcher.dispatched[0].submission_id == "submission-b"


@pytest.mark.asyncio
async def test_preparing_head_preserves_reservation_order() -> None:
    """后创建文本即使先拿到环境，也不能越过仍在准备的更早 reservation。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="submission-a", message_text="第一条")
    await sequencer.reserve_text(submission_id="submission-b", message_text="第二条")

    await sequencer.commit_context(
        submission_id="submission-b",
        environment_snapshot_id="environment-b",
    )
    assert dispatcher.dispatched == []

    await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id="environment-a",
    )
    assert [item.submission_id for item in dispatcher.dispatched] == ["submission-a"]

    sequencer.handle_output_settled(settled("text-input-submission-a"))
    await wait_until(lambda: len(dispatcher.dispatched) == 2)
    assert [item.submission_id for item in dispatcher.dispatched] == [
        "submission-a",
        "submission-b",
    ]


@pytest.mark.asyncio
async def test_pending_queue_has_hard_capacity() -> None:
    """达到容量后拒绝新 reservation，已有文本和顺序保持不变。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer(
        max_pending_submissions=2
    )
    await sequencer.reserve_text(submission_id="submission-a", message_text="第一条")
    await sequencer.reserve_text(submission_id="submission-b", message_text="第二条")

    with pytest.raises(TurnSubmissionQueueFull):
        await sequencer.reserve_text(
            submission_id="submission-c",
            message_text="第三条",
        )

    assert [item["submission_id"] for item in sequencer.snapshot_payload()["items"]] == [
        "submission-a",
        "submission-b",
    ]


@pytest.mark.asyncio
async def test_vad_only_holds_and_invalid_final_asr_resumes_dispatch() -> None:
    """VAD 开口不修改气泡状态，无效 Final ASR 解除 hold 后自动发送原文本。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    sequencer.hold_for_voice("voice-a")
    await sequencer.reserve_text(submission_id="submission-a", message_text="文本")
    snapshot = await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id=None,
    )

    assert snapshot["items"][0]["state"] == "queued"  # type: ignore[index]
    assert dispatcher.dispatched == []

    sequencer.resolve_voice_turn(turn_id="voice-a", assistant_request_id=None)
    await wait_until(lambda: len(dispatcher.dispatched) == 1)
    assert dispatcher.dispatched[0].submission_id == "submission-a"


@pytest.mark.asyncio
async def test_valid_voice_pauses_pending_text_until_explicit_resume() -> None:
    """有效语音暂停全部未派发文本，语音结算后也不能擅自补发。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    sequencer.hold_for_voice("voice-a")
    await sequencer.reserve_text(submission_id="submission-a", message_text="文本")
    await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id=None,
    )

    sequencer.resolve_voice_turn(
        turn_id="voice-a",
        assistant_request_id="final-asr-voice-a",
    )
    await wait_until(
        lambda: sequencer.snapshot_payload()["items"][0]["state"] == "paused"  # type: ignore[index]
    )
    sequencer.handle_output_settled(settled("final-asr-voice-a"))
    await asyncio.sleep(0)

    assert dispatcher.dispatched == []
    await sequencer.resume_text("submission-a")
    assert dispatcher.dispatched[0].submission_id == "submission-a"


@pytest.mark.asyncio
async def test_paused_preparing_text_still_waits_for_context_after_resume() -> None:
    """语音暂停发生在环境准备完成前时，恢复只回到 preparing，不伪造环境事实。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="submission-a", message_text="文本")
    sequencer.hold_for_voice("voice-a")
    sequencer.resolve_voice_turn(
        turn_id="voice-a",
        assistant_request_id="final-asr-voice-a",
    )

    sequencer.handle_output_settled(settled("final-asr-voice-a"))
    snapshot = await sequencer.resume_text("submission-a")

    assert snapshot["items"][0]["state"] == "preparing"  # type: ignore[index]
    assert dispatcher.dispatched == []


@pytest.mark.asyncio
async def test_edit_delete_and_retry_keep_one_authoritative_record() -> None:
    """编辑原位改正文，删除释放材料，失败重试回到重新准备环境的状态。"""

    sequencer, hub, lifecycle, _dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="submission-a", message_text="旧正文")
    edited = await sequencer.edit_text(
        submission_id="submission-a",
        message_text="新正文",
    )
    assert edited["items"][0]["message_text"] == "新正文"  # type: ignore[index]

    failed = await sequencer.fail_preparation(submission_id="submission-a")
    assert failed["items"][0]["state"] == "failed"  # type: ignore[index]
    retried = await sequencer.retry_text("submission-a")
    assert retried["items"][0]["state"] == "preparing"  # type: ignore[index]

    await sequencer.delete_text("submission-a")
    assert sequencer.snapshot_payload()["items"] == []
    assert hub.turn_materials.finished_context_ids == [
        "context-text_submission_submission-a"
    ]
    assert lifecycle.changed_batches[-1] == [
        "asset-context-text_submission_submission-a"
    ]


@pytest.mark.asyncio
async def test_dispatch_failure_keeps_text_as_retryable_failed() -> None:
    """正式用户消息公开前派发异常时保留原记录，避免用户输入静默丢失。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    dispatcher.outcomes.append(RuntimeError("broadcast failed"))
    await sequencer.reserve_text(submission_id="submission-a", message_text="文本")

    snapshot = await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id=None,
    )

    assert snapshot["items"][0]["state"] == "failed"  # type: ignore[index]
    assert snapshot["items"][0]["error_message"] == "文本发送失败，请重试。"  # type: ignore[index]


@pytest.mark.asyncio
async def test_assistant_start_failure_cleans_material_and_continues_queue() -> None:
    """用户消息已公开但助手未启动时，清理该轮材料并继续派发下一条。"""

    sequencer, hub, lifecycle, dispatcher, _cancellations = build_sequencer()
    dispatcher.outcomes.extend([False, True])
    await sequencer.reserve_text(submission_id="submission-a", message_text="第一条")
    await sequencer.reserve_text(submission_id="submission-b", message_text="第二条")
    await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id=None,
    )
    await sequencer.commit_context(
        submission_id="submission-b",
        environment_snapshot_id=None,
    )

    assert [item.submission_id for item in dispatcher.dispatched] == [
        "submission-a",
        "submission-b",
    ]
    assert hub.turn_materials.finished_context_ids == [
        "context-text_submission_submission-a"
    ]
    assert lifecycle.changed_batches[-1] == [
        "asset-context-text_submission_submission-a"
    ]
    failed_events = [
        event for event in hub.events if event["type"] == "assistant.request_failed"
    ]
    assert failed_events == [
        {
            "type": "assistant.request_failed",
            "payload": {
                    "request_id": "text-input-submission-a",
                    "code": "request_start_failed",
                    "message": "消息已经发送，但我现在没能开始回复，请稍后再试。",
            },
        }
    ]
    assert ("assistant_output", "assistant.request_failed") in hub.published_domains


@pytest.mark.asyncio
async def test_diverted_text_releases_material_without_request_failed_fact() -> None:
    """进入 Attention episode 的文本不伪造普通助手启动失败。"""

    sequencer, hub, lifecycle, dispatcher, _cancellations = build_sequencer()
    dispatcher.outcomes.append(
        TurnSubmissionOutcome(
            request_id="text-input-submission-a",
            assistant_request_started=False,
            diverted_to_episode=True,
        )
    )

    await sequencer.reserve_text(submission_id="submission-a", message_text="插话")
    await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id=None,
    )

    assert hub.turn_materials.finished_context_ids == [
        "context-text_submission_submission-a"
    ]
    assert lifecycle.changed_batches[-1] == [
        "asset-context-text_submission_submission-a"
    ]
    assert [
        event for event in hub.events if event["type"] == "assistant.request_failed"
    ] == []


@pytest.mark.asyncio
async def test_pending_text_cancels_only_system_notice_once() -> None:
    """待发文本只取消独立 System Notice，普通用户回复继续等待 settlement。"""

    sequencer, _hub, _lifecycle, _dispatcher, cancellations = build_sequencer()
    sequencer.register_assistant_request("notice-a", "system_notice")
    await sequencer.reserve_text(submission_id="submission-a", message_text="文本")
    await sequencer.reserve_text(submission_id="submission-b", message_text="另一条")

    assert cancellations == ["notice-a"]
    sequencer.handle_output_settled(settled("notice-a"))
    sequencer.register_assistant_request("reply-a", "final_asr")
    await sequencer.reserve_text(submission_id="submission-c", message_text="第三条")
    assert cancellations == ["notice-a"]


@pytest.mark.asyncio
async def test_timed_reminder_settlement_releases_cancellation_deduplication() -> None:
    """Reminder Notice 结算后必须释放取消去重记录，避免请求身份残留。"""

    sequencer, _hub, _lifecycle, _dispatcher, cancellations = build_sequencer()
    sequencer.register_assistant_request(
        "timed-reminder-a",
        "timed_reminder_notice",
    )

    await sequencer.reserve_text(submission_id="submission-a", message_text="知道了")

    assert cancellations == ["timed-reminder-a"]
    sequencer.handle_output_settled(settled("timed-reminder-a"))
    assert (
        "timed-reminder-a"
        not in sequencer._system_notice_cancellation_requested  # noqa: SLF001
    )


def test_unsettled_assistant_request_blocks_new_system_notice() -> None:
    """任一助手请求尚未结算时，独立 System Notice 都不能抢占输出顺序。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()

    assert sequencer.allows_system_notice() is True
    sequencer.register_assistant_request("reply-a", "final_asr")
    assert sequencer.allows_system_notice() is False

    sequencer.handle_output_settled(settled("reply-a"))
    assert sequencer.allows_system_notice() is True


@pytest.mark.asyncio
async def test_duplicate_settlement_is_idempotent() -> None:
    """同一输出重复结算不会重复派发或破坏下一条请求屏障。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="submission-a", message_text="第一条")
    await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id=None,
    )
    await sequencer.reserve_text(submission_id="submission-b", message_text="第二条")
    await sequencer.commit_context(
        submission_id="submission-b",
        environment_snapshot_id=None,
    )

    first_settlement = settled("text-input-submission-a")
    sequencer.handle_output_settled(first_settlement)
    await wait_until(lambda: len(dispatcher.dispatched) == 2)
    sequencer.handle_output_settled(first_settlement)
    await asyncio.sleep(0)

    assert [item.submission_id for item in dispatcher.dispatched] == [
        "submission-a",
        "submission-b",
    ]


@pytest.mark.asyncio
async def test_text_request_barrier_exists_before_async_dispatch_can_settle() -> None:
    """极快的直接运行时结算不能抢在文本请求屏障登记之前。"""

    sequencer, _hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    dispatcher.before_return = lambda outcome: sequencer.handle_output_settled(
        settled(outcome.request_id)
    )

    await sequencer.reserve_text(submission_id="submission-a", message_text="第一条")
    await sequencer.commit_context(
        submission_id="submission-a",
        environment_snapshot_id=None,
    )
    await sequencer.reserve_text(submission_id="submission-b", message_text="第二条")
    await sequencer.commit_context(
        submission_id="submission-b",
        environment_snapshot_id=None,
    )

    assert [item.submission_id for item in dispatcher.dispatched] == [
        "submission-a",
        "submission-b",
    ]


@pytest.mark.asyncio
async def test_voice_resolution_wins_while_context_broadcast_is_pending() -> None:
    """环境提交与 Final ASR 交错时，有效语音必须在派发检查前暂停文本。"""

    sequencer, hub, _lifecycle, dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="submission-a", message_text="文本")
    hub.broadcast_gate = asyncio.Event()
    commit_task: asyncio.Task[dict[str, object]] = asyncio.create_task(
        sequencer.commit_context(
            submission_id="submission-a",
            environment_snapshot_id="environment-a",
        )
    )
    await wait_until(
        lambda: sequencer.snapshot_payload()["items"][0]["state"] == "queued"  # type: ignore[index]
    )

    sequencer.hold_for_voice("voice-a")
    sequencer.resolve_voice_turn(
        turn_id="voice-a",
        assistant_request_id="final-asr-voice-a",
    )
    hub.broadcast_gate.set()
    await commit_task

    assert dispatcher.dispatched == []
    assert sequencer.snapshot_payload()["items"][0]["state"] == "paused"  # type: ignore[index]


@pytest.mark.asyncio
async def test_invalid_state_operations_are_rejected() -> None:
    """失败和暂停状态必须通过各自显式命令恢复，不能被 context 提交绕过。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(submission_id="submission-a", message_text="文本")
    await sequencer.fail_preparation(submission_id="submission-a")

    with pytest.raises(TurnSubmissionConflict):
        await sequencer.commit_context(
            submission_id="submission-a",
            environment_snapshot_id=None,
        )
    with pytest.raises(TurnSubmissionConflict):
        await sequencer.resume_text("submission-a")


@pytest.mark.asyncio
async def test_final_asr_dispatch_waits_for_previous_visible_output_settlement() -> None:
    """Final ASR 可以立即收口输入，但提交用户记忆前必须等待上一轮正文归档。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    dispatched_request_ids: list[str] = []
    sequencer.register_assistant_request("old-reply", "text_input")

    sequencer.resolve_voice_turn(
        turn_id="voice-a",
        assistant_request_id="final-asr-voice-a",
        dispatch_request=lambda: (
            dispatched_request_ids.append("final-asr-voice-a") or True
        ),
    )

    assert dispatched_request_ids == []

    sequencer.handle_output_settled(settled("old-reply"))
    await wait_until(lambda: bool(dispatched_request_ids))

    assert dispatched_request_ids == ["final-asr-voice-a"]
    assert sequencer.allows_system_notice() is False


def test_voice_invalidates_uncommitted_system_notice_reservation() -> None:
    """Final ASR 抢先到达时，尚未进入 Bridge 的 System Notice 必须原地回滚。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    submitted_notices: list[str] = []

    assert sequencer.reserve_system_notice("system-notice-a") is True
    sequencer.resolve_voice_turn(
        turn_id="voice-a",
        assistant_request_id="final-asr-voice-a",
        dispatch_request=lambda: True,
    )

    accepted = sequencer.submit_reserved_system_notice(
        "system-notice-a",
        lambda: submitted_notices.append("system-notice-a") or True,
    )

    assert accepted is False
    assert submitted_notices == []


def test_timed_reminder_can_take_over_ordinary_unsettled_request() -> None:
    """定时提醒必须越过普通回复屏障并保留同一原子提交位置。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    submitted: list[str] = []
    sequencer.register_assistant_request("ordinary-request", "text_input")

    assert sequencer.reserve_timed_reminder("timed-reminder-a") is True
    accepted = sequencer.submit_reserved_timed_reminder(
        "timed-reminder-a",
        lambda: submitted.append("timed-reminder-a") or True,
    )

    assert accepted is True
    assert submitted == ["timed-reminder-a"]


def test_timed_reminder_reserves_before_clearing_voice_hold() -> None:
    """Reminder 必须先取得准入位置，再由输入 owner 清理用户语音 hold。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    submitted: list[str] = []
    sequencer.hold_for_voice("voice-a")

    assert sequencer.reserve_timed_reminder("timed-reminder-a") is True
    sequencer.resolve_voice_turn(
        turn_id="voice-a",
        assistant_request_id=None,
    )
    accepted = sequencer.submit_reserved_timed_reminder(
        "timed-reminder-a",
        lambda: submitted.append("timed-reminder-a") or True,
    )

    assert accepted is True
    assert submitted == ["timed-reminder-a"]


@pytest.mark.asyncio
async def test_text_arriving_after_timed_reminder_reservation_waits_behind_it() -> None:
    """Reminder 先取得 reservation 后，新文本不能反向撤销已开始的 takeover。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    submitted: list[str] = []
    assert sequencer.reserve_timed_reminder("timed-reminder-a") is True

    await sequencer.reserve_text(
        submission_id="submission-after-reminder",
        message_text="提醒之后再处理",
    )
    accepted = sequencer.submit_reserved_timed_reminder(
        "timed-reminder-a",
        lambda: submitted.append("timed-reminder-a") or True,
    )

    assert accepted is True
    assert submitted == ["timed-reminder-a"]


@pytest.mark.parametrize(
    "protected_source",
    [
        "system_notice",
        "timed_reminder_notice",
        "self_reflection_entry",
        "self_reflection_recovery",
        "self_reflection_interruption_reply",
    ],
)
def test_timed_reminder_never_takes_over_protected_output(
    protected_source: str,
) -> None:
    """特殊独立输出未结算时，定时提醒只能等待。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    sequencer.register_assistant_request(
        "protected-request",
        protected_source,
    )

    assert sequencer.reserve_timed_reminder("timed-reminder-a") is False


@pytest.mark.asyncio
async def test_timed_reminder_rejects_pending_text_before_input_takeover() -> None:
    """已经接纳的用户文本必须阻止 Reminder 开始不可逆输入清理。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    await sequencer.reserve_text(
        submission_id="submission-a",
        message_text="先处理这句话",
    )

    assert sequencer.reserve_timed_reminder("timed-reminder-a") is False


def test_timed_reminder_rejects_voice_request_waiting_for_dispatch() -> None:
    """Final ASR 已经形成请求后，Reminder 不能再把它当作未完成语音丢弃。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    sequencer.register_assistant_request("ordinary-request", "text_input")
    sequencer.resolve_voice_turn(
        turn_id="voice-a",
        assistant_request_id="final-asr-a",
        dispatch_request=lambda: True,
    )

    assert sequencer.reserve_timed_reminder("timed-reminder-a") is False


def test_releasing_timed_reminder_reservation_allows_next_system_notice() -> None:
    """Reminder 构造失败释放 reservation 后，普通 Notice 必须恢复准入。"""

    sequencer, _hub, _lifecycle, _dispatcher, _cancellations = build_sequencer()
    assert sequencer.reserve_timed_reminder("timed-reminder-a") is True

    sequencer.release_notice_reservation("timed-reminder-a")

    assert sequencer.reserve_system_notice("system-notice-a") is True
