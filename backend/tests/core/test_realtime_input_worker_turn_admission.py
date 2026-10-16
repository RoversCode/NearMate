from __future__ import annotations

from queue import Queue
from types import SimpleNamespace
from typing import Any, cast

from companion.config.realtime_audio import load_realtime_audio_config
from companion.core.application.assistant_request_models import (
    AssistantRequestContextSnapshot,
)
from companion.realtime.asr_scheduler import AsrErrorResult
from companion.realtime.notify_queue import NotifyQueue
from companion.realtime.realtime_input_worker import (
    AudioControlCommand,
    AudioInputItem,
    EndpointEouPendingState,
    RealtimeInputWorker,
    SmartTurnEndState,
    TimedReminderInputTakeover,
)
from companion.foreground_self_reflection.models import (
    InputDiversionResult,
    UnattendedMaterialReference,
)


class RecordingAsrScheduler:
    """记录输入 reset 是否清掉 ASR 调度状态。"""

    def __init__(self) -> None:
        """初始化尚未 reset 的测试调度器。"""

        self.reset_count = 0

    def reset(self) -> None:
        """记录一次 partial/final/endpoint 结果清理。"""

        self.reset_count += 1


def build_takeover_worker() -> tuple[Any, Any, RecordingAsrScheduler]:
    """构造字段完整、可验证 Reminder takeover 的真实输入 Worker。

    Returns:
        输入 Worker、运行时替身和 ASR 调度器。
    """

    reset_reasons: list[str] = []
    resolved_turns: list[str] = []
    vad_provider = SimpleNamespace(reset=lambda: reset_reasons.append("vad_reset"))
    input_state = SimpleNamespace(active=True)
    input_turn_session = SimpleNamespace(current_turn_id="voice-a")

    def apply_input_reset(reason: str, *, now: float) -> list[dict[str, object]]:
        """模拟 Runtime 在同一输入 owner 内结束当前语音轮。

        Args:
            reason: 本次输入清理原因。
            now: 输入 owner 观察到的事实时间。

        Returns:
            测试不需要广播额外事件，固定返回空列表。
        """

        _ = now
        reset_reasons.append(reason)
        vad_provider.reset()
        input_state.active = False
        input_turn_session.current_turn_id = None
        return []

    runtime = SimpleNamespace(
        input_turn_session=input_turn_session,
        interaction_state=SimpleNamespace(input_state=input_state),
        apply_input_reset=apply_input_reset,
        reset_wakeword_provider=lambda: reset_reasons.append("wakeword_reset"),
    )
    asr_scheduler = RecordingAsrScheduler()
    worker = RealtimeInputWorker(
        realtime_input_runtime=runtime,
        vad_provider=vad_provider,
        smart_turn_provider=object(),
        realtime_audio_config=load_realtime_audio_config(),
        audio_queue=NotifyQueue(),
        control_queue=NotifyQueue(),
        event_queue=Queue(),
        assistant_request_submitter=None,
        assistant_control_submitter=None,
        assistant_output_enabled=False,
        asr_scheduler=cast(Any, asr_scheduler),
        now=lambda: 2.0,
        voice_turn_resolved_handler=(
            lambda turn_id, _request_id: resolved_turns.append(turn_id)
        ),
    )
    return worker, runtime, asr_scheduler


