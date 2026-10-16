from __future__ import annotations

from queue import Queue
from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.activities.notice.delivery_coordinator import NoticeDeliveryCoordinator
from companion.activities.notice.delivery_event_mapper import map_notice_delivery_event
from companion.activities.notice.models import (
    ActivityLifecycleEvent,
    NoticeDeliveryCommand,
)
from companion.character_cards.models import CharacterCardDefinition
from companion.llm.foreground_models import ForegroundEvent
from companion.foreground_self_reflection.models import InputDiversionResult
from companion.realtime.assistant_audio_output import (
    AssistantAudioOutputScheduler,
)
from companion.realtime.assistant_process_bridge import AssistantProcessBridge
from companion.realtime.assistant_events import AssistantRequest, RuntimeEvent
from companion.runtime_memory.models import RuntimeMemorySnapshot
from companion.realtime.assistant_generation import (
    AssistantRequestWorkItem,
    TtsSegmentWorkItem,
    TtsGenerationFinished,
)
from companion.realtime.assistant_output_lanes import AssistantOutputLanes
from companion.realtime.assistant_process_handle import SwappableAssistantProcessClient
from companion.realtime.notify_queue import NotifyQueue
from backend.tests.runtime_public_event_support import runtime_event_router

from backend.tests.activities.notice_delivery_test_support import (
    FailingBridgeRuntime,
    FailingRequestQueue,
    ImmediateBridgeAudioOutput,
    RecordingAssistantBridge,
    build_livekit_media_session_for_test,
    build_realtime_input_worker_for_test,
)


