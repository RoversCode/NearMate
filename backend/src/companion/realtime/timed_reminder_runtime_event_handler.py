"""在主进程执行 Foreground 已提交的定时提醒写命令。"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import (
    RuntimeEvent,
    TimedReminderCommandResultCommand,
)
from companion.timed_reminders.repository import (
    DuplicateTimedReminder,
    InvalidTimedReminder,
    TimedReminderCapacityReached,
    TimedReminderNotFound,
    TimedReminderStateConflict,
    TimedReminderRuntimeConflict,
)
from companion.timed_reminders.service import TimedReminderService


logger = get_backend_logger(__name__)
TIMED_REMINDER_RUNTIME_EVENT_TYPES = frozenset(
    {"llm.foreground.timed_reminder_command_requested"}
)


class TimedReminderRuntimeEventHandler:
    """调用主进程权威服务，并把 canonical 结果送回原助手世代。

    Args:
        service: 当前 Backend 运行期唯一的定时提醒应用服务。
        result_handler: 按原始运行时世代投递结构化回执的端口。
    """

    def __init__(
        self,
        *,
        service: TimedReminderService | None,
        result_handler: Callable[[TimedReminderCommandResultCommand, int], object]
        | None,
    ) -> None:
        self._service = service
        self._result_handler = result_handler

    async def handle(self, event: RuntimeEvent) -> None:
        """执行一笔已通过 Router 世代门禁的提醒命令。

        Args:
            event: 子进程在工具提交后产生的提醒写请求。
        """

        command_id = _clean_text(event.payload.get("command_id"))
        operation = _clean_text(event.payload.get("operation"))
        arguments_value = event.payload.get("arguments")
        arguments = dict(arguments_value) if isinstance(arguments_value, dict) else {}
        result: dict[str, object]
        if not command_id or operation not in {"create", "update", "cancel", "dismiss"}:
            result = _failure(
                "invalid_timed_reminder_command",
                "这次提醒操作无效，没有改变提醒。",
            )
        elif self._service is None:
            result = _failure(
                "timed_reminder_runtime_unavailable",
                "当前无法管理提醒，提醒没有改变。",
            )
        else:
            result = await self._execute(operation, arguments)

        if event.runtime_epoch is None or self._result_handler is None:
            logger.warning(
                "定时提醒命令回执无法投递 command_id=%s operation=%s",
                command_id,
                operation,
            )
            return
        command = TimedReminderCommandResultCommand(
            command_id=command_id,
            operation=operation if operation in {"create", "update", "cancel", "dismiss"} else "create",
            result=result,
            issued_at=time.monotonic(),
        )
        try:
            self._result_handler(command, event.runtime_epoch)
        except Exception:
            logger.warning(
                "定时提醒命令回执投递失败 command_id=%s runtime_epoch=%s",
                command_id,
                event.runtime_epoch,
                exc_info=True,
            )

    async def _execute(
        self,
        operation: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        """把已收窄命令映射到权威服务方法。

        Args:
            operation: 创建、修改或取消动作。
            arguments: 跨进程传入且需要再次验证的参数。

        Returns:
            canonical 成功事实，或不泄漏异常细节的稳定失败事实。
        """

        service = self._service
        if service is None:
            return _failure("timed_reminder_runtime_unavailable", "当前无法管理提醒。")
        try:
            if operation == "create":
                mutation = await service.create(
                    content=_required_text(arguments, "content"),
                    trigger_at=_optional_datetime(arguments, "trigger_at"),
                    delay_seconds=_optional_positive_int(arguments, "delay_seconds"),
                )
                return {"state": "created", **mutation.to_payload()}
            if operation == "update":
                reminder_id = _required_text(arguments, "reminder_id")
                mutation = await service.update(
                    reminder_id,
                    content=_optional_text(arguments, "content"),
                    trigger_at=_optional_datetime(arguments, "trigger_at"),
                    delay_seconds=_optional_positive_int(arguments, "delay_seconds"),
                )
                return {"state": "updated", **mutation.to_payload()}
            if operation == "cancel":
                reminder_id = _required_text(arguments, "reminder_id")
                reminder = await service.cancel(reminder_id)
                return {
                    "state": "cancelled",
                    "reminder": reminder.to_payload(),
                    "crosses_local_day": False,
                    "runtime_only": True,
                }
            runtime_id = _required_text(arguments, "runtime_id")
            reminder_ids = _required_string_list(arguments, "reminder_ids")
            dismissed = await service.dismiss(
                runtime_id=runtime_id,
                reminder_ids=tuple(reminder_ids),
            )
            return {
                "state": "dismissed",
                "dismissed_ids": [item.reminder_id for item in dismissed],
                "dismissed_contents": [item.content for item in dismissed],
                "runtime_only": True,
            }
        except (
            InvalidTimedReminder,
            DuplicateTimedReminder,
            TimedReminderCapacityReached,
            TimedReminderNotFound,
            TimedReminderStateConflict,
            TimedReminderRuntimeConflict,
        ) as exception:
            return _domain_failure(exception)
        except Exception:
            logger.exception("定时提醒命令执行失败 operation=%s", operation)
            return _failure(
                "timed_reminder_command_failed",
                "这次提醒操作没有成功，请查看当前提醒。",
            )


def _domain_failure(exception: Exception) -> dict[str, object]:
    """把提醒领域竞态映射成稳定且可向用户解释的事实。

    Args:
        exception: 权威服务抛出的公开领域错误。

    Returns:
        不会虚构成功的拒绝结果。
    """

    if isinstance(exception, TimedReminderNotFound):
        return _failure("timed_reminder_not_found", "这条提醒已经不存在。")
    if isinstance(exception, TimedReminderStateConflict):
        return _failure(
            "timed_reminder_state_conflict",
            "这条提醒的状态已经变化，当前操作没有执行。",
        )
    if isinstance(exception, DuplicateTimedReminder):
        return _failure(
            "duplicate_timed_reminder",
            "相同事项和时间的提醒已经存在，没有重复创建。",
        )
    if isinstance(exception, TimedReminderCapacityReached):
        return _failure(
            "timed_reminder_capacity_reached",
            "当前最多只能保留五条提醒，请先取消一条。",
        )
    return _failure("invalid_timed_reminder", str(exception))


def _required_text(arguments: dict[str, object], field_name: str) -> str:
    """读取一个必填非空文本字段。

    Args:
        arguments: 未信任的命令参数。
        field_name: 需要读取的字段名。

    Returns:
        去除首尾空白的文本。

    Raises:
        InvalidTimedReminder: 字段不是非空字符串。
    """

    value = _clean_text(arguments.get(field_name))
    if not value:
        raise InvalidTimedReminder(f"{field_name} 不能为空")
    return value


def _optional_text(
    arguments: dict[str, object],
    field_name: str,
) -> str | None:
    """读取一个可选文本字段，并拒绝显式空值。

    Args:
        arguments: 未信任的命令参数。
        field_name: 需要读取的字段名。

    Returns:
        字段缺失时为空，否则返回非空文本。

    Raises:
        InvalidTimedReminder: 字段存在但不是非空字符串。
    """

    if field_name not in arguments:
        return None
    return _required_text(arguments, field_name)


def _optional_datetime(
    arguments: dict[str, object],
    field_name: str,
) -> datetime | None:
    """读取一个可选的带时区 ISO 时间。

    Args:
        arguments: 未信任的命令参数。
        field_name: 需要读取的字段名。

    Returns:
        字段缺失时为空，否则返回带时区时间。

    Raises:
        InvalidTimedReminder: 字段格式错误或没有时区。
    """

    if field_name not in arguments:
        return None
    value = _required_text(arguments, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exception:
        raise InvalidTimedReminder("提醒时间格式无效") from exception
    if parsed.tzinfo is None:
        raise InvalidTimedReminder("提醒时间必须包含明确时区")
    return parsed


def _optional_positive_int(
    arguments: dict[str, object],
    field_name: str,
) -> int | None:
    """读取一个可选正整数。

    Args:
        arguments: 未信任的命令参数。
        field_name: 需要读取的字段名。

    Returns:
        字段缺失时为空，否则返回正整数。

    Raises:
        InvalidTimedReminder: 字段不是正整数。
    """

    if field_name not in arguments:
        return None
    value = arguments.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidTimedReminder(f"{field_name} 必须是正整数")
    return value


def _required_string_list(arguments: dict[str, object], field_name: str) -> list[str]:
    """读取批次命令中的非空字符串列表。"""

    value = arguments.get(field_name)
    if not isinstance(value, list) or not value:
        raise InvalidTimedReminder(f"{field_name} 必须是非空列表")
    normalized = [item.strip() if isinstance(item, str) else "" for item in value]
    if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise InvalidTimedReminder(f"{field_name} 不能包含重复或空编号")
    return normalized


def _failure(reason: str, safe_message: str) -> dict[str, object]:
    """构造主进程命令失败的稳定回执。

    Args:
        reason: 供程序判断的稳定原因。
        safe_message: Foreground 可以直接转述的说明。

    Returns:
        state 固定为 rejected 的结果字典。
    """

    return {"state": "rejected", "reason": reason, "safe_message": safe_message}


def _clean_text(value: object) -> str:
    """把动态字段清洗成无首尾空白文本。"""

    return value.strip() if isinstance(value, str) else ""