def test_timed_reminder_takeover_atomically_clears_all_input_state() -> None:
    """一次 takeover 必须清空音频、VAD、ASR、Smart Turn 和未完成 Voice Turn。"""

    worker, runtime, asr_scheduler = build_takeover_worker()
    worker.audio_queue.put_nowait(
        AudioInputItem(
            pcm16=b"queued-audio",
            received_at=1.0,
            input_frame_sequence_no=1,
            input_track_generation=0,
            sample_rate=16000,
            num_channels=1,
            duration_ms=32.0,
        )
    )
    worker._vad_buffer = b"vad"
    worker._speech_start_candidate_pcm16 = b"candidate"
    worker._speech_start_candidate_window_count = 2
    worker._current_turn_pcm16 = b"current"
    worker._current_turn_has_speech = True
    worker._sv_consecutive_low_count = 2
    worker._last_partial_submitted_byte_count = 7
    worker._last_partial_applied_byte_count = 6
    worker._last_partial_at_ms = 1.0
    worker._smart_turn_end_state = SmartTurnEndState(
        kind="waiting_user_continue",
        probability=0.4,
    )
    worker._endpoint_eou_pending_state = EndpointEouPendingState(
        turn_id="voice-a",
        input_track_generation=0,
        byte_count=7,
        submitted_at=1.0,
        smart_turn_probability=0.4,
    )
    worker._finalizing_turn_id = "voice-a"
    worker._finalizing_byte_count = 7
    worker._pending_next_turn_pcm16 = b"next"
    worker._pending_next_turn_has_speech = True

    takeover = TimedReminderInputTakeover()
    worker.control_queue.put_nowait(
        AudioControlCommand(
            kind="timed_reminder_takeover",
            payload={"takeover": takeover},
        )
    )
    worker._drain_control_queue()

    assert takeover.wait(timeout=0) is True
    assert takeover.interrupted is True
    assert worker.audio_queue.empty()
    assert worker._vad_buffer == b""
    assert worker._speech_start_candidate_pcm16 == b""
    assert worker._speech_start_candidate_window_count == 0
    assert worker._current_turn_pcm16 == b""
    assert worker._current_turn_has_speech is False
    assert worker._sv_consecutive_low_count == 0
    assert worker._last_partial_submitted_byte_count == 0
    assert worker._last_partial_applied_byte_count == 0
    assert worker._last_partial_at_ms is None
    assert worker._smart_turn_end_state is None
    assert worker._endpoint_eou_pending_state is None
    assert worker._finalizing_turn_id is None
    assert worker._pending_next_turn_pcm16 == b""
    assert worker._pending_next_turn_has_speech is False
    assert runtime.interaction_state.input_state.active is False
    assert runtime.input_turn_session.current_turn_id is None
    assert asr_scheduler.reset_count == 1


def test_cancelled_timed_reminder_takeover_never_clears_late_input() -> None:
    """调用方超时取消后，迟到的 worker 命令不得再清空用户语音。"""

    worker, runtime, asr_scheduler = build_takeover_worker()
    worker._current_turn_pcm16 = b"keep-this-input"
    worker._current_turn_has_speech = True
    takeover = TimedReminderInputTakeover()
    worker.control_queue.put_nowait(
        AudioControlCommand(
            kind="timed_reminder_takeover",
            payload={"takeover": takeover},
        )
    )

    assert takeover.cancel_if_pending() is True
    worker._drain_control_queue()

    assert takeover.interrupted is False
    assert worker._current_turn_pcm16 == b"keep-this-input"
    assert worker._current_turn_has_speech is True
    assert runtime.interaction_state.input_state.active is True
    assert asr_scheduler.reset_count == 0


def test_failed_timed_reminder_takeover_reports_unsuccessful_completion() -> None:
    """输入 reset 异常必须阻止媒体侧把命令当作成功 takeover。"""

    takeover = TimedReminderInputTakeover()

    executed = takeover.execute(
        lambda: (_ for _ in ()).throw(RuntimeError("reset failed"))
    )

    assert executed is True
    assert takeover.wait(timeout=0) is True
    assert takeover.completed_successfully is False
    assert takeover.interrupted is False


