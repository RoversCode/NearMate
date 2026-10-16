from __future__ import annotations

import asyncio
import base64
import threading
from queue import Queue
from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.activities.notice.delivery_coordinator import NoticeDeliveryCoordinator
from companion.activities.notice.delivery_event_mapper import map_notice_delivery_event
from companion.activities.notice.models import (
    ActivityEventApplyResult,
    ActivityProgressNoticeIntent,
    NoticeDeliveryCommand,
)
from companion.background_agent.interaction_registry import PendingAgentInteraction
from companion.character_cards.models import CharacterCardDefinition
from companion.core.application.turn_admission import TurnAdmissionSequencer
from companion.realtime.assistant_audio_output import (
    ActiveAssistantPlayout,
    AssistantAudioOutputScheduler,
    QueuedAssistantAudioEvent,
)
from companion.realtime.assistant_process_bridge import (
    AssistantOutputUnavailableError,
    AssistantProcessBridge,
)
from companion.realtime.assistant_events import AssistantControlCommand, AssistantRequest, RuntimeEvent
from companion.realtime.assistant_process_handle import SwappableAssistantProcessClient
from companion.realtime.livekit_media_session import LiveKitMediaSessionController
from companion.realtime.notify_queue import NotifyQueue
from companion.realtime.system_notice_playout import SystemNoticePlayoutTracker
from companion.realtime.visible_output_journal import VisibleOutputSettlement
from companion.runtime_memory.models import RuntimeMemorySnapshot

from backend.tests.activities.notice_delivery_test_support import (
    BlockingBridgeAudioOutput,
    OrderedBridgeRuntime,
    RecordingAssistantBridge,
    RecordingCoordinator,
    _apply_delivery_event,
    build_assistant_request_context_preparer_for_test,
    build_livekit_media_session_for_test,
)


def test_notice_supersede_interruption_restores_non_stale_batch_items() -> None:
    """弱提醒过期触发的取消必须走 supersede 回滚，而非用户打断语义。"""

    coordinator = RecordingCoordinator()
    _apply_delivery_event(coordinator,
        "assistant.generation_interrupted",
        {
            "request_id": "request-a",
            "reason": "activity_notice_superseded",
        },
    )

    assert coordinator.superseded == ["request-a"]
    assert coordinator.cancelled == []


