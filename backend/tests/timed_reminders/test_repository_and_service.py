from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta

import pytest

from companion.timed_reminders.repository import (
    DuplicateTimedReminder,
    InvalidTimedReminder,
    TimedReminderCapacityReached,
    TimedReminderRepository,
    TimedReminderStateConflict,
)
from companion.timed_reminders.service import TimedReminderService


RUNTIME_ID = "00000000-0000-4000-8000-0000000000dd"


class MutableClock:
    """提供测试可以显式推进的带时区当前时间。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        """返回测试当前设置的时间。"""

        return self.value


def _repository(*, max_active: int = 5, max_recent: int = 20) -> TimedReminderRepository:
    """构造编号稳定的测试仓储。

    Args:
        max_active: 测试需要的活跃上限。
        max_recent: 测试需要的终态上限。

    Returns:
        使用递增 UUID 的空仓储。
    """

    numbers = iter(range(1, 100))
    return TimedReminderRepository(
        runtime_id=RUNTIME_ID,
        max_active=max_active,
        max_recent=max_recent,
        reminder_id_factory=lambda: f"00000000-0000-4000-8000-{next(numbers):012d}",
    )


@pytest.mark.asyncio
async def test_service_creates_delay_from_backend_clock_and_broadcasts_snapshot() -> None:
    """相对时长必须由 Backend 当前时间换算，并返回跨日与运行期事实。"""

    clock = MutableClock(datetime(2026, 9, 3, 23, 59, 45, tzinfo=UTC))
    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    service = TimedReminderService(
        repository=_repository(),
        broadcast_snapshot=broadcast,
        now=clock.now,
    )

    result = await service.create(content="  准备明早的材料  ", delay_seconds=30)

    assert result.crosses_local_day is True
    assert result.reminder.content == "准备明早的材料"
    assert result.reminder.trigger_at == datetime(2026, 9, 4, 0, 0, 15, tzinfo=UTC)
    assert result.to_payload()["runtime_only"] is True
    assert broadcasts == [service.snapshot_event()]
    assert broadcasts[0]["payload"]["revision"] == 1  # type: ignore[index]


def test_repository_enforces_capacity_content_and_exact_duplicate() -> None:
    """仓储只拒绝同内容同时刻精确重复，并按活跃项执行容量上限。"""

    repository = _repository(max_active=2)
    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    trigger_at = now + timedelta(hours=1)
    repository.create(content="面试准备", trigger_at=trigger_at, now=now)

    with pytest.raises(DuplicateTimedReminder):
        repository.create(content="  面试准备  ", trigger_at=trigger_at, now=now)

    repository.create(
        content="面试准备",
        trigger_at=trigger_at + timedelta(minutes=1),
        now=now,
    )
    with pytest.raises(TimedReminderCapacityReached):
        repository.create(
            content="第三件事",
            trigger_at=trigger_at + timedelta(minutes=2),
            now=now,
        )
    with pytest.raises(InvalidTimedReminder):
        _repository().create(content=" ", trigger_at=trigger_at, now=now)


def test_repository_only_rejects_exact_content_and_trigger_duplicates() -> None:
    """重复保护保留大小写和正文内部空白，不把相似文本误判成同一事项。"""

    repository = _repository()
    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    trigger_at = now + timedelta(hours=1)
    repository.create(content="Call Mom", trigger_at=trigger_at, now=now)

    repository.create(content="call   mom", trigger_at=trigger_at, now=now)
    with pytest.raises(DuplicateTimedReminder):
        repository.create(content="Call Mom", trigger_at=trigger_at, now=now)


def test_repository_counts_non_bmp_content_as_unicode_characters() -> None:
    """非 BMP 字符也应按用户看到的单个 Unicode 字符执行长度上限。"""

    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    trigger_at = now + timedelta(hours=1)
    accepted = _repository().create(
        content="😀" * 500,
        trigger_at=trigger_at,
        now=now,
    )

    assert accepted.content == "😀" * 500
    with pytest.raises(InvalidTimedReminder):
        _repository().create(
            content="😀" * 501,
            trigger_at=trigger_at,
            now=now,
        )


def test_repository_rings_three_times_then_dismisses_at_attempt_limit() -> None:
    """到点任务必须按 Notice 入队次数计数，并在第三次生命周期结束后自动关闭。"""

    repository = _repository(max_recent=1)
    created_at = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    first = repository.create(
        content="结束专注",
        trigger_at=created_at + timedelta(seconds=30),
        now=created_at,
    )
    second = repository.create(
        content="参加面试",
        trigger_at=created_at + timedelta(seconds=30),
        now=created_at,
    )

    assert repository.activate_due(now=created_at + timedelta(seconds=29)) == ()
    activated = repository.activate_due(now=created_at + timedelta(seconds=30))
    assert [item.reminder_id for item in activated] == [first.reminder_id, second.reminder_id]
    assert all(item.state == "ringing" for item in activated)
    with pytest.raises(TimedReminderStateConflict):
        repository.update(
            first.reminder_id,
            content="改掉",
            trigger_at=None,
            now=created_at + timedelta(seconds=31),
        )

    reminder_ids = (first.reminder_id, second.reminder_id)
    for ring_number in range(1, 4):
        ring_at = created_at + timedelta(seconds=30 + (ring_number - 1) * 61)
        assert [item.reminder_id for item in repository.ready_to_ring(now=ring_at)] == list(
            reminder_ids
        )
        enqueued = repository.mark_ring_enqueued(reminder_ids, now=ring_at)
        assert all(item.ring_count == ring_number for item in enqueued)
        assert repository.ready_to_ring(now=ring_at) == ()
        settled = repository.finish_ring_attempt(
            reminder_ids,
            now=ring_at + timedelta(seconds=1),
        )
        expected_state = "dismissed" if ring_number == 3 else "ringing"
        assert all(item.state == expected_state for item in settled)
        if ring_number < 3:
            assert all(
                item.next_ring_at == ring_at + timedelta(seconds=61)
                for item in settled
            )

    snapshot = repository.snapshot()
    assert snapshot.active == ()
    assert [item.reminder_id for item in snapshot.recent] == [second.reminder_id]
    assert snapshot.recent[0].finish_reason == "attempt_limit_reached"


@pytest.mark.asyncio
async def test_service_dismisses_visible_ringing_batch_and_notifies_delivery_owner() -> None:
    """关闭响铃批次必须按当前运行期原子更新，并通知 Notice 所有者清理输出。"""

    clock = MutableClock(datetime(2026, 9, 3, 6, 0, tzinfo=UTC))

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        return None

    service = TimedReminderService(
        repository=_repository(),
        broadcast_snapshot=ignore_broadcast,
        now=clock.now,
    )
    dismissed_batches: list[tuple[str, ...]] = []
    service.bind_ringing_dismissed(dismissed_batches.append)
    created = await service.create(content="休息一下", delay_seconds=1)
    clock.value += timedelta(seconds=1)
    assert await service.activate_due()

    dismissed = await service.dismiss(
        runtime_id=RUNTIME_ID,
        reminder_ids=(created.reminder.reminder_id,),
    )

    assert dismissed[0].state == "dismissed"
    assert dismissed[0].finish_reason == "user_dismissed"
    assert dismissed_batches == [(created.reminder.reminder_id,)]


@pytest.mark.asyncio
async def test_repeated_dismiss_broadcasts_authoritative_snapshot() -> None:
    """幂等关闭没有新交集时也要广播，供丢过上一帧的界面完成收口。"""

    clock = MutableClock(datetime(2026, 9, 3, 6, 0, tzinfo=UTC))
    broadcasts: list[dict[str, object]] = []

    async def record_broadcast(event: dict[str, object]) -> None:
        """记录本次关闭产生的权威快照。"""

        broadcasts.append(event)

    service = TimedReminderService(
        repository=_repository(),
        broadcast_snapshot=record_broadcast,
        now=clock.now,
    )
    created = await service.create(content="休息一下", delay_seconds=1)
    clock.value += timedelta(seconds=1)
    await service.activate_due()
    await service.dismiss(
        runtime_id=RUNTIME_ID,
        reminder_ids=(created.reminder.reminder_id,),
    )
    broadcasts.clear()

    assert await service.dismiss(
        runtime_id=RUNTIME_ID,
        reminder_ids=(created.reminder.reminder_id,),
    ) == ()
    await asyncio.sleep(0)

    assert broadcasts == [service.snapshot_event()]


@pytest.mark.asyncio
async def test_service_rejects_ambiguous_time_inputs() -> None:
    """服务层也必须拒绝绕过 HTTP schema 的歧义时间输入。"""

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        return None

    service = TimedReminderService(
        repository=_repository(),
        broadcast_snapshot=ignore_broadcast,
        now=lambda: datetime(2026, 9, 3, 6, 0, tzinfo=UTC),
    )

    with pytest.raises(InvalidTimedReminder):
        await service.create(content="事项")
    with pytest.raises(InvalidTimedReminder):
        await service.create(
            content="事项",
            trigger_at=datetime(2026, 9, 3, 7, 0, tzinfo=UTC),
            delay_seconds=60,
        )


@pytest.mark.asyncio
async def test_service_keeps_dismissal_and_output_cleanup_when_broadcast_fails() -> None:
    """快照广播失败时，关闭状态和正在表达的请求清理仍必须完成。"""

    clock = MutableClock(datetime(2026, 9, 3, 6, 0, tzinfo=UTC))

    async def fail_broadcast(_event: dict[str, object]) -> None:
        raise RuntimeError("control connection failed")

    service = TimedReminderService(
        repository=_repository(),
        broadcast_snapshot=fail_broadcast,
        now=clock.now,
    )
    dismissed_batches: list[tuple[str, ...]] = []
    schedule_wakes: list[bool] = []
    service.bind_ringing_dismissed(dismissed_batches.append)
    service.bind_schedule_changed(lambda: schedule_wakes.append(True))

    created = await service.create(content="休息一下", delay_seconds=1)
    clock.value += timedelta(seconds=1)
    activated = await service.activate_due()
    dismissed = await service.dismiss(
        runtime_id=RUNTIME_ID,
        reminder_ids=(created.reminder.reminder_id,),
    )

    assert activated and activated[0].state == "ringing"
    assert dismissed[0].state == "dismissed"
    assert dismissed_batches == [(created.reminder.reminder_id,)]
    assert schedule_wakes


@pytest.mark.asyncio
async def test_service_keeps_successful_write_when_scheduler_wake_fails() -> None:
    """调度唤醒旁路异常不能把已经成立的领域写入伪装成失败。"""

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        return None

    service = TimedReminderService(
        repository=_repository(),
        broadcast_snapshot=ignore_broadcast,
        now=lambda: datetime(2026, 9, 3, 6, 0, tzinfo=UTC),
    )

    def fail_schedule_wake() -> None:
        """模拟调度器已经关闭或唤醒端口临时故障。"""

        raise RuntimeError("scheduler unavailable")

    service.bind_schedule_changed(fail_schedule_wake)

    mutation = await service.create(content="休息一下", delay_seconds=60)

    assert mutation.reminder.state == "scheduled"
    assert service.snapshot().active == (mutation.reminder,)


def test_repository_dismiss_is_idempotent_and_scoped_to_visible_ids() -> None:
    """批次关闭只能影响调用方列出的响铃项目，重复提交保持幂等。"""

    repository = _repository()
    created_at = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    first = repository.create(
        content="事项一",
        trigger_at=created_at + timedelta(seconds=1),
        now=created_at,
    )
    second = repository.create(
        content="事项二",
        trigger_at=created_at + timedelta(seconds=1),
        now=created_at,
    )
    repository.activate_due(now=created_at + timedelta(seconds=1))

    dismissed = repository.dismiss(
        runtime_id=RUNTIME_ID,
        reminder_ids=(first.reminder_id,),
        now=created_at + timedelta(seconds=2),
    )

    assert [item.reminder_id for item in dismissed] == [first.reminder_id]
    assert repository.dismiss(
        runtime_id=RUNTIME_ID,
        reminder_ids=(first.reminder_id,),
        now=created_at + timedelta(seconds=3),
    ) == ()
    assert repository.snapshot().active[0].reminder_id == second.reminder_id
