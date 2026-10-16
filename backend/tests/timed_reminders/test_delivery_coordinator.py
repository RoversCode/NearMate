from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion.activities.notice.models import NoticeDeliveryCommand
from companion.timed_reminders.delivery_coordinator import (
    TimedReminderDeliveryCoordinator,
)
from companion.timed_reminders.models import TimedReminder


class RecordingReminderService:
    """用可控权威状态记录协调器的计数和尝试结算。"""

    def __init__(self) -> None:
        self.reminders: dict[str, TimedReminder] = {}
        self.enqueued: list[tuple[str, ...]] = []
        self.finished: list[tuple[str, ...]] = []

    def snapshot(self) -> SimpleNamespace:
        """返回协调器冻结批次所需的运行身份。"""

        return SimpleNamespace(runtime_id="00000000-0000-4000-8000-0000000000aa")

    def ready_to_ring(self) -> tuple[TimedReminder, ...]:
        """返回仍到点且没有被在途请求占用的任务。"""

        return tuple(
            reminder
            for reminder in self.reminders.values()
            if reminder.state == "ringing" and reminder.next_ring_at is not None
        )

    async def mark_ring_enqueued(
        self,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """记录成功入队并把任务临时置为不可再次领取。"""

        marked: list[TimedReminder] = []
        for reminder_id in reminder_ids:
            current = self.reminders.get(reminder_id)
            if current is None or current.next_ring_at is None:
                continue
            updated = replace(
                current,
                ring_count=current.ring_count + 1,
                next_ring_at=None,
            )
            self.reminders[reminder_id] = updated
            marked.append(updated)
        if marked:
            self.enqueued.append(tuple(item.reminder_id for item in marked))
        return tuple(marked)

    async def finish_ring_attempt(
        self,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """记录本次 Notice 生命周期已经完整结束。"""

        self.finished.append(reminder_ids)
        for reminder_id in reminder_ids:
            current = self.reminders.get(reminder_id)
            if current is not None:
                self.reminders[reminder_id] = replace(
                    current,
                    next_ring_at=datetime.now(UTC) + timedelta(minutes=1),
                )
        return ()

    def add(self, *reminders: TimedReminder) -> None:
        """把新的到点任务加入假权威仓储。"""

        for reminder in reminders:
            self.reminders[reminder.reminder_id] = reminder


class BlockingMarkReminderService(RecordingReminderService):
    """让响铃计数停在可控边界，复现极快输出回执乱序。"""

    def __init__(self) -> None:
        """初始化计数门禁与结算完成信号。"""

        super().__init__()
        self.mark_started = asyncio.Event()
        self.allow_mark = asyncio.Event()
        self.finish_completed = asyncio.Event()

    async def mark_ring_enqueued(
        self,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """等待测试放行后才把入队次数写入假仓储。

        Args:
            reminder_ids: 本次 Reminder Notice 冻结的提醒编号。

        Returns:
            已增加响铃次数的提醒。
        """

        self.mark_started.set()
        await self.allow_mark.wait()
        return await super().mark_ring_enqueued(reminder_ids)

    async def finish_ring_attempt(
        self,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """记录尝试结算并通知测试等待方。

        Args:
            reminder_ids: 本次已经完成的提醒编号。

        Returns:
            假服务不需要返回变更记录，因此始终为空。
        """

        result = await super().finish_ring_attempt(reminder_ids)
        self.finish_completed.set()
        return result


class BlockingFinishReminderService(RecordingReminderService):
    """让响铃结算停在可控边界，复现媒体释放期间的重新领取。"""

    def __init__(self) -> None:
        """初始化结算开始和放行信号。"""

        super().__init__()
        self.finish_started = asyncio.Event()
        self.allow_finish = asyncio.Event()

    async def finish_ring_attempt(
        self,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """等待测试放行后再完成当前响铃尝试。

        Args:
            reminder_ids: 本次已经完成的提醒编号。

        Returns:
            假服务完成结算后的空变更集合。
        """

        self.finish_started.set()
        await self.allow_finish.wait()
        return await super().finish_ring_attempt(reminder_ids)


class FlakyFinishReminderService(RecordingReminderService):
    """第一次拒绝响铃结算，用来验证请求账本会保留并重试。"""

    def __init__(self) -> None:
        """初始化结算次数和重试成功信号。"""

        super().__init__()
        self.finish_attempt_count = 0
        self.finish_retried = asyncio.Event()

    async def finish_ring_attempt(
        self,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """首次抛出临时错误，第二次按正常假仓储完成结算。

        Args:
            reminder_ids: 本次已经完成正文生命周期的提醒编号。

        Returns:
            第二次调用时由父类返回的空变更集合。
        """

        self.finish_attempt_count += 1
        if self.finish_attempt_count == 1:
            raise RuntimeError("temporary repository failure")
        result = await super().finish_ring_attempt(reminder_ids)
        self.finish_retried.set()
        return result


def _reminder(reminder_id: str, *, seconds: int = 1) -> TimedReminder:
    """构造一条允许立即发起 Notice 的响铃任务。"""

    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    return TimedReminder(
        reminder_id=reminder_id,
        content=f"事项 {reminder_id}",
        state="ringing",
        trigger_at=now + timedelta(seconds=seconds),
        created_at=now,
        updated_at=now + timedelta(seconds=seconds),
        next_ring_at=now + timedelta(seconds=seconds),
    )


@pytest.mark.asyncio
async def test_ready_items_merge_before_notice_is_frozen() -> None:
    """开始消费前到达的任务必须冻结在同一条 Reminder Notice。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"), _reminder("reminder-b", seconds=2))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    frozen_batches = []

    def request_notice() -> bool:
        batch = coordinator.freeze_ready_batch(
            request_id="request-a",
            interrupted_user=False,
            interrupted_assistant=False,
        )
        frozen_batches.append(batch)
        return batch is not None

    coordinator.bind_request_notice(request_notice)
    coordinator.start()
    coordinator.wake()
    await coordinator.drain()

    assert frozen_batches[0] is not None
    assert [
        reminder.reminder_id for reminder in frozen_batches[0].reminders
    ] == ["reminder-a", "reminder-b"]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_rejected_notice_request_retries_without_external_wake() -> None:
    """媒体暂时拒绝请求后，即使没有新事件也必须再次尝试投递。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(
        service=service,  # type: ignore[arg-type]
        retry_delay_seconds=0.01,
    )
    request_attempts = 0
    request_enqueued = asyncio.Event()

    def request_notice() -> bool:
        """第一次模拟临时不可用，第二次提交冻结批次。"""

        nonlocal request_attempts
        request_attempts += 1
        if request_attempts == 1:
            return False
        batch = coordinator.freeze_ready_batch(
            request_id="request-a",
            interrupted_user=False,
            interrupted_assistant=False,
        )
        if batch is None:
            return False
        coordinator.commit_request_enqueued("request-a")
        request_enqueued.set()
        return True

    coordinator.bind_request_notice(request_notice)
    coordinator.start()
    try:
        await asyncio.wait_for(request_enqueued.wait(), timeout=0.2)
        await coordinator.drain()

        assert request_attempts == 2
        assert service.enqueued == [("reminder-a",)]
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_text_final_settles_attempt_but_keeps_audio_lane_until_playout_finishes() -> None:
    """正文结束立即结算响铃，但下一条提醒仍要等当前音频真正排空。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    requested_ids: list[str] = []

    def request_notice() -> bool:
        request_id = f"request-{len(requested_ids) + 1}"
        batch = coordinator.freeze_ready_batch(
            request_id=request_id,
            interrupted_user=False,
            interrupted_assistant=False,
        )
        if batch is None:
            return False
        requested_ids.append(request_id)
        coordinator.commit_request_enqueued(request_id)
        return True

    coordinator.bind_request_notice(request_notice)
    coordinator.start()
    await coordinator.drain()
    service.add(_reminder("reminder-b", seconds=2))
    coordinator.wake()
    coordinator.apply_delivery(
        NoticeDeliveryCommand("request-1", "llm_output_completed")
    )
    await coordinator.drain()

    assert requested_ids == ["request-1"]
    assert service.finished == [("reminder-a",)]

    coordinator.observe_audio_finished("request-1")
    await asyncio.sleep(0)
    await coordinator.drain()

    assert service.finished == [("reminder-a",)]
    assert requested_ids == ["request-1", "request-2"]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_finish_failure_keeps_request_ledger_until_retry_succeeds() -> None:
    """领域结算临时失败时不能释放串行占用或遗失本次提醒。"""

    service = FlakyFinishReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(
        service=service,  # type: ignore[arg-type]
        retry_delay_seconds=0.01,
    )
    requested_ids: list[str] = []

    def request_notice() -> bool:
        """冻结并提交当前可响批次。"""

        request_id = f"request-{len(requested_ids) + 1}"
        batch = coordinator.freeze_ready_batch(
            request_id=request_id,
            interrupted_user=False,
            interrupted_assistant=False,
        )
        if batch is None:
            return False
        requested_ids.append(request_id)
        coordinator.commit_request_enqueued(request_id)
        return True

    coordinator.bind_request_notice(request_notice)
    coordinator.start()
    await coordinator.drain()
    service.add(_reminder("reminder-b", seconds=2))
    coordinator.wake()
    coordinator.apply_delivery(
        NoticeDeliveryCommand("request-1", "llm_output_completed")
    )

    await asyncio.wait_for(service.finish_retried.wait(), timeout=1)
    assert service.finish_attempt_count == 2
    assert service.finished == [("reminder-a",)]
    assert requested_ids == ["request-1"]

    coordinator.observe_audio_finished("request-1")
    await coordinator.drain()

    assert requested_ids == ["request-1", "request-2"]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_partial_dismiss_retries_detached_attempt_settlement() -> None:
    """部分关闭撤销媒体请求后，剩余项目的结算失败也不能丢失。"""

    service = FlakyFinishReminderService()
    service.add(_reminder("reminder-a"), _reminder("reminder-b"))
    coordinator = TimedReminderDeliveryCoordinator(
        service=service,  # type: ignore[arg-type]
        retry_delay_seconds=0.01,
    )
    coordinator.start()
    await coordinator.drain()
    assert coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    ) is not None
    coordinator.commit_request_enqueued("request-a")
    await coordinator.drain()

    # 模拟服务已经原子关闭批次中的一项，Coordinator 只需结算仍响铃的交集。
    service.reminders["reminder-a"] = replace(
        service.reminders["reminder-a"],
        state="dismissed",
        next_ring_at=None,
    )
    coordinator.dismiss_reminders(("reminder-a",))

    await asyncio.wait_for(service.finish_retried.wait(), timeout=1)

    assert service.finish_attempt_count == 2
    assert service.finished == [("reminder-b",)]
    assert service.reminders["reminder-b"].next_ring_at is not None
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_output_receipts_wait_until_ring_count_is_committed() -> None:
    """极快的正文和音频回执不能越过尚未完成的响铃计数。"""

    service = BlockingMarkReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    coordinator.start()
    await coordinator.drain()
    batch = coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    )
    assert batch is not None

    coordinator.commit_request_enqueued("request-a")
    await asyncio.wait_for(service.mark_started.wait(), timeout=1)
    coordinator.apply_delivery(
        NoticeDeliveryCommand("request-a", "llm_output_completed")
    )
    coordinator.observe_audio_finished("request-a")
    await coordinator.drain()

    assert service.finished == []
    service.allow_mark.set()
    await asyncio.wait_for(service.finish_completed.wait(), timeout=1)

    assert service.enqueued == [("reminder-a",)]
    assert service.finished == [("reminder-a",)]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_dismiss_during_ring_count_commit_settles_remaining_items() -> None:
    """计数落账期间关闭批次项目时，未关闭项目仍必须完成本次尝试。"""

    service = BlockingMarkReminderService()
    service.add(_reminder("reminder-a"), _reminder("reminder-b"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    cancelled_requests: list[tuple[str, ...]] = []
    coordinator.bind_cancel_requests(cancelled_requests.append)
    coordinator.start()
    await coordinator.drain()

    batch = coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    )
    assert batch is not None
    coordinator.commit_request_enqueued("request-a")
    await asyncio.wait_for(service.mark_started.wait(), timeout=1)

    # 模拟 HTTP dismiss 已经先在权威仓储关闭 reminder-a，但计数协程仍在等待。
    service.reminders["reminder-a"] = replace(
        service.reminders["reminder-a"],
        state="dismissed",
        next_ring_at=None,
    )
    coordinator.dismiss_reminders(("reminder-a",))
    await coordinator.drain()

    assert cancelled_requests == [("request-a",)]
    assert service.finished == []

    service.allow_mark.set()
    await asyncio.wait_for(service.finish_completed.wait(), timeout=1)

    assert service.enqueued == [("reminder-b",)]
    assert service.finished == [("reminder-b",)]
    assert cancelled_requests == [("request-a",)]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_audio_receipt_before_text_final_is_not_lost() -> None:
    """跨线程先到的音频完成回执必须等待正文完成后共同结算。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    coordinator.start()
    await coordinator.drain()
    assert coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    ) is not None
    coordinator.commit_request_enqueued("request-a")
    await coordinator.drain()

    coordinator.observe_audio_finished("request-a")
    coordinator.apply_delivery(
        NoticeDeliveryCommand("request-a", "llm_output_completed")
    )
    await coordinator.drain()

    assert service.finished == [("reminder-a",)]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_release_waits_for_ring_count_before_finishing_attempt() -> None:
    """媒体重建必须等入队计数落账后再释放当前响铃尝试。"""

    service = BlockingMarkReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    coordinator.start()
    await coordinator.drain()
    assert coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    ) is not None
    coordinator.commit_request_enqueued("request-a")
    await asyncio.wait_for(service.mark_started.wait(), timeout=1)

    release_task = asyncio.create_task(coordinator.release_active_requests())
    await asyncio.sleep(0)
    assert release_task.done() is False
    service.allow_mark.set()
    await asyncio.wait_for(release_task, timeout=1)

    assert service.enqueued == [("reminder-a",)]
    assert service.finished == [("reminder-a",)]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_release_pauses_new_requests_until_media_session_resumes() -> None:
    """媒体释放期间和释放完成后都不能把等待项重新交给旧会话。"""

    service = BlockingFinishReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    coordinator.start()
    await coordinator.drain()
    assert coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    ) is not None
    coordinator.commit_request_enqueued("request-a")
    await coordinator.drain()

    requested_ids: list[str] = []

    def request_notice() -> bool:
        """记录媒体端口在测试窗口内真正领取的新请求。"""

        request_id = f"request-{len(requested_ids) + 2}"
        batch = coordinator.freeze_ready_batch(
            request_id=request_id,
            interrupted_user=False,
            interrupted_assistant=False,
        )
        if batch is None:
            return False
        requested_ids.append(request_id)
        return True

    coordinator.bind_request_notice(request_notice)
    release_task = asyncio.create_task(coordinator.release_active_requests())
    await asyncio.wait_for(service.finish_started.wait(), timeout=1)
    service.add(_reminder("reminder-b", seconds=2))
    coordinator.wake()
    await coordinator.drain()

    assert requested_ids == []

    service.allow_finish.set()
    await asyncio.wait_for(release_task, timeout=1)
    coordinator.wake()
    await coordinator.drain()
    assert requested_ids == []

    coordinator.resume_requests()
    await coordinator.drain()
    assert requested_ids == ["request-2"]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_release_active_requests_can_be_called_from_another_event_loop() -> None:
    """媒体重建从其它事件循环调用时也必须回投 owner 并真实释放。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    coordinator.start()
    await coordinator.drain()
    assert coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    ) is not None
    coordinator.commit_request_enqueued("request-a")
    await coordinator.drain()

    await asyncio.to_thread(
        lambda: asyncio.run(coordinator.release_active_requests())
    )

    assert service.finished == [("reminder-a",)]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_release_still_settles_attempt_when_media_cancellation_fails() -> None:
    """媒体撤销异常不能阻止已计数响铃完成领域结算。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]

    def reject_cancellation(_request_ids: tuple[str, ...]) -> None:
        """模拟媒体链路已经失效。"""

        raise RuntimeError("media unavailable")

    coordinator.bind_cancel_requests(reject_cancellation)
    coordinator.start()
    await coordinator.drain()
    assert coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    ) is not None
    coordinator.commit_request_enqueued("request-a")
    await coordinator.drain()

    await coordinator.release_active_requests()

    assert service.finished == [("reminder-a",)]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_shutdown_from_another_event_loop_releases_in_flight_attempt() -> None:
    """跨循环关闭必须先结算在途响铃，再取消 owner worker。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    coordinator.start()
    await coordinator.drain()
    assert coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    ) is not None
    coordinator.commit_request_enqueued("request-a")
    await coordinator.drain()

    await asyncio.to_thread(lambda: asyncio.run(coordinator.shutdown()))

    assert service.finished == [("reminder-a",)]
    assert coordinator._worker is None  # noqa: SLF001 - 生命周期测试检查 owner 已释放


@pytest.mark.asyncio
async def test_user_interruption_finishes_attempt_and_continues_waiting_notice() -> None:
    """用户打断当前提醒后，等待项无需等用户说完即可继续。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    requested_ids: list[str] = []

    def request_notice() -> bool:
        request_id = f"request-{len(requested_ids) + 1}"
        batch = coordinator.freeze_ready_batch(
            request_id=request_id,
            interrupted_user=False,
            interrupted_assistant=False,
        )
        if batch is None:
            return False
        requested_ids.append(request_id)
        coordinator.commit_request_enqueued(request_id)
        return True

    coordinator.bind_request_notice(request_notice)
    coordinator.start()
    await coordinator.drain()
    service.add(_reminder("reminder-b", seconds=2))
    coordinator.wake()

    coordinator.apply_delivery(
        NoticeDeliveryCommand("request-1", "delivery_cancelled")
    )
    await asyncio.sleep(0)
    await coordinator.drain()

    assert service.finished == [("reminder-a",)]
    assert requested_ids == ["request-1", "request-2"]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_prepared_rollback_does_not_consume_ring_count() -> None:
    """入队前失败只释放 reservation，不能消耗响铃次数。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    coordinator.start()
    await coordinator.drain()

    batch = coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    )
    assert batch is not None
    assert coordinator.rollback_prepared_request("request-a") is True
    await coordinator.drain()

    assert service.enqueued == []
    assert service.finished == []
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_dismiss_cancels_intersecting_request_and_releases_remaining_items() -> None:
    """关闭冻结批次的一部分时，相关请求必须整体停播且其余项结束本次尝试。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"), _reminder("reminder-b"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    cancelled_requests: list[tuple[str, ...]] = []
    coordinator.bind_cancel_requests(cancelled_requests.append)
    coordinator.start()
    await coordinator.drain()
    batch = coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    )
    assert batch is not None
    coordinator.commit_request_enqueued("request-a")
    await coordinator.drain()

    coordinator.dismiss_reminders(("reminder-a",))
    await coordinator.drain()

    assert cancelled_requests == [("request-a",)]
    assert service.finished == [("reminder-b",)]
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_cross_thread_receipts_only_post_commands_to_owner_loop() -> None:
    """跨线程回执不能为了同步返回值读取事件循环独占的请求账本。"""

    service = RecordingReminderService()
    service.add(_reminder("reminder-a"))
    coordinator = TimedReminderDeliveryCoordinator(service=service)  # type: ignore[arg-type]
    coordinator.start()
    await coordinator.drain()
    batch = coordinator.freeze_ready_batch(
        request_id="request-a",
        interrupted_user=False,
        interrupted_assistant=False,
    )
    assert batch is not None
    coordinator.commit_request_enqueued("request-a")
    await coordinator.drain()

    delivery_result = await asyncio.to_thread(
        coordinator.apply_delivery,
        NoticeDeliveryCommand("request-a", "llm_output_completed"),
    )
    audio_result = await asyncio.to_thread(
        coordinator.observe_audio_finished,
        "request-a",
    )
    await coordinator.drain()

    assert delivery_result is None
    assert audio_result is None
    assert service.finished == [("reminder-a",)]
    await coordinator.shutdown()