def test_interrupted_final_before_interruption_event_restores_valid_batch_items() -> None:
    """带尾注 final 即使先到，也必须按 supersede 语义归还同批仍有效的提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    coordinator.apply_event(
        ActivityProgressNoticeIntent(
            activity_id="cognitive-a",
            interaction_session_id="session-a",
            source="cognitive",
            sequence=1,
        ),
        1,
    )
    coordinator.apply_event(
        ActivityProgressNoticeIntent(
            activity_id="task-result-a",
            interaction_session_id="session-a",
            source="task_result",
            sequence=1,
        ),
        1,
    )
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    coordinator.apply_event(
        ActivityProgressNoticeIntent(
            activity_id="cognitive-a",
            interaction_session_id="session-a",
            source="cognitive",
            sequence=2,
        ),
        1,
    )
    interrupted_final = map_notice_delivery_event(
        "assistant.text_final",
        {
            "request_id": "request-a",
            "text": "旧提醒【状态已更新，此回复已中断】",
            "interrupted": True,
        },
    )
    assert interrupted_final is not None

    coordinator.apply_delivery(interrupted_final)
    restored = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert [
        (notice["activity_id"], notice["sequence"])
        for notice in restored["notice_batch"]
    ] == [
        ("task-result-a", 1),
        ("cognitive-a", 2),
    ]


def test_bridge_clears_only_matching_playout_after_notice_supersession() -> None:
    """子进程确认取消旧 Notice 后，Bridge 只能清掉对应请求的待播音频。"""

    class SupersededNoticeRuntime(OrderedBridgeRuntime):
        """返回一次已经由子进程来源门禁接受的 Notice 中断事件。"""

        def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
            """返回一条 Notice 失效中断事件。"""

            _ = limit
            return [
                RuntimeEvent(
                    type="assistant.generation_interrupted",
                    source="assistant_output_lanes",
                    payload={
                        "request_id": "request-a",
                        "source": "system_notice",
                        "reason": "activity_notice_superseded",
                    },
                )
            ]

    clear_calls: list[str] = []
    playout_tracker = SystemNoticePlayoutTracker()
    playout_tracker.register_request(
        request_id="request-a",
        interaction_request_ids=("permission-a",),
    )
    event_queue: NotifyQueue[RuntimeEvent] = NotifyQueue()
    bridge = AssistantProcessBridge(
        assistant_runtime=SwappableAssistantProcessClient(
            cast(Any, SupersededNoticeRuntime())
        ),
        assistant_request_queue=NotifyQueue(),
        assistant_control_queue=NotifyQueue(),
        event_queue=event_queue,
        audio_output_scheduler=cast(
            Any,
            SimpleNamespace(
                clear_pending_audio_for_request=lambda request_id: clear_calls.append(
                    request_id
                ),
            ),
        ),
        system_notice_playout_tracker=playout_tracker,
    )

    assert bridge.drain_assistant_runtime_once() is True

    assert clear_calls == ["request-a"]
    forwarded = event_queue.get_nowait()
    assert forwarded.type == "assistant.generation_interrupted"
    assert forwarded.payload["reason"] == "activity_notice_superseded"
    assert playout_tracker.resolve_interaction("permission-a") == ()


def test_resolved_permission_cancels_only_tracked_system_notice_playout() -> None:
    """权限生效后只停止承载该请求的 System Notice 语音。"""

    clear_calls: list[str] = []
    tracker = SystemNoticePlayoutTracker()
    tracker.register_request(
        request_id="system-notice-a",
        interaction_request_ids=("permission-a",),
    )
    tracker.register_request(
        request_id="system-notice-b",
        interaction_request_ids=("permission-b",),
    )
    media_session = build_livekit_media_session_for_test()
    media_session.system_notice_playout_tracker = tracker
    media_session.assistant_audio_output_scheduler = SimpleNamespace(
        clear_pending_audio_for_request=lambda request_id: clear_calls.append(
            request_id
        )
    )
    media_session.activity_notice_handler = lambda *_args: None

    media_session._handle_activity_event(
        "background_agent.interaction_resolved",
        {"request_id": "permission-a"},
        1,
    )

    assert clear_calls == ["system-notice-a"]
    assert tracker.resolve_interaction("permission-b") == ("system-notice-b",)


@pytest.mark.parametrize(
    ("superseded_request_ids", "expected_requests_after_grant"),
    [
        (("old-permission-notice",), 0),
        ((), 1),
    ],
)
def test_mouse_permission_acknowledgement_waits_only_for_unsettled_notice(
    superseded_request_ids: tuple[str, ...],
    expected_requests_after_grant: int,
) -> None:
    """短确认只等待仍在结算的旧请求，已结算音频取消后可以直接确认。"""

    submitted_requests: list[AssistantRequest] = []
    reserve_allowed = True

    class RecordingTurnAdmission:
        """模拟 System Notice 的原子预留和提交边界。"""

        def reserve_system_notice(self, _request_id: str) -> bool:
            """按测试开关决定是否仍有准入位置。"""

            return reserve_allowed

        def submit_reserved_system_notice(
            self,
            _request_id: str,
            submit_request: object,
        ) -> bool:
            """在测试中立即执行已预留的提交回调。"""

            return cast(Any, submit_request)()

        def is_request_unsettled(self, _request_id: str) -> bool:
            """测试替身没有额外的未结算请求。"""

            return False

        def release_notice_reservation(self, _request_id: str) -> None:
            """测试无需额外处理释放。"""

    snapshot = SimpleNamespace(
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        foreground_context={},
        runtime_memory_snapshot=RuntimeMemorySnapshot(),
        history_policy="stable_dialogue",
        activity_progress_references=(),
        derived_from_activity_ids=(),
    )
    tracker = SystemNoticePlayoutTracker()
    tracker.register_request(
        request_id="old-permission-notice",
        interaction_request_ids=("permission-a",),
    )
    cleared_audio: list[str] = []
    notice_event_types: list[str] = []

    def apply_notice_event(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityEventApplyResult:
        """记录进入 Notice Mapper 的事件并返回测试指定的过期请求。

        Args:
            event_type: 当前交给 Notice 链路的 Runtime Event 类型。
            _payload: 本用例不读取的事件载荷。
            _runtime_epoch: 本用例不读取的运行时世代。

        Returns:
            当前参数分支指定的过期 System Notice 请求。
        """

        notice_event_types.append(event_type)
        return ActivityEventApplyResult(
            superseded_request_ids=superseded_request_ids
        )

    media_session = build_livekit_media_session_for_test()
    media_session.assistant_output_enabled = True
    media_session.realtime_input_runtime = SimpleNamespace(
        current_audio_session_id="audio-a",
        interaction_state=SimpleNamespace(interaction_session_id="session-a"),
    )
    media_session.system_notice_playout_tracker = tracker
    media_session.assistant_audio_output_scheduler = SimpleNamespace(
        clear_pending_audio_for_request=lambda request_id: cleared_audio.append(
            request_id
        )
    )
    media_session.activity_notice_handler = apply_notice_event
    media_session.assistant_request_context_preparer = SimpleNamespace(
        prepare_system_output_snapshot=lambda: snapshot
    )
    media_session.turn_admission_sequencer = cast(Any, RecordingTurnAdmission())
    media_session.assistant_process_bridge = cast(
        Any,
        SimpleNamespace(
            remove_pending_requests=lambda _predicate: [],
            submit_control=lambda _command: True,
        ),
    )
    media_session.enqueue_assistant_request = lambda request: (  # type: ignore[method-assign]
        submitted_requests.append(request) or True
    )
    interaction = PendingAgentInteraction(
        request_id="permission-a",
        activity_id="activity-a",
        runtime_epoch=3,
        kind="permission",
        interaction_session_id="session-a",
        action_summary="打开网页",
        grant_scope="activity",
        state="answering",
    )

    media_session.handle_permission_card_decision(interaction, "allow")
    media_session._handle_activity_event(
        "background_agent.permission_granted",
        {
            "request_id": "permission-a",
            "activity_id": "activity-a",
            "interaction_session_id": "session-a",
            "grant_scope": "activity",
            "suppress_notice": False,
        },
        3,
    )

    assert cleared_audio == ["old-permission-notice"]
    assert notice_event_types == ["background_agent.interaction_resolved"]
    assert len(submitted_requests) == expected_requests_after_grant

    media_session._handle_visible_output_settled(
        _visible_output_settlement("old-permission-notice")
    )

    assert len(submitted_requests) == 1
    acknowledgement_request = submitted_requests[0]
    assert acknowledgement_request.source == "system_notice"
    assert acknowledgement_request.user_text == ""
    assert acknowledgement_request.direct_output_text in {
        "好，本次授权已经生效。",
        "收到，这次操作已经允许。",
        "嗯，这次权限已经放行。",
    }

    # 重放同一 Gate 事实和 settlement 也不能产生第二次确认。
    media_session._handle_activity_event(
        "background_agent.permission_granted",
        {
            "request_id": "permission-a",
            "activity_id": "activity-a",
            "interaction_session_id": "session-a",
            "grant_scope": "activity",
            "suppress_notice": False,
        },
        3,
    )
    media_session._handle_visible_output_settled(
        _visible_output_settlement("old-permission-notice")
    )
    assert len(submitted_requests) == 1


def test_permission_acknowledgement_waits_for_turn_admission_after_notice_text_final() -> None:
    """Coordinator 已闭合正文时，短确认仍须等待真实输出 settlement。"""

    async def fail_on_text_dispatch(_dispatch: object) -> object:
        """禁止本用例意外进入用户文本派发链路。

        Args:
            _dispatch: 本用例不应产生的文本派发参数。

        Raises:
            AssertionError: 任何文本派发都表示权限短确认走错了链路。
        """

        raise AssertionError("权限短确认测试不应派发用户文本")

    submitted_requests: list[AssistantRequest] = []
    turn_admission = TurnAdmissionSequencer(
        hub=cast(Any, SimpleNamespace()),
        material_lifecycle=cast(Any, SimpleNamespace()),
        dispatch_text_turn=cast(Any, fail_on_text_dispatch),
        cancel_system_notice=lambda _request_id: None,
    )
    turn_admission.register_assistant_request(
        "old-permission-notice",
        "system_notice",
    )
    snapshot = SimpleNamespace(
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        foreground_context={},
        runtime_memory_snapshot=RuntimeMemorySnapshot(),
        history_policy="stable_dialogue",
        activity_progress_references=(),
        derived_from_activity_ids=(),
    )
    tracker = SystemNoticePlayoutTracker()
    tracker.register_request(
        request_id="old-permission-notice",
        interaction_request_ids=("permission-a",),
    )
    media_session = build_livekit_media_session_for_test()
    media_session.assistant_output_enabled = True
    media_session.realtime_input_runtime = SimpleNamespace(
        current_audio_session_id="audio-a",
        interaction_state=SimpleNamespace(interaction_session_id="session-a"),
    )
    media_session.system_notice_playout_tracker = tracker
    media_session.assistant_audio_output_scheduler = SimpleNamespace(
        clear_pending_audio_for_request=lambda _request_id: None
    )
    # text_final 已让 Coordinator 闭合 Notice，但 Turn Admission 尚未收到 output_settled。
    media_session.activity_notice_handler = lambda *_args: ActivityEventApplyResult(
        superseded_request_ids=()
    )
    media_session.assistant_request_context_preparer = SimpleNamespace(
        prepare_system_output_snapshot=lambda: snapshot
    )
    media_session.turn_admission_sequencer = turn_admission
    media_session.assistant_process_bridge = cast(
        Any,
        SimpleNamespace(
            remove_pending_requests=lambda _predicate: [],
            submit_control=lambda _command: True,
        ),
    )
    media_session.enqueue_assistant_request = lambda request: (  # type: ignore[method-assign]
        submitted_requests.append(request) or True
    )
    interaction = PendingAgentInteraction(
        request_id="permission-a",
        activity_id="activity-a",
        runtime_epoch=3,
        kind="permission",
        interaction_session_id="session-a",
        action_summary="打开网页",
        grant_scope="activity",
        state="answering",
    )

    media_session.handle_permission_card_decision(interaction, "allow")
    media_session._handle_activity_event(
        "background_agent.permission_granted",
        {
            "request_id": "permission-a",
            "activity_id": "activity-a",
            "interaction_session_id": "session-a",
            "grant_scope": "activity",
            "suppress_notice": False,
        },
        3,
    )

    assert submitted_requests == []

    settlement = _visible_output_settlement("old-permission-notice")
    turn_admission.handle_output_settled(settlement)
    media_session._handle_visible_output_settled(settlement)

    assert len(submitted_requests) == 1
    assert submitted_requests[0].direct_output_text in {
        "好，本次授权已经生效。",
        "收到，这次操作已经允许。",
        "嗯，这次权限已经放行。",
    }

    # 重放 settlement 只能命中 Turn Admission 和媒体会话各自的幂等边界。
    turn_admission.handle_output_settled(settlement)
    media_session._handle_visible_output_settled(settlement)
    assert len(submitted_requests) == 1


def test_new_user_turn_causes_pending_mouse_permission_acknowledgement_to_be_omitted() -> None:
    """旧权限播报结算前若已有用户轮抢占，迟到的短确认应直接省略。"""

    submitted_requests: list[AssistantRequest] = []

    class UserWinningTurnAdmission:
        """模拟用户轮已经占用 System Notice 准入位置。"""

        def is_request_unsettled(self, _request_id: str) -> bool:
            """旧权限播报仍在等待结算。"""

            return True

        def reserve_system_notice(self, _request_id: str) -> bool:
            """拒绝迟到的权限短确认。"""

            return False

    tracker = SystemNoticePlayoutTracker()
    tracker.register_request(
        request_id="old-permission-notice",
        interaction_request_ids=("permission-a",),
    )
    media_session = build_livekit_media_session_for_test()
    media_session.assistant_output_enabled = True
    media_session.system_notice_playout_tracker = tracker
    media_session.assistant_audio_output_scheduler = SimpleNamespace(
        clear_pending_audio_for_request=lambda _request_id: None
    )
    media_session.activity_notice_handler = lambda *_args: ActivityEventApplyResult(
        superseded_request_ids=("old-permission-notice",)
    )
    media_session.turn_admission_sequencer = cast(Any, UserWinningTurnAdmission())
    media_session.assistant_process_bridge = cast(
        Any,
        SimpleNamespace(
            remove_pending_requests=lambda _predicate: [],
            submit_control=lambda _command: True,
        ),
    )
    media_session.enqueue_assistant_request = lambda request: (  # type: ignore[method-assign]
        submitted_requests.append(request) or True
    )
    interaction = PendingAgentInteraction(
        request_id="permission-a",
        activity_id="activity-a",
        runtime_epoch=3,
        kind="permission",
        interaction_session_id="session-a",
        action_summary="打开网页",
        grant_scope="activity",
        state="answering",
    )

    media_session.handle_permission_card_decision(interaction, "allow")
    media_session._handle_activity_event(
        "background_agent.permission_granted",
        {
            "request_id": "permission-a",
            "activity_id": "activity-a",
            "interaction_session_id": "session-a",
            "grant_scope": "activity",
            "suppress_notice": False,
        },
        3,
    )
    media_session._handle_visible_output_settled(
        _visible_output_settlement("old-permission-notice")
    )

    assert submitted_requests == []


def _visible_output_settlement(request_id: str) -> VisibleOutputSettlement:
    """构造权限短确认时序测试使用的最小结算事实。

    Args:
        request_id: 已经停止表达的旧 System Notice 请求编号。

    Returns:
        可直接交给媒体会话 settlement 回调的不可变事实。
    """

    return VisibleOutputSettlement(
        settlement_id=f"settlement-{request_id}",
        request_id=request_id,
        generation_id=f"generation-{request_id}",
        history_policy="stable_dialogue",
        outcome="superseded",
        visible_text="",
        progress_references=(),
        derived_from_activity_ids=(),
        settled_at=2.0,
        request_source="system_notice",
    )


def test_output_unavailable_interruption_uses_terminal_notice_closure() -> None:
    """Bridge 永久失败必须走不可重排队的提醒收口语义。"""

    coordinator = RecordingCoordinator()
    _apply_delivery_event(coordinator,
        "assistant.generation_interrupted",
        {
            "request_id": "request-a",
            "reason": "output_unavailable",
        },
    )

    assert coordinator.output_unavailable == ["request-a"]
    assert coordinator.cancelled == []


def test_activity_invalidation_returns_superseded_request_to_transport() -> None:
    """上下文服务只应用提醒事实，由传输层决定移除队列或条件取消。"""

    coordinator = NoticeDeliveryCoordinator()
    service = build_assistant_request_context_preparer_for_test()
    service.notice_coordinator = coordinator
    service.handle_activity_event(
        "cognitive_task.progress",
        {
            "activity_id": "cognitive-a",
            "interaction_session_id": "session-a",
            "notice_seq": 1,
        },
        1,
    )
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    result = service.handle_activity_event(
        "cognitive_task.progress_invalidated",
        {
            "activity_id": "cognitive-a",
            "interaction_session_id": "session-a",
        },
        1,
    )

    assert result.superseded_request_ids == ("request-a",)


def test_livekit_removes_superseded_notice_before_bridge_submission() -> None:
    """尚在主进程队列的过期 system notice 应直接移除并恢复同批有效提醒。"""

    delivery_commands: list[NoticeDeliveryCommand] = []
    media_session = build_livekit_media_session_for_test()
    media_session.activity_notice_handler = lambda _event_type, _payload, _runtime_epoch: ActivityEventApplyResult(
        superseded_request_ids=("request-a",)
    )
    media_session.notice_delivery_handler = delivery_commands.append
    media_session.assistant_output_enabled = True
    bridge = RecordingAssistantBridge()
    media_session.assistant_process_bridge = bridge
    bridge.request_queue.put(
        AssistantRequest(
            request_id="request-a",
            source_session_id="session-a",
            interaction_session_id="interaction-a",
            character_card=cast(CharacterCardDefinition, None),
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            user_text="汇报进度",
            source="system_notice",
            source_turn_id=None,
            requested_at=1.0,
        )
    )

    media_session._handle_activity_event("cognitive_task.progress_invalidated", {}, 1)

    assert bridge.request_queue.empty()
    assert bridge.control_queue.empty()
    assert delivery_commands == [
        NoticeDeliveryCommand(
            request_id="request-a",
            outcome="superseded",
        )
    ]


def test_livekit_conditionally_cancels_superseded_notice_after_bridge_submission() -> None:
    """过期请求已离开主进程队列时，才向子进程提交按请求编号的条件取消。"""

    media_session = build_livekit_media_session_for_test()
    media_session.activity_notice_handler = lambda _event_type, _payload, _runtime_epoch: ActivityEventApplyResult(
        superseded_request_ids=("request-a",)
    )
    media_session.notice_delivery_handler = None
    media_session.assistant_output_enabled = True
    bridge = RecordingAssistantBridge()
    media_session.assistant_process_bridge = bridge

    media_session._handle_activity_event("cognitive_task.progress_invalidated", {}, 1)

    command = bridge.control_queue.get_nowait()
    assert command.kind == "cancel_superseded_system_notice"
    assert command.request_id == "request-a"
    assert command.reason == "activity_notice_superseded"


def test_bridge_submits_inflight_notice_before_queued_conditional_cancel() -> None:
    """请求已被 bridge 取走时，条件取消必须在该请求完成 IPC 提交后到达。"""

    runtime = OrderedBridgeRuntime()
    stable_runtime = SwappableAssistantProcessClient(cast(Any, runtime))
    audio_output = BlockingBridgeAudioOutput()
    request_queue: NotifyQueue[AssistantRequest] = NotifyQueue()
    control_queue: NotifyQueue[AssistantControlCommand] = NotifyQueue()
    request_queue.put(
        AssistantRequest(
            request_id="request-a",
            source_session_id="session-a",
            interaction_session_id="interaction-a",
            character_card=cast(CharacterCardDefinition, None),
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            user_text="汇报进度",
            source="system_notice",
            source_turn_id=None,
            requested_at=1.0,
        )
    )
    bridge = AssistantProcessBridge(
        assistant_runtime=stable_runtime,
        assistant_request_queue=request_queue,
        assistant_control_queue=control_queue,
        event_queue=NotifyQueue(),
        audio_output_scheduler=audio_output,
    )
    bridge.start()
    assert audio_output.request_dequeued.wait(timeout=1.0)

    media_session = build_livekit_media_session_for_test()
    media_session.activity_notice_handler = lambda _event_type, _payload, _runtime_epoch: ActivityEventApplyResult(
        superseded_request_ids=("request-a",)
    )
    media_session.notice_delivery_handler = None
    media_session.assistant_output_enabled = True
    media_session.assistant_process_bridge = bridge
    media_session._handle_activity_event("cognitive_task.progress_invalidated", {}, 1)

    audio_output.release_request.set()
    for _ in range(100):
        if len(runtime.submissions) == 2:
            break
        threading.Event().wait(0.01)
    bridge.stop(timeout=1.0)
    assert runtime.submissions == [
        ("request", "request-a"),
        ("control", "request-a"),
    ]


def test_active_media_session_output_failure_does_not_fall_back_to_direct_runtime() -> None:
    """活跃媒体会话的 Bridge 失败必须与“没有媒体会话”明确区分。"""

    controller = LiveKitMediaSessionController(
        media_session_factory=lambda: cast(Any, None),
    )
    controller._media_session = SimpleNamespace(
        enqueue_assistant_request=lambda _request: False
    )
    controller._started_room_name = "companion-local"

    with pytest.raises(AssistantOutputUnavailableError, match="输出链路不可用"):
        controller.enqueue_assistant_request(
            AssistantRequest(
                request_id="request-a",
                source_session_id="session-a",
                interaction_session_id="interaction-a",
                character_card=cast(CharacterCardDefinition, None),
                user_call_name="用户",
                user_self_intro="",
                user_extra_notes="",
                user_text="你好",
                source="text_input",
                source_turn_id=None,
                requested_at=1.0,
            )
        )


@pytest.mark.asyncio
async def test_clear_during_inflight_publish_does_not_emit_late_playout_started() -> None:
    """清空代际推进后，正在 await 的旧音频帧不得重新登记为活跃播放。"""

    publish_entered = asyncio.Event()
    release_publish = asyncio.Event()

    class BlockingPublisher:
        """把单帧发布暂停在可控竞态窗口内。"""

        async def publish_pcm16(self, *, pcm16: bytes, sample_rate: int) -> None:
            """等待测试释放当前发布调用。"""

            assert pcm16 == b"\x00\x00"
            assert sample_rate == 24_000
            publish_entered.set()
            await release_publish.wait()

        async def wait_for_playout(self) -> None:
            """本测试没有完成事件需要等待。"""

        def clear_queue(self) -> None:
            """模拟清空底层音频源。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._publisher = BlockingPublisher()  # type: ignore[attr-defined]
    scheduler._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    queued = QueuedAssistantAudioEvent(
        event=RuntimeEvent(
            type="assistant.audio_chunk",
            source="assistant_runtime",
            payload={
                "pcm16_b64": base64.b64encode(b"\x00\x00").decode("ascii"),
                "sample_rate": 24_000,
                "generation_id": "generation-a",
                "request_id": "request-a",
            },
        ),
        clear_generation=0,
    )

    publish_task = asyncio.create_task(scheduler._handle_audio_event(queued))  # type: ignore[attr-defined]
    await asyncio.wait_for(publish_entered.wait(), timeout=1.0)
    scheduler.clear_pending_audio()
    release_publish.set()
    await asyncio.wait_for(publish_task, timeout=1.0)
    await asyncio.sleep(0)

    cancelled = event_queue.get_nowait()
    assert cancelled.type == "assistant.audio_publishing_cancelled"
    assert cancelled.payload["request_id"] == "request-a"
    assert event_queue.empty()