def test_protocol_failure_final_is_queued_before_tts_finish_signal() -> None:
    """协议失败 final 必须先入事件队列，避免 audio_finished 抢先清理重试状态。"""

    class ProtocolFailureAgent:
        """只产生协议失败终态的前台代理。"""

        def stream_user_text(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield ForegroundEvent(
                type="assistant.text_final",
                payload={
                    "text": "",
                    "notice_delivery_failed": True,
                    "notice_failure_reason": "provider_protocol_leak",
                },
            )

    class OrderingTtsQueue:
        """记录收到 TTS 收尾信号时 final 是否已经入队。"""

        def __init__(self, event_queue: Queue[RuntimeEvent]) -> None:
            self._event_queue = event_queue
            self.final_was_queued = False

        def put(self, item: object) -> None:
            """记录 TTS 收尾信号与 final 的相对顺序。

            Args:
                item: LLM lane 投递的 TTS 工作项。
            """

            if isinstance(item, TtsGenerationFinished):
                self.final_was_queued = not self._event_queue.empty()

    runner = AssistantOutputLanes(
        agent=ProtocolFailureAgent(),  # type: ignore[arg-type]
        tts_provider=None,  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-a",
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="汇报提醒",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)
    ordering_queue = OrderingTtsQueue(runner.event_queue)
    runner._tts_queue = cast(Any, ordering_queue)  # type: ignore[attr-defined]

    runner._run_llm_generation(work_item.context)  # type: ignore[attr-defined]

    assert ordering_queue.final_was_queued is True
    assert runner.event_queue.get_nowait().type == "assistant.text_final"


def test_direct_output_bypasses_foreground_llm_and_keeps_text_tts_settlement() -> None:
    """确定性权限短句不调用模型，但仍走正常可见文本、TTS 与结算协议。"""

    class DirectOutputAgent:
        """拒绝模型和错误兜底调用，只提供直接正文最终事件。"""

        def __init__(self) -> None:
            """初始化模型调用计数。"""

            self.stream_call_count = 0

        def stream_user_text(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
            """记录并拒绝本测试不应发生的模型调用。"""

            self.stream_call_count += 1
            raise AssertionError("确定性正文不应调用 Foreground LLM")

        def create_direct_output_event(
            self,
            text: str,
            **_kwargs: object,
        ) -> ForegroundEvent:
            """构造无需模型参与的最终正文事件。

            Args:
                text: 已确定的权限确认短句。
                _kwargs: 输出 Lane 透传的历史策略字段。

            Returns:
                可由 Supervisor 唯一提交的最终正文事件。
            """

            return ForegroundEvent(
                type="assistant.text_final",
                payload={"text": text},
            )

        def create_runtime_fallback_event(
            self,
            _text: str,
            **_kwargs: object,
        ) -> ForegroundEvent:
            """拒绝把正常权限确认伪装成运行时错误兜底。"""

            raise AssertionError("权限确认不应使用 runtime fallback 事件")

    agent = DirectOutputAgent()
    runner = AssistantOutputLanes(
        agent=agent,  # type: ignore[arg-type]
        tts_provider=SimpleNamespace(config=SimpleNamespace(model="")),  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    acknowledgement = "收到，这次操作已经允许。"
    request = AssistantRequest(
        request_id="permission-ack-a",
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
        direct_output_text=acknowledgement,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)

    runner._run_llm_generation(work_item.context)  # type: ignore[attr-defined]

    text_events = [runner.event_queue.get_nowait(), runner.event_queue.get_nowait()]
    tts_segment = runner._tts_queue.get_nowait()  # type: ignore[attr-defined]
    tts_finished = runner._tts_queue.get_nowait()  # type: ignore[attr-defined]
    assert agent.stream_call_count == 0
    assert [event.type for event in text_events] == [
        "assistant.text_chunk",
        "assistant.text_final",
    ]
    assert [event.payload["text"] for event in text_events] == [
        acknowledgement,
        acknowledgement,
    ]
    assert isinstance(tts_segment, TtsSegmentWorkItem)
    assert tts_segment.text == acknowledgement
    assert isinstance(tts_finished, TtsGenerationFinished)

    runner._finish_tts_generation(tts_finished.context)  # type: ignore[attr-defined]

    terminal_events = [
        runner.event_queue.get_nowait(),
        runner.event_queue.get_nowait(),
    ]
    assert [event.type for event in terminal_events] == [
        "assistant.audio_finished",
        "assistant.output_settled",
    ]


@pytest.mark.asyncio
async def test_empty_failed_notice_keeps_delivery_closure() -> None:
    """过期空提醒仍须交给提醒协调器完成取消闭环。"""

    delivery_commands: list[NoticeDeliveryCommand] = []
    broadcasts: list[dict[str, object]] = []

    async def record_broadcast(event: dict[str, object]) -> None:
        """记录仍需传播的原始控制事件。"""

        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=record_broadcast,
        publish_ephemeral=record_broadcast,
        realtime_input_runtime=cast(Any, object()),
        notice_delivery_handler=delivery_commands.append,
    )
    payload: dict[str, object] = {
        "request_id": "request-a",
        "session_id": "session-a",
        "generation_id": "generation-a",
        "text": "  ",
        "notice_delivery_failed": True,
    }

    await event_router.dispatch(
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            payload=payload,
        )
    )

    assert delivery_commands == [
        NoticeDeliveryCommand(
            request_id="request-a",
            outcome="delivery_cancelled",
        )
    ]
    assert broadcasts == [
        {
            "type": "assistant.text_final",
            "payload": {
                "request_id": "request-a",
                "session_id": "session-a",
                "generation_id": "generation-a",
                "text": "",
                "notice_delivery_failed": True,
            },
        }
    ]


def test_playout_cancellation_keeps_request_identity() -> None:
    """清空真实播放队列时必须把原请求编号带回控制面。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._emit_playout_started_if_needed(
        RuntimeEvent(
            type="assistant.audio_chunk",
            source="assistant_runtime",
            payload={"generation_id": "generation-a", "request_id": "request-a"},
        )
    )
    event_queue.get_nowait()

    scheduler._emit_playout_cancelled_if_needed()

    cancelled = event_queue.get_nowait()
    assert cancelled.type == "assistant.audio_publishing_cancelled"
    assert cancelled.payload == {
        "generation_id": "generation-a",
        "request_id": "request-a",
    }


def test_playout_cancellation_before_normal_final_cannot_requeue_notice() -> None:
    """播放取消先到也不能抢在正常 final 前改变 Notice 生命周期。"""

    coordinator = NoticeDeliveryCoordinator()
    coordinator.apply_event(
        ActivityLifecycleEvent(
            lifecycle="terminal",
            activity_id="task_result-a",
            interaction_session_id="session-a",
            source="task_result",
            category="内容生成",
            goal="生成一份结果",
            terminal_state="completed",
        ),
        runtime_epoch=1,
    )
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    coordinator.apply_delivery(
        NoticeDeliveryCommand(request_id="request-a", outcome="request_enqueued")
    )

    cancel_command = map_notice_delivery_event(
        "assistant.audio_publishing_cancelled",
        {"request_id": "request-a", "generation_id": "generation-a"},
    )
    assert cancel_command is None

    completed_command = map_notice_delivery_event(
        "assistant.text_final",
        {"request_id": "request-a", "text": "结果已经完成。"},
    )
    assert completed_command is not None
    coordinator.apply_delivery(completed_command)

    assert coordinator.has_active_system_notice("session-a") is False
    assert coordinator.has_pending_notice("session-a") is False


def test_livekit_queue_put_failure_emits_enqueue_rollback_event() -> None:
    """queue.put 失败时必须通知 coordinator 回收尚未入队的提醒批次。"""

    delivery_commands: list[NoticeDeliveryCommand] = []
    media_session = build_livekit_media_session_for_test()
    media_session.assistant_output_enabled = True
    media_session.notice_delivery_handler = delivery_commands.append
    media_session.assistant_process_bridge = RecordingAssistantBridge(
        request_queue=FailingRequestQueue(),
        request_committed_handler=media_session._handle_assistant_request_committed,
    )
    request = AssistantRequest(
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

    with pytest.raises(RuntimeError, match="queue unavailable"):
        media_session.enqueue_assistant_request(request)

    assert delivery_commands == [
        NoticeDeliveryCommand(
            request_id="request-a",
            outcome="request_enqueue_failed",
        )
    ]


def test_livekit_post_enqueue_handler_failure_keeps_request_committed() -> None:
    """请求已入队后，状态通知异常只能告警，不能让调用方误判入队失败。"""

    request_queue: NotifyQueue[AssistantRequest] = NotifyQueue()
    media_session = build_livekit_media_session_for_test()
    media_session.assistant_output_enabled = True
    media_session.turn_admission_sequencer = None
    media_session.assistant_request_context_preparer = SimpleNamespace(
        runtime_memory_store=SimpleNamespace(commit_request=lambda _request: None)
    )
    media_session.realtime_input_runtime = SimpleNamespace(
        interaction_state=SimpleNamespace(
            register_assistant_request=lambda _now, *, request_id: None
        )
    )
    media_session.notice_delivery_handler = lambda _command: (_ for _ in ()).throw(
        RuntimeError("notice handler failed")
    )
    media_session.assistant_process_bridge = RecordingAssistantBridge(
        request_queue=request_queue,
        request_committed_handler=media_session._handle_assistant_request_committed,
    )
    request = AssistantRequest(
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

    assert media_session.enqueue_assistant_request(request) is True
    assert request_queue.get_nowait() is request


def test_bridge_runtime_submit_failure_emits_clean_cancellation_closure() -> None:
    """请求离开主队列却未进入 runtime 时必须回写可交付的取消闭环。"""

    request_queue: NotifyQueue[AssistantRequest] = NotifyQueue()
    event_queue: NotifyQueue[RuntimeEvent] = NotifyQueue()
    request_queue.put(
        AssistantRequest(
            request_id="request-a",
            source_session_id="session-a",
            interaction_session_id="interaction-a",
            character_card=cast(CharacterCardDefinition, None),
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            user_text="汇报结果",
            source="text_input",
            source_turn_id=None,
            requested_at=1.0,
            turn_material_context_id="context-a",
        )
    )
    bridge = AssistantProcessBridge(
        assistant_runtime=SwappableAssistantProcessClient(
            cast(Any, FailingBridgeRuntime())
        ),
        assistant_request_queue=request_queue,
        assistant_control_queue=NotifyQueue(),
        event_queue=event_queue,
        audio_output_scheduler=ImmediateBridgeAudioOutput(),
    )

    assert bridge.bridge_assistant_requests_once() is True
    closure = event_queue.get_nowait()
    assert closure.type == "assistant.generation_interrupted"
    assert closure.payload == {
        "request_id": "request-a",
        "source": "text_input",
        "source_turn_id": None,
        "turn_material_context_id": "context-a",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
        "derived_from_activity_ids": [],
        "reason": "runtime_submit_failed",
    }
    settlement = event_queue.get_nowait()
    assert settlement.type == "assistant.output_settled"
    assert settlement.payload["outcome"] == "failed"


def test_final_asr_enqueue_failure_queues_material_cleanup_event() -> None:
    """final ASR 入队失败必须携带 context id 回到主分发线程清理材料。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    worker = build_realtime_input_worker_for_test()
    worker.event_queue = event_queue
    worker.now = lambda: 2.0

    worker._queue_final_asr_material_cleanup_event(
        request_id="request-a",
        source="final_asr",
        source_turn_id="turn-a",
        turn_material_context_id="context-a",
    )

    expected_payload = {
        "request_id": "request-a",
        "source": "final_asr",
        "source_turn_id": "turn-a",
        "turn_material_context_id": "context-a",
    }
    cleanup = event_queue.get_nowait()
    assert cleanup.type == "assistant.request_enqueue_failed"
    assert cleanup.payload == expected_payload
    assert cleanup.source == "realtime_input"


def test_final_asr_queue_failure_preserves_frozen_material_identity() -> None:
    """请求队列失败时，cleanup 事件必须使用本次 profile 冻结出的 context id。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    worker = build_realtime_input_worker_for_test()
    worker.now = lambda: 3.0
    worker.assistant_output_enabled = True
    worker.voice_turn_resolved_handler = None
    worker.assistant_request_submission_failed_handler = None
    worker.realtime_input_runtime = SimpleNamespace(
        current_audio_session_id="session-a",
        interaction_state=SimpleNamespace(interaction_session_id="interaction-a"),
        apply_final_transcript=lambda _text, **_kwargs: [],
    )
    worker.active_input_track_generation = 1
    worker.debug_audio_recorder = SimpleNamespace(
        record_final_asr_result=lambda *_args, **_kwargs: None,
    )
    worker._confirm_barge_in_if_actionable_decision = lambda **_kwargs: None
    worker._enqueue_runtime_events = lambda _events, **_kwargs: None
    snapshot = SimpleNamespace(
        character_card=None,
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        turn_material_context_id="context-frozen-a",
        foreground_context={},
        environment_snapshot=None,
        frozen_visual_input=None,
        runtime_memory_snapshot=RuntimeMemorySnapshot(),
        history_policy="stable_dialogue",
        activity_progress_references=(),
        derived_from_activity_ids=(),
    )
    worker.assistant_request_context_preparer = SimpleNamespace(
        prepare_final_asr_request_context=lambda **_kwargs: SimpleNamespace(
            snapshot=snapshot,
            commit_enqueued=lambda: True,
            rollback=lambda: True,
        )
    )
    worker.assistant_request_submitter = FailingRequestQueue().put
    worker.event_queue = event_queue

    with pytest.raises(RuntimeError, match="queue unavailable"):
        worker._apply_final_transcript_text(
            final_text="请总结材料",
            source_turn_id="turn-a",
            byte_count=100,
            input_track_generation=1,
            submitted_at=1.0,
        )

    assert event_queue.get_nowait().payload["turn_material_context_id"] == "context-frozen-a"


def test_final_asr_commit_failure_does_not_undo_submitted_request() -> None:
    """Final ASR 已入队后，lease 异常不得回滚或上抛。"""

    submitted_requests: list[AssistantRequest] = []
    attention_commits: list[str] = []

    class AttentionLease:
        """记录 Final ASR 是否越过 Attention 提交边界。"""

        def commit_enqueued(self) -> bool:
            """记录 Bridge 已接管请求。"""

            attention_commits.append("committed")
            return True

        def rollback(self) -> bool:
            """已入队请求不得回滚 Attention 资格。"""

            pytest.fail("已入队请求不得回滚 Attention lease")

    def fail_commit() -> bool:
        """模拟 Bridge 接管请求后 Notice 账本异常。"""

        raise RuntimeError("notice commit failed")

    def submit_request(request: AssistantRequest) -> bool:
        """记录 Bridge 已接收的 Final ASR 请求。"""

        submitted_requests.append(request)
        return True

    worker = build_realtime_input_worker_for_test()
    worker.now = lambda: 3.0
    worker.assistant_output_enabled = True
    worker.voice_turn_resolved_handler = None
    worker.assistant_request_submission_failed_handler = None
    worker.input_diversion_port = SimpleNamespace(
        divert_final_input=lambda _command: InputDiversionResult(
            status="not_diverted",
            reason="assistant_request_reserved",
            assistant_request_lease=AttentionLease(),
        )
    )
    worker.current_runtime_epoch = lambda: 7
    worker.realtime_input_runtime = SimpleNamespace(
        current_audio_session_id="session-a",
        interaction_state=SimpleNamespace(interaction_session_id="interaction-a"),
        apply_final_transcript=lambda _text, **_kwargs: [],
    )
    worker.active_input_track_generation = 1
    worker.debug_audio_recorder = SimpleNamespace(
        record_final_asr_result=lambda *_args, **_kwargs: None,
        record_assistant_request_enqueued=lambda _request: None,
    )
    worker._confirm_barge_in_if_actionable_decision = lambda **_kwargs: None
    worker._enqueue_runtime_events = lambda _events, **_kwargs: None
    worker._reset_turn_buffers = lambda: None
    worker._promote_pending_next_turn_if_needed = lambda **_kwargs: None
    worker.latency_recorder = None
    snapshot = SimpleNamespace(
        character_card=None,
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        turn_material_context_id="context-frozen-a",
        foreground_context={},
        environment_snapshot=None,
        frozen_visual_input=None,
        runtime_memory_snapshot=RuntimeMemorySnapshot(),
        history_policy="stable_dialogue",
        activity_progress_references=(),
        derived_from_activity_ids=(),
    )
    worker.assistant_request_context_preparer = SimpleNamespace(
        prepare_final_asr_request_context=lambda **_kwargs: SimpleNamespace(
            snapshot=snapshot,
            commit_enqueued=fail_commit,
            rollback=lambda: pytest.fail("已入队请求不得回滚"),
        )
    )
    worker.assistant_request_submitter = submit_request

    worker._apply_final_transcript_text(
        final_text="请总结材料",
        source_turn_id="turn-a",
        byte_count=100,
        input_track_generation=1,
        submitted_at=1.0,
    )

    assert len(submitted_requests) == 1
    assert attention_commits == ["committed"]
