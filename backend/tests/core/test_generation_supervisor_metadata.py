from __future__ import annotations

from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace

from companion.llm.foreground_models import ForegroundEvent
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.cancellation import CancellationToken
from companion.realtime.generation_supervisor import GenerationSupervisor


def _supervisor_context(
    *,
    source: str = "system_notice",
) -> tuple[GenerationSupervisor, Queue[RuntimeEvent], SimpleNamespace]:
    """构造带材料归属信息的当前 generation 测试上下文。

    Returns:
        supervisor、事件队列和当前 generation 上下文。
    """

    event_queue: Queue[RuntimeEvent] = Queue()
    supervisor = GenerationSupervisor(
        request_queue=Queue(),
        auxiliary_request_queue=Queue(),
        tts_queue=Queue(),
        event_queue=event_queue,
        now=lambda: 1.0,
    )
    context = SimpleNamespace(
        cancellation_event=Event(),
        request=SimpleNamespace(
            request_id="request-a",
            source=source,
            source_turn_id=None,
            turn_material_context_id="material-context-a",
            history_policy="stable_dialogue",
            activity_progress_references=(),
            derived_from_activity_ids=("cognitive-a",),
        ),
        source_session_id="session-a",
        generation_id="generation-a",
        visible_parts=[],
        response_committed=False,
        output_settled=False,
    )
    context.lane = "primary"
    supervisor._current_by_lane["primary"] = context
    return supervisor, event_queue, context


def test_standard_assistant_event_inherits_request_identity() -> None:
    """所有助手输出事件都应继承当前请求身份，供播放取消闭环使用。"""

    supervisor, event_queue, context = _supervisor_context()

    assert supervisor.put_event_if_current(
        context,
        "assistant.audio_chunk",
        {"pcm16_b64": "AAA="},
    ) is True

    assert event_queue.get_nowait().payload == {
        "pcm16_b64": "AAA=",
        "session_id": "session-a",
        "generation_id": "generation-a",
        "request_id": "request-a",
        "source": "system_notice",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
        "derived_from_activity_ids": ["cognitive-a"],
    }


def test_final_memory_commit_inherits_cognitive_result_derivation() -> None:
    """最终回复和其稳定 assistant 消息必须使用同一 Cognitive 成果来源。"""

    supervisor, event_queue, context = _supervisor_context()
    assert supervisor.put_event_if_current(
        context,
        "assistant.text_final",
        {
            "text": "这是认知任务的结果。",
            "memory_commit": {
                "commit_id": "commit-a",
                "request_id": "request-a",
                "base_memory_epoch": 0,
                "messages": [
                    {
                        "role": "assistant",
                        "content": "这是认知任务的结果。",
                        "metadata": {},
                    }
                ],
            },
        },
    ) is True

    payload = event_queue.get_nowait().payload
    assert payload["derived_from_activity_ids"] == ["cognitive-a"]
    memory_message = payload["memory_commit"]["messages"][0]
    assert memory_message["metadata"]["derived_from_activity_ids"] == [
        "cognitive-a"
    ]


def test_text_final_inherits_turn_material_context() -> None:
    """助手文本终态必须携带材料上下文，异常回复也能开放 task_result 交付边界。"""

    supervisor, event_queue, context = _supervisor_context()

    assert supervisor.put_event_if_current(
        context,
        "assistant.text_final",
        {"text": "生成失败，请稍后再试。"},
    ) is True

    assert event_queue.get_nowait().payload["turn_material_context_id"] == "material-context-a"


def test_self_reflection_chunk_and_final_share_complete_identity() -> None:
    """Self-Reflection 的 chunk 与 final 必须携带同一套反思身份。"""

    supervisor, event_queue, context = _supervisor_context(
        source="self_reflection_entry"
    )
    context.request.self_reflection_context = SimpleNamespace(
        self_reflection_id="reflection-a",
        episode_id="episode-a",
        runtime_epoch=1,
        trigger_input_identity="input-a",
    )

    assert supervisor.put_visible_text_event_if_current(
        context,
        ForegroundEvent(type="assistant.text_chunk", payload={"text": "正在整理。"}),
        text="正在整理。",
    ) is True
    assert supervisor.put_event_if_current(
        context,
        "assistant.text_final",
        {"text": "已经整理好了。"},
    ) is True

    chunk = event_queue.get_nowait().payload
    final = event_queue.get_nowait().payload
    identity_fields = (
        "self_reflection_id",
        "episode_id",
        "runtime_epoch",
        "trigger_input_identity",
    )
    assert {field: chunk[field] for field in identity_fields} == {
        "self_reflection_id": "reflection-a",
        "episode_id": "episode-a",
        "runtime_epoch": 1,
        "trigger_input_identity": "input-a",
    }
    assert {field: final[field] for field in identity_fields} == {
        field: chunk[field] for field in identity_fields
    }


