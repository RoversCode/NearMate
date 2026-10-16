"""定义定时提醒的运行期模型与公共快照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


TimedReminderState = Literal["scheduled", "ringing", "dismissed", "cancelled"]
TimedReminderFinishReason = Literal[
    "user_cancelled",
    "user_dismissed",
    "attempt_limit_reached",
]


@dataclass(frozen=True, slots=True)
class TimedReminder:
    """保存一条一次性定时提醒的权威运行期事实。

    Args:
        reminder_id: 当前 Backend 运行期内唯一的提醒编号。
        content: 到点时需要让当前角色提醒用户的事项。
        state: 等待、响铃中、已关闭或已取消。
        trigger_at: 归一化后的带时区绝对触发时刻。
        created_at: 提醒创建时刻。
        updated_at: 最近一次事实变更时刻。
        finished_at: 进入已关闭或已取消终态的时刻。
        ring_count: 已经成功进入助手请求队列的响铃次数。
        next_ring_at: 响铃中任务下一次允许发起 Reminder Notice 的时刻。
        finish_reason: 任务进入终态的业务原因。
    """

    reminder_id: str
    content: str
    state: TimedReminderState
    trigger_at: datetime
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    ring_count: int = 0
    next_ring_at: datetime | None = None
    finish_reason: TimedReminderFinishReason | None = None

    def to_payload(self) -> dict[str, object]:
        """转换成 Desktop、Console 和 HTTP 共用的公开载荷。

        Returns:
            Desktop、Console、HTTP 和 Prompt 可以共同消费的 JSON 友好字典。
        """

        return {
            "reminder_id": self.reminder_id,
            "content": self.content,
            "state": self.state,
            "trigger_at": self.trigger_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "finished_at": (
                self.finished_at.isoformat()
                if self.finished_at is not None
                else None
            ),
            "ring_count": self.ring_count,
            "next_ring_at": (
                self.next_ring_at.isoformat()
                if self.next_ring_at is not None
                else None
            ),
            "finish_reason": self.finish_reason,
            "can_modify": self.state == "scheduled",
            "can_cancel": self.state == "scheduled",
            "can_dismiss": self.state == "ringing",
        }


@dataclass(frozen=True, slots=True)
class TimedReminderSnapshot:
    """保存一个版本下完整且自洽的定时提醒列表。

    Args:
        runtime_id: 当前 Backend 进程的唯一编号。
        revision: 当前进程内严格递增的快照版本。
        active: 等待或响铃中的提醒，按触发时间排序。
        recent: 最近进入终态的提醒，按结束时间倒序排列。
    """

    runtime_id: str
    revision: int
    active: tuple[TimedReminder, ...]
    recent: tuple[TimedReminder, ...]

    def to_payload(self) -> dict[str, object]:
        """转换成 HTTP 与控制 WebSocket 共用的完整快照。

        Returns:
            包含运行编号、版本和两个状态分区的字典。
        """

        return {
            "runtime_id": self.runtime_id,
            "revision": self.revision,
            "active": [reminder.to_payload() for reminder in self.active],
            "recent": [reminder.to_payload() for reminder in self.recent],
        }


@dataclass(frozen=True, slots=True)
class TimedReminderMutation:
    """保存创建或修改后的提醒，以及 Foreground 必须说明的事实。

    Args:
        reminder: 已写入权威仓储的提醒。
        crosses_local_day: 触发时刻是否跨越创建或修改时的本地自然日。
    """

    reminder: TimedReminder
    crosses_local_day: bool

    def to_payload(self) -> dict[str, object]:
        """转换成工具与 HTTP 可直接返回的结构化事实。

        Returns:
            提醒详情、跨日标记和运行期限制。
        """

        return {
            "reminder": self.reminder.to_payload(),
            "crosses_local_day": self.crosses_local_day,
            "runtime_only": True,
        }
