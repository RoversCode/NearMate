"""镜像 Claude Code 原生 Task 工具形成的真实执行计划。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, cast

from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock

from companion.activities.work_plan import (
    AgentWorkPlan,
    AgentWorkPlanItem,
    AgentWorkPlanItemStatus,
)


_TASK_TOOL_NAMES = frozenset({"TaskCreate", "TaskUpdate", "TaskList"})
_VALID_STATUSES = frozenset({"pending", "in_progress", "completed"})
_TASK_CREATED_PATTERN = re.compile(
    r"\bTask\s+#?([A-Za-z0-9._:-]+)\s+created\b",
    re.IGNORECASE,
)
_TASK_LIST_LINE_PATTERN = re.compile(
    r"^\s*#?([A-Za-z0-9_:-]+)[.)]?\s+\[(pending|in_progress|completed)\]\s+(.+?)\s*$",
    re.IGNORECASE,
)
_TASK_LIST_ROW_PATTERN = re.compile(
    r"^\s*#?[A-Za-z0-9_:-]+[.)]?\s+\[[^\]]+\]\s+.*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _PendingTaskOperation:
    """保存尚未收到成功结果的 Task 工具修改意图。"""

    tool_name: str
    tool_input: dict[str, Any]
    sequence: int
    base_revision: int


class AgentWorkPlanMirror:
    """按 ToolUse/ToolResult 确认规则维护单项 Agent TodoList。"""

    def __init__(self) -> None:
        """初始化空计划、待确认操作和乱序保护计数。"""

        self._items_by_id: dict[str, AgentWorkPlanItem] = {}
        self._pending_by_tool_use_id: dict[str, _PendingTaskOperation] = {}
        self._last_sequence_by_task_id: dict[str, int] = {}
        self._operation_sequence = 0
        self._revision = 0
        self._snapshot: AgentWorkPlan | None = None

    @property
    def snapshot(self) -> AgentWorkPlan | None:
        """返回最近一次已经确认的计划快照。"""

        return self._snapshot

    def record_tool_use(self, block: ToolUseBlock) -> bool:
        """暂存一项 Claude Code Task 工具修改意图。

        Args:
            block: AssistantMessage 中的 TaskCreate、TaskUpdate 或 TaskList 调用。

        Returns:
            属于需要镜像的 Task 工具时返回真；ToolUse 本身仍不修改公开计划。
        """

        if block.name not in _TASK_TOOL_NAMES or not block.id.strip():
            return False
        if block.id in self._pending_by_tool_use_id:
            return True
        self._operation_sequence += 1
        self._pending_by_tool_use_id[block.id] = _PendingTaskOperation(
            tool_name=block.name,
            tool_input=dict(block.input),
            sequence=self._operation_sequence,
            base_revision=self._revision,
        )
        return True

    def record_tool_result(
        self,
        block: ToolResultBlock,
        *,
        tool_use_result: dict[str, Any] | None,
    ) -> AgentWorkPlan | None:
        """确认一项 Task 操作，并在计划真实变化时返回新快照。

        Args:
            block: UserMessage 中与 ToolUse 编号对应的工具结果。
            tool_use_result: SDK 同一 UserMessage 提供的结构化工具结果。

        Returns:
            本次确认产生的新计划；失败、重复、乱序或无法识别时为空。
        """

        operation = self._pending_by_tool_use_id.pop(block.tool_use_id, None)
        if operation is None or block.is_error is True:
            return None
        if operation.tool_name == "TaskCreate":
            return self._apply_create(operation, block, tool_use_result)
        if operation.tool_name == "TaskUpdate":
            return self._apply_update(operation)
        return self._apply_task_list(operation, block, tool_use_result)

    def _apply_create(
        self,
        operation: _PendingTaskOperation,
        block: ToolResultBlock,
        tool_use_result: dict[str, Any] | None,
    ) -> AgentWorkPlan | None:
        """提交一项成功 TaskCreate。

        Args:
            operation: 已确认成功的创建意图。
            block: 创建工具返回的文本结果。
            tool_use_result: 创建工具返回的结构化结果。

        Returns:
            新计划；无法取得任务编号或内容无效时为空。
        """

        task_id = _created_task_id(tool_use_result, block.content)
        subject = _clean_text(operation.tool_input.get("subject"), 500)
        active_form = _clean_text(operation.tool_input.get("activeForm"), 500) or subject
        if not task_id or not subject or task_id in self._items_by_id:
            return None
        order = max((item.order for item in self._items_by_id.values()), default=0) + 1
        self._items_by_id[task_id] = AgentWorkPlanItem(
            task_id=task_id,
            subject=subject,
            active_form=active_form,
            status="pending",
            order=order,
        )
        self._last_sequence_by_task_id[task_id] = operation.sequence
        return self._commit_snapshot()

    def _apply_update(
        self,
        operation: _PendingTaskOperation,
    ) -> AgentWorkPlan | None:
        """提交一项成功 TaskUpdate，并按操作时序隔离迟到结果。

        Args:
            operation: 已确认成功的更新意图。

        Returns:
            实际改变计划时的新快照；未知任务或迟到操作返回空。
        """

        task_id = _clean_identifier(operation.tool_input.get("taskId"))
        current = self._items_by_id.get(task_id)
        if current is None:
            return None
        if operation.sequence <= self._last_sequence_by_task_id.get(task_id, 0):
            return None
        requested_status = operation.tool_input.get("status")
        if requested_status == "deleted":
            self._items_by_id.pop(task_id, None)
            self._last_sequence_by_task_id[task_id] = operation.sequence
            self._normalize_orders()
            return self._commit_snapshot()

        next_status = (
            cast(AgentWorkPlanItemStatus, requested_status)
            if requested_status in _VALID_STATUSES
            else current.status
        )
        subject = _clean_text(operation.tool_input.get("subject"), 500) or current.subject
        active_form = (
            _clean_text(operation.tool_input.get("activeForm"), 500)
            or current.active_form
        )
        updated = replace(
            current,
            subject=subject,
            active_form=active_form,
            status=next_status,
        )
        self._last_sequence_by_task_id[task_id] = operation.sequence
        if updated == current:
            return None
        self._items_by_id[task_id] = updated
        return self._commit_snapshot()

    def _apply_task_list(
        self,
        operation: _PendingTaskOperation,
        block: ToolResultBlock,
        tool_use_result: dict[str, Any] | None,
    ) -> AgentWorkPlan | None:
        """用成功 TaskList 全量对账计划，并拒绝请求后的迟到列表。

        Args:
            operation: 已确认成功的列表读取意图。
            block: TaskList 返回的文本结果。
            tool_use_result: TaskList 返回的结构化结果。

        Returns:
            对账后实际变化的新快照；列表过期或无法解析时为空。
        """

        if operation.base_revision != self._revision:
            return None
        listed_items = _task_list_items(tool_use_result, block.content)
        if listed_items is None:
            return None
        reconciled: dict[str, AgentWorkPlanItem] = {}
        for order, listed_item in enumerate(listed_items, start=1):
            reconciled[listed_item.task_id] = replace(
                listed_item,
                order=order,
            )
            self._last_sequence_by_task_id[listed_item.task_id] = operation.sequence
        if tuple(reconciled.values()) == tuple(self._ordered_items()):
            return None
        self._items_by_id = reconciled
        return self._commit_snapshot()

    def _normalize_orders(self) -> None:
        """在删除任务后重新生成连续展示顺序。"""

        self._items_by_id = {
            item.task_id: replace(item, order=order)
            for order, item in enumerate(self._ordered_items(), start=1)
        }

    def _ordered_items(self) -> list[AgentWorkPlanItem]:
        """按稳定顺序返回当前全部任务项。"""

        return sorted(self._items_by_id.values(), key=lambda item: item.order)

    def _commit_snapshot(self) -> AgentWorkPlan:
        """提交当前计划并推进单项 Activity 的计划版本。"""

        self._revision += 1
        self._snapshot = AgentWorkPlan(
            revision=self._revision,
            items=tuple(self._ordered_items()),
        )
        return self._snapshot


def _created_task_id(
    tool_use_result: dict[str, Any] | None,
    content: str | list[dict[str, Any]] | None,
) -> str:
    """优先从结构化结果读取 TaskCreate 编号，必要时使用窄文本格式。

    Args:
        tool_use_result: SDK 结构化工具结果。
        content: ToolResultBlock 可见内容。

    Returns:
        非空任务编号；无法确认时返回空字符串。
    """

    structured_id = _identifier_from_result(tool_use_result)
    if structured_id:
        return structured_id
    match = _TASK_CREATED_PATTERN.search(_tool_result_text(content))
    return match.group(1) if match is not None else ""


def _identifier_from_result(value: object) -> str:
    """从常见结构化 Task 结果层级读取稳定编号。"""

    if not isinstance(value, dict):
        return ""
    for key in ("taskId", "task_id", "id"):
        identifier = _clean_identifier(value.get(key))
        if identifier:
            return identifier
    for key in ("task", "result", "data"):
        identifier = _identifier_from_result(value.get(key))
        if identifier:
            return identifier
    return ""


def _task_list_items(
    tool_use_result: dict[str, Any] | None,
    content: str | list[dict[str, Any]] | None,
) -> list[AgentWorkPlanItem] | None:
    """解析 TaskList 的结构化列表或受控文本列表。"""

    raw_items = _raw_task_items(tool_use_result)
    if raw_items is not None:
        parsed_items: list[AgentWorkPlanItem] = []
        task_ids: set[str] = set()
        for order, raw_item in enumerate(raw_items, start=1):
            parsed = _task_item_from_result(raw_item, order)
            if parsed is None or parsed.task_id in task_ids:
                return None
            task_ids.add(parsed.task_id)
            parsed_items.append(parsed)
        return parsed_items

    text = _tool_result_text(content).strip()
    if not text:
        return []
    if text.startswith(("[", "{")):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        decoded_items = _raw_task_items(decoded)
        if decoded_items is not None:
            parsed_items: list[AgentWorkPlanItem] = []
            task_ids: set[str] = set()
            for order, raw_item in enumerate(decoded_items, start=1):
                parsed = _task_item_from_result(raw_item, order)
                if parsed is None or parsed.task_id in task_ids:
                    return None
                task_ids.add(parsed.task_id)
                parsed_items.append(parsed)
            return parsed_items

    parsed_lines: list[AgentWorkPlanItem] = []
    task_ids: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _TASK_LIST_LINE_PATTERN.match(line)
        if match is None:
            # 普通说明行可以忽略，但看起来像任务行却解析失败时必须拒绝整个快照。
            if _TASK_LIST_ROW_PATTERN.match(line):
                return None
            continue
        task_id, status, subject = match.groups()
        if task_id in task_ids:
            return None
        task_ids.add(task_id)
        parsed_lines.append(
            AgentWorkPlanItem(
                task_id=task_id,
                subject=subject.strip(),
                active_form=subject.strip(),
                status=cast(AgentWorkPlanItemStatus, status.lower()),
                order=len(parsed_lines) + 1,
            )
        )
    return parsed_lines or None


def _raw_task_items(value: object) -> list[object] | None:
    """从结构化 TaskList 结果中定位任务数组。"""

    if isinstance(value, list):
        return list(value)
    if not isinstance(value, dict):
        return None
    for key in ("tasks", "items"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return list(candidate)
    for key in ("result", "data"):
        candidate = _raw_task_items(value.get(key))
        if candidate is not None:
            return candidate
    return None


def _task_item_from_result(value: object, order: int) -> AgentWorkPlanItem | None:
    """把一项 TaskList 结果转换成公开计划项。"""

    if not isinstance(value, dict):
        return None
    task_id = ""
    for key in ("taskId", "task_id", "id"):
        task_id = _clean_identifier(value.get(key))
        if task_id:
            break
    subject = _clean_text(value.get("subject"), 500)
    active_form = (
        _clean_text(value.get("activeForm"), 500)
        or _clean_text(value.get("active_form"), 500)
        or subject
    )
    status = value.get("status")
    if not task_id or not subject or status not in _VALID_STATUSES:
        return None
    return AgentWorkPlanItem(
        task_id=task_id,
        subject=subject,
        active_form=active_form,
        status=cast(AgentWorkPlanItemStatus, status),
        order=order,
    )


def _tool_result_text(content: str | list[dict[str, Any]] | None) -> str:
    """合并 ToolResultBlock 中可解析的文本片段。"""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _clean_identifier(value: object) -> str:
    """把 Task 编号转换成长度受控的稳定字符串。"""

    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int)):
        identifier = str(value).strip()
        return identifier if 0 < len(identifier) <= 200 else ""
    return ""


def _clean_text(value: object, limit: int) -> str:
    """读取长度受控的非空工具输入文本。"""

    text = value.strip() if isinstance(value, str) else ""
    return text if 0 < len(text) <= limit else ""