def test_final_asr_request_registers_materials_only_after_bridge_accepts() -> None:
    """普通 Final ASR 只有成功进入 Bridge 后才提交近期材料上下文。"""

    committed_context_ids: list[str] = []

    def build_worker(*, bridge_accepts: bool) -> Any:
        """构造只覆盖 Final ASR 请求提交边界的最小 Worker。"""

        prepared = SimpleNamespace(
            snapshot=AssistantRequestContextSnapshot(
                character_card=None,
                user_call_name="用户",
                user_self_intro="",
                user_extra_notes="",
                turn_material_context_id="context-voice-a",
                environment_snapshot=None,
                foreground_context={},
            ),
            commit_enqueued=lambda: True,
            rollback=lambda: True,
        )
        worker = cast(Any, object.__new__(RealtimeInputWorker))
        worker.assistant_request_context_preparer = SimpleNamespace(
            prepare_final_asr_request_context=lambda **_kwargs: prepared,
        )
        worker.assistant_request_submitter = lambda _request: bridge_accepts
        worker.final_input_material_commit_handler = committed_context_ids.append
        worker._queue_final_asr_material_cleanup_event = lambda **_kwargs: None
        worker.debug_audio_recorder = SimpleNamespace(
            record_assistant_request_enqueued=lambda _request: None,
        )
        worker.latency_recorder = None
        worker.now = lambda: 2.0
        return worker

    accepted = RealtimeInputWorker._dispatch_final_asr_request(
        build_worker(bridge_accepts=True),
        request_id="final-asr-a",
        source_turn_id="voice-a",
        final_text="处理这个文件",
        requested_at=1.0,
        source_session_id="audio-a",
        interaction_session_id="interaction-a",
    )

    assert accepted is True
    assert committed_context_ids == ["context-voice-a"]

    committed_context_ids.clear()
    rejected = RealtimeInputWorker._dispatch_final_asr_request(
        build_worker(bridge_accepts=False),
        request_id="final-asr-b",
        source_turn_id="voice-b",
        final_text="处理这个文件",
        requested_at=1.0,
        source_session_id="audio-a",
        interaction_session_id="interaction-a",
    )

    assert rejected is False
    assert committed_context_ids == []


def test_input_reset_releases_confirmed_voice_hold() -> None:
    """任何结束当前语音轮次的 reset 都必须解除 VAD 建立的文本派发 hold。"""

    resolved_turns: list[tuple[str, str | None]] = []
    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker.now = lambda: 2.0
    worker.realtime_input_runtime = SimpleNamespace(
        input_turn_session=SimpleNamespace(current_turn_id="voice-a"),
        apply_input_reset=lambda _reason, now: [],
        reset_wakeword_provider=lambda: None,
    )
    worker.voice_turn_resolved_handler = (
        lambda turn_id, request_id: resolved_turns.append((turn_id, request_id))
    )
    worker._clear_smart_turn_end_state = lambda **_kwargs: None
    worker._enqueue_runtime_events = lambda *_args, **_kwargs: None
    worker._reset_speech_start_candidate = lambda: None
    worker._reset_turn_buffers = lambda: None
    worker._reset_pending_next_turn_buffers = lambda: None
    worker.debug_audio_recorder = SimpleNamespace(reset=lambda _reason: None)
    worker.asr_scheduler = SimpleNamespace(reset=lambda: None)
    worker._vad_buffer = b"voice"

    RealtimeInputWorker._reset_input_buffers(worker, reason="short_turn_ignored")

    assert resolved_turns == [("voice-a", None)]


def test_final_asr_error_resets_matching_voice_turn() -> None:
    """当前 Final ASR 失败时必须结束输入轮次，不能留下永久 voice hold。"""

    reset_reasons: list[str] = []
    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker.asr_scheduler = SimpleNamespace(
        drain_errors=lambda: [
            AsrErrorResult(
                kind="final",
                turn_id="voice-a",
                input_track_generation=3,
                byte_count=6400,
                submitted_at=1.0,
                message="provider unavailable",
            )
        ]
    )
    worker.event_queue = Queue()
    worker.now = lambda: 2.0
    worker.active_input_track_generation = 3
    worker._finalizing_turn_id = "voice-a"
    worker._finalizing_byte_count = 6400
    worker._endpoint_eou_pending_state = None
    worker._reset_input_buffers = lambda *, reason: reset_reasons.append(reason)

    RealtimeInputWorker._drain_asr_error_results(worker)

    assert reset_reasons == ["final_asr_failed"]
    assert worker.event_queue.get_nowait().type == "runtime.internal_error"