@pytest.mark.asyncio
async def test_clear_between_generation_check_and_registration_does_not_emit_playout_started() -> None:
    """代际检查后发生清空时，播放身份登记必须再次受同一原子边界保护。"""

    class ImmediatePublisher:
        """立即完成单帧发布，便于精确注入检查后的清空竞态。"""

        async def publish_pcm16(self, *, pcm16: bytes, sample_rate: int) -> None:
            """确认音频载荷后立即返回。"""

            assert pcm16 == b"\x00\x00"
            assert sample_rate == 24_000

        async def wait_for_playout(self) -> None:
            """本测试没有完成事件需要等待。"""

        def clear_queue(self) -> None:
            """本测试直接推进代际，不调用底层清空。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._publisher = ImmediatePublisher()  # type: ignore[attr-defined]
    original_register = scheduler._emit_playout_started_if_needed  # type: ignore[attr-defined]

    def clear_before_registration(event: RuntimeEvent, **kwargs: object) -> None:
        """在生产代码完成代际检查后、登记播放身份前模拟清空。"""

        scheduler._advance_clear_generation()  # type: ignore[attr-defined]
        original_register(event, **kwargs)

    scheduler._emit_playout_started_if_needed = clear_before_registration  # type: ignore[method-assign]
    queued = QueuedAssistantAudioEvent(
        event=RuntimeEvent(
            type="assistant.audio_chunk",
            source="assistant_runtime",
            payload={
                "pcm16_b64": base64.b64encode(b"\x00\x00").decode("ascii"),
                "sample_rate": 24_000,
                "generation_id": "generation-a",
                "request_id": "request-a",
            },
        ),
        clear_generation=0,
    )

    await scheduler._handle_audio_event(queued)  # type: ignore[attr-defined]

    assert event_queue.empty()


@pytest.mark.asyncio
async def test_audio_finished_generation_check_and_playout_cleanup_are_atomic() -> None:
    """清空操作不得插入完成事件的代际校验与播放身份清理之间。"""

    class ImmediatePublisher:
        """立即完成播放等待，暴露完成事件内部的锁边界。"""

        async def publish_pcm16(self, *, pcm16: bytes, sample_rate: int) -> None:
            """本测试不发布音频。"""

        async def wait_for_playout(self) -> None:
            """立即结束底层播放等待。"""

        def clear_queue(self) -> None:
            """本测试不需要真实底层缓存。"""

    class CommitBarrierQueue(Queue[RuntimeEvent]):
        """在完成事件入队时等待清空线程开始争用状态锁。"""

        def __init__(self) -> None:
            super().__init__()
            self.finished_commit_entered = threading.Event()
            self.clear_attempted = threading.Event()

        def put(
            self,
            item: RuntimeEvent,
            block: bool = True,
            timeout: float | None = None,
        ) -> None:
            """确认清空线程已经开始后再允许完成事件提交结束。"""

            if item.type == "assistant.audio_finished":
                self.finished_commit_entered.set()
                assert self.clear_attempted.wait(timeout=1.0)
            super().put(item, block=block, timeout=timeout)

    event_queue = CommitBarrierQueue()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._publisher = ImmediatePublisher()  # type: ignore[attr-defined]
    scheduler._active_playout = ActiveAssistantPlayout(  # type: ignore[attr-defined]
        generation_id="generation-a",
        request_id="request-a",
        delivery_id=None,
    )

    def clear_during_finished_commit() -> None:
        """在完成事件准备入队时尝试推进清空代际。"""

        assert event_queue.finished_commit_entered.wait(timeout=1.0)
        event_queue.clear_attempted.set()
        scheduler._advance_clear_generation()  # type: ignore[attr-defined]
        scheduler._emit_playout_cancelled_if_needed()  # type: ignore[attr-defined]

    clear_thread = threading.Thread(target=clear_during_finished_commit)
    clear_thread.start()
    await scheduler._handle_audio_event(  # type: ignore[attr-defined]
        QueuedAssistantAudioEvent(
            event=RuntimeEvent(
                type="assistant.audio_finished",
                source="assistant_runtime",
                payload={"generation_id": "generation-a", "request_id": "request-a"},
            ),
            clear_generation=0,
        )
    )
    clear_thread.join(timeout=1.0)

    assert clear_thread.is_alive() is False
    assert [event_queue.get_nowait().type] == ["assistant.audio_finished"]
