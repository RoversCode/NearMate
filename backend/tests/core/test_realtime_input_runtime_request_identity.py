from __future__ import annotations

from threading import Event, Thread

from companion.realtime.interaction_state import InteractionState
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.realtime_input_worker import RealtimeInputWorker
from companion.realtime.runtime_session import InputTurnSession


def _realtime_input_runtime() -> RealtimeInputRuntime:
    """构造只包含请求状态测试所需字段的 RealtimeInputRuntime。"""

    runtime = object.__new__(RealtimeInputRuntime)
    runtime.interaction_state = InteractionState()
    runtime.current_audio_session_id = "session-a"
    runtime.input_turn_session = InputTurnSession.empty()
    runtime.input_runtime_id = "input-runtime-00000000-0000-4000-8000-000000000001"
    runtime.input_runtime_epoch = 1
    return runtime


def test_final_transcript_binds_request_id_to_event_and_runtime_state() -> None:
    """可回复 final ASR 必须让聊天消息和状态共享同一 request id。"""

    runtime = _realtime_input_runtime()
    start_events = runtime.apply_user_turn_started(now=0.0)

    assert start_events[-1]["payload"]["turn_state"] == "listening"
    assert runtime.interaction_state.input_state.active is True

    events = runtime.apply_final_transcript(
        "请回答",
        now=1.0,
        assistant_request_id="final-asr-turn-a",
    )

    assert events[0]["payload"]["request_id"] == "final-asr-turn-a"
    assert [
        event["payload"]["turn_state"]
        for event in events
        if event["type"] == "turn.state_changed"
    ] == ["processing"]
    assert runtime.interaction_state.input_state.active is False
    assert runtime.interaction_state.assistant_output_state.request_id == "final-asr-turn-a"
    assert runtime.interaction_state.turn_state == "processing"


def test_final_asr_audio_lifecycle_reaches_idle_without_stale_input() -> None:
    """有效 Final ASR 移交给输出侧后，真实音频结束必须让整个 Turn 回到 idle。"""

    runtime = _realtime_input_runtime()
    runtime.apply_user_turn_started(now=0.0)
    final_events = runtime.apply_final_transcript(
        "请继续",
        now=1.0,
        assistant_request_id="final-asr-turn-a",
    )

    publishing_events = runtime.apply_assistant_audio_publishing_started(
        now=2.0,
        generation_id="generation-a",
        request_id="final-asr-turn-a",
    )
    finish_events = runtime.apply_assistant_output_finished(
        reason="assistant_audio_finished",
        now=3.0,
        request_id="final-asr-turn-a",
        generation_id="generation-a",
    )

    assert [
        event["payload"]["turn_state"]
        for event in final_events
        if event["type"] == "turn.state_changed"
    ] == ["processing"]
    assert publishing_events[-1]["payload"]["turn_state"] == "speaking"
    assert finish_events[-1]["payload"]["turn_state"] == "idle"
    assert runtime.interaction_state.input_state.active is False
    assert runtime.interaction_state.assistant_output_state.request_id is None


def test_cancelling_queued_audio_broadcasts_processing_to_idle() -> None:
    """尚未开播的请求被清理后，前端也必须收到 processing 结束的事实。"""

    runtime = _realtime_input_runtime()
    runtime.apply_text_input_started(now=1.0, request_id="request-a")
    events = runtime.apply_assistant_audio_publishing_cancelled(
        now=2.0, request_id="request-a", generation_id="generation-a",
    )
    assert runtime.interaction_state.turn_state == "idle"
    assert [event["type"] for event in events] == ["turn.state_changed", "interaction.state_changed"]
    assert events[0]["payload"]["turn_state"] == "idle"


def test_cancelling_audio_does_not_close_an_active_user_input() -> None:
    """用户已开口时只结束旧输出，保留 listening，不能广播假 idle。"""

    runtime = _realtime_input_runtime()
    runtime.apply_text_input_started(now=1.0, request_id="request-a")
    runtime.apply_user_turn_started(now=2.0)
    events = runtime.apply_assistant_audio_publishing_cancelled(
        now=3.0, request_id="request-a", generation_id="generation-a",
    )
    assert runtime.interaction_state.turn_state == "listening"
    assert runtime.interaction_state.assistant_output_state.state == "idle"
    assert events == []