def test_superseded_system_notice_requires_matching_request_id() -> None:
    """过期提醒取消必须严格匹配当前独立 System Notice 的请求编号。"""

    supervisor, event_queue, context = _supervisor_context()

    assert supervisor.cancel_superseded_system_notice(
        request_id="other-request",
        reason="activity_notice_superseded",
    ) is False
    assert supervisor.cancel_superseded_system_notice(
        request_id="request-a",
        reason="activity_notice_superseded",
    ) is True

    interrupted = event_queue.get_nowait()
    assert interrupted.type == "assistant.generation_interrupted"
    assert interrupted.payload["request_id"] == "request-a"
    assert interrupted.payload["reason"] == "activity_notice_superseded"


def test_visible_system_notice_is_still_cancelled_when_superseded() -> None:
    """独立 System Notice 即使已经出字，事实失效后也必须立即停止。"""

    supervisor, event_queue, context = _supervisor_context()

    assert supervisor.put_visible_text_event_if_current(
        context,
        ForegroundEvent(type="assistant.text_chunk", payload={"text": "已经开口"}),
        text="已经开口",
    ) is True
    assert supervisor.cancel_superseded_system_notice(
        request_id="request-a",
        reason="activity_notice_superseded",
    ) is True

    visible = event_queue.get_nowait()
    assert visible.type == "assistant.text_chunk"
    interrupted = event_queue.get_nowait()
    assert interrupted.type == "assistant.generation_interrupted"
    assert interrupted.payload["reason"] == "activity_notice_superseded"
    assert context.visible_parts == ["已经开口"]
    assert context.cancellation_event.is_set() is True


def test_superseded_notice_command_never_cancels_user_generation() -> None:
    """即使请求编号误传，用户输入也不能被 Notice 失效取消。"""

    for source in ("text_input", "final_asr"):
        supervisor, event_queue, context = _supervisor_context(source=source)

        assert supervisor.cancel_superseded_system_notice(
            request_id="request-a",
            reason="activity_notice_superseded",
        ) is False
        assert context.cancellation_event.is_set() is False
        assert event_queue.empty()


def test_superseded_notice_command_rejects_committed_final() -> None:
    """非空 final 已提交后，Notice generation 已结束，迟到命令不能截断剩余播音。"""

    supervisor, event_queue, context = _supervisor_context()
    context.response_committed = True

    assert supervisor.cancel_superseded_system_notice(
        request_id="request-a",
        reason="activity_notice_superseded",
    ) is False
    assert context.cancellation_event.is_set() is False
    assert supervisor.current_context() is context
    assert event_queue.empty()


def test_reminder_cancel_stops_matching_request_after_text_final() -> None:
    """Reminder 正文结束后仍要允许按请求身份停止剩余 TTS。"""

    supervisor, event_queue, context = _supervisor_context(
        source="timed_reminder_notice"
    )
    context.response_committed = True

    assert supervisor.cancel_timed_reminder_notice(
        request_id="request-a",
        reason="timed_reminder_cancelled",
    ) is True

    interrupted = event_queue.get_nowait()
    assert interrupted.type == "assistant.generation_interrupted"
    assert interrupted.payload["request_id"] == "request-a"
    assert interrupted.payload["reason"] == "timed_reminder_cancelled"
    assert context.cancellation_event.is_set() is True
    assert supervisor.current_context() is None


def test_reminder_cancel_never_stops_non_reminder_request() -> None:
    """Reminder 取消命令即使编号相同，也不能碰普通用户输出。"""

    for source in ("text_input", "final_asr", "system_notice"):
        supervisor, event_queue, context = _supervisor_context(source=source)

        assert supervisor.cancel_timed_reminder_notice(
            request_id="request-a",
            reason="timed_reminder_cancelled",
        ) is False
        assert context.cancellation_event.is_set() is False
        assert supervisor.current_context() is context
        assert event_queue.empty()


def test_cancel_callback_can_reenter_supervisor_without_lock_wait() -> None:
    """底层流关闭回调读取 supervisor 时不能被取消路径自身持有的锁阻塞。"""

    supervisor, _event_queue, context = _supervisor_context()
    context.cancellation_event = CancellationToken()
    callback_observations: list[bool] = []

    def close_stream() -> None:
        """模拟 SDK close 等待内部线程读取当前 generation。"""

        state_read = Event()
        worker = Thread(
            target=lambda: (supervisor.current_context(), state_read.set()),
            daemon=True,
        )
        worker.start()
        callback_observations.append(state_read.wait(timeout=0.2))
        worker.join(timeout=0.2)

    context.cancellation_event.register(close_stream)

    supervisor.cancel_current(reason="user_barge_in")

    assert callback_observations == [True]
    assert supervisor.current_context() is None


