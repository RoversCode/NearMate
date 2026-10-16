"""在 Backend 主进程内保存定时提醒并维护生命周期不变量。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from companion.timed_reminders.models import TimedReminder, TimedReminderSnapshot


MAX_ACTIVE_TIMED_REMINDERS = 5
MAX_RECENT_TIMED_REMINDERS = 20
MAX_TIMED_REMINDER_CONTENT_LENGTH = 500
MAX_TIMED_REMINDER_RING_COUNT = 3
TIMED_REMINDER_RING_INTERVAL = timedelta(minutes=1)


class TimedReminderError(ValueError):
    """表示调用方提交的提醒操作无法改变权威状态。"""


class TimedReminderNotFound(TimedReminderError):
    """表示提醒编号不在当前运行期记录中。"""


class TimedReminderStateConflict(TimedReminderError):
    """表示提醒当前状态不允许所请求的动作。"""


class TimedReminderCapacityReached(TimedReminderError):
    """表示当前已经达到五条活跃提醒上限。"""


class DuplicateTimedReminder(TimedReminderError):
    """表示相同规范化内容和触发时刻已经存在。"""


class InvalidTimedReminder(TimedReminderError):
    """表示内容或触发时间违反提醒输入合同。"""


class TimedReminderRuntimeConflict(TimedReminderError):
    """表示批次关闭请求来自已经失效的 Backend 运行期。"""


class TimedReminderRepository:
    """维护当前 Backend 运行期内的提醒记录与快照版本。

    Args:
        runtime_id: 当前 Backend 进程的唯一编号。
        max_active: 同时允许存在的等待中或提醒中记录数。
        max_recent: 保留的最近终态记录数。
        reminder_id_factory: 生成提醒编号的函数。
    """

    def __init__(
        self,
        *,
        runtime_id: str,
        max_active: int = MAX_ACTIVE_TIMED_REMINDERS,
        max_recent: int = MAX_RECENT_TIMED_REMINDERS,
        reminder_id_factory=lambda: str(uuid4()),
    ) -> None:
        if not runtime_id.strip():
            raise ValueError("定时提醒 runtime_id 不能为空")
        if max_active <= 0 or max_recent < 0:
            raise ValueError("定时提醒容量必须是合法正数")
        self._runtime_id = runtime_id.strip()
        self._max_active = max_active
        self._max_recent = max_recent
        self._reminder_id_factory = reminder_id_factory
        self._records: dict[str, TimedReminder] = {}
        self._terminal_order: list[str] = []
        self._revision = 0
        self._lock = RLock()

    def create(
        self,
        *,
        content: str,
        trigger_at: datetime,
        now: datetime,
    ) -> TimedReminder:
        """创建一条尚未到点的一次性提醒。

        Args:
            content: 用户希望到点获知的事项。
            trigger_at: 已解析的绝对触发时刻。
            now: 本次写事务采用的权威当前时刻。

        Returns:
            已写入仓储的提醒。

        Raises:
            InvalidTimedReminder: 内容为空、过长或触发时刻不在未来。
            TimedReminderCapacityReached: 当前已有五条活跃提醒。
            DuplicateTimedReminder: 同内容同一触发时刻已经存在。
        """

        normalized_content = _validate_content(content)
        normalized_now = _require_aware_datetime(now, field_name="now")
        normalized_trigger_at = _validate_future_trigger(trigger_at, normalized_now)
        with self._lock:
            if len(self._active_records_locked()) >= self._max_active:
                raise TimedReminderCapacityReached("当前最多同时保留五条定时提醒")
            self._reject_duplicate_locked(normalized_content, normalized_trigger_at)
            reminder_id = str(self._reminder_id_factory()).strip()
            if not reminder_id or reminder_id in self._records:
                raise InvalidTimedReminder("无法生成唯一的定时提醒编号")
            reminder = TimedReminder(
                reminder_id=reminder_id,
                content=normalized_content,
                state="scheduled",
                trigger_at=normalized_trigger_at,
                created_at=normalized_now,
                updated_at=normalized_now,
            )
            self._records[reminder_id] = reminder
            self._revision += 1
            return reminder

    def update(
        self,
        reminder_id: str,
        *,
        content: str | None,
        trigger_at: datetime | None,
        now: datetime,
    ) -> TimedReminder:
        """修改一条仍在等待触发的提醒。

        Args:
            reminder_id: 需要修改的提醒编号。
            content: 新事项；为空时保留原内容。
            trigger_at: 新触发时刻；为空时保留原时刻。
            now: 本次写事务采用的权威当前时刻。

        Returns:
            修改后的权威提醒。

        Raises:
            TimedReminderNotFound: 当前运行期找不到编号。
            TimedReminderStateConflict: 提醒已经开始表达或进入终态。
            InvalidTimedReminder: 没有变更字段或新字段不合法。
            DuplicateTimedReminder: 修改结果与另一活跃提醒精确重复。
        """

        normalized_id = reminder_id.strip()
        normalized_now = _require_aware_datetime(now, field_name="now")
        if content is None and trigger_at is None:
            raise InvalidTimedReminder("修改提醒时至少提供内容或触发时间")
        with self._lock:
            current = self._records.get(normalized_id)
            if current is None:
                raise TimedReminderNotFound("定时提醒不存在")
            if current.state != "scheduled":
                raise TimedReminderStateConflict("只有等待中的提醒可以修改")
            next_content = _validate_content(content) if content is not None else current.content
            next_trigger_at = (
                _validate_future_trigger(trigger_at, normalized_now)
                if trigger_at is not None
                else current.trigger_at
            )
            self._reject_duplicate_locked(
                next_content,
                next_trigger_at,
                excluded_reminder_id=normalized_id,
            )
            updated = replace(
                current,
                content=next_content,
                trigger_at=next_trigger_at,
                updated_at=normalized_now,
            )
            self._records[normalized_id] = updated
            self._revision += 1
            return updated

    def cancel(self, reminder_id: str, *, now: datetime) -> TimedReminder:
        """取消一条尚未到点的提醒。

        Args:
            reminder_id: 需要取消的提醒编号。
            now: 本次写事务采用的权威当前时刻。

        Returns:
            已取消提醒。

        Raises:
            TimedReminderNotFound: 当前运行期找不到编号。
            TimedReminderStateConflict: 提醒已经响铃或进入终态。
        """

        normalized_now = _require_aware_datetime(now, field_name="now")
        with self._lock:
            current = self._records.get(reminder_id.strip())
            if current is None:
                raise TimedReminderNotFound("定时提醒不存在")
            if current.state != "scheduled":
                raise TimedReminderStateConflict("只有等待中的提醒可以取消")
            cancelled = replace(
                current,
                state="cancelled",
                updated_at=normalized_now,
                finished_at=normalized_now,
                next_ring_at=None,
                finish_reason="user_cancelled",
            )
            self._records[current.reminder_id] = cancelled
            self._append_terminal_locked(current.reminder_id)
            self._revision += 1
            return cancelled

    def activate_due(self, *, now: datetime) -> tuple[TimedReminder, ...]:
        """把所有已经到点的等待任务推进到响铃中。

        Args:
            now: 调度器本次检查采用的权威当前时刻。

        Returns:
            按原触发时刻排序并已进入响铃中状态的记录。
        """

        normalized_now = _require_aware_datetime(now, field_name="now")
        with self._lock:
            due = [
                reminder
                for reminder in self._records.values()
                if reminder.state == "scheduled"
                and reminder.trigger_at <= normalized_now
            ]
            due.sort(key=lambda reminder: (reminder.trigger_at, reminder.created_at))
            if not due:
                return ()
            activated: list[TimedReminder] = []
            for reminder in due:
                next_reminder = replace(
                    reminder,
                    state="ringing",
                    updated_at=normalized_now,
                    next_ring_at=normalized_now,
                )
                self._records[reminder.reminder_id] = next_reminder
                activated.append(next_reminder)
            self._revision += 1
            return tuple(activated)

    def ready_to_ring(self, *, now: datetime) -> tuple[TimedReminder, ...]:
        """读取当前已经允许发起下一次 Notice 的响铃任务。

        Args:
            now: 本次读取采用的权威当前时刻。

        Returns:
            按原触发时刻排序、尚未被某条 Notice 占用的任务。
        """

        normalized_now = _require_aware_datetime(now, field_name="now")
        with self._lock:
            ready = [
                reminder
                for reminder in self._records.values()
                if reminder.state == "ringing"
                and reminder.next_ring_at is not None
                and reminder.next_ring_at <= normalized_now
            ]
            ready.sort(key=lambda reminder: (reminder.trigger_at, reminder.created_at))
            return tuple(ready)

    def mark_ring_enqueued(
        self,
        reminder_ids: tuple[str, ...],
        *,
        now: datetime,
    ) -> tuple[TimedReminder, ...]:
        """在 Reminder Notice 成功入队后原子消耗一次响铃额度。

        Args:
            reminder_ids: 本次冻结 Notice 包含的提醒编号。
            now: 请求成功进入助手队列的权威时刻。

        Returns:
            实际绑定到该 Notice 且响铃次数已经增加的记录。
        """

        normalized_now = _require_aware_datetime(now, field_name="now")
        with self._lock:
            enqueued: list[TimedReminder] = []
            for reminder_id in reminder_ids:
                current = self._records.get(reminder_id.strip())
                if (
                    current is None
                    or current.state != "ringing"
                    or current.next_ring_at is None
                    or current.next_ring_at > normalized_now
                ):
                    continue
                next_reminder = replace(
                    current,
                    updated_at=normalized_now,
                    ring_count=current.ring_count + 1,
                    next_ring_at=None,
                )
                self._records[current.reminder_id] = next_reminder
                enqueued.append(next_reminder)
            if enqueued:
                self._revision += 1
            return tuple(enqueued)

    def finish_ring_attempt(
        self,
        reminder_ids: tuple[str, ...],
        *,
        now: datetime,
        ring_interval: timedelta = TIMED_REMINDER_RING_INTERVAL,
        max_ring_count: int = MAX_TIMED_REMINDER_RING_COUNT,
    ) -> tuple[TimedReminder, ...]:
        """结束一条 Reminder Notice，并重排或自动关闭参与任务。

        Args:
            reminder_ids: 当前 Notice 冻结的提醒编号。
            now: 本批提醒满足结算条件的时刻；前两次为正文结束，第三次为播完，中断则立即结束。
            ring_interval: 未达到上限时，下一次响铃前等待的时间。
            max_ring_count: 自动结束前最多允许成功入队的次数。

        Returns:
            实际完成本次响铃结算的记录。
        """

        normalized_now = _require_aware_datetime(now, field_name="now")
        if ring_interval.total_seconds() <= 0 or max_ring_count <= 0:
            raise ValueError("响铃间隔和次数上限必须大于零")
        with self._lock:
            settled: list[TimedReminder] = []
            for reminder_id in reminder_ids:
                current = self._records.get(reminder_id.strip())
                if (
                    current is None
                    or current.state != "ringing"
                    or current.next_ring_at is not None
                ):
                    continue
                if current.ring_count >= max_ring_count:
                    next_reminder = replace(
                        current,
                        state="dismissed",
                        updated_at=normalized_now,
                        finished_at=normalized_now,
                        finish_reason="attempt_limit_reached",
                    )
                    self._append_terminal_locked(current.reminder_id)
                else:
                    next_reminder = replace(
                        current,
                        updated_at=normalized_now,
                        next_ring_at=normalized_now + ring_interval,
                    )
                self._records[current.reminder_id] = next_reminder
                settled.append(next_reminder)
            if settled:
                self._revision += 1
            return tuple(settled)

    def dismiss(
        self,
        *,
        runtime_id: str,
        reminder_ids: tuple[str, ...],
        now: datetime,
    ) -> tuple[TimedReminder, ...]:
        """原子关闭界面或 Foreground 当前看见的一批响铃任务。

        Args:
            runtime_id: 调用方冻结批次所属的 Backend 运行编号。
            reminder_ids: 当前气泡或 Prompt 明确列出的提醒编号。
            now: 本次关闭事务采用的权威当前时刻。

        Returns:
            本次实际从响铃中进入已关闭终态的任务。

        Raises:
            TimedReminderRuntimeConflict: 调用方仍持有旧 Backend 的批次。
            InvalidTimedReminder: 编号集合为空、重复或超过活跃容量。
        """

        normalized_now = _require_aware_datetime(now, field_name="now")
        normalized_ids = _validate_batch_ids(reminder_ids, max_count=self._max_active)
        with self._lock:
            if runtime_id.strip() != self._runtime_id:
                raise TimedReminderRuntimeConflict("定时提醒运行期已经变化")
            dismissed: list[TimedReminder] = []
            for reminder_id in normalized_ids:
                current = self._records.get(reminder_id)
                if current is None or current.state != "ringing":
                    continue
                next_reminder = replace(
                    current,
                    state="dismissed",
                    updated_at=normalized_now,
                    finished_at=normalized_now,
                    next_ring_at=None,
                    finish_reason="user_dismissed",
                )
                self._records[current.reminder_id] = next_reminder
                self._append_terminal_locked(current.reminder_id)
                dismissed.append(next_reminder)
            if dismissed:
                self._revision += 1
            return tuple(dismissed)

    def snapshot(self) -> TimedReminderSnapshot:
        """读取当前完整权威快照。

        Returns:
            活跃项与最近终态项组成的不可变快照。
        """

        with self._lock:
            active = self._active_records_locked()
            active.sort(key=lambda reminder: (reminder.trigger_at, reminder.created_at))
            recent = [
                self._records[reminder_id]
                for reminder_id in reversed(self._terminal_order)
                if reminder_id in self._records
            ]
            return TimedReminderSnapshot(
                runtime_id=self._runtime_id,
                revision=self._revision,
                active=tuple(active),
                recent=tuple(recent),
            )

    def next_wake_at(self, checked_at: datetime) -> datetime | None:
        """跳过已经交给交付者的待播项，读取下一次时间检查的时刻。

        Args:
            checked_at: 本轮激活和待播检查开始前的带时区时间。

        Returns:
            等待项触发时刻与尚未检查到的重响时刻中的最早值。
        """

        normalized_checked_at = _require_aware_datetime(checked_at, field_name="checked_at")
        with self._lock:
            candidates = []
            for reminder in self._records.values():
                if reminder.state == "scheduled":
                    candidates.append(reminder.trigger_at)
                elif (
                    reminder.state == "ringing"
                    and reminder.next_ring_at is not None
                    and reminder.next_ring_at > normalized_checked_at
                ):
                    candidates.append(reminder.next_ring_at)
            return min(candidates) if candidates else None

    def _active_records_locked(self) -> list[TimedReminder]:
        """返回锁内当前活跃记录副本。

        Returns:
            等待中和提醒中的记录列表。
        """

        return [
            reminder
            for reminder in self._records.values()
            if reminder.state in {"scheduled", "ringing"}
        ]

    def _reject_duplicate_locked(
        self,
        content: str,
        trigger_at: datetime,
        *,
        excluded_reminder_id: str = "",
    ) -> None:
        """拒绝与另一条活跃记录内容和时刻都相同的输入。

        Args:
            content: 已通过基本校验的提醒内容。
            trigger_at: 已归一化成 UTC 的触发时刻。
            excluded_reminder_id: 修改时需要排除的当前记录编号。

        Raises:
            DuplicateTimedReminder: 找到精确重复项。
        """

        normalized_content = _duplicate_key(content)
        for reminder in self._records.values():
            if (
                reminder.reminder_id != excluded_reminder_id
                and reminder.state in {"scheduled", "ringing"}
                and _duplicate_key(reminder.content) == normalized_content
                and reminder.trigger_at == trigger_at
            ):
                raise DuplicateTimedReminder("相同事项和触发时间的提醒已经存在")

    def _append_terminal_locked(self, reminder_id: str) -> None:
        """记录终态顺序并裁剪超过上限的旧记录。

        Args:
            reminder_id: 刚刚首次进入终态的提醒编号。
        """

        self._terminal_order.append(reminder_id)
        while len(self._terminal_order) > self._max_recent:
            expired_id = self._terminal_order.pop(0)
            self._records.pop(expired_id, None)


def _validate_content(content: str) -> str:
    """清理并校验提醒事项正文。

    Args:
        content: 尚未信任的提醒事项。

    Returns:
        去除首尾空白但保留用户内部排版的正文。

    Raises:
        InvalidTimedReminder: 内容不是非空字符串或超过五百字符。
    """

    normalized = content.strip() if isinstance(content, str) else ""
    if not normalized:
        raise InvalidTimedReminder("定时提醒内容不能为空")
    if len(normalized) > MAX_TIMED_REMINDER_CONTENT_LENGTH:
        raise InvalidTimedReminder("定时提醒内容不能超过五百个字符")
    return normalized


def _validate_batch_ids(
    reminder_ids: tuple[str, ...],
    *,
    max_count: int,
) -> tuple[str, ...]:
    """校验批次关闭使用的提醒编号集合。

    Args:
        reminder_ids: 调用方提交的原始编号。
        max_count: 当前运行期允许的最大活跃提醒数量。

    Returns:
        已去除首尾空白且保持原顺序的编号。

    Raises:
        InvalidTimedReminder: 集合为空、包含空编号、重复或超过容量。
    """

    normalized = tuple(
        reminder_id.strip() if isinstance(reminder_id, str) else ""
        for reminder_id in reminder_ids
    )
    if not normalized or len(normalized) > max_count:
        raise InvalidTimedReminder("关闭的定时提醒数量必须在一到五条之间")
    if any(not reminder_id for reminder_id in normalized):
        raise InvalidTimedReminder("关闭的定时提醒编号不能为空")
    if len(set(normalized)) != len(normalized):
        raise InvalidTimedReminder("关闭的定时提醒编号不能重复")
    return normalized


def _validate_future_trigger(trigger_at: datetime, now: datetime) -> datetime:
    """把触发时刻归一化成 UTC 并确认它仍在未来。

    Args:
        trigger_at: 调用方给出的绝对时刻。
        now: 同一事务采用的权威当前时刻。

    Returns:
        带 UTC 时区的未来时刻。

    Raises:
        InvalidTimedReminder: 时间没有时区或已经到达。
    """

    normalized = _require_aware_datetime(trigger_at, field_name="trigger_at")
    if normalized <= now:
        raise InvalidTimedReminder("定时提醒触发时间必须晚于当前时间")
    return normalized


def _require_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    """校验并把一个带时区时间转换成 UTC。

    Args:
        value: 需要校验的时间。
        field_name: 错误消息中使用的字段名。

    Returns:
        等价的 UTC 时间。

    Raises:
        InvalidTimedReminder: 输入不是带时区的 datetime。
    """

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidTimedReminder(f"{field_name} 必须包含明确时区")
    return value.astimezone(UTC)


def _duplicate_key(content: str) -> str:
    """生成只用于精确重复保护的稳定内容键。

    Args:
        content: 已通过基本校验的提醒内容。

    Returns:
        已完成首尾清理、但保留大小写和内部空白的原始正文。
    """

    return content
