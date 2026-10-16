"""验证前台自我反思主进程编排的事务和身份边界。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from companion.character_cards.store import CharacterCardStore
from companion.core.application.material_lifecycle import MaterialLifecycleService
from companion.foreground_self_reflection.models import (
    DivertFinalInputCommand,
    ExpressionSettlementDisposition,
    SelfReflectionAdmissionCommand,
    SelfReflectionOutcomeFact,
    SelfReflectionOutcomeKind,
    SelfReflectionState,
    SelfReflectionToolFact,
    SelfReflectionToolStatus,
)
from companion.foreground_self_reflection.contracts import (
    SelfReflectionEntryContext,
    SelfReflectionInterruptionReplyContext,
    SelfReflectionRecoveryContext,
    SelfReflectionToolCommitDecisionCommand,
)
from companion.foreground_self_reflection.policy import (
    input_diversion_blocks_normal_request,
)
from companion.foreground_self_reflection.registry import ForegroundAttentionCoordinator
from companion.foreground_self_reflection.service import ForegroundSelfReflectionService
from companion.foreground_self_reflection.tool_commit_broker import (
    SelfReflectionToolCommitBroker,
)
from companion.realtime.assistant_events import AssistantRequest, RuntimeEvent
from companion.realtime.assistant_process_handle import AssistantProcessReplacement
from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
from companion.realtime.visible_output_journal import VisibleOutputSettlement
from companion.runtime_memory.models import RuntimeMemorySnapshot
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.runtime_memory.unattended_episode import UnattendedEpisodeIdentity
from companion.materials.models import MaterialAttachment
from companion.materials.turn_context import TurnMaterialContextService


CARDS_DIR = Path(__file__).resolve().parents[2] / "data" / "character-cards"
CHARACTER_CARD = CharacterCardStore(CARDS_DIR).load_card("xiaolian")


class _RequestSnapshots:
    """保存测试使用的唯一来源请求。"""

    def __init__(self, request: AssistantRequest) -> None:
        self.request = request

    def get_request(self, request_id: str) -> AssistantRequest | None:
        """按请求编号返回来源请求。"""

        return self.request if request_id == self.request.request_id else None


class _MaterialLifecycle:
    """记录材料租约，并允许注入一次释放失败。"""

    def __init__(
        self,
        *,
        fail_first_release: bool = False,
        fail_retain: bool = False,
    ) -> None:
        self.fail_first_release = fail_first_release
        self.fail_retain = fail_retain
        self.retain_calls: list[str] = []
        self.release_calls: list[str] = []
        self.lease_counts: dict[str, int] = {}

    def retain_turn(self, context_id: str) -> bool:
        """记录一次成功的材料保留。"""

        self.retain_calls.append(context_id)
        if self.fail_retain:
            return False
        self.lease_counts[context_id] = self.lease_counts.get(context_id, 0) + 1
        return True

    def release_turn(self, context_id: str) -> object:
        """记录材料释放，并按配置让第一次调用失败。"""

        self.release_calls.append(context_id)
        if self.fail_first_release and len(self.release_calls) == 1:
            raise RuntimeError("temporary_material_release_failure")
        current_count = self.lease_counts.get(context_id, 0)
        if current_count > 0:
            if current_count == 1:
                self.lease_counts.pop(context_id, None)
            else:
                self.lease_counts[context_id] = current_count - 1
        return []


def _run_overlapping_reapers(
    service: ForegroundSelfReflectionService,
    *,
    cleanup_started: Event,
    allow_cleanup_to_finish: Event,
) -> tuple[list[int], bool]:
    """让两个 reaper 在第一轮清理尚未结束时重叠执行。

    Args:
        service: 已经保存待重试清理凭据的自我反思服务。
        cleanup_started: 第一轮清理进入阻塞副作用后设置的事件。
        allow_cleanup_to_finish: 测试允许阻塞副作用继续执行的事件。

    Returns:
        两轮 reaper 的完成数量，以及第二轮是否在第一轮解锁前结束。
    """

    completed_counts: list[int] = []
    second_reaper_started = Event()

    def run_first_reaper() -> None:
        """执行会领取清理凭据并阻塞在副作用中的第一轮 reaper。"""

        completed_counts.append(service.reap_expired())

    def run_second_reaper() -> None:
        """记录线程已经启动，再尝试领取相同的清理凭据。"""

        second_reaper_started.set()
        completed_counts.append(service.reap_expired())

    first_reaper = Thread(target=run_first_reaper)
    second_reaper = Thread(target=run_second_reaper)
    first_reaper.start()
    first_cleanup_was_reached = cleanup_started.wait(timeout=2.0)

    second_reaper.start()
    second_reaper_did_start = second_reaper_started.wait(timeout=2.0)
    second_reaper.join(timeout=1.0)
    second_finished_while_first_blocked = not second_reaper.is_alive()

    # 无论断言是否满足，都先放行线程，避免失败用例把测试进程挂死。
    allow_cleanup_to_finish.set()
    first_reaper.join(timeout=2.0)
    second_reaper.join(timeout=2.0)

    assert first_cleanup_was_reached
    assert second_reaper_did_start
    assert not first_reaper.is_alive()
    assert not second_reaper.is_alive()
    return completed_counts, second_finished_while_first_blocked


def _request() -> AssistantRequest:
    """构造具备恢复所需全部冻结事实的来源请求。"""

    return AssistantRequest(
        request_id="foreground-request-1",
        source_session_id="desktop-text",
        interaction_session_id="interaction-1",
        character_card=CHARACTER_CARD,
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="把 input.wav 转成 mp3",
        source="text_input",
        source_turn_id=None,
        requested_at=1.0,
        turn_material_context_id="material-context-1",
        foreground_context={
            "active_turn_material_context": {"id": "material-context-1"}
        },
        runtime_memory_snapshot=RuntimeMemorySnapshot(),
    )


def _service(
    *,
    clock: list[float] | None = None,
    runtime_memory: RuntimeMemoryStore | None = None,
    material_lifecycle: _MaterialLifecycle | None = None,
    submit_expression: Any | None = None,
    request: AssistantRequest | None = None,
    material_event_sink: list[dict[str, object]] | None = None,
    episode_event_sink: list[RuntimeEvent] | None = None,
    wall_clock: list[datetime] | None = None,
    current_runtime_epoch: list[int] | None = None,
    submit_control: Any | None = None,
    request_recovery_fallback: Any | None = None,
    current_foreground_output_identity: tuple[str, str | None] | None = None,
    runtime_epoch_mutation_lock: Any | None = None,
) -> tuple[
    ForegroundSelfReflectionService,
    ForegroundAttentionCoordinator,
    RuntimeMemoryStore,
    _MaterialLifecycle,
    list[object],
    list[tuple[object, AssistantRequest]],
]:
    """构造身份、时钟和副作用都可观测的完整编排服务。"""

    now = clock if clock is not None else [10.0]
    identities: dict[str, int] = {}

    def next_identity(prefix: str) -> str:
        identities[prefix] = identities.get(prefix, 0) + 1
        return f"{prefix}-{identities[prefix]}"

    attention = ForegroundAttentionCoordinator(
        runtime_epoch=7,
        monotonic_now=lambda: now[0],
        identity_factory=next_identity,
    )
    memory = runtime_memory or RuntimeMemoryStore()
    materials = material_lifecycle or _MaterialLifecycle()
    controls: list[object] = []
    expressions: list[tuple[object, AssistantRequest]] = []

    def record_expression(context: object, request: AssistantRequest) -> bool:
        expressions.append((context, request))
        if submit_expression is None:
            return True
        return bool(submit_expression(context, request))

    runtime_epoch = current_runtime_epoch if current_runtime_epoch is not None else [7]

    def record_control(command: object, epoch: int) -> bool:
        """保存控制命令，并允许测试观察真实提交顺序。

        Args:
            command: Service 要交给 Assistant Runtime 的控制命令。
            epoch: 命令明确绑定的运行时世代。

        Returns:
            自定义端口的结果；没有自定义端口时固定返回真。
        """

        controls.append(command)
        if submit_control is None:
            return True
        return bool(submit_control(command, epoch))

    service = ForegroundSelfReflectionService(
        attention=attention,
        runtime_memory=memory,
        request_snapshots=_RequestSnapshots(request or _request()),
        material_lifecycle=materials,
        publish_material_event=(
            material_event_sink.append if material_event_sink is not None else None
        ),
        publish_episode_committed=(
            episode_event_sink.append if episode_event_sink is not None else None
        ),
        current_runtime_epoch=lambda: runtime_epoch[0],
        submit_control=record_control,
        submit_expression=record_expression,
        request_recovery_fallback=request_recovery_fallback,
        current_foreground_output_identity=(
            lambda: current_foreground_output_identity
        ),
        runtime_epoch_mutation_lock=runtime_epoch_mutation_lock,
        now=(lambda: wall_clock[0]) if wall_clock is not None else None,
    )
    return service, attention, memory, materials, controls, expressions


def _settle_entry_expression(
    service: ForegroundSelfReflectionService,
    expressions: list[tuple[object, AssistantRequest]],
) -> None:
    """模拟输出链确认入场表达已经结算，推进可见 Recovery。"""

    entry_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionEntryContext)
    )
    assert service.observe_expression_settled(
        _visible_settlement(
            request_source="self_reflection_entry",
            self_reflection_id=entry_context.self_reflection_id,
            episode_id=entry_context.episode_id,
            runtime_epoch=entry_context.runtime_epoch,
        )
    ) == "committed"


def _settle_recovery_expression(
    service: ForegroundSelfReflectionService,
    self_reflection_id: str,
    *,
    outcome: str = "completed",
) -> ExpressionSettlementDisposition:
    """模拟可见 Recovery 正文完成统一输出结算。

    Args:
        service: 当前测试使用的 Self-Reflection 服务。
        self_reflection_id: Recovery 所属反思身份。
        outcome: 输出链归一化后的结算状态。

    Returns:
        Service 对本次结算给出的已提交、忽略或重试裁决。
    """

    snapshot = service._attention.snapshot(self_reflection_id)
    assert snapshot is not None
    return service.observe_expression_settled(
        _visible_settlement(
            request_source="self_reflection_recovery",
            self_reflection_id=self_reflection_id,
            episode_id=snapshot.episode_id,
            runtime_epoch=7,
            outcome=outcome,
        )
    )


def _settle_auxiliary_expressions(
    service: ForegroundSelfReflectionService,
    expressions: list[tuple[object, AssistantRequest]],
) -> None:
    """把测试中尚未专门验证的辅助回应推进到 committed settlement。"""

    for context, _request in expressions:
        if not isinstance(context, SelfReflectionInterruptionReplyContext):
            continue
        if context.request_id not in service._interruption_reply_reservations:
            continue
        snapshot = service._attention.snapshot(context.self_reflection_id)
        assert snapshot is not None
        assert service.observe_expression_settled(
            _visible_settlement(
                request_source="self_reflection_interruption_reply",
                self_reflection_id=context.self_reflection_id,
                episode_id=context.episode_id,
                runtime_epoch=context.runtime_epoch,
                request_id=context.request_id,
                visible_text="我听到了。",
                trigger_input_identity=context.trigger_input_identity,
            )
        ) == "committed"


def _visible_settlement(
    *,
    request_source: str,
    self_reflection_id: str,
    episode_id: str,
    runtime_epoch: int,
    outcome: str = "completed",
    request_id: str | None = None,
    visible_text: str = "",
    trigger_input_identity: str | None = None,
) -> VisibleOutputSettlement:
    """构造已经完成 Journal 提交的 Self-Reflection 输出结算。

    Args:
        request_source: 当前表达使用的助手请求来源。
        self_reflection_id: 表达所属的自我反思编号。
        episode_id: 表达所属的未处理插话片段编号。
        runtime_epoch: 表达所属的 Assistant Runtime 世代。
        outcome: 输出链归一化后的结算结果。
        request_id: 可选的真实辅助请求编号；为空时按来源生成固定编号。
        visible_text: 已经成功公开并完成归档的正文。
        trigger_input_identity: 触发辅助回应的用户输入身份。

    Returns:
        带完整 Self-Reflection 身份的可见输出结算。
    """

    resolved_request_id = request_id or {
        "self_reflection_entry": f"self-reflection-entry-{self_reflection_id}",
        "self_reflection_recovery": f"self-reflection-recovery-{self_reflection_id}",
    }.get(request_source, f"{request_source}-request")
    return VisibleOutputSettlement(
        settlement_id=f"settlement:{resolved_request_id}",
        request_id=resolved_request_id,
        generation_id=f"generation:{resolved_request_id}",
        history_policy=(
            "unattended_episode"
            if request_source == "self_reflection_interruption_reply"
            else "stable_dialogue"
        ),
        outcome=outcome,  # type: ignore[arg-type]
        visible_text=visible_text,
        progress_references=(),
        derived_from_activity_ids=(),
        settled_at=1.0,
        request_source=request_source,
        self_reflection_id=self_reflection_id,
        episode_id=episode_id,
        runtime_epoch=runtime_epoch,
        trigger_input_identity=trigger_input_identity,
    )


def _tool_commit_request(
    self_reflection_id: str,
    *,
    commit_request_id: str = "commit-request-1",
    runtime_epoch: int = 7,
) -> RuntimeEvent:
    """构造携带完整 Background canonical 事实的同步提交请求。

    Args:
        self_reflection_id: 当前已经进入 reflecting 的自我反思编号。
        commit_request_id: 子进程 Broker 在重试期间保持不变的请求编号。
        runtime_epoch: 请求所属 Assistant Runtime 世代。

    Returns:
        主进程可以原子授权并登记的内部 Runtime Event。
    """

    canonical_payload: dict[str, object] = {
        "interaction_session_id": "interaction-1",
        "source_user_message_id": None,
        "tool_call_id": "background-tool-call-1",
        "protocol_transaction_id": f"self-reflection-{self_reflection_id}",
        "name": "start_background_work",
        "activity_id": "background-activity-1",
        "state": "accepted",
        "request_id": "foreground-request-1",
        "result": {
            "state": "accepted",
            "activity_id": "background-activity-1",
            "interaction_session_id": "interaction-1",
            "recovery_of_activity_id": "cognitive-1",
        },
        "recovery_of_activity_id": "cognitive-1",
        "self_reflection_id": self_reflection_id,
        "deliverable": True,
        "turn_material_context_id": "material-context-1",
    }
    return RuntimeEvent(
        type="foreground.self_reflection_tool_commit_requested",
        source="assistant_runtime",
        runtime_epoch=runtime_epoch,
        produced_at=11.0,
        payload={
            "commit_request_id": commit_request_id,
            "self_reflection_id": self_reflection_id,
            "source_activity_id": "cognitive-1",
            "tool_call_id": "background-tool-call-1",
            "tool_name": "start_background_work",
            "execution_channel": "background",
            "canonical_event_payload": canonical_payload,
            "canonical_event_produced_at": 10.5,
        },
    )


def _register_started_source(
    service: ForegroundSelfReflectionService,
    memory: RuntimeMemoryStore,
    *,
    material_context_id: str = "material-context-1",
) -> None:
    """登记 Cognitive 工具、真实 started 和失败 Task Memory 前置事实。"""

    assert service.observe_tool_call_completed(
        RuntimeEvent(
            type="llm.foreground.tool_call_completed",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "name": "analyze_deeply",
                "state": "accepted",
                "activity_id": "cognitive-1",
                "request_id": "foreground-request-1",
                "protocol_transaction_id": "transaction-1",
                "interaction_session_id": "interaction-1",
                "turn_material_context_id": material_context_id,
            },
        )
    )
    assert service.observe_cognitive_started(
        RuntimeEvent(
            type="cognitive_task.started",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "activity_id": "cognitive-1",
                "interaction_session_id": "interaction-1",
            },
        )
    )
    memory.record_task_terminal(
        "cognitive_task.failed",
        {
            "activity_id": "cognitive-1",
            "brief": "把 input.wav 转成 mp3",
            "summary": "",
            "failure_reason": "当前方法不能交付文件",
        },
    )


def _failed_event(
    failure_code: str = "cognitive_task_failed",
) -> RuntimeEvent:
    """构造具备恢复资格的 Cognitive 失败事件。

    Args:
        failure_code: Activity 执行器已经归一化的系统错误码。
    """

    return RuntimeEvent(
        type="cognitive_task.failed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={
            "activity_id": "cognitive-1",
            "interaction_session_id": "interaction-1",
            "failure_reason": "当前方法不能交付文件",
            "failure": {"code": failure_code},
        },
    )


def _managed_material() -> MaterialAttachment:
    """构造一个会在租约释放时产生资产事件的真实材料。"""

    return MaterialAttachment(
        material_id="material-a",
        turn_id="staged",
        kind="dragged_file",
        source="drag_drop",
        display_name="input.wav",
        path="C:/managed/input.wav",
        created_at="2026-08-14T10:00:00+08:00",
        node_kind="file",
        media_class="audio",
        managed_asset_id="asset-a",
    )


def test_before_start_cognitive_failure_never_enters_self_reflection() -> None:
    """Runtime 未接管的 Cognitive 失败必须只走普通启动失败收口。"""

    service, attention, _memory, materials, controls, expressions = _service()
    assert service.observe_tool_call_completed(
        RuntimeEvent(
            type="llm.foreground.tool_call_completed",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "name": "analyze_deeply",
                "state": "accepted",
                "activity_id": "cognitive-before-start",
                "request_id": "foreground-request-1",
                "protocol_transaction_id": "transaction-before-start",
                "interaction_session_id": "interaction-1",
                "turn_material_context_id": "material-context-1",
            },
        )
    ) is True

    recovery_taken = service.observe_cognitive_failed(
        RuntimeEvent(
            type="cognitive_task.failed",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "activity_id": "cognitive-before-start",
                "interaction_session_id": "interaction-1",
                "failure_reason": "后台执行器拒绝了启动请求。",
                "failure": {"code": "unclassified_error"},
                "execution_phase": "before_start",
                "started_at": None,
            },
        )
    ).suppress_source_notice

    assert recovery_taken is False
    assert attention.active_snapshot() is None
    assert controls == []
    assert expressions == []
    assert service._sources == {}
    assert materials.release_calls == ["material-context-1"]
    assert materials.lease_counts == {}


def test_started_business_failure_enters_self_reflection() -> None:
    """真实启动后的合法业务失败进入一次恢复。"""

    service, attention, memory, _materials, controls, _expressions = _service()
    _register_started_source(service, memory)

    assert service.observe_cognitive_failed(
        _failed_event("cognitive_task_failed")
    ).accepted is True
    assert attention.active_snapshot() is not None
    assert controls == []


def test_recovery_submission_failure_suppresses_original_notice_after_entry_acceptance() -> None:
    """入场已进入输出链后，Recovery 提交失败也不能补发普通 Notice。"""

    def reject_recovery(context: object, _request: AssistantRequest) -> bool:
        """只拒绝可见 Recovery，不影响入场表达。"""

        return not isinstance(context, SelfReflectionRecoveryContext)

    service, attention, memory, _materials, _controls, expressions = _service(
        submit_expression=reject_recovery,
    )
    _register_started_source(service, memory)

    receipt = service.observe_cognitive_failed(_failed_event())

    assert receipt.status == "accepted"
    assert receipt.entry_output_accepted is True
    assert receipt.accepted is True
    assert receipt.suppress_source_notice is True
    _settle_entry_expression(service, expressions)
    assert attention.active_snapshot() is None
    assert [type(context) for context, _ in expressions] == [
        SelfReflectionEntryContext,
        SelfReflectionRecoveryContext,
    ]


def test_entry_settlement_backfills_entry_public_text_into_recovery_facts() -> None:
    """入场结算正文必须随 Recovery 提交回填，供恢复指令建立对话连续性。"""

    service, _attention, memory, _materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    entry_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionEntryContext)
    )
    assert service.observe_expression_settled(
        _visible_settlement(
            request_source="self_reflection_entry",
            self_reflection_id=entry_context.self_reflection_id,
            episode_id=entry_context.episode_id,
            runtime_epoch=entry_context.runtime_epoch,
            visible_text="等等——那件事没办成，容我想清楚。",
        )
    ) == "committed"
    recovery_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionRecoveryContext)
    )
    assert (
        recovery_context.prompt_facts.entry_public_text
        == "等等——那件事没办成，容我想清楚。"
    )


def test_reflection_inputs_start_auxiliary_expression_until_body_begins() -> None:
    """正式正文公开前的每项新输入都会触发独立辅助回应。"""

    service, attention, memory, _materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    snapshot = attention.active_snapshot()
    assert snapshot is not None

    first = service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="queued-input-1",
            assistant_request_id="queued-request-1",
            source="text",
            text="第一条插话",
        )
    )
    assert first.status == "accepted"
    assert [type(context) for context, _ in expressions] == [
        SelfReflectionEntryContext,
        SelfReflectionInterruptionReplyContext,
    ]

    _settle_entry_expression(service, expressions)
    assert [type(context) for context, _ in expressions] == [
        SelfReflectionEntryContext,
        SelfReflectionInterruptionReplyContext,
        SelfReflectionRecoveryContext,
    ]

    second = service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="queued-input-2",
            assistant_request_id="queued-request-2",
            source="text",
            text="第二条插话",
        )
    )
    assert second.status == "accepted"
    assert [type(context) for context, _ in expressions] == [
        SelfReflectionEntryContext,
        SelfReflectionInterruptionReplyContext,
        SelfReflectionRecoveryContext,
        SelfReflectionInterruptionReplyContext,
    ]


def test_interrupted_auxiliary_reply_is_committed_to_episode_ledger() -> None:
    """辅助回应公开一部分后被取消时，正文和 interrupted 事实必须一起入账。"""

    service, attention, memory, _materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None

    diversion = service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="interrupted-reply-input",
            assistant_request_id="interrupted-reply-user-request",
            source="text",
            text="你还在想吗",
        )
    )
    assert diversion.status == "accepted"
    reply_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionInterruptionReplyContext)
    )
    assert service.observe_expression_settled(
        _visible_settlement(
            request_source="self_reflection_interruption_reply",
            self_reflection_id=active.self_reflection_id,
            episode_id=active.episode_id,
            runtime_epoch=active.runtime_epoch,
            outcome="interrupted",
            request_id=reply_context.request_id,
            visible_text="我听到了，",
            trigger_input_identity="interrupted-reply-input",
        )
    ) == "committed"
    assert reply_context.request_id not in service._interruption_reply_reservations

    _settle_entry_expression(service, expressions)
    _settle_auxiliary_expressions(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert len(episode.interruption_replies) == 1
    reply = episode.interruption_replies[0]
    assert reply.request_id == reply_context.request_id
    assert reply.generation_id == f"generation:{reply_context.request_id}"
    assert reply.content == "我听到了，"
    assert reply.settlement == "interrupted"


def test_auxiliary_submission_failure_records_failed_attempt_without_fake_text() -> None:
    """辅助请求未进入输出域时只记录失败事实，不能伪造用户没听到的正文。"""

    def reject_auxiliary(context: object, _request: AssistantRequest) -> bool:
        """只拒绝辅助回应，让 Entry 与 Recovery 继续走正常生命周期。"""

        return not isinstance(context, SelfReflectionInterruptionReplyContext)

    service, attention, memory, _materials, _controls, expressions = _service(
        submit_expression=reject_auxiliary,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None

    diversion = service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="unstarted-reply-input",
            assistant_request_id="unstarted-reply-user-request",
            source="text",
            text="先听我说",
        )
    )
    assert diversion.status == "accepted"
    assert diversion.interruption_reply_eligible is False

    _settle_entry_expression(service, expressions)
    _settle_auxiliary_expressions(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert len(episode.interruption_replies) == 1
    failed_reply = episode.interruption_replies[0]
    assert failed_reply.generation_id is None
    assert failed_reply.content == ""
    assert failed_reply.settlement == "failed"
    episode_messages = [
        message
        for message in memory.snapshot().recent_messages
        if message.metadata.get("episode_id") == active.episode_id
    ]
    assert [message.role for message in episode_messages] == [
        "user",
        "assistant",
    ]
    assert all(
        message.metadata.get("episode_role") != "interruption_reply"
        for message in episode_messages
    )


def test_auxiliary_identity_conflict_records_failure_and_releases_episode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """辅助 settlement 身份损坏时必须补写失败事实，不能永久占住 Episode。"""

    service, attention, memory, _materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    assert service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="conflicting-reply-input",
            assistant_request_id="conflicting-reply-user-request",
            source="text",
            text="你还在吗",
        )
    ).status == "accepted"
    reply_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionInterruptionReplyContext)
    )
    conflicting_settlement = replace(
        _visible_settlement(
            request_source="self_reflection_interruption_reply",
            self_reflection_id=active.self_reflection_id,
            episode_id=active.episode_id,
            runtime_epoch=active.runtime_epoch,
            request_id=reply_context.request_id,
            visible_text="不能信任的正文",
            trigger_input_identity="conflicting-reply-input",
        ),
        history_policy="stable_dialogue",
    )

    with caplog.at_level("ERROR"):
        assert service.observe_expression_settled(conflicting_settlement) == "committed"

    assert "field=history_policy" in caplog.text
    assert reply_context.request_id not in service._interruption_reply_reservations
    _settle_entry_expression(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.CLOSED
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert len(episode.interruption_replies) == 1
    failed_reply = episode.interruption_replies[0]
    assert failed_reply.generation_id is None
    assert failed_reply.content == ""
    assert failed_reply.settlement == "failed"


def test_auxiliary_identity_conflict_retries_only_failed_fact_append() -> None:
    """永久身份冲突不重试原结算，只重试暂时失败的补账副作用。"""

    memory = RuntimeMemoryStore()
    original_append = memory.append_unattended_interruption_reply
    append_calls = [0]

    def fail_first_append(identity: object, reply: object) -> object:
        """第一次拒绝失败事实，第二次执行真实写入。"""

        append_calls[0] += 1
        if append_calls[0] == 1:
            raise RuntimeError("temporary_conflict_failure_append")
        return original_append(identity, reply)  # type: ignore[arg-type]

    memory.append_unattended_interruption_reply = fail_first_append  # type: ignore[method-assign]
    service, attention, _memory, _materials, _controls, expressions = _service(
        runtime_memory=memory,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    assert service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="retry-conflicting-input",
            assistant_request_id="retry-conflicting-user-request",
            source="text",
            text="先别漏掉我",
        )
    ).status == "accepted"
    reply_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionInterruptionReplyContext)
    )
    conflicting_settlement = replace(
        _visible_settlement(
            request_source="self_reflection_interruption_reply",
            self_reflection_id=active.self_reflection_id,
            episode_id=active.episode_id,
            runtime_epoch=active.runtime_epoch,
            request_id=reply_context.request_id,
            visible_text="不能信任的正文",
            trigger_input_identity="retry-conflicting-input",
        ),
        history_policy="stable_dialogue",
    )

    assert service.observe_expression_settled(conflicting_settlement) == "retryable"
    assert append_calls == [1]
    assert service.observe_expression_settled(conflicting_settlement) == "committed"
    assert append_calls == [2]
    assert reply_context.request_id not in service._interruption_reply_reservations
    _settle_entry_expression(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert len(episode.interruption_replies) == 1
    assert episode.interruption_replies[0].settlement == "failed"


def test_primary_identity_conflict_closes_current_episode_as_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entry 或 Recovery 身份损坏时必须用 reservation 关闭当前反思。"""

    service, attention, memory, materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    entry_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionEntryContext)
    )
    conflicting_settlement = replace(
        _visible_settlement(
            request_source="self_reflection_entry",
            self_reflection_id=active.self_reflection_id,
            episode_id=active.episode_id,
            runtime_epoch=active.runtime_epoch,
        ),
        episode_id="episode-wrong",
    )

    with caplog.at_level("ERROR"):
        assert service.observe_expression_settled(conflicting_settlement) == "committed"

    assert "field=episode_id" in caplog.text
    closed = attention.snapshot(active.self_reflection_id)
    assert closed is not None
    assert closed.state is SelfReflectionState.CLOSED_FAILED
    assert closed.outcome is not None
    assert closed.outcome.reason == "assistant_output_identity_conflict"
    assert entry_context.request_id not in service._primary_output_reservations
    assert materials.lease_counts == {}