def test_stale_final_asr_error_does_not_reset_new_voice_turn() -> None:
    """旧音轨迟到的 Final ASR 错误只能留诊断，不能清掉新轮次。"""

    reset_reasons: list[str] = []
    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker.asr_scheduler = SimpleNamespace(
        drain_errors=lambda: [
            AsrErrorResult(
                kind="final",
                turn_id="voice-old",
                input_track_generation=2,
                byte_count=3200,
                submitted_at=1.0,
                message="late error",
            )
        ]
    )
    worker.event_queue = Queue()
    worker.now = lambda: 2.0
    worker.active_input_track_generation = 3
    worker._finalizing_turn_id = "voice-new"
    worker._finalizing_byte_count = 6400
    worker._endpoint_eou_pending_state = None
    worker._reset_input_buffers = lambda *, reason: reset_reasons.append(reason)

    RealtimeInputWorker._drain_asr_error_results(worker)

    assert reset_reasons == []


def test_final_asr_diversion_happens_after_public_event_before_request_prepare() -> None:
    """Attention active 时 Final ASR 不创建普通 AssistantRequest，材料恰好释放一次。"""

    resolved: list[tuple[str, str | None]] = []
    diverted_commands: list[object] = []
    released_turns: list[tuple[str, bool]] = []
    public_events: list[object] = []

    class Diversion:
        def divert_final_input(self, command: object) -> InputDiversionResult:
            diverted_commands.append(command)
            return InputDiversionResult(
                status="accepted",
                reason="input_diverted_to_episode",
            )

    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker.now = lambda: 3.0
    worker.assistant_output_enabled = True
    worker.input_diversion_port = Diversion()
    worker.current_runtime_epoch = lambda: 9
    worker.final_input_materials_provider = lambda _turn_id: (
        UnattendedMaterialReference(file_name="a.wav", media_type="audio/wav"),
    )
    worker.diverted_material_release_handler = (
        lambda turn_id, remember: released_turns.append((turn_id, remember))
    )
    worker.realtime_input_runtime = SimpleNamespace(
        current_audio_session_id="audio-session",
        interaction_state=SimpleNamespace(interaction_session_id="interaction-a"),
        apply_final_transcript=lambda text, **kwargs: [
            {"type": "user.final_transcript", "payload": {"text": text}}
        ],
    )
    worker.debug_audio_recorder = SimpleNamespace(
        record_final_asr_result=lambda *_args, **_kwargs: None,
    )
    worker.runtime_identity_factory = SimpleNamespace(new=lambda prefix: f"{prefix}-request")
    worker._confirm_barge_in_if_actionable_decision = lambda **_kwargs: None
    worker._enqueue_runtime_events = lambda events, **_kwargs: public_events.extend(events)
    worker._reset_turn_buffers = lambda: None
    worker._promote_pending_next_turn_if_needed = lambda **_kwargs: None
    worker.voice_turn_resolved_handler = (
        lambda turn_id, request_id: resolved.append((turn_id, request_id))
    )
    worker.assistant_request_context_preparer = SimpleNamespace(
        prepare_final_asr_request_context=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("diverted Final ASR must not prepare context")
        )
    )
    worker.assistant_request_submitter = lambda _request: (_ for _ in ()).throw(
        AssertionError("diverted Final ASR must not submit a request")
    )

    RealtimeInputWorker._apply_final_transcript_text(
        worker,
        final_text="改道语音",
        source_turn_id="voice-turn-a",
        byte_count=6400,
        input_track_generation=1,
        submitted_at=2.0,
    )

    assert public_events[0]["type"] == "user.final_transcript"
    assert len(diverted_commands) == 1
    command = diverted_commands[0]
    assert command.runtime_epoch == 9
    assert command.input_identity == "voice-turn-a"
    assert command.source == "final_asr"
    assert command.materials == (
        UnattendedMaterialReference(file_name="a.wav", media_type="audio/wav"),
    )
    assert released_turns == [("voice-turn-a", True)]
    assert resolved == [("voice-turn-a", None)]


