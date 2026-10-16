"""验证 Self-Reflection 跨子进程输出与主进程 Journal 的结算身份。"""

from __future__ import annotations

from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace

from companion.llm.foreground_models import ForegroundEvent
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.generation_supervisor import GenerationSupervisor
from companion.realtime.visible_output_journal import (
    VisibleOutputJournal,
    VisibleOutputSettlement,
)


SELF_REFLECTION_ID = "self-reflection-integration"
EPISODE_ID = "episode-integration"
TRIGGER_INPUT_IDENTITY = "turn-integration"
AUXILIARY_REQUEST_ID = "self-reflection-interruption-integration"


def test_recovery_handoff_preserves_committed_auxiliary_settlement_identity() -> None:
    """辅助 final 已公开且 TTS 活跃时，Recovery 取消后仍保留完整身份。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    supervisor = GenerationSupervisor(
        request_queue=Queue(),
        auxiliary_request_queue=Queue(),
        tts_queue=Queue(),
        event_queue=event_queue,
        now=lambda: 1.0,
    )
    supervisor.start_generation(
        _request(
            request_id=f"self-reflection-recovery-{SELF_REFLECTION_ID}",
            source="self_reflection_recovery",
            trigger_input_identity=None,
        )
    )
    recovery_context = supervisor.current_context(lane="primary")
    assert recovery_context is not None
    supervisor.start_generation(
        _request(
            request_id=AUXILIARY_REQUEST_ID,
            source="self_reflection_interruption_reply",
            trigger_input_identity=TRIGGER_INPUT_IDENTITY,
        )
    )
    auxiliary_context = supervisor.current_context(lane="auxiliary")
    assert auxiliary_context is not None

    # 生产事故发生时辅助文字已经公开，只有 TTS 尚未结束。Recovery 首段必须
    # 取消这条 generation、等到音频 lane 结算，再公开自己的第一段正文。
    assert supervisor.put_foreground_event_if_current(
        auxiliary_context,
        ForegroundEvent(
            type="assistant.text_final",
            payload={"text": "我还在想办法。"},
        ),
    )
    supervisor.mark_lane_finished(auxiliary_context, lane="llm")
    auxiliary_cancelled = Event()
    auxiliary_context.cancellation_event.register(auxiliary_cancelled.set)
    publication_results: list[bool] = []
    publication_thread = Thread(
        target=lambda: publication_results.append(
            supervisor.put_visible_text_event_if_current(
                recovery_context,
                ForegroundEvent(
                    type="assistant.text_chunk",
                    payload={"text": "重新核对后，我现在无法取得实时榜单。"},
                ),
                text="重新核对后，我现在无法取得实时榜单。",
            )
        )
    )

    publication_thread.start()
    assert auxiliary_cancelled.wait(timeout=1.0)
    supervisor.mark_lane_finished(auxiliary_context, lane="tts")
    publication_thread.join(timeout=1.0)
    assert publication_thread.is_alive() is False
    assert publication_results == [True]

    events: list[RuntimeEvent] = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    auxiliary_final = next(
        event
        for event in events
        if event.type == "assistant.text_final"
        and event.payload.get("request_id") == AUXILIARY_REQUEST_ID
    )
    auxiliary_settled = next(
        event
        for event in events
        if event.type == "assistant.output_settled"
        and event.payload.get("request_id") == AUXILIARY_REQUEST_ID
    )

    journal = VisibleOutputJournal()
    assert journal.record_public_text_event(
        auxiliary_final.type,
        auxiliary_final.payload,
    )
    committed: list[VisibleOutputSettlement] = []
    settlement = journal.settle(
        auxiliary_settled.payload,
        settled_at=auxiliary_settled.produced_at,
        commit_settlement=committed.append,
    )

    assert settlement is not None
    assert committed == [settlement]
    assert settlement.outcome == "interrupted"
    assert settlement.history_policy == "unattended_episode"
    assert settlement.self_reflection_id == SELF_REFLECTION_ID
    assert settlement.episode_id == EPISODE_ID
    assert settlement.runtime_epoch == 1
    assert settlement.trigger_input_identity == TRIGGER_INPUT_IDENTITY
    assert settlement.generation_id == auxiliary_context.generation_id


def _request(
    *,
    request_id: str,
    source: str,
    trigger_input_identity: str | None,
) -> SimpleNamespace:
    """构造 Supervisor 与 Journal 集成测试所需的最小请求。

    Args:
        request_id: 本次表达请求的稳定编号。
        source: 请求属于 Recovery 还是辅助回应。
        trigger_input_identity: 触发辅助回应的用户输入身份。

    Returns:
        可以直接交给 Supervisor 的最小请求对象。
    """

    return SimpleNamespace(
        request_id=request_id,
        source=source,
        source_session_id="session-integration",
        source_turn_id=None,
        user_text="集成测试正文",
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
            self_reflection_id=SELF_REFLECTION_ID,
            episode_id=EPISODE_ID,
            runtime_epoch=1,
            trigger_input_identity=trigger_input_identity,
        ),
    )