def test_recovery_identity_conflict_closes_current_episode_as_failed() -> None:
    """Recovery settlement 损坏时同样必须走权威失败 outcome 收口。"""

    service, attention, memory, materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, expressions)
    recovery_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionRecoveryContext)
    )
    conflicting_settlement = _visible_settlement(
        request_source="plain_foreground",
        self_reflection_id=active.self_reflection_id,
        episode_id=active.episode_id,
        runtime_epoch=active.runtime_epoch,
        request_id=recovery_context.request_id,
    )

    assert service.observe_expression_settled(conflicting_settlement) == "committed"

    closed = attention.snapshot(active.self_reflection_id)
    assert closed is not None
    assert closed.state is SelfReflectionState.CLOSED_FAILED
    assert closed.outcome is not None
    assert closed.outcome.reason == "assistant_output_identity_conflict"
    assert recovery_context.request_id not in service._primary_output_reservations
    assert materials.lease_counts == {}


def test_recovery_waits_for_auxiliary_settlement_retry_before_episode_close() -> None:
    """辅助 settlement 瞬时写入失败时，Recovery 不能抢先关闭 episode。"""

    memory = RuntimeMemoryStore()
    original_append = memory.append_unattended_interruption_reply
    append_calls = [0]

    def fail_first_append(identity: object, reply: object) -> object:
        """第一次模拟 Runtime Memory 瞬时失败，重试时执行真实写入。"""

        append_calls[0] += 1
        if append_calls[0] == 1:
            raise RuntimeError("temporary_reply_append_failure")
        return original_append(identity, reply)  # type: ignore[arg-type]

    memory.append_unattended_interruption_reply = fail_first_append  # type: ignore[method-assign]
    service, attention, _memory, _materials, _controls, expressions = _service(
        runtime_memory=memory,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    assert service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="retrying-reply-input",
            assistant_request_id="retrying-reply-user-request",
            source="text",
            text="你听到了吗",
        )
    ).status == "accepted"
    reply_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionInterruptionReplyContext)
    )
    reply_settlement = _visible_settlement(
        request_source="self_reflection_interruption_reply",
        self_reflection_id=active.self_reflection_id,
        episode_id=active.episode_id,
        runtime_epoch=active.runtime_epoch,
        outcome="completed",
        request_id=reply_context.request_id,
        visible_text="我听到了。",
        trigger_input_identity="retrying-reply-input",
    )

    with pytest.raises(RuntimeError, match="temporary_reply_append_failure"):
        service.observe_expression_settled(reply_settlement)
    _settle_entry_expression(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "retryable"
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.REFLECTING

    assert service.observe_expression_settled(reply_settlement) == "committed"
    assert service.reap_expired() == 1
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.CLOSED


def test_unstarted_auxiliary_failure_retries_before_episode_close() -> None:
    """辅助请求未启动且失败事实暂时写不进去时，reaper 必须补账后再关闭。"""

    memory = RuntimeMemoryStore()
    original_append = memory.append_unattended_interruption_reply
    append_calls = [0]

    def fail_first_append(identity: object, reply: object) -> object:
        """第一次拒绝失败事实，第二次允许 reaper 写入。"""

        append_calls[0] += 1
        if append_calls[0] == 1:
            raise RuntimeError("temporary_unstarted_reply_append_failure")
        return original_append(identity, reply)  # type: ignore[arg-type]

    def reject_auxiliary(context: object, _request: AssistantRequest) -> bool:
        """让辅助请求停在尚未创建 generation 的提交失败边界。"""

        return not isinstance(context, SelfReflectionInterruptionReplyContext)

    memory.append_unattended_interruption_reply = fail_first_append  # type: ignore[method-assign]
    service, attention, _memory, _materials, _controls, expressions = _service(
        runtime_memory=memory,
        submit_expression=reject_auxiliary,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    assert service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="retry-unstarted-input",
            assistant_request_id="retry-unstarted-user-request",
            source="text",
            text="先别漏掉我",
        )
    ).status == "accepted"

    _settle_entry_expression(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "retryable"
    assert service.reap_expired() == 2
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.CLOSED
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert len(episode.interruption_replies) == 1
    assert episode.interruption_replies[0].settlement == "failed"


@pytest.mark.parametrize(
    "failure_code",
    [
        "authentication_failed",
        "rate_limited",
        "provider_unavailable",
        "network_unavailable",
        "request_timed_out",
        "provider_stream_interrupted",
    ],
)
def test_started_infrastructure_failures_do_not_enter_self_reflection(
    failure_code: str,
) -> None:
    """真实启动后的基础设施故障只保留普通失败路径。

    Args:
        failure_code: Provider、网络或请求层的稳定故障码。
    """

    service, attention, memory, _materials, controls, expressions = _service()
    _register_started_source(service, memory)

    assert service.observe_cognitive_failed(_failed_event(failure_code)).accepted is False
    assert attention.active_snapshot() is None
    assert controls == []
    assert expressions == []


def test_cognitive_correction_activity_registers_the_same_recovery_source() -> None:
    """已接纳的 Cognitive 纠正 Activity 失败后使用同一套恢复资格。"""

    service, attention, memory, _materials, _controls, _expressions = _service()
    activity_id = "cognitive-correction-1"
    assert service.observe_tool_call_completed(
        RuntimeEvent(
            type="llm.foreground.tool_call_completed",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "name": "correct_cognitive_activity",
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": "foreground-request-1",
                "protocol_transaction_id": "correction-transaction-1",
                "interaction_session_id": "interaction-1",
            },
        )
    )
    assert service.observe_cognitive_started(
        RuntimeEvent(
            type="cognitive_task.started",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "activity_id": activity_id,
                "interaction_session_id": "interaction-1",
            },
        )
    )
    memory.record_task_terminal(
        "cognitive_task.failed",
        {
            "activity_id": activity_id,
            "brief": "执行纠正后的完整认知目标",
            "failure_reason": "纠正后的任务仍未完成。",
        },
    )

    assert service.observe_cognitive_failed(
        RuntimeEvent(
            type="cognitive_task.failed",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "activity_id": activity_id,
                "interaction_session_id": "interaction-1",
                "failure_reason": "纠正后的任务仍未完成。",
                "failure": {"code": "cognitive_task_failed"},
            },
        )
    ).accepted
    active = attention.active_snapshot()
    assert active is not None
    assert active.source_activity_id == activity_id


