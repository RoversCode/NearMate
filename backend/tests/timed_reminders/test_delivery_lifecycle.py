"""用真实提醒仓储验证混合次数批次与播放结束的先后关系。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from queue import Queue

import pytest
import pytest_asyncio

from companion.activities.notice.models import NoticeDeliveryCommand
from companion.realtime.assistant_audio_output import (
    ActiveAssistantPlayout,
    AssistantAudioOutputScheduler,
    QueuedAssistantAudioEvent,
)
from companion.realtime.assistant_events import RuntimeEvent
from companion.timed_reminders.delivery_coordinator import TimedReminderDeliveryCoordinator
from companion.timed_reminders.repository import TimedReminderRepository
from companion.timed_reminders.service import TimedReminderService


@pytest_asyncio.fixture
async def mixed_batch():
    """准备一条第三次提醒和一条首次提醒，共同进入一个已计数的请求。"""

    clock = [datetime(2026, 9, 5, 6, tzinfo=UTC)]
    repository = TimedReminderRepository(runtime_id="00000000-0000-4000-8000-0000000000e2")
    service = TimedReminderService(
        repository=repository,
        broadcast_snapshot=lambda _event: asyncio.sleep(0),
        now=lambda: clock[0],
    )
    third = (await service.create(content="第三次事项", delay_seconds=1)).reminder.reminder_id
    clock[0] += timedelta(seconds=1)
    await service.activate_due()
    for _ in range(2):
        await service.mark_ring_enqueued((third,))
        await service.finish_ring_attempt((third,))
        clock[0] += timedelta(minutes=1)
    first = (await service.create(content="首次事项", delay_seconds=1)).reminder.reminder_id
    clock[0] += timedelta(seconds=1)
    await service.activate_due()
    cancelled: list[str] = []
    coordinator = TimedReminderDeliveryCoordinator(service=service, retry_delay_seconds=0.01)
    coordinator.bind_cancel_requests(cancelled.extend)
    service.bind_ringing_dismissed(coordinator.dismiss_reminders)
    coordinator.start()
    assert coordinator.freeze_ready_batch(
        request_id="mixed-request",
        interrupted_user=False,
        interrupted_assistant=False,
    ) is not None
    assert coordinator.commit_request_enqueued("mixed-request")
    await coordinator._drain_state_tasks()
    try:
        yield service, coordinator, clock, third, first, cancelled
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_third_attempt_waits_for_audio_while_first_reschedules(mixed_batch) -> None:
    """第三次保持可关闭，首次按文字结束计时，混合批次不能整批提前终结。"""

    service, coordinator, clock, third, first, _ = mixed_batch
    coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", "llm_output_completed"))
    await coordinator._drain_state_tasks()
    active = {item.reminder_id: item for item in service.snapshot().active}
    assert active[third].ring_count == 3
    assert active[third].next_ring_at is None
    assert active[third].to_payload()["can_dismiss"] is True
    assert active[first].next_ring_at == clock[0] + timedelta(minutes=1)

    clock[0] += timedelta(seconds=10)
    coordinator.observe_audio_finished("mixed-request")
    await coordinator._drain_state_tasks()
    snapshot = service.snapshot()
    assert snapshot.active[0].reminder_id == first
    assert snapshot.active[0].next_ring_at == clock[0] + timedelta(seconds=50)
    assert snapshot.recent[0].reminder_id == third
    assert snapshot.recent[0].finish_reason == "attempt_limit_reached"
    assert coordinator._current_request_id is None
    coordinator.observe_audio_finished("mixed-request")
    coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", "llm_output_completed"))
    await coordinator._drain_state_tasks()
    assert service.snapshot() == snapshot


@pytest.mark.asyncio
async def test_third_attempt_can_be_dismissed_during_audio(mixed_batch) -> None:
    """第三次文字结束后点知道了，必须取消剩余音频且不改首次的重响时间。"""

    service, coordinator, clock, third, first, cancelled = mixed_batch
    coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", "llm_output_completed"))
    await coordinator._drain_state_tasks()
    clock[0] += timedelta(seconds=10)
    dismissed = await service.dismiss(runtime_id=service.snapshot().runtime_id, reminder_ids=(third,))
    await coordinator._drain_state_tasks()
    assert [item.reminder_id for item in dismissed] == [third]
    assert cancelled == ["mixed-request"]
    assert service.snapshot().recent[0].finish_reason == "user_dismissed"
    assert service.snapshot().active[0].reminder_id == first
    assert service.snapshot().active[0].next_ring_at == clock[0] + timedelta(seconds=50)
    coordinator.observe_audio_finished("mixed-request")
    await service.dismiss(runtime_id=service.snapshot().runtime_id, reminder_ids=(third,))
    assert cancelled == ["mixed-request"]


@pytest.mark.asyncio
async def test_audio_before_text_does_not_close_third_attempt_early(mixed_batch) -> None:
    """音频先结束也要等文字终态，零音频和乱序回执使用相同边界。"""

    service, coordinator, _, third, _, _ = mixed_batch
    coordinator.observe_audio_finished("mixed-request")
    await coordinator._drain_state_tasks()
    assert len(service.snapshot().active) == 2
    coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", "llm_output_completed"))
    await coordinator._drain_state_tasks()
    assert service.snapshot().recent[0].reminder_id == third
    assert coordinator._current_request_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["delivery_cancelled", "delivery_failed", "superseded", "rebuild"])
async def test_interruption_settles_only_remaining_attempts(mixed_batch, outcome: str) -> None:
    """正文后中断或重建只结束第三次，不能把首次的重响时间向后推。"""

    service, coordinator, clock, third, _, _ = mixed_batch
    coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", "llm_output_completed"))
    await coordinator._drain_state_tasks()
    clock[0] += timedelta(seconds=10)
    if outcome == "rebuild":
        await coordinator.release_active_requests()
    else:
        coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", outcome))
        await coordinator._drain_state_tasks()
    assert service.snapshot().recent[0].reminder_id == third
    assert service.snapshot().recent[0].finished_at == clock[0]
    assert service.snapshot().active[0].next_ring_at == clock[0] + timedelta(seconds=50)
    assert coordinator._current_request_id is None


@pytest.mark.asyncio
async def test_partial_dismiss_during_settlement_does_not_repeat_first_attempt(mixed_batch, monkeypatch) -> None:
    """首次结算已经写入但尚未返回时关闭第三次，不能为首次再执行一遍结算。"""

    service, coordinator, clock, third, first, cancelled = mixed_batch
    original_finish = service.finish_ring_attempt
    started = asyncio.Event()
    proceed = asyncio.Event()
    attempts = []

    async def finish_then_wait(reminder_ids):
        """把领域提交与返回分开，允许关闭请求在中间进入。

        Args:
            reminder_ids: 本次要结算的提醒子集。

        Returns:
            仓储实际发生变更的提醒记录。
        """

        attempts.append(reminder_ids)
        result = await original_finish(reminder_ids)
        started.set()
        await proceed.wait()
        return result

    monkeypatch.setattr(service, "finish_ring_attempt", finish_then_wait)
    coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", "llm_output_completed"))
    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        clock[0] += timedelta(seconds=10)
        await service.dismiss(runtime_id=service.snapshot().runtime_id, reminder_ids=(third,))
        assert cancelled == ["mixed-request"]
    finally:
        proceed.set()
        await coordinator._drain_state_tasks()
    assert attempts == [(first,)]
    assert coordinator._current_request_id is None
    assert service.snapshot().active[0].next_ring_at == clock[0] + timedelta(seconds=50)


@pytest.mark.asyncio
async def test_third_settlement_failure_retries_only_third(mixed_batch, monkeypatch) -> None:
    """第三次播放后的暂时结算失败不能丢失账本，也不能重排已结算的首次提醒。"""

    service, coordinator, clock, third, first, _ = mixed_batch
    coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", "llm_output_completed"))
    await coordinator._drain_state_tasks()
    original_finish = service.finish_ring_attempt
    attempts = []
    completed = asyncio.Event()

    async def fail_once(reminder_ids):
        """首次失败，重试完成后通知断言方。

        Args:
            reminder_ids: 本次要结算的提醒子集。

        Returns:
            重试成功后实际发生变更的提醒记录。
        """

        attempts.append(reminder_ids)
        if len(attempts) == 1:
            raise RuntimeError("temporary settlement failure")
        result = await original_finish(reminder_ids)
        completed.set()
        return result

    monkeypatch.setattr(service, "finish_ring_attempt", fail_once)
    clock[0] += timedelta(seconds=10)
    coordinator.observe_audio_finished("mixed-request")
    await asyncio.wait_for(completed.wait(), timeout=1)
    await coordinator._drain_state_tasks()
    assert attempts == [(third,), (third,)]
    assert coordinator._current_request_id is None
    assert service.snapshot().active[0].reminder_id == first
    assert service.snapshot().active[0].next_ring_at == clock[0] + timedelta(seconds=50)


class ControlledPublisher:
    """用可控制的底层排空信号验证生产音频调度器的完成与取消路径。"""

    def __init__(self, outcome: str) -> None:
        """保存本次需要模拟的播放结果。

        Args:
            outcome: 正常排空、失败、超时或用户关闭。
        """

        self.outcome = outcome
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.cleared = False

    async def wait_for_playout(self) -> None:
        """等待底层排空，或按测试安排抛出播放错误。"""

        self.started.set()
        await self.finished.wait()
        if self.outcome == "error":
            raise RuntimeError("audio source unavailable")

    def clear_queue(self) -> None:
        """实际清空测试播放源并解除排空等待。"""

        self.cleared = True
        self.finished.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["drained", "error", "timeout", "dismiss"])
async def test_audio_scheduler_finishes_or_cancels_third_attempt(mixed_batch, outcome: str) -> None:
    """连接生产音频调度器与协调器，验证合成终态必须等排空或取消才能关闭第三次。"""

    service, coordinator, _, third, _, _ = mixed_batch
    events = Queue()
    publisher = ControlledPublisher(outcome)
    scheduler = AssistantAudioOutputScheduler(
        event_queue=events,
        audio_finished_handler=coordinator.observe_audio_finished,
        playout_wait_timeout_sec=0.05 if outcome == "timeout" else 1.0,
    )
    scheduler._publisher = publisher
    scheduler._loop = asyncio.get_running_loop()
    scheduler._active_playout = ActiveAssistantPlayout(
        request_id="mixed-request",
        generation_id="mixed-generation",
        delivery_id="mixed-delivery",
    )

    def cancel_audio(request_ids: tuple[str, ...]) -> None:
        """将关闭请求交给真实音频调度器。

        Args:
            request_ids: 协调器判断需要停止播放的请求编号。
        """

        for request_id in request_ids:
            scheduler.clear_pending_audio_for_request(request_id)

    coordinator.bind_cancel_requests(cancel_audio)
    coordinator.apply_delivery(NoticeDeliveryCommand("mixed-request", "llm_output_completed"))
    await coordinator._drain_state_tasks()
    audio_event = QueuedAssistantAudioEvent(
        event=RuntimeEvent(
            type="assistant.audio_finished",
            source="assistant_runtime",
            payload={
                "request_id": "mixed-request",
                "generation_id": "mixed-generation",
                "delivery_id": "mixed-delivery",
            },
        ),
        clear_generation=0,
    )
    audio_task = asyncio.create_task(scheduler._handle_audio_event(audio_event))
    try:
        await asyncio.wait_for(publisher.started.wait(), timeout=1)
        assert any(item.reminder_id == third for item in service.snapshot().active)
        if outcome == "dismiss":
            await service.dismiss(runtime_id=service.snapshot().runtime_id, reminder_ids=(third,))
        elif outcome != "timeout":
            publisher.finished.set()
        await asyncio.wait_for(audio_task, timeout=1)
        await coordinator._drain_state_tasks()
    finally:
        publisher.finished.set()
        await asyncio.gather(audio_task, return_exceptions=True)
    terminal = service.snapshot().recent[0]
    assert terminal.reminder_id == third
    assert terminal.finish_reason == ("user_dismissed" if outcome == "dismiss" else "attempt_limit_reached")
    assert publisher.cleared is (outcome != "drained")
    assert coordinator._current_request_id is None
    event = events.get_nowait()
    if outcome == "dismiss":
        assert event.type == "assistant.audio_publishing_cancelled"
        assert events.empty()
    else:
        assert event.payload["playout_completion"] == {
            "drained": "drained",
            "error": "unconfirmed_error",
            "timeout": "unconfirmed_timeout",
        }[outcome]