def test_non_diverted_final_asr_discards_only_temporary_material_index() -> None:
    """普通 Final ASR 清理安全投影索引，但不调用改道材料释放端口。"""

    discarded: list[str] = []
    released: list[str] = []

    class Diversion:
        def divert_final_input(self, _command: object) -> InputDiversionResult:
            return InputDiversionResult(
                status="not_diverted",
                reason="attention_not_accepting_inputs",
            )

    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker.input_diversion_port = Diversion()
    worker.current_runtime_epoch = lambda: 9
    worker.realtime_input_runtime = SimpleNamespace(input_runtime_epoch=9)
    worker.final_input_materials_provider = lambda _turn_id: (
        UnattendedMaterialReference(file_name="a.wav", media_type="audio/wav"),
    )
    worker.final_input_materials_discard_handler = discarded.append
    worker.diverted_material_release_handler = (
        lambda turn_id, _remember: released.append(turn_id)
    )

    result = worker._divert_final_input(
        input_identity="voice-turn-normal",
        assistant_request_id="final-asr-request-normal",
        text="普通语音",
    )
    assert result is not None
    assert result.status == "not_diverted"
    assert result.reason == "attention_not_accepting_inputs"
    assert discarded == ["voice-turn-normal"]
    assert released == []


def test_final_asr_diversion_failure_does_not_fall_through_to_normal_request() -> None:
    """改道端口异常时，Final ASR 不能继续创建普通 AssistantRequest。"""

    resolved: list[tuple[str, str | None]] = []
    submitted: list[str] = []
    settled: list[str] = []
    released: list[tuple[str, bool]] = []
    public_events: list[object] = []

    class FailingDiversion:
        """模拟 Attention 主进程暂时不可用。"""

        def divert_final_input(self, _command: object) -> InputDiversionResult:
            """让输入归属裁决抛出基础设施异常。"""

            raise RuntimeError("attention unavailable")

    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker.now = lambda: 3.0
    worker.assistant_output_enabled = True
    worker.input_diversion_port = FailingDiversion()
    worker.current_runtime_epoch = lambda: 9
    worker.realtime_input_runtime = SimpleNamespace(
        current_audio_session_id="audio-session",
        interaction_state=SimpleNamespace(interaction_session_id="interaction-a"),
        close_uncommitted_final_transcript=lambda **_kwargs: [
            {"type": "turn.state_changed", "payload": {"turn_state": "idle"}}
        ],
        apply_assistant_output_finished=lambda **_kwargs: settled.append("finished")
        or [],
    )
    worker.debug_audio_recorder = SimpleNamespace(
        record_final_asr_result=lambda *_args, **_kwargs: None,
    )
    worker.runtime_identity_factory = SimpleNamespace(
        new=lambda prefix: f"{prefix}-request"
    )
    worker._confirm_barge_in_if_actionable_decision = lambda **_kwargs: None
    worker._enqueue_runtime_events = (
        lambda events, **_kwargs: public_events.extend(events)
    )
    worker._reset_turn_buffers = lambda: None
    worker._promote_pending_next_turn_if_needed = lambda **_kwargs: None
    worker.voice_turn_resolved_handler = (
        lambda turn_id, request_id: resolved.append((turn_id, request_id))
    )
    worker.diverted_material_release_handler = (
        lambda turn_id, remember: released.append((turn_id, remember))
    )
    worker._dispatch_final_asr_request = (
        lambda **_kwargs: submitted.append("ordinary")
        or True
    )

    RealtimeInputWorker._apply_final_transcript_text(
        worker,
        final_text="不能偷偷降级",
        source_turn_id="voice-turn-failure",
        byte_count=6400,
        input_track_generation=1,
        submitted_at=2.0,
    )

    assert submitted == []
    assert all(
        event["type"] != "user.final_transcript" for event in public_events
    )
    assert settled == ["finished"]
    assert released == [("voice-turn-failure", False)]
    assert resolved == [("voice-turn-failure", None)]