def test_cognitive_recovery_retains_material_before_foreground_final() -> None:
    """普通 Foreground final 先结束时，Cognitive Recovery 仍能读取原始材料。"""

    turn_materials = TurnMaterialContextService()
    turn_materials._staged_materials = [_managed_material()]  # type: ignore[attr-defined]
    context = turn_materials.activate_turn(
        turn_id="foreground-turn-1",
        created_at="2026-08-14T10:00:00+08:00",
    )
    material_hub = SimpleNamespace(
        material_revision=0,
        turn_materials=turn_materials,
    )
    material_hub.current_material_context_payload = lambda: {
        "revision": material_hub.material_revision,
        "turn_material_context": turn_materials.current_payload()[
            "turn_material_context"
        ],
    }
    material_lifecycle = MaterialLifecycleService(hub=material_hub)
    request = replace(
        _request(),
        turn_material_context_id=context.context_id,
        foreground_context={
            "active_turn_material_context": {"context_id": context.context_id}
        },
    )
    material_events: list[dict[str, object]] = []
    service, attention, memory, _materials, _controls, expressions = _service(
        material_lifecycle=material_lifecycle,  # type: ignore[arg-type]
        request=request,
        material_event_sink=material_events,
    )
    _register_started_source(
        service,
        memory,
        material_context_id=context.context_id,
    )

    # 真实输出提交先结束 active turn；如果 acceptance 没有提前 retain，下面的
    # Recovery Admission 会错误地看不到上下文，正是线上竞态的复现时序。
    lifecycle_events = material_lifecycle.finish_turn(context.context_id)
    assert lifecycle_events[-1]["type"] == "materials.contexts_changed"
    assert turn_materials.active_context(context.context_id) is None

    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"

    # Recovery 是最后一个所有者，必须只释放一次，并把领域返回的材料事件原样
    # 交给上层发布端口；再次释放不能再产生资产事件。
    assert [event["type"] for event in material_events] == [
        "materials.assets_released",
        "materials.contexts_changed",
    ]
    assert material_events[0]["payload"] == {"managed_asset_ids": ["asset-a"]}
    assert material_lifecycle.release_turn(context.context_id) == []


def test_material_retain_failure_fails_closed_without_recovery_source() -> None:
    """来源材料租约拿不到时只保留普通 Cognitive 失败路径。"""

    materials = _MaterialLifecycle(fail_retain=True)
    service, _attention, _memory, _materials, _controls, _expressions = _service(
        material_lifecycle=materials,
    )
    acceptance = RuntimeEvent(
        type="llm.foreground.tool_call_completed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={
            "name": "analyze_deeply",
            "state": "accepted",
            "activity_id": "cognitive-1",
            "request_id": "foreground-request-1",
            "protocol_transaction_id": "transaction-1",
            "interaction_session_id": "interaction-1",
            "turn_material_context_id": "material-context-1",
        },
    )

    assert service.observe_tool_call_completed(acceptance) is False
    assert service.observe_cognitive_started(
        RuntimeEvent(
            type="cognitive_task.started",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={"activity_id": "cognitive-1"},
        )
    ) is False
    assert service.observe_cognitive_failed(_failed_event()).accepted is False
    assert service._sources == {}
    assert materials.lease_counts == {}


def test_busy_attention_rejects_new_recovery_and_releases_its_source() -> None:
    """另一项反思占用 Attention 时，新失败必须正常拒绝并释放来源租约。"""

    service, attention, memory, materials, _controls, _expressions = _service()
    occupied = attention.admit(
        SelfReflectionAdmissionCommand(
            runtime_epoch=7,
            source_activity_id="already-reflecting",
            source_request_id="already-reflecting-request",
            source_protocol_transaction_id="already-reflecting-transaction",
        )
    )
    assert occupied.status == "admitted"
    _register_started_source(service, memory)

    receipt = service.observe_cognitive_failed(_failed_event())

    assert receipt.accepted is False
    assert service._sources == {}
    assert materials.release_calls == ["material-context-1"]
    assert materials.lease_counts == {}


