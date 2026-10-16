"""定义 Background Agent Activity 使用的公开执行计划。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast


AgentWorkPlanItemStatus = Literal["pending", "in_progress", "completed"]
_WORK_PLAN_STATUSES = frozenset({"pending", "in_progress", "completed"})


@dataclass(frozen=True, slots=True)
class AgentWorkPlanItem:
    """保存 Claude Code 自己规划的一项真实待办。

    Args:
        task_id: Claude Code Task 系统分配的稳定编号。
        subject: 完成态和待办态使用的简短任务名称。
        active_form: 执行中使用的自然动作名称。
        status: pending、in_progress 或 completed。
        order: 计划中的一位起始展示顺序。
    """

    task_id: str
    subject: str
    active_form: str
    status: AgentWorkPlanItemStatus
    order: int

    def to_payload(self) -> dict[str, object]:
        """转换为 Activity 与 Console 共用的公开载荷。

        Returns:
            不包含 Task 描述和 SDK 内部字段的待办字典。
        """

        return {
            "task_id": self.task_id,
            "subject": self.subject,
            "active_form": self.active_form,
            "status": self.status,
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class AgentWorkPlanSummary:
    """保存 Foreground 只需读取的压缩计划事实。

    Args:
        total: 当前计划总项数。
        completed: 已完成项数。
        current: 当前正在执行的动作；没有时为空。
        next: 下一项待办名称；没有时为空。
    """

    total: int
    completed: int
    current: str | None
    next: str | None

    def to_payload(self) -> dict[str, object]:
        """转换为 Foreground 动态环境载荷。

        Returns:
            只含计数和当前、下一项名称的字典。
        """

        return {
            "total": self.total,
            "completed": self.completed,
            "current": self.current,
            "next": self.next,
        }


@dataclass(frozen=True, slots=True)
class AgentWorkPlan:
    """保存一项 Background Agent Activity 的真实 TodoList 快照。

    Args:
        revision: 单项 Activity 内严格递增的计划版本。
        items: 按展示顺序排列的当前计划项。
    """

    revision: int
    items: tuple[AgentWorkPlanItem, ...]

    def to_payload(self) -> dict[str, object]:
        """转换为跨进程和 Console 共用的计划载荷。

        Returns:
            带版本和全部安全待办项的字典。
        """

        return {
            "revision": self.revision,
            "items": [item.to_payload() for item in self.items],
        }

    def summary(self) -> AgentWorkPlanSummary:
        """压缩为 Foreground 理解任务方向所需的最少事实。

        Returns:
            计划总数、完成数、当前动作和下一项名称。
        """

        current_item = next(
            (item for item in self.items if item.status == "in_progress"),
            None,
        )
        pending_items = [item for item in self.items if item.status == "pending"]
        next_item = next(
            (
                item
                for item in pending_items
                if current_item is None or item.order > current_item.order
            ),
            pending_items[0] if pending_items else None,
        )
        return AgentWorkPlanSummary(
            total=len(self.items),
            completed=sum(item.status == "completed" for item in self.items),
            current=(
                current_item.active_form or current_item.subject
                if current_item is not None
                else None
            ),
            next=next_item.subject if next_item is not None else None,
        )


def parse_agent_work_plan(value: object) -> AgentWorkPlan:
    """严格解析跨进程传回的 WorkPlan 全量快照。

    Args:
        value: `background_agent.work_plan_changed` 的 work_plan 字段。

    Returns:
        经过字段白名单、编号和顺序校验的不可变计划。

    Raises:
        ValueError: 计划或任一待办字段不符合公开契约。
    """

    if not isinstance(value, dict) or set(value) != {"revision", "items"}:
        raise ValueError("后台任务计划结构非法")
    revision = value.get("revision")
    raw_items = value.get("items")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision <= 0
        or not isinstance(raw_items, list)
        or len(raw_items) > 200
    ):
        raise ValueError("后台任务计划版本或数量非法")

    items: list[AgentWorkPlanItem] = []
    task_ids: set[str] = set()
    for raw_item in raw_items:
        item = _parse_work_plan_item(raw_item)
        if item.task_id in task_ids:
            raise ValueError("后台任务计划包含重复编号")
        task_ids.add(item.task_id)
        items.append(item)
    expected_orders = list(range(1, len(items) + 1))
    if [item.order for item in items] != expected_orders:
        raise ValueError("后台任务计划顺序非法")
    return AgentWorkPlan(revision=revision, items=tuple(items))


def _parse_work_plan_item(value: object) -> AgentWorkPlanItem:
    """解析一项跨进程待办。

    Args:
        value: 未受信任的待办字典。

    Returns:
        字段完整且长度受控的计划项。

    Raises:
        ValueError: 字段缺失、类型错误或状态不受支持。
    """

    expected_fields = {"task_id", "subject", "active_form", "status", "order"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("后台任务计划项结构非法")
    task_id = _limited_non_empty_text(value.get("task_id"), 200)
    subject = _limited_non_empty_text(value.get("subject"), 500)
    active_form = _limited_non_empty_text(value.get("active_form"), 500)
    status = value.get("status")
    order = value.get("order")
    if status not in _WORK_PLAN_STATUSES:
        raise ValueError("后台任务计划项状态非法")
    if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
        raise ValueError("后台任务计划项顺序非法")
    return AgentWorkPlanItem(
        task_id=task_id,
        subject=subject,
        active_form=active_form,
        status=cast(AgentWorkPlanItemStatus, status),
        order=order,
    )


def _limited_non_empty_text(value: object, limit: int) -> str:
    """读取一段非空且长度受控的公开文本。

    Args:
        value: 待校验字段。
        limit: 最大字符数。

    Returns:
        去除首尾空白后的文本。

    Raises:
        ValueError: 字段不是字符串、为空或超长。
    """

    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > limit:
        raise ValueError("后台任务计划文本非法")
    return text