def test_request_scoped_cancel_does_not_interrupt_a_newer_request() -> None:
    """旧快捷键取消迟到时，只认旧请求，不能打断已经开始的新回复。"""

    supervisor, event_queue, context = _supervisor_context()
    supervisor.cancel_current(reason="followup_toggle", request_id="request-old")
    assert supervisor.current_context() is context
    assert context.cancellation_event.is_set() is False
    assert event_queue.empty()
    supervisor.cancel_current(reason="followup_toggle", request_id="request-a")
    assert context.cancellation_event.is_set() is True


def test_request_cancel_before_generation_starts_cannot_revive_queued_reply() -> None:
    """控制命令先于排队请求到达时，旧请求随后也必须保持取消。"""

    supervisor, _events, current = _supervisor_context()
    supervisor.cancel_current(reason="followup_toggle", request_id="queued-request")
    supervisor.start_generation(_generation_request(request_id="queued-request", source="text_input"))
    cancelled = supervisor.request_queue.get_nowait().context
    assert cancelled.request.request_id == "queued-request"
    assert cancelled.cancellation_event.is_set() is True
    assert supervisor.current_context() is current
    assert current.cancellation_event.is_set() is False


def test_followup_shortcut_keeps_recovery_generation_running() -> None:
    """快捷键只停 Recovery 语音，不取消不可打断的恢复推理或工具。"""

    supervisor, events, context = _supervisor_context(source="self_reflection_recovery")
    supervisor.cancel_current(reason="followup_toggle", request_id="request-a")
    assert supervisor.current_context() is context
    assert context.cancellation_event.is_set() is False
    assert events.empty()


def test_followup_shortcut_before_recovery_start_preserves_recovery() -> None:
    """取消记录先到也只能静音，不能在 Recovery 真正启动时取消它。"""

    supervisor, _events, _context = _supervisor_context()
    supervisor.cancel_current(reason="followup_toggle", request_id="recovery-a")
    supervisor.start_generation(_generation_request(request_id="recovery-a", source="self_reflection_recovery"))
    context = supervisor.request_queue.get_nowait().context
    assert context.request.request_id == "recovery-a"
    assert context.cancellation_event.is_set() is False
    assert supervisor.current_context() is context


def test_interruption_reply_has_an_independent_generation_lane() -> None:
    """新打扰回应只能替换旧打扰回应，不能取消正在运行的 Recovery。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    supervisor = GenerationSupervisor(
        request_queue=Queue(),
        auxiliary_request_queue=Queue(),
        tts_queue=Queue(),
        event_queue=event_queue,
        now=lambda: 1.0,
    )
    recovery_request = _generation_request(
        request_id="recovery-request",
        source="self_reflection_recovery",
    )
    first_reply = _generation_request(
        request_id="reply-request-1",
        source="self_reflection_interruption_reply",
    )
    second_reply = _generation_request(
        request_id="reply-request-2",
        source="self_reflection_interruption_reply",
    )

    supervisor.start_generation(recovery_request)
    recovery_context = supervisor.current_context(lane="primary")
    supervisor.start_generation(first_reply)
    first_reply_context = supervisor.current_context(lane="auxiliary")
    supervisor.start_generation(second_reply)

    assert recovery_context is not None
    assert recovery_context.cancellation_event.is_set() is False
    assert first_reply_context is not None
    assert first_reply_context.cancellation_event.is_set() is True
    assert supervisor.current_context(lane="primary") is recovery_context
    assert (
        supervisor.current_context(lane="auxiliary").request.request_id
        == "reply-request-2"
    )


def test_first_recovery_chunk_drains_auxiliary_started_during_handoff() -> None:
    """首正文等待旧回应时，新到辅助 generation 也必须先完成 settlement。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    supervisor = GenerationSupervisor(
        request_queue=Queue(),
        auxiliary_request_queue=Queue(),
        tts_queue=Queue(),
        event_queue=event_queue,
        now=lambda: 1.0,
    )
    supervisor.start_generation(
        _generation_request(
            request_id="recovery-request",
            source="self_reflection_recovery",
        )
    )
    recovery_context = supervisor.current_context(lane="primary")
    assert recovery_context is not None
    supervisor.start_generation(
        _generation_request(
            request_id="reply-request-1",
            source="self_reflection_interruption_reply",
        )
    )
    first_reply_context = supervisor.current_context(lane="auxiliary")
    assert first_reply_context is not None
    first_reply_cancelled = Event()
    first_reply_context.cancellation_event.register(first_reply_cancelled.set)
    publication_results: list[bool] = []
    publication_thread = Thread(
        target=lambda: publication_results.append(
            supervisor.put_visible_text_event_if_current(
                recovery_context,
                ForegroundEvent(
                    type="assistant.text_chunk",
                    payload={"text": "我重新想清楚了。"},
                ),
                text="我重新想清楚了。",
            )
        )
    )

    publication_thread.start()
    assert first_reply_cancelled.wait(timeout=1.0)
    second_generation_id = supervisor.start_generation(
        _generation_request(
            request_id="reply-request-2",
            source="self_reflection_interruption_reply",
        )
    )
    second_reply_context = supervisor._live_contexts[second_generation_id]
    assert second_reply_context.cancellation_event.is_set() is True

    supervisor.mark_lane_finished(first_reply_context, lane="llm")
    supervisor.mark_lane_finished(first_reply_context, lane="tts")
    assert publication_thread.is_alive()

    supervisor.mark_lane_finished(second_reply_context, lane="llm")
    supervisor.mark_lane_finished(second_reply_context, lane="tts")
    publication_thread.join(timeout=1.0)

    assert publication_thread.is_alive() is False
    assert publication_results == [True]
    events: list[RuntimeEvent] = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    body_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "assistant.text_chunk"
    )
    reply_settlement_indices = [
        index
        for index, event in enumerate(events)
        if event.type == "assistant.output_settled"
        and event.payload.get("request_id") in {
            "reply-request-1",
            "reply-request-2",
        }
    ]
    assert len(reply_settlement_indices) == 2
    assert max(reply_settlement_indices) < body_index