def test_conflicting_duplicate_acceptance_cannot_replace_material_identity() -> None:
    """同一 Activity 的冲突 acceptance 不能改写首次冻结的材料上下文。"""

    materials = _MaterialLifecycle()
    service, _attention, _memory, _materials, _controls, _expressions = _service(
        material_lifecycle=materials,
    )
    first = RuntimeEvent(
        type="llm.foreground.tool_call_completed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={
            "name": "analyze_deeply",
            "state": "accepted",
            "activity_id": "cognitive-1",
            "request_id": "foreground-request-1",
            "protocol_transaction_id": "transaction-1",
            "interaction_session_id": "interaction-1",
            "turn_material_context_id": "material-context-1",
        },
    )
    conflicting = replace(
        first,
        payload={**first.payload, "turn_material_context_id": "material-context-2"},
    )

    assert service.observe_tool_call_completed(first) is True
    assert service.observe_tool_call_completed(conflicting) is False
    assert materials.retain_calls == ["material-context-1"]
    assert service._sources["cognitive-1"].material_context_id == "material-context-1"


def _no_action_outcome(reflection_id: str) -> SelfReflectionOutcomeFact:
    """构造不调用新工具的合法恢复结果。"""

    return SelfReflectionOutcomeFact.direct_response(
        self_reflection_id=reflection_id,
        source_activity_id="cognitive-1",
    )


def test_recovery_request_excludes_current_episode_but_future_history_keeps_it() -> None:
    """Recovery 不读反思期间输入，最终结算后普通历史仍保留该片段。"""

    service, attention, memory, _materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    diversion = service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="text-during-reflection",
            assistant_request_id="text-request-during-reflection",
            source="text",
            text="改成 b.wav",
        )
    )
    assert diversion.status == "accepted"

    _settle_entry_expression(service, expressions)
    _settle_auxiliary_expressions(service, expressions)

    recovery_requests = [
        request
        for context, request in expressions
        if isinstance(context, SelfReflectionRecoveryContext)
    ]
    assert len(recovery_requests) == 1
    assert all(
        "改成 b.wav" not in message.content
        for message in recovery_requests[0].runtime_memory_snapshot.recent_messages
    )
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    assert any(
        "改成 b.wav" in message.content
        for message in memory.snapshot().recent_messages
    )


def test_recovery_settlement_publishes_episode_once_before_attention_release() -> None:
    """最终结算只发布一次完整 episode，重复 settlement 不重复公开。"""

    episode_events: list[RuntimeEvent] = []
    service, attention, memory, _materials, _controls, expressions = _service(
        episode_event_sink=episode_events,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    assert service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="episode-input-1",
            assistant_request_id="episode-request-1",
            source="text",
            text="先别急",
        )
    ).status == "accepted"
    _settle_entry_expression(service, expressions)
    _settle_auxiliary_expressions(service, expressions)

    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "ignored"
    assert attention.active_snapshot() is None
    assert len(episode_events) == 1
    event = episode_events[0]
    assert event.type == "self_reflection.episode_committed"
    assert event.source == "main_process"
    assert event.runtime_epoch is None
    assert event.payload["episode_id"] == active.episode_id
    assert event.payload["actionable"] is False
    assert [item["episode_role"] for item in event.payload["history_block"]] == [
        "input",
        "interruption_reply",
        "closure",
    ]


def test_episode_publication_outbox_waits_for_public_broadcast_ack() -> None:
    """闭合事件进入 Runtime 队列后仍须等待公共广播成功回执。"""

    publication_attempts: list[RuntimeEvent] = []
    published_events: list[RuntimeEvent] = []

    def fail_first_publication(event: RuntimeEvent) -> None:
        """第一次模拟公共事件队列故障，第二次保存真实事件。"""

        publication_attempts.append(event)
        if len(publication_attempts) == 1:
            raise RuntimeError("temporary_episode_publication_failure")
        published_events.append(event)

    service, attention, memory, _materials, _controls, expressions = _service()
    service._episode_lifecycle._publish_episode_committed = fail_first_publication
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, expressions)

    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    assert attention.active_snapshot() is None
    assert service._episode_lifecycle.publication_pending(
        runtime_epoch=active.runtime_epoch,
        episode_id=active.episode_id,
    )

    assert service.reap_expired() == 1
    assert len(publication_attempts) == 2
    assert [event.payload["episode_id"] for event in published_events] == [
        active.episode_id
    ]
    assert service._episode_lifecycle.publication_pending(
        runtime_epoch=active.runtime_epoch,
        episode_id=active.episode_id,
    )

    assert service.acknowledge_episode_publication(
        runtime_epoch=active.runtime_epoch,
        episode_id=active.episode_id,
    ) is True
    assert not service._episode_lifecycle.publication_pending(
        runtime_epoch=active.runtime_epoch,
        episode_id=active.episode_id,
    )


def test_duplicate_episode_input_preserves_first_registry_and_memory_record() -> None:
    """同一输入重试不得重建时间戳，也不能回滚 Registry 中的首条事实。"""

    wall_clock = [datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)]
    service, attention, memory, _materials, _controls, expressions = _service(
        wall_clock=wall_clock,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    command = DivertFinalInputCommand(
        runtime_epoch=7,
        input_identity="duplicate-text",
        assistant_request_id="duplicate-text-request",
        source="text",
        text="只应保存一次",
    )

    first = service.divert_final_input(command)
    wall_clock[0] += timedelta(seconds=1)
    duplicate = service.divert_final_input(command)

    assert first.status == "accepted"
    assert duplicate.status == "duplicate"
    snapshot = attention.snapshot(active.self_reflection_id)
    assert snapshot is not None
    assert snapshot.input_count == 1
    _settle_entry_expression(service, expressions)
    _settle_auxiliary_expressions(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            episode_id=active.episode_id,
            runtime_epoch=7,
        )
    )
    assert episode is not None
    assert len(episode.inputs) == 1
    assert episode.inputs[0].accepted_at == datetime(
        2026,
        8,
        15,
        9,
        0,
        tzinfo=timezone.utc,
    )


def test_admission_open_is_atomic_against_input_diversion() -> None:
    """Attention 可见时 episode 必须已经打开，并发输入不能落进准入缝隙。"""

    service, attention, memory, _materials, _controls, _expressions = _service()
    _register_started_source(service, memory)
    open_started = Event()
    allow_open_to_finish = Event()
    original_open = memory.open_unattended_input_episode

    def block_episode_open(*args: object, **kwargs: object) -> bool:
        """阻塞真实 open 前的窗口，证明输入与准入共用同一事务锁。"""

        open_started.set()
        assert allow_open_to_finish.wait(timeout=2.0)
        return original_open(*args, **kwargs)

    memory.open_unattended_input_episode = block_episode_open  # type: ignore[method-assign]
    takeover_results: list[object] = []
    diversion_results: list[object] = []
    takeover_thread = Thread(
        target=lambda: takeover_results.append(
            service.observe_cognitive_failed(_failed_event())
        )
    )
    diversion_thread = Thread(
        target=lambda: diversion_results.append(
            service.divert_final_input(
                DivertFinalInputCommand(
                    runtime_epoch=7,
                    input_identity="input-during-episode-open",
                    assistant_request_id="request-during-episode-open",
                    source="text",
                    text="这条输入必须等 episode 打开后再归属",
                )
            )
        )
    )

    takeover_thread.start()
    assert open_started.wait(timeout=2.0)
    assert attention.active_snapshot() is not None
    diversion_thread.start()
    diversion_thread.join(timeout=0.2)
    diversion_waited_for_open = diversion_thread.is_alive()
    allow_open_to_finish.set()
    takeover_thread.join(timeout=2.0)
    diversion_thread.join(timeout=2.0)

    assert diversion_waited_for_open is True
    assert not takeover_thread.is_alive()
    assert not diversion_thread.is_alive()
    assert takeover_results[0].accepted is True
    assert diversion_results[0].status == "accepted"


def test_input_append_wins_close_race_and_is_frozen_into_episode() -> None:
    """输入先取得事务锁时，关闭必须等待它写完并把它封进同一片段。"""

    service, attention, memory, _materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, expressions)
    assert service.observe_reply_admission_closed(
        RuntimeEvent(
            type="foreground.self_reflection_reply_admission_closed",
            source="assistant_output_lanes",
            runtime_epoch=active.runtime_epoch,
            payload={
                "self_reflection_id": active.self_reflection_id,
                "episode_id": active.episode_id,
                "runtime_epoch": active.runtime_epoch,
            },
        )
    ) is True
    append_started = Event()
    allow_append_to_finish = Event()
    original_append = memory.append_unattended_input

    def append_then_wait(*args: object, **kwargs: object) -> object:
        """先完成真实追加，再阻塞关闭线程以固定输入优先顺序。

        Args:
            *args: Runtime Memory 追加入口的位置参数。
            **kwargs: Runtime Memory 追加入口的命名参数。

        Returns:
            真实追加状态。
        """

        status = original_append(*args, **kwargs)
        append_started.set()
        assert allow_append_to_finish.wait(timeout=2.0)
        return status

    memory.append_unattended_input = append_then_wait  # type: ignore[method-assign]
    diversion_results: list[object] = []
    close_results: list[ExpressionSettlementDisposition] = []
    diversion_thread = Thread(
        target=lambda: diversion_results.append(
            service.divert_final_input(
                DivertFinalInputCommand(
                    runtime_epoch=7,
                    input_identity="input-wins-close-race",
                    assistant_request_id="request-wins-close-race",
                    source="text",
                    text="这句话必须进入片段",
                )
            )
        )
    )
    close_thread = Thread(
        target=lambda: close_results.append(
            _settle_recovery_expression(
                service,
                active.self_reflection_id,
            )
        )
    )

    diversion_thread.start()
    assert append_started.wait(timeout=2.0)
    close_thread.start()
    assert close_thread.is_alive()
    allow_append_to_finish.set()
    diversion_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not diversion_thread.is_alive()
    assert not close_thread.is_alive()
    assert diversion_results[0].status == "accepted"
    assert close_results == ["committed"]
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert [item.input_identity for item in episode.inputs] == [
        "input-wins-close-race"
    ]


def test_input_append_is_atomic_against_runtime_replacement() -> None:
    """热替换必须等待已经开始的输入追加，不能先关闭它正在写入的 episode。"""

    episode_events: list[RuntimeEvent] = []
    service, attention, memory, _materials, _controls, _expressions = _service(
        episode_event_sink=episode_events,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    append_started = Event()
    allow_append_to_finish = Event()
    original_append = memory.append_unattended_input

    def block_before_append(*args: object, **kwargs: object) -> object:
        """在真实写入前阻塞，让热替换尝试越过相同生命周期边界。"""

        append_started.set()
        assert allow_append_to_finish.wait(timeout=2.0)
        return original_append(*args, **kwargs)

    memory.append_unattended_input = block_before_append  # type: ignore[method-assign]
    diversion_results: list[object] = []
    diversion_thread = Thread(
        target=lambda: diversion_results.append(
            service.divert_final_input(
                DivertFinalInputCommand(
                    runtime_epoch=7,
                    input_identity="input-before-runtime-replacement",
                    assistant_request_id="request-before-runtime-replacement",
                    source="text",
                    text="这条输入必须完整进入旧 episode",
                )
            )
        )
    )
    replacement_thread = Thread(
        target=lambda: service.terminate_runtime_epoch(7, new_runtime_epoch=8)
    )

    diversion_thread.start()
    assert append_started.wait(timeout=2.0)
    replacement_thread.start()
    replacement_thread.join(timeout=0.2)
    replacement_waited_for_append = replacement_thread.is_alive()
    allow_append_to_finish.set()
    diversion_thread.join(timeout=2.0)
    replacement_thread.join(timeout=2.0)

    assert replacement_waited_for_append is True
    assert not diversion_thread.is_alive()
    assert not replacement_thread.is_alive()
    assert diversion_results[0].status == "accepted"
    assert attention.runtime_epoch == 8
    assert episode_events == []


def test_close_wins_input_race_and_leaves_input_for_normal_submission() -> None:
    """关闭先取得事务锁时，迟到输入不得再写入已经封存的片段。"""

    service, attention, memory, _materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, expressions)
    close_started = Event()
    allow_close_to_finish = Event()
    original_close = memory.close_unattended_input_episode

    def close_then_wait(*args: object, **kwargs: object) -> object:
        """先完成真实关闭，再阻塞输入线程以固定关闭优先顺序。

        Args:
            *args: Runtime Memory 关闭入口的位置参数。
            **kwargs: Runtime Memory 关闭入口的命名参数。

        Returns:
            真实关闭得到的片段。
        """

        episode = original_close(*args, **kwargs)
        close_started.set()
        assert allow_close_to_finish.wait(timeout=2.0)
        return episode

    memory.close_unattended_input_episode = close_then_wait  # type: ignore[method-assign]
    close_results: list[ExpressionSettlementDisposition] = []
    diversion_results: list[object] = []
    close_thread = Thread(
        target=lambda: close_results.append(
            _settle_recovery_expression(
                service,
                active.self_reflection_id,
            )
        )
    )
    diversion_thread = Thread(
        target=lambda: diversion_results.append(
            service.divert_final_input(
                DivertFinalInputCommand(
                    runtime_epoch=7,
                    input_identity="close-wins-input-race",
                    assistant_request_id="close-wins-request-race",
                    source="final_asr",
                    text="这句话应交给普通请求",
                )
            )
        )
    )

    close_thread.start()
    assert close_started.wait(timeout=2.0)
    diversion_thread.start()
    assert diversion_thread.is_alive()
    allow_close_to_finish.set()
    close_thread.join(timeout=2.0)
    diversion_thread.join(timeout=2.0)

    assert not close_thread.is_alive()
    assert not diversion_thread.is_alive()
    assert close_results == ["committed"]
    diversion = diversion_results[0]
    assert diversion.status == "not_diverted"
    assert diversion.reason == "assistant_request_reserved"
    assert diversion.assistant_request_lease is not None
    assert input_diversion_blocks_normal_request(diversion) is False
    assert diversion.assistant_request_lease.rollback() is True
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert episode.inputs == ()


