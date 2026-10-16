from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from companion.timed_reminders.repository import TimedReminderRepository
from companion.timed_reminders.scheduler import TimedReminderScheduler
from companion.timed_reminders.service import TimedReminderService
from companion.timed_reminders.delivery_coordinator import TimedReminderDeliveryCoordinator


@pytest.mark.asyncio
async def test_scheduler_activates_due_items_and_only_notifies_delivery_owner() -> None:
    """Scheduler 只推进到响铃状态，并让 Coordinator 自己读取权威集合。"""

    clock_value = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    repository = TimedReminderRepository(
        runtime_id="00000000-0000-4000-8000-0000000000dd",
        reminder_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174010",
    )
    service = TimedReminderService(
        repository=repository,
        broadcast_snapshot=lambda _event: asyncio.sleep(0),
        now=lambda: clock_value,
    )
    await service.create(content="开始面试", delay_seconds=1)
    clock_value += timedelta(seconds=1)
    ready_notified = asyncio.Event()
    scheduler = TimedReminderScheduler(
        service=service,
        notify_ready=ready_notified.set,
        now=lambda: clock_value,
    )

    scheduler.start()
    await asyncio.wait_for(ready_notified.wait(), timeout=1)

    reminder = service.snapshot().active[0]
    assert reminder.state == "ringing"
    assert reminder.ring_count == 0
    assert reminder.next_ring_at == clock_value
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_survives_activation_failure_and_can_be_woken_again() -> None:
    """一次领域异常不能让当前运行期唯一调度任务退出。"""

    clock_value = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    repository = TimedReminderRepository(
        runtime_id="00000000-0000-4000-8000-0000000000de",
        reminder_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174011",
    )
    service = TimedReminderService(
        repository=repository,
        broadcast_snapshot=lambda _event: asyncio.sleep(0),
        now=lambda: clock_value,
    )
    original_activate_due = service.activate_due
    calls = 0

    async def flaky_activate_due():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary activation failure")
        return await original_activate_due()

    service.activate_due = flaky_activate_due  # type: ignore[method-assign]
    await service.create(content="开始面试", delay_seconds=1)
    clock_value += timedelta(seconds=1)
    ready_notified = asyncio.Event()
    scheduler = TimedReminderScheduler(
        service=service,
        notify_ready=ready_notified.set,
        now=lambda: clock_value,
    )

    scheduler.start()
    await asyncio.sleep(0)
    scheduler.wake()
    await asyncio.wait_for(ready_notified.wait(), timeout=1)

    assert scheduler._task is not None
    assert scheduler._task.done() is False
    assert service.snapshot().active[0].state == "ringing"
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_wake_is_safe_from_another_thread() -> None:
    """跨线程状态变化只能线程安全地回投到 Scheduler 所属事件循环。"""

    repository = TimedReminderRepository(
        runtime_id="00000000-0000-4000-8000-0000000000df",
    )
    service = TimedReminderService(
        repository=repository,
        broadcast_snapshot=lambda _event: asyncio.sleep(0),
    )
    scheduler = TimedReminderScheduler(service=service, notify_ready=lambda: None)
    scheduler.start()

    await asyncio.to_thread(scheduler.wake)
    await asyncio.sleep(0)

    assert scheduler._task is not None
    assert scheduler._task.done() is False
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_wait_does_not_clear_a_new_wake_level() -> None:
    """等待函数返回时不能顺手清掉检查期间到达的新通知。"""

    repository = TimedReminderRepository(
        runtime_id="00000000-0000-4000-8000-0000000000e0",
    )
    service = TimedReminderService(
        repository=repository,
        broadcast_snapshot=lambda _event: asyncio.sleep(0),
    )
    scheduler = TimedReminderScheduler(service=service, notify_ready=lambda: None)
    scheduler._loop = asyncio.get_running_loop()
    scheduler._wake_event = asyncio.Event()
    scheduler._wake_event.set()

    await scheduler._wait_for_wake(timeout=0.1)

    assert scheduler._wake_event.is_set() is True