def test_deadline_fallback_stops_model_lane_without_cancelling_visible_output() -> None:
    """Recovery deadline 只取消模型流，主 generation 仍可提交正文和 TTS。"""

    supervisor = GenerationSupervisor(
        request_queue=Queue(),
        auxiliary_request_queue=Queue(),
        tts_queue=Queue(),
        event_queue=Queue(),
        now=lambda: 1.0,
    )
    request = _generation_request(
        request_id="recovery-request",
        source="self_reflection_recovery",
    )
    supervisor.start_generation(request)
    context = supervisor.current_context(lane="primary")
    assert context is not None

    assert supervisor.request_recovery_fallback(
        self_reflection_id="reflection-a",
        reason="reflection_deadline_exceeded",
    ) is True
    assert context.model_cancellation_event.is_set() is True
    assert context.cancellation_event.is_set() is False
    assert context.forced_fallback_requested is True
    assert supervisor.current_context(lane="primary") is context


def test_deadline_fallback_waits_for_recovery_generation_to_start() -> None:
    """reaper 先于 Entry settlement 时，fallback 请求必须保留到 Recovery 启动。"""

    supervisor = GenerationSupervisor(
        request_queue=Queue(),
        auxiliary_request_queue=Queue(),
        tts_queue=Queue(),
        event_queue=Queue(),
        now=lambda: 1.0,
    )
    assert supervisor.request_recovery_fallback(
        self_reflection_id="reflection-a",
        reason="reflection_deadline_exceeded",
    ) is True
    supervisor.start_generation(
        _generation_request(
            request_id="recovery-request",
            source="self_reflection_recovery",
        )
    )
    context = supervisor.current_context(lane="primary")
    assert context is not None
    assert context.model_cancellation_event.is_set() is True
    assert context.forced_fallback_requested is True


def _generation_request(*, request_id: str, source: str) -> SimpleNamespace:
    """构造 supervisor 双 lane 测试所需的最小请求。"""

    return SimpleNamespace(
        request_id=request_id,
        source=source,
        source_session_id="session-a",
        source_turn_id=None,
        user_text="测试正文",
        generation_preemption_reason=None,
        turn_material_context_id=None,
        history_policy=(
            "unattended_episode"
            if source == "self_reflection_interruption_reply"
            else "stable_dialogue"
        ),
        activity_progress_references=(),
        derived_from_activity_ids=(),
        self_reflection_context=SimpleNamespace(
            self_reflection_id="reflection-a",
            episode_id="episode-a",
            runtime_epoch=1,
            trigger_input_identity=(
                "input-a"
                if source == "self_reflection_interruption_reply"
                else None
            ),
        ),
    )
