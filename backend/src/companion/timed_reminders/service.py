"""编排定时提醒写事务、公共快照和 Foreground 只读事实。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from companion.timed_reminders.models import (
    TimedReminder,
    TimedReminderMutation,
    TimedReminderSnapshot,
)
from companion.observability.logging_config import get_backend_logger
from companion.timed_reminders.repository import TimedReminderRepository


logger = get_backend_logger(__name__)


class TimedReminderService:
    """提供提醒领域唯一的应用访问入口。

    Args:
        repository: 保存当前 Backend 运行期权威记录的仓储。
        broadcast_snapshot: 向控制连接广播完整提醒快照的异步端口。
        now: 返回带本地时区当前时间的时钟。
    """

    def __init__(
        self,
        *,
        repository: TimedReminderRepository,
        broadcast_snapshot: Callable[[dict[str, object]], Awaitable[None]],
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._repository = repository
        self._broadcast_snapshot = broadcast_snapshot
        self._now = now
        self._schedule_changed: Callable[[], None] | None = None
        self._ringing_dismissed: Callable[[tuple[str, ...]], None] | None = None
        self._transition_cleanup: set[str] | None = None

    def bind_schedule_changed(self, callback: Callable[[], None]) -> None:
        """绑定调度器唤醒端口。

        Args:
            callback: 创建、修改、取消或重排后唤醒调度器的函数。
        """

        self._schedule_changed = callback

    def bind_ringing_dismissed(
        self,
        callback: Callable[[tuple[str, ...]], None],
    ) -> None:
        """绑定响铃批次被关闭后的请求与音频清理端口。

        Args:
            callback: 接收实际关闭的提醒编号并撤销相关 Reminder Notice 的函数。
        """

        self._ringing_dismissed = callback

    async def create(
        self,
        *,
        content: str,
        trigger_at: datetime | None = None,
        delay_seconds: int | None = None,
    ) -> TimedReminderMutation:
        """按绝对时刻或相对秒数创建一次性提醒。

        Args:
            content: 到点需要提醒的事项。
            trigger_at: 带时区绝对触发时刻。
            delay_seconds: 从主进程当前时刻开始计算的正整数秒数。

        Returns:
            已创建提醒与跨自然日事实。
        """

        current = self._now()
        resolved_trigger = _resolve_trigger_at(
            now=current,
            trigger_at=trigger_at,
            delay_seconds=delay_seconds,
        )
        reminder = self._repository.create(
            content=content,
            trigger_at=resolved_trigger,
            now=current,
        )
        await self._publish_changed()
        return TimedReminderMutation(
            reminder=reminder,
            crosses_local_day=_crosses_local_day(current, resolved_trigger),
        )

    async def update(
        self,
        reminder_id: str,
        *,
        content: str | None = None,
        trigger_at: datetime | None = None,
        delay_seconds: int | None = None,
    ) -> TimedReminderMutation:
        """修改一条等待中的提醒。

        Args:
            reminder_id: 当前快照中的提醒编号。
            content: 可选的新事项。
            trigger_at: 可选的新绝对时刻。
            delay_seconds: 可选的相对秒数。

        Returns:
            修改后的提醒与跨自然日事实。
        """

        current = self._now()
        resolved_trigger = (
            _resolve_trigger_at(
                now=current,
                trigger_at=trigger_at,
                delay_seconds=delay_seconds,
            )
            if trigger_at is not None or delay_seconds is not None
            else None
        )
        reminder = self._repository.update(
            reminder_id,
            content=content,
            trigger_at=resolved_trigger,
            now=current,
        )
        await self._publish_changed()
        return TimedReminderMutation(
            reminder=reminder,
            crosses_local_day=_crosses_local_day(current, reminder.trigger_at),
        )

    async def cancel(self, reminder_id: str) -> TimedReminder:
        """取消一条活跃提醒并广播新快照。

        Args:
            reminder_id: 当前运行期提醒编号。

        Returns:
            已进入取消终态的提醒。
        """

        reminder = self._repository.cancel(reminder_id, now=self._now())
        await self._publish_changed()
        return reminder

    async def activate_due(self) -> tuple[TimedReminder, ...]:
        """把当前已经到点的等待任务推进为响铃中。

        Returns:
            首次进入响铃中状态的有序记录。
        """

        reminders = self._repository.activate_due(now=self._now())
        if reminders:
            await self._publish_changed(notify_scheduler=False)
        return reminders

    def ready_to_ring(self) -> tuple[TimedReminder, ...]:
        """读取当前可以进入下一条 Reminder Notice 的任务。

        Returns:
            已到达首次或再次响铃时刻、且没有被其它 Notice 占用的任务。
        """

        return self._repository.ready_to_ring(now=self._now())

    async def mark_ring_enqueued(
        self,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """在 Reminder Notice 成功入队后消耗本批次一次响铃额度。

        Args:
            reminder_ids: 本次 Notice 冻结的提醒编号。

        Returns:
            实际绑定到请求且响铃次数已经增加的任务。
        """

        reminders = self._repository.mark_ring_enqueued(reminder_ids, now=self._now())
        if reminders:
            await self._publish_changed()
        return reminders

    async def finish_ring_attempt(
        self,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """重排或关闭已经满足本次结束条件的提醒子集。

        Args:
            reminder_ids: 协调器按各项响铃次数和播放状态选出的待结算编号。

        Returns:
            实际完成本次响铃结算的任务。
        """

        reminders = self._repository.finish_ring_attempt(reminder_ids, now=self._now())
        if reminders:
            await self._publish_changed()
        return reminders

    async def dismiss(
        self,
        *,
        runtime_id: str,
        reminder_ids: tuple[str, ...],
    ) -> tuple[TimedReminder, ...]:
        """关闭调用方当前看见的一批响铃任务并停止相关播音。

        Args:
            runtime_id: 调用方快照所属的 Backend 运行编号。
            reminder_ids: 当前气泡或 Prompt 明确展示的提醒编号。

        Returns:
            本次实际进入已关闭终态的任务；重复关闭时可以为空。
        """

        reminders = self._repository.dismiss(
            runtime_id=runtime_id,
            reminder_ids=reminder_ids,
            now=self._now(),
        )
        dismissed_ids = tuple(reminder.reminder_id for reminder in reminders)
        if dismissed_ids and self._ringing_dismissed is not None:
            try:
                self._ringing_dismissed(dismissed_ids)
            except Exception:
                logger.exception(
                    "定时提醒关闭后的请求清理回调失败 reminder_ids=%s",
                    ",".join(dismissed_ids),
                )
        # 即使本次交集为空也广播当前快照：这通常表示调用方丢过上一帧关闭结果，
        # 幂等重试必须给它一次用权威状态收起气泡的机会。
        await self._publish_changed()
        return reminders

    async def clear_for_runtime_transition(self) -> None:
        """替换运行时前逐项取消等待提醒并关闭响铃提醒，失败项留到账本重试。

        Returns:
            无返回值；所有当前提醒均收口后才清空清理账本。
        """
        snapshot = self.snapshot()
        if self._transition_cleanup is None:
            self._transition_cleanup = {
                reminder.reminder_id for reminder in snapshot.active
            }
        first_error: Exception | None = None
        for reminder_id in tuple(self._transition_cleanup):
            try:
                current = self.snapshot()
                reminder = next(
                    (item for item in current.active if item.reminder_id == reminder_id),
                    None,
                )
                if reminder is None:
                    self._transition_cleanup.discard(reminder_id)
                    continue
                if reminder.state == "scheduled":
                    await self.cancel(reminder_id)
                elif reminder.state == "ringing":
                    await self.dismiss(
                        runtime_id=current.runtime_id,
                        reminder_ids=(reminder_id,),
                    )
                self._transition_cleanup.discard(reminder_id)
            except Exception as exception:
                first_error = first_error or exception
        if not self._transition_cleanup:
            self._transition_cleanup = None
        if first_error is not None:
            raise first_error

    def snapshot(self) -> TimedReminderSnapshot:
        """读取 HTTP 和控制连接使用的完整权威快照。

        Returns:
            当前运行编号、版本、活跃项和最近终态项。
        """

        return self._repository.snapshot()

    def snapshot_event(self) -> dict[str, object]:
        """构造控制 WebSocket 使用的完整快照事件。

        Returns:
            `reminders.snapshot_changed` 公共事件。
        """

        return {
            "type": "reminders.snapshot_changed",
            "payload": self.snapshot().to_payload(),
        }

    def foreground_snapshot(self) -> list[dict[str, object]]:
        """读取可直接注入 Foreground Prompt 的当前提醒清单。

        Returns:
            只包含等待中和响铃中项目的公开字典列表。
        """

        return [reminder.to_payload() for reminder in self.snapshot().active]

    def next_wake_at(self, checked_at: datetime) -> datetime | None:
        """读取调度器下一次应检查的绝对时刻。

        Args:
            checked_at: 本轮激活和待播检查开始前的带时区时间。

        Returns:
            等待提醒的触发时刻或晚于本轮检查时间的重响时刻。
        """

        return self._repository.next_wake_at(checked_at)

    async def _publish_changed(self, *, notify_scheduler: bool = True) -> None:
        """先唤醒调度器，再尽力异步广播最新完整快照。

        Args:
            notify_scheduler: 本次变化是否可能改变下一次调度时刻。
        """

        if notify_scheduler and self._schedule_changed is not None:
            try:
                self._schedule_changed()
            except Exception:
                # 领域写入已经成立，调度唤醒失败只能降级记录。把异常回给调用方会让
                # 用户误以为写入失败并重试，从而制造重复操作或状态冲突。
                logger.exception("定时提醒调度唤醒失败")
        event = self.snapshot_event()
        asyncio.create_task(self._broadcast_snapshot_safely(event))
        # 让无等待的测试发布器和快速连接尽快完成，但绝不等待慢连接。
        await asyncio.sleep(0)

    async def _broadcast_snapshot_safely(self, event: dict[str, object]) -> None:
        """尽力广播快照，连接异常不能影响提醒状态机。"""

        try:
            await self._broadcast_snapshot(event)
        except Exception:
            logger.exception("定时提醒快照广播失败")


def _resolve_trigger_at(
    *,
    now: datetime,
    trigger_at: datetime | None,
    delay_seconds: int | None,
) -> datetime:
    """把两种创建时间输入收敛成一个绝对 UTC 时刻。

    Args:
        now: Backend 主进程当前时间。
        trigger_at: 调用方给出的绝对时刻。
        delay_seconds: 调用方给出的相对秒数。

    Returns:
        带 UTC 时区的绝对触发时刻。

    Raises:
        InvalidTimedReminder: 时间输入不是严格二选一或相对秒数不合法。
    """

    from companion.timed_reminders.repository import InvalidTimedReminder

    if (trigger_at is None) == (delay_seconds is None):
        raise InvalidTimedReminder("绝对触发时间和相对秒数必须且只能提供一个")
    if trigger_at is not None:
        return trigger_at
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or delay_seconds <= 0:
        raise InvalidTimedReminder("相对提醒秒数必须是正整数")
    try:
        return now.astimezone(UTC) + timedelta(seconds=delay_seconds)
    except OverflowError as exception:
        raise InvalidTimedReminder("相对提醒时间超出可支持范围") from exception


def _crosses_local_day(now: datetime, trigger_at: datetime) -> bool:
    """判断触发时刻是否跨越当前本地自然日。

    Args:
        now: 创建或修改发生时的本地当前时间。
        trigger_at: 已确定的绝对触发时刻。

    Returns:
        两个时刻按当前本地时区投影后日期不同时返回真。
    """

    # `now` 来自主进程本地时钟或测试注入，它携带的时区就是本次判断的权威本地时区。
    # 再调用无参数 astimezone 会偷偷改用宿主机时区，让相同输入在不同机器上得到不同结论。
    local_timezone = now.tzinfo
    return now.astimezone(local_timezone).date() != trigger_at.astimezone(local_timezone).date()