def test_material_release_retry_keeps_accepting_episode_inputs_until_freeze() -> None:
    """材料释放重试期间保持 finalizing，新输入仍能进入同一 episode。"""

    materials = _MaterialLifecycle(fail_first_release=True)
    service, attention, memory, _materials, _controls, expressions = _service(
        material_lifecycle=materials
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, expressions)

    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "retryable"
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.FINALIZING
    assert active.self_reflection_id in service._pending_outcomes
    late_input = service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="input-during-material-retry",
            assistant_request_id="request-during-material-retry",
            source="text",
            text="别漏掉这句话",
        )
    )
    assert late_input.status == "accepted"
    assert late_input.interruption_reply_eligible is False

    assert service.reap_expired() == 1
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.CLOSED
    assert materials.release_calls == ["material-context-1", "material-context-1"]
    assert len(
        [
            context
            for context, _request in expressions
            if isinstance(context, SelfReflectionRecoveryContext)
        ]
    ) == 1
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert [item.input_identity for item in episode.inputs] == [
        "input-during-material-retry"
    ]


def test_conflicting_duplicate_outcome_uses_first_frozen_fact() -> None:
    """收口重试只能沿用首次冻结结果，冲突重复事件不能改写权威事实。"""

    materials = _MaterialLifecycle()
    release_attempts: list[str] = []

    def fail_first_two_releases(context_id: str) -> object:
        """让两个事件都停在材料释放阶段，再由 reaper 完成第三次尝试。"""

        release_attempts.append(context_id)
        if len(release_attempts) == 1:
            raise RuntimeError("temporary_material_release_failure")
        return []

    materials.release_turn = fail_first_two_releases  # type: ignore[method-assign]
    service, attention, memory, _materials, _controls, expressions = _service(
        material_lifecycle=materials
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    first_outcome = SelfReflectionOutcomeFact(
        self_reflection_id=active.self_reflection_id,
        source_activity_id="cognitive-1",
        kind=SelfReflectionOutcomeKind.INLINE_RESULT_AVAILABLE,
        tool_fact=SelfReflectionToolFact(
            tool_name="lookup_weather",
            status=SelfReflectionToolStatus.COMPLETED,
            safe_result="天气查询已完成。",
        ),
    )
    conflicting_outcome = SelfReflectionOutcomeFact(
        self_reflection_id=active.self_reflection_id,
        source_activity_id="cognitive-1",
        kind=SelfReflectionOutcomeKind.INLINE_TOOL_FAILED,
        tool_fact=SelfReflectionToolFact(
            tool_name="lookup_weather",
            status=SelfReflectionToolStatus.FAILED,
            safe_result="天气查询失败。",
        ),
    )

    _settle_entry_expression(service, expressions)

    # 工具事实先冻结，直到同一 Recovery 的可见正文完成结算才允许关闭。
    assert service.observe_outcome(
        RuntimeEvent(
            type="foreground.self_reflection_outcome",
            source="assistant_runtime",
            runtime_epoch=7,
            payload=first_outcome.to_payload(),
        )
    ) is True
    reflecting = attention.snapshot(active.self_reflection_id)
    assert reflecting is not None
    assert reflecting.state is SelfReflectionState.REFLECTING
    assert reflecting.outcome is None

    # 迟到的冲突事件不能替换已经冻结的 canonical 工具事实。
    assert service.observe_outcome(
        RuntimeEvent(
            type="foreground.self_reflection_outcome",
            source="assistant_runtime",
            runtime_epoch=7,
            payload=conflicting_outcome.to_payload(),
        )
    ) is False

    assert service._recovery_outcomes[active.self_reflection_id] == first_outcome
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "retryable"
    still_closing = attention.snapshot(active.self_reflection_id)
    assert still_closing is not None
    assert still_closing.state is SelfReflectionState.FINALIZING
    assert still_closing.outcome == first_outcome
    assert service._pending_outcomes[active.self_reflection_id] == first_outcome
    assert service.reap_expired() == 1

    closed = attention.snapshot(active.self_reflection_id)
    assert closed is not None
    assert closed.state is SelfReflectionState.CLOSED
    assert closed.outcome == first_outcome
    assert active.self_reflection_id not in service._pending_outcomes
    source_record = memory.task_record("cognitive-1")
    assert source_record is not None
    assert source_record.recovery_outcome is not None
    assert source_record.recovery_outcome.kind == "inline_result_available"
    assert source_record.recovery_outcome.safe_summary == "天气查询已完成。"
    assert release_attempts == ["material-context-1"] * 2
    assert service.reap_expired() == 0


def test_deadline_during_reflecting_transition_keeps_episode_open_for_fallback() -> None:
    """反思刚开始就超时时只冻结失败事实，仍要走可见 fallback settlement。"""

    clock = [0.0]
    service, attention, memory, materials, _controls, _expressions = _service(
        clock=clock
    )
    _register_started_source(service, memory)
    transition_to_reflecting = attention.transition_to_reflecting

    def expire_before_transition(
        self_reflection_id: str,
        *,
        runtime_epoch: int,
    ) -> object:
        clock[0] = 45.0
        return transition_to_reflecting(
            self_reflection_id,
            runtime_epoch=runtime_epoch,
        )

    attention.transition_to_reflecting = expire_before_transition  # type: ignore[method-assign]

    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    snapshot = attention.snapshot("self-reflection-1")
    assert snapshot is not None
    assert snapshot.state is SelfReflectionState.REFLECTING
    assert snapshot.outcome is not None
    assert snapshot.outcome.reason == "reflection_deadline_exceeded"
    assert memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=snapshot.self_reflection_id,
            runtime_epoch=snapshot.runtime_epoch,
            episode_id=snapshot.episode_id,
        )
    ) is None
    assert materials.release_calls == []
    assert service._material_contexts == {"self-reflection-1": "material-context-1"}

    _settle_entry_expression(service, _expressions)
    assert _settle_recovery_expression(
        service,
        snapshot.self_reflection_id,
    ) == "committed"
    assert attention.snapshot(snapshot.self_reflection_id).state is SelfReflectionState.CLOSED_FAILED
    assert materials.release_calls == ["material-context-1"]
    assert service._material_contexts == {}


def test_self_reflection_events_require_outer_epoch_and_source_identity() -> None:
    """旧世代或伪造来源 Activity 的工具与结果事件不能推进状态。"""

    service, attention, memory, _materials, controls, _expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None

    controls.clear()
    registered: list[RuntimeEvent] = []

    async def register_canonical_event(event: RuntimeEvent) -> None:
        """保存身份不合法时绝不能登记的 canonical 事件。"""

        registered.append(event)

    valid_request = _tool_commit_request(active.self_reflection_id)
    mismatched_payload = dict(valid_request.payload)
    mismatched_payload["source_activity_id"] = "different-cognitive"
    mismatched_request = replace(valid_request, payload=mismatched_payload)

    assert asyncio.run(
        service.handle_tool_commit_request(
            mismatched_request,
            register_canonical_event=register_canonical_event,
        )
    ) is True
    assert registered == []
    assert isinstance(controls[-1], SelfReflectionToolCommitDecisionCommand)
    assert controls[-1].approved is False
    assert asyncio.run(
        service.handle_tool_commit_request(
            replace(valid_request, runtime_epoch=6),
            register_canonical_event=register_canonical_event,
        )
    ) is False
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.REFLECTING
    assert service.observe_outcome(
        RuntimeEvent(
            type="foreground.self_reflection_outcome",
            source="assistant_runtime",
            runtime_epoch=6,
            payload=_no_action_outcome(active.self_reflection_id).to_payload(),
        )
    ) is False
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.REFLECTING


def test_tool_commit_retries_canonical_registration_before_approval() -> None:
    """canonical 登记异常时不回批准，同一请求重试成功后才放行。"""

    service, attention, memory, _materials, controls, _expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    controls.clear()
    registration_attempts: list[RuntimeEvent] = []

    async def register_canonical_event(event: RuntimeEvent) -> None:
        """第一次模拟主进程登记异常，第二次成功。"""

        registration_attempts.append(event)
        if len(registration_attempts) == 1:
            raise RuntimeError("temporary_canonical_registration_failure")

    request = _tool_commit_request(active.self_reflection_id)

    first = asyncio.run(
        service.handle_tool_commit_request(
            request,
            register_canonical_event=register_canonical_event,
        )
    )
    second = asyncio.run(
        service.handle_tool_commit_request(
            request,
            register_canonical_event=register_canonical_event,
        )
    )
    third = asyncio.run(
        service.handle_tool_commit_request(
            request,
            register_canonical_event=register_canonical_event,
        )
    )

    assert first is False
    assert second is True
    assert third is True
    assert len(registration_attempts) == 2
    assert [event.type for event in registration_attempts] == [
        "llm.foreground.tool_call_completed",
        "llm.foreground.tool_call_completed",
    ]
    decisions = [
        command
        for command in controls
        if isinstance(command, SelfReflectionToolCommitDecisionCommand)
    ]
    assert len(decisions) == 2
    assert all(command.approved for command in decisions)
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.COMMITTED