def test_final_asr_handoff_hides_intermediate_idle_from_concurrent_reader() -> None:
    """Notice 线程读取 Turn 时不能看到 Final ASR 输入与输出交接之间的假 idle。"""

    runtime = _realtime_input_runtime()
    runtime.apply_user_turn_started(now=0.0)
    original_finish = runtime.interaction_state.input_state.finish
    input_finished = Event()
    allow_handoff_to_continue = Event()
    turn_read_started = Event()
    turn_read_finished = Event()
    observed_turn_states: list[str] = []
    final_asr_errors: list[BaseException] = []

    def pause_after_input_finished() -> None:
        """把 Final ASR 暂停在输入已关闭、输出尚未登记的位置。"""

        original_finish()
        input_finished.set()
        if not allow_handoff_to_continue.wait(timeout=1.0):
            raise TimeoutError("测试未能继续 Final ASR 状态交接")

    def apply_final_asr() -> None:
        """在音频线程模型下执行有效 Final ASR。"""

        try:
            runtime.apply_final_transcript(
                "请回答",
                now=1.0,
                assistant_request_id="final-asr-turn-a",
            )
        except BaseException as exception:
            final_asr_errors.append(exception)

    def read_turn_for_notice_gate() -> None:
        """模拟 LiveKit 事件循环读取 Notice 空闲门禁。"""

        turn_read_started.set()
        observed_turn_states.append(runtime.interaction_state.turn_state)
        turn_read_finished.set()

    runtime.interaction_state.input_state.finish = pause_after_input_finished
    final_asr_thread = Thread(target=apply_final_asr, daemon=True)
    final_asr_thread.start()
    assert input_finished.wait(timeout=1.0)

    notice_reader_thread = Thread(target=read_turn_for_notice_gate, daemon=True)
    notice_reader_thread.start()
    assert turn_read_started.wait(timeout=1.0)
    try:
        assert turn_read_finished.wait(timeout=0.05) is False
    finally:
        allow_handoff_to_continue.set()

    final_asr_thread.join(timeout=1.0)
    notice_reader_thread.join(timeout=1.0)

    assert final_asr_thread.is_alive() is False
    assert notice_reader_thread.is_alive() is False
    assert final_asr_errors == []
    assert observed_turn_states == ["processing"]


def test_ignored_final_transcript_closes_only_input_side() -> None:
    """被过滤的短 Final 只结束本次输入，不能清除仍在播音的助手请求。"""

    runtime = _realtime_input_runtime()
    runtime.apply_text_input_started(now=0.0, request_id="request-a")
    runtime.apply_assistant_audio_publishing_started(
        now=1.0,
        generation_id="generation-a",
        request_id="request-a",
    )
    runtime.apply_user_turn_started(now=2.0)

    events = runtime.apply_final_transcript(
        "嗯",
        now=3.0,
        assistant_request_id=None,
        should_request_assistant=False,
    )

    assert events[-1]["payload"]["turn_state"] == "speaking"
    assert runtime.interaction_state.input_state.active is False
    assert runtime.interaction_state.assistant_output_state.request_id == "request-a"
    assert runtime.interaction_state.assistant_output_state.playout_active is True


def test_empty_final_transcript_closes_input_idempotently() -> None:
    """空 Final 和重复收口都应保持 idle，不得创建助手输出身份。"""

    runtime = _realtime_input_runtime()
    runtime.apply_user_turn_started(now=0.0)

    first_events = runtime.apply_final_transcript(
        "  ",
        now=1.0,
        assistant_request_id=None,
    )
    second_events = runtime.apply_final_transcript(
        "",
        now=2.0,
        assistant_request_id=None,
    )

    assert first_events[-1]["payload"]["turn_state"] == "idle"
    assert second_events[-1]["payload"]["turn_state"] == "idle"
    assert all(event["type"] != "user.final_transcript" for event in first_events)
    assert all(event["type"] != "user.final_transcript" for event in second_events)
    assert runtime.interaction_state.input_state.active is False
    assert runtime.interaction_state.assistant_output_state.request_id is None