@pytest.mark.asyncio
async def test_blocked_ring_does_not_hide_later_scheduled_deadline() -> None:
    """第一项一直无法播报时，第二项仍必须在自己的触发时刻进入响铃中。"""

    repository = TimedReminderRepository(runtime_id="00000000-0000-4000-8000-0000000000e1")
    now = datetime.now(UTC)
    repository.create(content="第一项", trigger_at=now + timedelta(milliseconds=20), now=now)
    second = repository.create(content="第二项", trigger_at=now + timedelta(milliseconds=80), now=now)
    second_activated = asyncio.Event()

    async def broadcast(event: dict) -> None:
        """只在第二项真实进入响铃中时通知等待方。"""

        if any(
            item["reminder_id"] == second.reminder_id and item["state"] == "ringing"
            for item in event["payload"]["active"]
        ):
            second_activated.set()

    service = TimedReminderService(repository=repository, broadcast_snapshot=broadcast)
    coordinator = TimedReminderDeliveryCoordinator(service=service, retry_delay_seconds=0.01)
    coordinator.bind_request_notice(lambda: False)
    scheduler = TimedReminderScheduler(service=service, notify_ready=coordinator.wake)
    service.bind_schedule_changed(scheduler.wake)
    coordinator.start()
    scheduler.start()
    try:
        await asyncio.wait_for(second_activated.wait(), timeout=1)
        assert [item.state for item in service.snapshot().active] == ["ringing", "ringing"]
        assert all(item.ring_count == 0 for item in service.snapshot().active)
    finally:
        await scheduler.shutdown()
        await coordinator.shutdown()


def test_next_wake_excludes_ready_items_but_preserves_unchecked_deadlines() -> None:
    """只排除已经检查过的重响项，检查期间到期的重响与等待项仍需唤醒。"""

    repository = TimedReminderRepository(runtime_id="00000000-0000-4000-8000-0000000000e3")
    now = datetime(2026, 9, 5, 6, tzinfo=UTC)
    first = repository.create(content="已有待播", trigger_at=now + timedelta(seconds=1), now=now)
    repository.activate_due(now=now + timedelta(seconds=1))
    assert repository.next_wake_at(now + timedelta(seconds=1)) is None
    repository.mark_ring_enqueued((first.reminder_id,), now=now + timedelta(seconds=1))
    repository.finish_ring_attempt((first.reminder_id,), now=now + timedelta(seconds=1))
    second = repository.create(content="后续任务", trigger_at=now + timedelta(seconds=90), now=now)
    assert repository.next_wake_at(now + timedelta(seconds=60)) == now + timedelta(seconds=61)
    assert repository.next_wake_at(now + timedelta(seconds=61)) == second.trigger_at
    # 等待项即使已经过期也必须保留，下一轮才有机会把它激活。
    assert repository.next_wake_at(now + timedelta(seconds=100)) == second.trigger_at
    repository.cancel(second.reminder_id, now=now)
    assert repository.next_wake_at(now + timedelta(seconds=100)) is None


@pytest.mark.asyncio
async def test_scheduler_rechecks_deadline_crossed_during_state_read() -> None:
    """检查待播后刚越过触发时刻时，必须立即重查，不能进入无限等待。"""

    now = datetime(2026, 9, 5, 6, tzinfo=UTC)
    repository = TimedReminderRepository(runtime_id="00000000-0000-4000-8000-0000000000e4")
    repository.create(content="刚刚到点", trigger_at=now + timedelta(seconds=1), now=now)
    service = TimedReminderService(repository=repository, broadcast_snapshot=lambda _: asyncio.sleep(0))
    scheduler = TimedReminderScheduler(
        service=service, notify_ready=lambda: None, now=lambda: now + timedelta(seconds=2),
    )
    scheduler._wake_event = asyncio.Event()
    await asyncio.wait_for(scheduler._wait_until_next_change(now), timeout=0.1)
    assert not scheduler._wake_event.is_set()