def test_tool_commit_registers_canonical_fact_before_sending_approval() -> None:
    """主进程必须先完成 canonical 应用，再把批准命令写回子进程。"""

    order: list[str] = []

    def submit_control(command: object, _epoch: int) -> bool:
        """记录批准命令真正进入控制通道的时刻。"""

        if isinstance(command, SelfReflectionToolCommitDecisionCommand):
            order.append("approval_sent")
        return True

    service, attention, memory, _materials, controls, _expressions = _service(
        submit_control=submit_control,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    controls.clear()

    async def register_canonical_event(_event: RuntimeEvent) -> None:
        """记录 canonical 事实已经完整应用。"""

        order.append("canonical_registered")

    handled = asyncio.run(
        service.handle_tool_commit_request(
            _tool_commit_request(active.self_reflection_id),
            register_canonical_event=register_canonical_event,
        )
    )

    assert handled is True
    assert order == ["canonical_registered", "approval_sent"]


def test_canonical_registration_crossing_deadline_still_finishes_one_commit() -> None:
    """登记已成功时必须冻结提交完成，不能随后同时批准和判定超时。"""

    clock = [10.0]
    service, attention, memory, _materials, controls, _expressions = _service(
        clock=clock,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    controls.clear()

    async def register_canonical_event(_event: RuntimeEvent) -> None:
        """模拟 canonical 应用完成时单调时钟刚好到达 deadline。"""

        clock[0] = active.deadline

    handled = asyncio.run(
        service.handle_tool_commit_request(
            _tool_commit_request(active.self_reflection_id),
            register_canonical_event=register_canonical_event,
        )
    )

    assert handled is True
    decisions = [
        command
        for command in controls
        if isinstance(command, SelfReflectionToolCommitDecisionCommand)
    ]
    assert len(decisions) == 1
    assert decisions[0].approved is True
    snapshot = attention.snapshot(active.self_reflection_id)
    assert snapshot is not None
    assert snapshot.state is SelfReflectionState.COMMITTED
    assert service.reap_expired() == 0


def test_background_commit_handoff_waits_for_main_decision_after_local_deadline() -> None:
    """Background 请求入队后，即使本地 deadline 到达也要服从主进程裁决。"""

    clock = [10.0]
    emitted_requests: list[RuntimeEvent] = []
    request_emitted = Event()
    cancellation = Event()
    broker_completed = Event()
    canonical_registration_started = Event()
    allow_canonical_registration_to_finish = Event()
    applied_decisions: list[bool] = []

    def emit_request(event: RuntimeEvent) -> None:
        """保存已经转交给主进程的请求，并唤醒测试线程。"""

        emitted_requests.append(event)
        request_emitted.set()

    broker = SelfReflectionToolCommitBroker(
        emit_event=emit_request,
        response_timeout_seconds=1.0,
        retry_interval_seconds=0.02,
        commit_request_id_factory=lambda: "commit-request-1",
        monotonic_now=lambda: clock[0],
    )

    def submit_control(command: object, _epoch: int) -> bool:
        """把主进程决定真实送回 Broker，并记录是否仍有 pending 接收。"""

        if not isinstance(command, SelfReflectionToolCommitDecisionCommand):
            return True
        applied = broker.apply_decision(command)
        applied_decisions.append(applied)
        return applied

    service, attention, memory, _materials, _controls, _expressions = _service(
        clock=clock,
        submit_control=submit_control,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None

    canonical_event = RuntimeEvent(
        type="llm.foreground.tool_call_completed",
        source="assistant_runtime",
        runtime_epoch=7,
        produced_at=10.5,
        payload=dict(
            _tool_commit_request(active.self_reflection_id).payload[
                "canonical_event_payload"
            ]
        ),
    )

    async def register_canonical_event(_event: RuntimeEvent) -> None:
        """让登记跨过 deadline，复现子进程取消早于主进程批准的窗口。"""

        clock[0] = active.deadline
        cancellation.set()
        canonical_registration_started.set()
        assert allow_canonical_registration_to_finish.wait(timeout=2.0)

    executor = ThreadPoolExecutor(max_workers=2)
    broker_future = None
    try:
        broker_future = executor.submit(
            broker.request_and_wait,
            self_reflection_id=active.self_reflection_id,
            source_activity_id="cognitive-1",
            runtime_epoch=7,
            tool_call_id="background-tool-call-1",
            tool_name="start_background_work",
            execution_channel="background",
            canonical_event=canonical_event,
            deadline=active.deadline,
            cancellation_event=cancellation,
        )
        broker_future.add_done_callback(lambda _future: broker_completed.set())
        assert request_emitted.wait(timeout=2.0)
        forwarded_requests: list[RuntimeEvent] = []
        assert broker.forward_request_to_main(
            emitted_requests[0],
            send_event=forwarded_requests.append,
        ) is True
        service_future = executor.submit(
            asyncio.run,
            service.handle_tool_commit_request(
                forwarded_requests[0],
                register_canonical_event=register_canonical_event,
            ),
        )
        assert canonical_registration_started.wait(timeout=2.0)

        # canonical 登记已经开始后，子进程不能再凭本地 timer 回滚真实动作。
        assert broker_completed.wait(timeout=0.1) is False
        allow_canonical_registration_to_finish.set()

        assert service_future.result(timeout=2.0) is True
        decision = broker_future.result(timeout=2.0)
    finally:
        allow_canonical_registration_to_finish.set()
        if broker_future is not None and not broker_future.done():
            broker.apply_decision(
                SelfReflectionToolCommitDecisionCommand(
                    commit_request_id="commit-request-1",
                    self_reflection_id=active.self_reflection_id,
                    source_activity_id="cognitive-1",
                    runtime_epoch=7,
                    tool_call_id="background-tool-call-1",
                    tool_name="start_background_work",
                    approved=False,
                    reason="test_cleanup",
                    issued_at=clock[0],
                )
            )
        executor.shutdown(wait=True, cancel_futures=True)

    assert decision.approved is True
    assert decision.reason == "tool_commit_approved"
    assert applied_decisions == [True]
    snapshot = attention.snapshot(active.self_reflection_id)
    assert snapshot is not None
    assert snapshot.state is SelfReflectionState.COMMITTED


def test_deadline_reaper_cannot_rewrite_approved_background_commit() -> None:
    """批准后 outcome 尚未返回时，reaper 不能制造第二个超时终态。"""

    clock = [10.0]
    service, attention, memory, _materials, controls, _expressions = _service(
        clock=clock,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    controls.clear()

    async def register_canonical_event(_event: RuntimeEvent) -> None:
        """模拟 deadline 前正常完成的 canonical 应用。"""

    assert asyncio.run(
        service.handle_tool_commit_request(
            _tool_commit_request(active.self_reflection_id),
            register_canonical_event=register_canonical_event,
        )
    ) is True
    clock[0] = active.deadline

    assert service.reap_expired() == 0
    snapshot = attention.snapshot(active.self_reflection_id)
    assert snapshot is not None
    assert snapshot.state is SelfReflectionState.COMMITTED
    decisions = [
        command
        for command in controls
        if isinstance(command, SelfReflectionToolCommitDecisionCommand)
    ]
    assert len(decisions) == 1
    assert decisions[0].approved is True


def test_deadline_before_tool_commit_rejects_without_canonical_registration() -> None:
    """工具尚未越过授权边界时拒绝调用，但把关闭留给 Recovery fallback。"""

    clock = [10.0]
    service, attention, memory, _materials, controls, _expressions = _service(
        clock=clock,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    controls.clear()
    clock[0] = active.deadline
    registered: list[RuntimeEvent] = []

    async def register_canonical_event(event: RuntimeEvent) -> None:
        """保存不应发生的 canonical 登记。"""

        registered.append(event)

    handled = asyncio.run(
        service.handle_tool_commit_request(
            _tool_commit_request(active.self_reflection_id),
            register_canonical_event=register_canonical_event,
        )
    )

    assert handled is True
    assert registered == []
    decisions = [
        command
        for command in controls
        if isinstance(command, SelfReflectionToolCommitDecisionCommand)
    ]
    assert len(decisions) == 1
    assert decisions[0].approved is False
    assert decisions[0].reason == "reflection_deadline_exceeded"
    snapshot = attention.snapshot(active.self_reflection_id)
    assert snapshot is not None
    assert snapshot.state is SelfReflectionState.REFLECTING
    assert snapshot.outcome is not None
    assert snapshot.outcome.reason == "reflection_deadline_exceeded"
    _settle_entry_expression(service, _expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.CLOSED_FAILED


def test_reaper_requests_fallback_without_closing_episode() -> None:
    """reaper 只发出一次 fallback 请求，正文 settlement 前不释放资源。"""

    clock = [10.0]
    fallback_requests: list[tuple[str, int, str]] = []
    service, attention, memory, materials, _controls, _expressions = _service(
        clock=clock,
        request_recovery_fallback=lambda reflection_id, epoch, reason: (
            fallback_requests.append((reflection_id, epoch, reason)) or True
        ),
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None

    clock[0] = active.deadline
    assert service.reap_expired() == 1
    assert fallback_requests == [
        (active.self_reflection_id, active.runtime_epoch, "reflection_deadline_exceeded")
    ]
    snapshot = attention.snapshot(active.self_reflection_id)
    assert snapshot is not None
    assert snapshot.state is SelfReflectionState.REFLECTING
    assert memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    ) is None
    assert materials.release_calls == []

    # deadline 已被标记，重复 reaper 不重复发送同一个 fallback。
    assert service.reap_expired() == 0
    assert len(fallback_requests) == 1


def test_reaper_does_not_cancel_decision_after_first_recovery_body() -> None:
    """首段正文关闭辅助准入后，迟到 deadline 不能夺走主 Recovery。"""

    clock = [10.0]
    fallback_requests: list[tuple[str, int, str]] = []
    service, attention, memory, _materials, _controls, expressions = _service(
        clock=clock,
        request_recovery_fallback=lambda reflection_id, epoch, reason: (
            fallback_requests.append((reflection_id, epoch, reason)) or True
        ),
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, expressions)

    assert service.observe_reply_admission_closed(
        RuntimeEvent(
            type="foreground.self_reflection_reply_admission_closed",
            source="assistant_output_lanes",
            runtime_epoch=active.runtime_epoch,
            payload={
                "self_reflection_id": active.self_reflection_id,
                "episode_id": active.episode_id,
                "runtime_epoch": active.runtime_epoch,
            },
        )
    ) is True
    clock[0] = active.deadline
    assert service.reap_expired() == 0
    assert fallback_requests == []


def test_runtime_replacement_waits_for_auxiliary_settlement_before_episode_commit() -> None:
    """热替换保留辅助 reservation，已公开正文结算后才提交完整 episode。"""

    episode_events: list[RuntimeEvent] = []
    service, attention, memory, _materials, _controls, expressions = _service(
        episode_event_sink=episode_events,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    assert service.divert_final_input(
        DivertFinalInputCommand(
            runtime_epoch=7,
            input_identity="input-before-runtime-replacement",
            assistant_request_id="request-before-runtime-replacement",
            source="text",
            text="这条辅助回应会被热替换终止",
        )
    ).status == "accepted"
    reply_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionInterruptionReplyContext)
    )
    assert reply_context.request_id in service._interruption_reply_reservations

    service.terminate_runtime_epoch(7, new_runtime_epoch=8)
    assert service._interruption_reply_reservations != {}
    assert episode_events == []
    assert _settle_entry_expression(service, expressions) is None
    assert episode_events == []
    replacement_settlement = _visible_settlement(
        request_source="self_reflection_interruption_reply",
        self_reflection_id=active.self_reflection_id,
        episode_id=active.episode_id,
        runtime_epoch=active.runtime_epoch,
        request_id=reply_context.request_id,
        visible_text="我听到了，但旧运行时已经结束。",
        trigger_input_identity="input-before-runtime-replacement",
        outcome="interrupted",
    )
    late_disposition = service.observe_expression_settled(replacement_settlement)
    duplicate_disposition = service.observe_expression_settled(
        replacement_settlement
    )
    service.reap_expired()

    assert service._interruption_reply_reservations == {}
    assert late_disposition == "committed"
    assert duplicate_disposition == "ignored"
    assert len(episode_events) == 1
    assert episode_events[0].payload["episode_id"] == active.episode_id
    assert [
        item["episode_role"]
        for item in episode_events[0].payload["history_block"]
    ] == ["input", "interruption_reply", "closure"]
    episode = memory.unattended_input_episode(
        UnattendedEpisodeIdentity(
            self_reflection_id=active.self_reflection_id,
            runtime_epoch=active.runtime_epoch,
            episode_id=active.episode_id,
        )
    )
    assert episode is not None
    assert len(episode.interruption_replies) == 1
    assert service.observe_expression_settled(replacement_settlement) == "ignored"
    assert service.reap_expired() == 0


def test_recovery_settlement_cannot_cross_auxiliary_reservation_registration() -> None:
    """辅助顺序号与 reservation 登记之间不能让 Recovery 抢先关闭 Episode。"""

    service, attention, memory, _materials, _controls, expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, expressions)

    sequence_reserved = Event()
    allow_reservation_registration = Event()
    original_reserve_sequence = attention.reserve_interruption_reply_sequence

    def block_after_sequence_reservation(**kwargs: object) -> int | None:
        """在 Attention 已分配顺序号后阻塞 Service reservation 登记。"""

        sequence = original_reserve_sequence(**kwargs)  # type: ignore[arg-type]
        sequence_reserved.set()
        allow_reservation_registration.wait(timeout=2.0)
        return sequence

    attention.reserve_interruption_reply_sequence = block_after_sequence_reservation  # type: ignore[method-assign]
    diversion_thread = Thread(
        target=lambda: service.divert_final_input(
            DivertFinalInputCommand(
                runtime_epoch=7,
                input_identity="input-reservation-window",
                assistant_request_id="request-reservation-window",
                source="text",
                text="请等辅助回应登记完成",
            )
        )
    )
    settlement_results: list[ExpressionSettlementDisposition] = []
    settlement_thread = Thread(
        target=lambda: settlement_results.append(
            _settle_recovery_expression(service, active.self_reflection_id)
        )
    )

    diversion_thread.start()
    assert sequence_reserved.wait(timeout=2.0)
    settlement_thread.start()
    settlement_thread.join(timeout=0.2)
    settlement_waited_for_registration = settlement_thread.is_alive()
    allow_reservation_registration.set()
    diversion_thread.join(timeout=2.0)
    settlement_thread.join(timeout=2.0)

    assert settlement_waited_for_registration is True
    assert settlement_results == ["retryable"]
    assert attention.active_snapshot() is not None
    _settle_auxiliary_expressions(service, expressions)
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"


def test_runtime_replacement_consumes_conflicting_primary_settlement() -> None:
    """旧主输出身份损坏时只清理旧世代，不能阻塞新世代。"""

    episode_events: list[RuntimeEvent] = []
    current_epoch = [7]
    service, attention, memory, _materials, _controls, expressions = _service(
        episode_event_sink=episode_events,
        current_runtime_epoch=current_epoch,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    entry_context = next(
        context
        for context, _request in expressions
        if isinstance(context, SelfReflectionEntryContext)
    )

    service.terminate_runtime_epoch(7, new_runtime_epoch=8)
    current_epoch[0] = 8

    assert episode_events == []
    assert active.self_reflection_id in service._pending_runtime_termination_cleanups
    assert service.observe_expression_settled(
        _visible_settlement(
            request_source="self_reflection_entry",
            self_reflection_id=active.self_reflection_id,
            episode_id="late-corrupted-episode",
            runtime_epoch=7,
            request_id=entry_context.request_id,
            outcome="interrupted",
        )
    ) == "committed"
    assert attention.runtime_epoch == 8
    assert len(episode_events) == 1
    assert service._pending_runtime_termination_cleanups == {}


def test_runtime_epoch_publication_and_attention_termination_share_one_lock() -> None:
    """候选发布和旧 Attention 终止之间不能接纳新输入。"""

    episode_events: list[RuntimeEvent] = []
    current_epoch = [7]
    service, attention, memory, _materials, _controls, _expressions = _service(
        episode_event_sink=episode_events,
        current_runtime_epoch=current_epoch,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    candidate_published = Event()
    allow_termination = Event()

    def publish_candidate() -> AssistantProcessReplacement:
        """模拟稳定句柄已经切换，但在生命周期交接前暂停。"""

        current_epoch[0] = 8
        candidate_published.set()
        allow_termination.wait(timeout=2.0)
        return AssistantProcessReplacement(
            old_runtime=object(),
            old_epoch=7,
            current_epoch=8,
        )

    replacement_thread = Thread(
        target=lambda: service.publish_runtime_candidate(
            publish_candidate,
            lambda _replacement: None,
        )
    )
    diversion_results: list[object] = []
    diversion_thread = Thread(
        target=lambda: diversion_results.append(
            service.divert_final_input(
                DivertFinalInputCommand(
                    runtime_epoch=8,
                    input_identity="input-during-epoch-publication",
                    assistant_request_id="request-during-epoch-publication",
                    source="text",
                    text="不能进入中间世代",
                )
            )
        )
    )

    replacement_thread.start()
    assert candidate_published.wait(timeout=2.0)
    diversion_thread.start()
    diversion_thread.join(timeout=0.2)
    input_waited_for_termination = diversion_thread.is_alive()
    allow_termination.set()
    replacement_thread.join(timeout=2.0)
    diversion_thread.join(timeout=2.0)

    assert input_waited_for_termination is True
    assert diversion_results
    assert diversion_results[0].status == "not_diverted"  # type: ignore[union-attr]
    assert attention.runtime_epoch == 8
    assert active.self_reflection_id in service._pending_runtime_termination_cleanups
    assert episode_events == []


def test_runtime_recovery_from_detached_process_advances_attention() -> None:
    """旧进程已清空句柄时，恢复发布仍使文本、语音和提醒取得新世代准入。"""
    current_epoch = [7]
    service, attention, *_ = _service(current_runtime_epoch=current_epoch)
    service.terminate_runtime_epoch(7, new_runtime_epoch=8)
    current_epoch[0] = 9
    service.publish_runtime_candidate(
        lambda: AssistantProcessReplacement(
            old_runtime=None,
            old_epoch=None,
            current_epoch=9,
        ),
        lambda _replacement: None,
    )
    assert attention.runtime_epoch == 9
    for source in ("text", "final_asr"):
        result = service.divert_final_input(DivertFinalInputCommand(runtime_epoch=9, input_identity="input-" + source,
            assistant_request_id="request-" + source, source=source, text="恢复后输入"))
        assert result.status == "not_diverted"
        assert result.reason != "runtime_epoch_mismatch"
    lease = attention.reserve_assistant_request(request_id="reminder", runtime_epoch=9, request_source="timed_reminder_notice")
    assert lease is not None
    assert lease.commit_enqueued()


def test_runtime_publication_cleanup_failure_still_returns_committed_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """稳定句柄交换后的旧世代收口失败不能再向配置事务抛错。"""

    service, _attention, *_ = _service(current_runtime_epoch=[7])
    replacement = AssistantProcessReplacement(
        old_runtime=object(),
        old_epoch=7,
        current_epoch=8,
    )
    publication_calls: list[str] = []

    def fail_post_publication_cleanup(
        runtime_epoch: int,
        *,
        new_runtime_epoch: int,
        reason: str = "runtime_reconfigured",
    ) -> None:
        """模拟句柄已交换后旧 Self-Reflection 收口发生诊断异常。"""

        assert runtime_epoch == 7
        assert new_runtime_epoch == 8
        assert reason == "runtime_reconfigured"
        publication_calls.append("cleanup")
        raise RuntimeError("post-publication cleanup failed")

    monkeypatch.setattr(
        service,
        "terminate_runtime_epoch",
        fail_post_publication_cleanup,
    )

    committed = service.publish_runtime_candidate(
        lambda: publication_calls.append("published") or replacement,
        lambda _replacement: publication_calls.append("committed"),
    )

    assert publication_calls == ["published", "committed", "cleanup"]
    assert committed is replacement


def test_runtime_replacement_before_tool_commit_never_registers_old_fact() -> None:
    """热替换先线性化时，旧世代同步请求不能登记 canonical 工具事实。"""

    current_epoch = [7]
    service, attention, memory, _materials, controls, _expressions = _service(
        current_runtime_epoch=current_epoch,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    service.terminate_runtime_epoch(7, new_runtime_epoch=8)
    current_epoch[0] = 8
    registered: list[RuntimeEvent] = []

    async def register_canonical_event(event: RuntimeEvent) -> None:
        """保存不应由旧世代触发的 canonical 登记。"""

        registered.append(event)

    handled = asyncio.run(
        service.handle_tool_commit_request(
            _tool_commit_request(active.self_reflection_id),
            register_canonical_event=register_canonical_event,
        )
    )

    assert handled is False
    assert registered == []
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.TERMINATED


def test_runtime_epoch_publication_lock_rejects_waiting_old_commit() -> None:
    """热替换先取得共享锁时，等待中的旧提交不能登记 canonical 事实。"""

    async def run_scenario() -> tuple[bool, list[RuntimeEvent], SelfReflectionState]:
        """在同一事件循环内让热替换先于旧工具提交越过共享锁。

        Returns:
            handler 结果、canonical 登记列表和旧恢复最终状态。
        """

        runtime_epoch_lock = asyncio.Lock()
        current_epoch = [7]
        service, attention, memory, _materials, _controls, _expressions = _service(
            current_runtime_epoch=current_epoch,
            runtime_epoch_mutation_lock=runtime_epoch_lock,
        )
        _register_started_source(service, memory)
        assert service.observe_cognitive_failed(_failed_event()).accepted is True
        active = attention.active_snapshot()
        assert active is not None
        registered: list[RuntimeEvent] = []

        async def register_canonical_event(event: RuntimeEvent) -> None:
            """保存热替换胜出后绝不能出现的 canonical 事实。"""

            registered.append(event)

        async with runtime_epoch_lock:
            handler = asyncio.create_task(
                service.handle_tool_commit_request(
                    _tool_commit_request(active.self_reflection_id),
                    register_canonical_event=register_canonical_event,
                )
            )
            await asyncio.sleep(0)
            current_epoch[0] = 8
            service.terminate_runtime_epoch(7, new_runtime_epoch=8)
        handled = await handler
        snapshot = attention.snapshot(active.self_reflection_id)
        assert snapshot is not None
        return handled, registered, snapshot.state

    handled, registered, state = asyncio.run(run_scenario())

    assert handled is False
    assert registered == []
    assert state is SelfReflectionState.TERMINATED


def test_tool_commit_lock_finishes_approval_before_runtime_epoch_publication() -> None:
    """旧提交先取得共享锁时，热替换必须等批准送回后才能发布新世代。"""

    async def run_scenario() -> tuple[bool, list[str], list[object]]:
        """阻塞 canonical callback，证明热替换不能越过正在提交的旧 Runtime。

        Returns:
            handler 结果、跨锁时序和全部控制命令。
        """

        runtime_epoch_lock = asyncio.Lock()
        current_epoch = [7]
        order: list[str] = []

        def submit_control(command: object, _epoch: int) -> bool:
            """记录批准真正进入旧 Runtime 控制端口的锁内时刻。"""

            if isinstance(command, SelfReflectionToolCommitDecisionCommand):
                order.append("approval_sent")
            return True

        service, attention, memory, _materials, controls, _expressions = _service(
            current_runtime_epoch=current_epoch,
            runtime_epoch_mutation_lock=runtime_epoch_lock,
            submit_control=submit_control,
        )
        _register_started_source(service, memory)
        assert service.observe_cognitive_failed(_failed_event()).accepted is True
        active = attention.active_snapshot()
        assert active is not None
        canonical_started = asyncio.Event()
        allow_canonical_to_finish = asyncio.Event()

        async def register_canonical_event(_event: RuntimeEvent) -> None:
            """占住提交事务，直到测试确认热替换正在等待同一把锁。"""

            order.append("canonical_started")
            canonical_started.set()
            await allow_canonical_to_finish.wait()
            order.append("canonical_finished")

        handler = asyncio.create_task(
            service.handle_tool_commit_request(
                _tool_commit_request(active.self_reflection_id),
                register_canonical_event=register_canonical_event,
            )
        )
        await canonical_started.wait()

        async def publish_new_epoch() -> None:
            """复用生产配置锁发布新世代并终止旧 Attention。"""

            async with runtime_epoch_lock:
                order.append("epoch_publication_started")
                current_epoch[0] = 8
                service.terminate_runtime_epoch(7, new_runtime_epoch=8)

        replacement = asyncio.create_task(publish_new_epoch())
        await asyncio.sleep(0)
        assert current_epoch == [7]
        allow_canonical_to_finish.set()
        handled = await handler
        await replacement
        return handled, order, controls

    handled, order, controls = asyncio.run(run_scenario())

    assert handled is True
    assert order == [
        "canonical_started",
        "canonical_finished",
        "approval_sent",
        "epoch_publication_started",
    ]
    decisions = [
        command
        for command in controls
        if isinstance(command, SelfReflectionToolCommitDecisionCommand)
    ]
    assert len(decisions) == 1
    assert decisions[0].approved is True


def test_admission_rollback_closes_episode_and_releases_resources_once() -> None:
    """准入后构造 Recovery 失败时，每项已取得资源只能执行一次释放。"""

    episode_events: list[RuntimeEvent] = []
    service, _attention, memory, materials, _controls, _expressions = _service(
        episode_event_sink=episode_events,
    )
    _register_started_source(service, memory)
    close_calls: list[UnattendedEpisodeIdentity] = []
    original_close = memory.close_unattended_input_episode

    def record_close(identity: UnattendedEpisodeIdentity) -> object:
        """记录 episode 关闭调用并复用真实幂等实现。"""

        close_calls.append(identity)
        return original_close(identity)

    def fail_recovery_context(*_args: object, **_kwargs: object) -> object:
        """在三项准入资源都取得后注入 Recovery 上下文构造失败。"""

        raise RuntimeError("recovery_context_build_failed")

    memory.close_unattended_input_episode = record_close  # type: ignore[method-assign]
    service._build_recovery_context = fail_recovery_context  # type: ignore[method-assign]

    assert service.observe_cognitive_failed(_failed_event()).accepted is False
    assert len(close_calls) == 1
    assert materials.release_calls == ["material-context-1"]
    assert memory._recovery_compaction_pins.active is False
    assert service._pending_admission_cleanups == {}
    assert service._sources == {}
    assert len(episode_events) == 1
    assert episode_events[0].type == "self_reflection.episode_committed"
    assert episode_events[0].payload["actionable"] is False
    assert episode_events[0].payload["history_block"] == []


def test_admission_cleanup_retry_skips_already_released_phases() -> None:
    """清理后半段失败时，重试不能再次关闭片段或释放同一 pin。"""

    materials = _MaterialLifecycle(fail_first_release=True)
    service, _attention, memory, _materials, _controls, _expressions = _service(
        material_lifecycle=materials,
    )
    _register_started_source(service, memory)
    close_calls: list[UnattendedEpisodeIdentity] = []
    pin_release_calls: list[str] = []
    original_close = memory.close_unattended_input_episode
    original_release_pin = memory.release_recovery_compaction_pin

    def record_close(identity: UnattendedEpisodeIdentity) -> object:
        """记录片段关闭次数并复用真实实现。"""

        close_calls.append(identity)
        return original_close(identity)

    def record_pin_release(self_reflection_id: str) -> bool:
        """记录 pin 释放次数并复用真实实现。"""

        pin_release_calls.append(self_reflection_id)
        return original_release_pin(self_reflection_id)

    def fail_recovery_context(*_args: object, **_kwargs: object) -> object:
        """在所有 Admission 资源取得后模拟上下文构造失败。"""

        raise RuntimeError("recovery_context_build_failed")

    memory.close_unattended_input_episode = record_close  # type: ignore[method-assign]
    memory.release_recovery_compaction_pin = record_pin_release  # type: ignore[method-assign]
    service._build_recovery_context = fail_recovery_context  # type: ignore[method-assign]

    assert service.observe_cognitive_failed(_failed_event()).accepted is False
    assert len(close_calls) == 1
    assert pin_release_calls == ["self-reflection-1"]
    assert materials.release_calls == ["material-context-1"]
    assert "self-reflection-1" in service._pending_admission_cleanups

    assert service.reap_expired() == 1
    assert len(close_calls) == 1
    assert pin_release_calls == ["self-reflection-1"]
    assert materials.release_calls == ["material-context-1", "material-context-1"]
    assert service._pending_admission_cleanups == {}


def test_concurrent_reapers_cannot_claim_same_admission_cleanup() -> None:
    """两个并发 reaper 不能重复释放同一份 Admission 清理凭据。"""

    materials = _MaterialLifecycle(fail_first_release=True)
    service, _attention, memory, _materials, _controls, _expressions = _service(
        material_lifecycle=materials,
    )
    _register_started_source(service, memory)

    def fail_recovery_context(*_args: object, **_kwargs: object) -> object:
        """在所有 Admission 资源取得后模拟上下文构造失败。"""

        raise RuntimeError("recovery_context_build_failed")

    service._build_recovery_context = fail_recovery_context  # type: ignore[method-assign]
    assert service.observe_cognitive_failed(_failed_event()).accepted is False
    assert "self-reflection-1" in service._pending_admission_cleanups

    cleanup_started = Event()
    allow_cleanup_to_finish = Event()
    retry_release_calls: list[str] = []

    def block_material_release(context_id: str) -> object:
        """阻塞首轮重试，为第二个 reaper 留出确定的重叠窗口。"""

        retry_release_calls.append(context_id)
        cleanup_started.set()
        assert allow_cleanup_to_finish.wait(timeout=3.0)
        return []

    materials.release_turn = block_material_release  # type: ignore[method-assign]

    completed_counts, second_finished_while_first_blocked = _run_overlapping_reapers(
        service,
        cleanup_started=cleanup_started,
        allow_cleanup_to_finish=allow_cleanup_to_finish,
    )

    assert second_finished_while_first_blocked is True
    assert sorted(completed_counts) == [0, 1]
    assert retry_release_calls == ["material-context-1"]
    assert service._pending_admission_cleanups == {}


def test_recovery_expression_is_submitted_before_normal_input_gate_reopens() -> None:
    """Recovery 请求提交时保持 Attention，直到它的可见正文结算。"""

    recovery_submission_states: list[SelfReflectionState] = []
    attention_holder: list[ForegroundAttentionCoordinator] = []

    def observe_submission(context: object, _request: AssistantRequest) -> bool:
        """记录 Recovery 提交瞬间 Attention 仍处于哪个状态。"""

        if isinstance(context, SelfReflectionRecoveryContext):
            active = attention_holder[0].active_snapshot()
            assert active is not None
            recovery_submission_states.append(active.state)
        return True

    service, attention, memory, _materials, _controls, _expressions = _service(
        submit_expression=observe_submission
    )
    attention_holder.append(attention)
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None

    _settle_entry_expression(service, _expressions)

    assert recovery_submission_states == [SelfReflectionState.REFLECTING]
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.REFLECTING
    assert _settle_recovery_expression(
        service,
        active.self_reflection_id,
    ) == "committed"
    assert attention.snapshot(active.self_reflection_id).state is SelfReflectionState.CLOSED


def test_runtime_replacement_retries_failed_resource_cleanup() -> None:
    """热替换释放材料失败时保留凭据，后续重试只释放尚未成功的资源。"""

    service, attention, memory, materials, _controls, _expressions = _service(
        material_lifecycle=_MaterialLifecycle(fail_first_release=True),
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    close_calls: list[UnattendedEpisodeIdentity] = []
    recovery_commit_calls: list[str] = []
    original_close = memory.close_unattended_input_episode
    original_commit_recovery = memory.commit_task_recovery

    def record_close(identity: UnattendedEpisodeIdentity) -> object:
        """记录旧世代片段实际关闭次数。"""

        close_calls.append(identity)
        return original_close(identity)

    def record_recovery_commit(**kwargs: object) -> object:
        """记录旧世代恢复关系实际提交次数。"""

        recovery_commit_calls.append(str(kwargs["source_activity_id"]))
        return original_commit_recovery(**kwargs)  # type: ignore[arg-type]

    memory.close_unattended_input_episode = record_close  # type: ignore[method-assign]
    memory.commit_task_recovery = record_recovery_commit  # type: ignore[method-assign]

    service.terminate_runtime_epoch(7, new_runtime_epoch=8)

    entry_context = next(
        context
        for context, _request in _expressions
        if isinstance(context, SelfReflectionEntryContext)
    )
    assert service.observe_expression_settled(
        _visible_settlement(
            request_source="self_reflection_entry",
            self_reflection_id=active.self_reflection_id,
            episode_id=active.episode_id,
            runtime_epoch=active.runtime_epoch,
            request_id=entry_context.request_id,
            outcome="interrupted",
        )
    ) == "committed"
    assert attention.runtime_epoch == 8
    assert service._sources == {}
    assert active.self_reflection_id in service._pending_runtime_termination_cleanups
    assert materials.release_calls == ["material-context-1"]
    assert memory._recovery_compaction_pins.active is False
    assert len(close_calls) == 1
    assert recovery_commit_calls == ["cognitive-1"]

    assert service.reap_expired() == 1
    assert service._pending_runtime_termination_cleanups == {}
    assert materials.release_calls == ["material-context-1", "material-context-1"]
    assert materials.lease_counts == {}
    assert len(close_calls) == 1
    assert recovery_commit_calls == ["cognitive-1"]


def test_concurrent_reapers_cannot_claim_same_runtime_termination_cleanup() -> None:
    """两个并发 reaper 不能重复释放同一份热替换清理凭据。"""

    service, attention, memory, materials, _controls, _expressions = _service(
        material_lifecycle=_MaterialLifecycle(fail_first_release=True),
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None

    service.terminate_runtime_epoch(7, new_runtime_epoch=8)
    assert active.self_reflection_id in service._pending_runtime_termination_cleanups

    entry_context = next(
        context
        for context, _request in _expressions
        if isinstance(context, SelfReflectionEntryContext)
    )
    assert service.observe_expression_settled(
        _visible_settlement(
            request_source="self_reflection_entry",
            self_reflection_id=active.self_reflection_id,
            episode_id=active.episode_id,
            runtime_epoch=active.runtime_epoch,
            request_id=entry_context.request_id,
            outcome="interrupted",
        )
    ) == "committed"

    cleanup_started = Event()
    allow_cleanup_to_finish = Event()
    retry_release_calls: list[str] = []

    def block_material_release(context_id: str) -> object:
        """阻塞首轮材料释放，让第二个 reaper 尝试领取同一凭据。"""

        retry_release_calls.append(context_id)
        cleanup_started.set()
        assert allow_cleanup_to_finish.wait(timeout=3.0)
        return []

    materials.release_turn = block_material_release  # type: ignore[method-assign]

    completed_counts, second_finished_while_first_blocked = _run_overlapping_reapers(
        service,
        cleanup_started=cleanup_started,
        allow_cleanup_to_finish=allow_cleanup_to_finish,
    )

    assert second_finished_while_first_blocked is True
    assert sorted(completed_counts) == [0, 1]
    assert retry_release_calls == ["material-context-1"]
    assert service._pending_runtime_termination_cleanups == {}


def test_rejected_recovery_submission_closes_without_periodic_visible_retry() -> None:
    """Recovery 请求被拒绝时关闭失败状态，周期 reaper 不得重复抢占。"""

    recovery_attempts: list[str] = []

    def reject_recovery(context: object, _request: AssistantRequest) -> bool:
        """记录 Recovery 提交并模拟当前输出链路拒绝。"""

        if isinstance(context, SelfReflectionRecoveryContext):
            recovery_attempts.append("recovery")
            return False
        return True

    service, attention, memory, _materials, _controls, _expressions = _service(
        submit_expression=reject_recovery,
    )
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    _settle_entry_expression(service, _expressions)
    assert recovery_attempts == ["recovery"]
    closed = attention.snapshot(active.self_reflection_id)
    assert closed is not None
    assert closed.state is SelfReflectionState.CLOSED_FAILED
    assert service._sources == {}

    assert service.reap_expired() == 0
    assert recovery_attempts == ["recovery"]

    service.terminate_runtime_epoch(7, new_runtime_epoch=8)

    assert service._sources == {}
    assert service.reap_expired() == 0
    assert recovery_attempts == ["recovery"]


def test_background_acceptance_adopts_recovery_lease_when_retain_fails() -> None:
    """TaskResult retain 失败时应接管 Recovery 租约，并在终态恰好释放一次。"""

    service, attention, memory, materials, _controls, _expressions = _service()
    _register_started_source(service, memory)
    assert service.observe_cognitive_failed(_failed_event()).accepted is True
    active = attention.active_snapshot()
    assert active is not None
    acceptance = RuntimeEvent(
        type="llm.foreground.tool_call_completed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={
            "name": "start_background_work",
            "state": "accepted",
            "activity_id": "background-recovery-1",
            "request_id": "foreground-request-1",
            "deliverable": False,
            "turn_material_context_id": "material-context-1",
            "self_reflection_id": active.self_reflection_id,
            "recovery_of_activity_id": "cognitive-1",
        },
    )
    task_results = TaskResultDeliveryCoordinator(
        retain_material=lambda _context_id: False,
        release_material=lambda context_id: materials.release_turn(context_id),  # type: ignore[arg-type]
    )

    tracking = task_results.track_tool_acceptance(acceptance)

    assert tracking.accepted is True
    assert tracking.material_retained is False
    assert service.settle_background_material_acceptance(
        acceptance,
        material_context_id=tracking.material_context_id,
        task_result_material_retained=tracking.material_retained,
        adopt_material_lease=task_results.adopt_material_lease,
    ) is True
    assert active.self_reflection_id not in service._material_contexts
    assert materials.release_calls == []

    terminal = RuntimeEvent(
        type="background_agent.completed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={
            "activity_id": "background-recovery-1",
            "interaction_session_id": "interaction-1",
            "goal": "转换音频",
            "display_content": "转换完成。",
            "source_materials": [],
            "created_at": 12.0,
        },
    )
    delivery = task_results.track_terminal(terminal)
    assert delivery is not None
    task_results.release_terminal(delivery.source_event)
    assert materials.release_calls == ["material-context-1"]

    assert service.observe_outcome(
        RuntimeEvent(
            type="foreground.self_reflection_outcome",
            source="assistant_runtime",
            runtime_epoch=7,
            payload=_no_action_outcome(active.self_reflection_id).to_payload(),
        )
    ) is True
    assert materials.release_calls == ["material-context-1"]


def test_cognitive_non_recovery_terminal_releases_frozen_source_request() -> None:
    """Cognitive 正常完成或取消后不能永久保留整份 Foreground 请求快照。"""

    service, _attention, memory, materials, _controls, _expressions = _service()
    _register_started_source(service, memory)

    assert service.observe_cognitive_terminal(
        RuntimeEvent(
            type="cognitive_task.completed",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "activity_id": "cognitive-1",
                "interaction_session_id": "interaction-1",
            },
        )
    ) is True
    assert service._sources == {}
    assert materials.lease_counts == {}


def test_cognitive_terminal_material_release_retries_after_transient_failure() -> None:
    """普通 Cognitive 终态释放失败时，周期收口只重试一次未完成租约。"""

    materials = _MaterialLifecycle(fail_first_release=True)
    service, _attention, memory, _materials, _controls, _expressions = _service(
        material_lifecycle=materials,
    )
    _register_started_source(service, memory)

    assert service.observe_cognitive_terminal(
        RuntimeEvent(
            type="cognitive_task.cancelled",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={"activity_id": "cognitive-1"},
        )
    ) is True
    assert service._pending_source_material_cleanups
    assert materials.lease_counts == {"material-context-1": 1}

    assert service.reap_expired() == 1
    assert service._pending_source_material_cleanups == {}
    assert materials.release_calls == ["material-context-1"] * 2
    assert materials.lease_counts == {}


def test_runtime_replacement_releases_source_material_before_admission() -> None:
    """热替换发生在失败事件前时，也必须收回 Cognitive 来源租约。"""

    service, _attention, memory, materials, _controls, _expressions = _service()
    _register_started_source(service, memory)

    service.terminate_runtime_epoch(7, new_runtime_epoch=8)

    assert service._sources == {}
    assert materials.release_calls == ["material-context-1"]
    assert materials.lease_counts == {}
    assert service._pending_source_material_cleanups == {}


def test_protocol_failure_releases_source_without_takeover_tombstone() -> None:
    """最终协议故障继续生成普通 Notice，不能留下恢复接管墓碑。"""

    service, _attention, memory, materials, _controls, _expressions = _service()
    _register_started_source(service, memory)
    ineligible = RuntimeEvent(
        type="cognitive_task.failed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={
            "activity_id": "cognitive-1",
            "interaction_session_id": "interaction-1",
            "failure": {"code": "cognitive_result_protocol_failed"},
        },
    )

    assert service.observe_cognitive_failed(ineligible).accepted is False
    assert service._sources == {}
    assert service._settled_source_activity_ids == set()
    assert materials.lease_counts == {}
    assert service.observe_cognitive_failed(ineligible).accepted is False