def test_stale_assistant_failure_cannot_finish_newer_request() -> None:
    """旧请求的延迟失败不得把正在处理的新请求改回 idle。"""

    runtime = _realtime_input_runtime()
    runtime.apply_text_input_started(now=1.0, request_id="request-new")

    events = runtime.apply_assistant_output_finished(
        reason="assistant_request_start_failed",
        now=2.0,
        request_id="request-old",
    )

    assert events == []
    assert runtime.interaction_state.turn_state == "processing"
    assert runtime.interaction_state.assistant_output_state.request_id == "request-new"


def test_matching_assistant_failure_finishes_active_request() -> None:
    """与当前身份匹配的失败应释放 processing 并清除请求归属。"""

    runtime = _realtime_input_runtime()
    runtime.apply_text_input_started(now=1.0, request_id="request-a")

    events = runtime.apply_assistant_output_finished(
        reason="assistant_request_start_failed",
        now=2.0,
        request_id="request-a",
    )

    assert [event["payload"]["turn_state"] for event in events if event["type"] == "turn.state_changed"] == ["idle"]
    assert runtime.interaction_state.assistant_output_state.request_id is None


def test_playout_start_requires_previously_activated_request_identity() -> None:
    """无归属或旧请求的播放开始不得把当前产品状态推进为 speaking。"""

    runtime = _realtime_input_runtime()

    assert runtime.apply_assistant_audio_publishing_started(
        now=1.0,
        generation_id="generation-old",
        request_id="request-old",
    ) == []
    assert runtime.interaction_state.turn_state == "idle"

    runtime.register_assistant_request("system-notice-1", now=1.5)
    events = runtime.apply_assistant_audio_publishing_started(
        now=2.0,
        generation_id="generation-notice",
        request_id="system-notice-1",
    )

    assert [event["payload"]["turn_state"] for event in events if event["type"] == "turn.state_changed"] == ["speaking"]


def test_short_input_reset_keeps_active_assistant_output_until_real_audio_finish() -> None:
    """短人声只重置输入侧，真实音频结束前助手身份和播音状态必须保留。"""

    runtime = _realtime_input_runtime()
    runtime.apply_text_input_started(now=1.0, request_id="request-a")
    runtime.apply_assistant_audio_publishing_started(
        now=2.0,
        generation_id="generation-a",
        request_id="request-a",
    )

    reset_events = runtime.apply_input_reset("short_turn_ignored", now=3.0)

    assert reset_events[0]["payload"]["turn_state"] == "speaking"
    assert runtime.interaction_state.turn_state == "speaking"
    assert runtime.interaction_state.assistant_output_state.request_id == "request-a"
    assert runtime.interaction_state.assistant_output_state.playout_active is True

    finish_events = runtime.apply_assistant_output_finished(
        reason="assistant_audio_finished",
        now=4.0,
        request_id="request-a",
    )

    assert finish_events[0]["payload"]["turn_state"] == "idle"
    assert runtime.interaction_state.assistant_output_state.request_id is None
    assert runtime.interaction_state.assistant_output_state.playout_active is False


def test_input_reset_clears_active_turn_without_reusing_turn_number() -> None:
    """输入链路重置只能清当前 Turn，下一轮编号必须继续递增。

    Returns:
        None: 通过重置前后的 Turn 编号验证序号没有回退。
    """

    runtime = _realtime_input_runtime()
    for _ in range(9):
        runtime.input_turn_session.begin_turn()
        runtime.input_turn_session.mark_smart_turn_result(is_turn_finished=True)

    assert runtime.input_turn_session.current_turn_id == "turn-9"

    runtime.apply_input_reset("short_turn_ignored", now=10.0)
    runtime.input_turn_session.begin_turn()

    assert runtime.input_turn_session.current_turn_id == "turn-10"


def test_final_asr_failure_fact_is_emitted_without_material_context() -> None:
    """Final ASR 没有材料时仍必须产生失败事实，释放对应 Turn 屏障。"""

    from queue import Queue

    worker = object.__new__(RealtimeInputWorker)
    worker.event_queue = Queue()
    worker.now = lambda: 3.0

    worker._queue_final_asr_material_cleanup_event(
        request_id="final-asr-request-a",
        source="final_asr",
        source_turn_id="turn-1",
        turn_material_context_id=None,
    )

    event = worker.event_queue.get_nowait()
    assert event.type == "assistant.request_enqueue_failed"
    assert event.payload == {
        "request_id": "final-asr-request-a",
        "source": "final_asr",
        "source_turn_id": "turn-1",
    }
