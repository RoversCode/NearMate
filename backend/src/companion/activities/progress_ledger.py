"""维护 Backend 主进程权威的 Activity Progress 临时账本。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Literal

from companion.core.local_clock import local_wall_datetime
from companion.realtime.assistant_output_contracts import ActivityProgressReference


ActivityProgressKey = tuple[str, str]
_TERMINAL_ACTIVITY_SOURCE_BY_EVENT = {
    "cognitive_task.completed": "cognitive",
    "cognitive_task.failed": "cognitive",
    "cognitive_task.rejected": "cognitive",
    "cognitive_task.cancelled": "cognitive",
    "cognitive_task.interrupted": "cognitive",
    "background_agent.completed": "background_agent",
    "background_agent.failed": "background_agent",
    "background_agent.cancelled": "background_agent",
    "background_agent.interrupted": "background_agent",
}


@dataclass(frozen=True, slots=True)
class ActivityProgressEntrySnapshot:
    """保存一项 Activity 已经公开的 Progress 快照。

    Args:
        source: Activity 的内部来源类别。
        activity_id: Activity 编号。
        lifecycle: Progress 当前仍属于 active 动态环境还是 frozen 终态交付。
        reports: 按实际公开顺序排列的模型正文。
    """

    source: str
    activity_id: str
    lifecycle: Literal["active", "frozen"]
    reports: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        """转换为请求快照使用的普通字典。

        Returns:
            可安全跨进程传递的 Progress 快照。
        """

        return {
            "source": self.source,
            "activity_id": self.activity_id,
            "lifecycle": self.lifecycle,
            "reports": list(self.reports),
        }


@dataclass(slots=True)
class _ActivityProgressEntry:
    """保存一项 Activity 的可变 Progress 生命周期。"""

    source: str
    activity_id: str
    lifecycle: Literal["active", "frozen"] = "active"
    reports: list[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=local_wall_datetime)


class ActivityProgressLedger:
    """保存已公开 Progress，并在 Activity 终态后冻结到 Notice 收口。

    Args:
        max_activities: 账本允许同时保留的 Activity 数量。
        released_activity_keys: 进程生命周期内永久保留的已释放 Activity 身份。
    """

    def __init__(
        self,
        *,
        max_activities: int = 256,
    ) -> None:
        """初始化空的 Progress 临时账本。

        Args:
            max_activities: Activity 记录容量上限。
        Raises:
            ValueError: Activity 容量不是正整数时抛出。
        """

        if max_activities <= 0:
            raise ValueError("max_activities 必须大于零")
        self._max_activities = max_activities
        self._entries: OrderedDict[ActivityProgressKey, _ActivityProgressEntry] = (
            OrderedDict()
        )
        # Backend 运行期内 Activity 编号不会复用。永久 tombstone 才能保证极迟 settlement
        # 在账本容量变化后仍只能被吸收，不能把已经结束的 Activity 重新创建成 active。
        self._released_activity_keys: set[ActivityProgressKey] = set()
        self._lock = RLock()

    def record_visible_progress(
        self,
        *,
        visible_text: str,
        references: tuple[ActivityProgressReference, ...],
        recorded_at: datetime | None = None,
    ) -> int:
        """把真正公开的模型正文登记到关联 Activity。

        Args:
            visible_text: 已成功广播的模型正文，不包含 UI 中断尾注。
            references: 本次正文关联的 Activity 和冻结序号。
            recorded_at: 可选的真实记录时间。

        Returns:
            本次成功追加正文的 Activity 数量。
        """

        text = visible_text.strip()
        if not text or not references:
            return 0

        recorded_count = 0
        now = recorded_at or local_wall_datetime()
        with self._lock:
            unique_keys: set[ActivityProgressKey] = set()
            recordable_keys: list[ActivityProgressKey] = []
            for reference in references:
                key = (reference.source.strip(), reference.activity_id.strip())
                if (
                    not key[0]
                    or not key[1]
                    or key in unique_keys
                    or key in self._released_activity_keys
                ):
                    continue
                unique_keys.add(key)
                recordable_keys.append(key)

            missing_entry_count = sum(
                key not in self._entries for key in recordable_keys
            )
            # 先确认整批都放得下，再修改任何已有报告。否则多 Activity 的同一句公开正文
            # 可能只写入前半批，随后重试又把前半批重复追加。
            self._ensure_activity_capacity(
                required_new_entries=missing_entry_count
            )
            for key in recordable_keys:
                entry = self._entries.get(key)
                if entry is None:
                    entry = _ActivityProgressEntry(
                        source=key[0],
                        activity_id=key[1],
                        updated_at=now,
                    )
                    self._entries[key] = entry
                entry.reports.append(text)
                entry.updated_at = now
                self._entries.move_to_end(key)
                recorded_count += 1
        return recorded_count

    def freeze(self, *, source: str, activity_id: str) -> bool:
        """在 Activity 首次进入终态时冻结它的 Progress。

        Args:
            source: Activity 的内部来源类别。
            activity_id: 首次进入终态的 Activity 编号。

        Returns:
            本次创建或推进了冻结状态时返回真；重复冻结返回假。
        """

        key = (source.strip(), activity_id.strip())
        if not key[0] or not key[1]:
            return False
        with self._lock:
            if key in self._released_activity_keys:
                return False
            entry = self._entries.get(key)
            if entry is None:
                self._ensure_activity_capacity(required_new_entries=1)
                self._entries[key] = _ActivityProgressEntry(
                    source=key[0],
                    activity_id=key[1],
                    lifecycle="frozen",
                )
                return True
            if entry.lifecycle == "frozen":
                return False
            entry.lifecycle = "frozen"
            entry.updated_at = local_wall_datetime()
            self._entries.move_to_end(key)
            return True

    def freeze_from_runtime_event(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        """在权威 Activity 终态进入任务记忆前冻结对应 Progress。

        Args:
            event_type: Cognitive 或后台工作的来源终态事件名。
            payload: 包含稳定 Activity 编号的运行时载荷。

        Returns:
            当前事件是合法终态且推进了冻结状态时返回真。
        """

        source = _TERMINAL_ACTIVITY_SOURCE_BY_EVENT.get(event_type)
        raw_activity_id = payload.get("activity_id")
        activity_id = raw_activity_id.strip() if isinstance(raw_activity_id, str) else ""
        if source is None or not activity_id:
            return False
        return self.freeze(source=source, activity_id=activity_id)

    def release_frozen(self, activity_keys: tuple[ActivityProgressKey, ...]) -> int:
        """在终态 Notice 完成或明确放弃后删除 frozen Progress。

        Args:
            activity_keys: 允许删除的来源与 Activity 编号集合。

        Returns:
            本次实际删除的 frozen Activity 数量。
        """

        removed = 0
        with self._lock:
            for source, activity_id in activity_keys:
                key = (source.strip(), activity_id.strip())
                entry = self._entries.get(key)
                if entry is None or entry.lifecycle != "frozen":
                    continue
                self._entries.pop(key, None)
                self._released_activity_keys.add(key)
                removed += 1
        return removed

    def snapshot(self) -> tuple[ActivityProgressEntrySnapshot, ...]:
        """读取当前 active 与 frozen Progress 的不可变快照。

        Returns:
            按最近更新时间排序的 Activity Progress 快照。
        """

        with self._lock:
            return tuple(
                ActivityProgressEntrySnapshot(
                    source=entry.source,
                    activity_id=entry.activity_id,
                    lifecycle=entry.lifecycle,
                    reports=tuple(entry.reports),
                )
                for entry in self._entries.values()
                if entry.reports
            )

    def snapshot_payload(self) -> list[dict[str, object]]:
        """生成助手请求可冻结的 Progress 动态环境载荷。

        Returns:
            不共享账本内部列表的普通字典集合。
        """

        return [entry.to_payload() for entry in self.snapshot()]

    def active_snapshot_payload(self) -> list[dict[str, object]]:
        """生成只包含仍在运行 Activity 的动态环境载荷。

        Returns:
            已冻结终态 Progress 被排除后的独立字典列表。
        """

        return [
            entry.to_payload()
            for entry in self.snapshot()
            if entry.lifecycle == "active"
        ]

    def snapshot_payload_for(
        self,
        activity_keys: set[ActivityProgressKey],
    ) -> list[dict[str, object]]:
        """按明确 Activity 身份读取终态 Notice 需要的 Progress。

        Args:
            activity_keys: 当前 Notice 批次真实引用的来源与 Activity 编号。

        Returns:
            保持账本顺序且只包含指定 Activity 的独立字典列表。
        """

        normalized_keys = {
            (source.strip(), activity_id.strip())
            for source, activity_id in activity_keys
            if source.strip() and activity_id.strip()
        }
        return [
            entry.to_payload()
            for entry in self.snapshot()
            if (entry.source, entry.activity_id) in normalized_keys
        ]

    def snapshot_payload_for_user_request(
        self,
        notice_activity_keys: set[ActivityProgressKey],
    ) -> list[dict[str, object]]:
        """生成用户轮需要的 active Progress 与定向 frozen Progress。

        Args:
            notice_activity_keys: 本轮附着强提醒明确引用的 Activity 身份。

        Returns:
            全部 active 项，以及本轮提醒引用的 frozen 项；其它终态进度被排除。
        """

        normalized_notice_keys = {
            (source.strip(), activity_id.strip())
            for source, activity_id in notice_activity_keys
            if source.strip() and activity_id.strip()
        }
        return [
            entry.to_payload()
            for entry in self.snapshot()
            if entry.lifecycle == "active"
            or (entry.source, entry.activity_id) in normalized_notice_keys
        ]

    def reports_by_activity(
        self,
        activity_keys: set[ActivityProgressKey],
    ) -> dict[ActivityProgressKey, tuple[str, ...]]:
        """读取指定 Activity 已经公开的 Progress 正文。

        Args:
            activity_keys: 需要读取的来源与 Activity 编号集合。

        Returns:
            只包含存在非空正文的 Activity 映射。
        """

        normalized_keys = {
            (source.strip(), activity_id.strip())
            for source, activity_id in activity_keys
            if source.strip() and activity_id.strip()
        }
        with self._lock:
            return {
                key: tuple(entry.reports)
                for key in normalized_keys
                if (entry := self._entries.get(key)) is not None and entry.reports
            }

    def _ensure_activity_capacity(self, *, required_new_entries: int) -> None:
        """为整批新 Activity 预留空间，优先淘汰最旧 frozen 空记录。

        Args:
            required_new_entries: 本次事务需要创建的新 Activity 记录数量。

        Returns:
            无。容量足够时一次性完成必要淘汰。

        Raises:
            RuntimeError: 全部记录仍有实际正文，继续写入会丢失公开 Progress 时抛出。
        """

        if required_new_entries <= 0:
            return
        available_slots = self._max_activities - len(self._entries)
        required_evictions = max(0, required_new_entries - available_slots)
        if required_evictions == 0:
            return

        evictable_keys = [
            key
            for key, entry in self._entries.items()
            if entry.lifecycle == "frozen" and not entry.reports
        ][:required_evictions]
        if len(evictable_keys) < required_evictions:
            raise RuntimeError("Activity Progress 账本已达到容量上限")
        for key in evictable_keys:
            self._entries.pop(key)
