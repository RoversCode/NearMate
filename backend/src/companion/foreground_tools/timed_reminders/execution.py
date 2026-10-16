"""准备定时提醒工具命令，并把副作用绑定到提交后事务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Literal

from companion.foreground_tools.orchestration.contracts import ToolFollowupFact
from companion.foreground_tools.orchestration.transactions import CommitAction
from companion.foreground_tools.timed_reminders.broker import (
    TimedReminderCommandBroker,
    TimedReminderCommandOperation,
)
from companion.foreground_tools.timed_reminders.schemas import (
    CREATE_TIMED_REMINDER_TOOL,
    MANAGE_TIMED_REMINDER_TOOL,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.timed_reminders.repository import MAX_TIMED_REMINDER_CONTENT_LENGTH


@dataclass(frozen=True)
class TimedReminderCommandReservation:
    """保存一笔已准备、但尚未越过 Foreground 提交边界的提醒命令。

    Args:
        command_id: 本次提醒写命令的唯一编号。
        operation: 创建、修改、取消或关闭动作。
        reminder_id: 修改或取消目标；创建时为空。
        source: 供提交账本识别的固定资源来源。
        action: 工具事件提交后才会向主进程发命令的一次性事务。
    """

    command_id: str
    operation: TimedReminderCommandOperation
    reminder_id: str
    source: str
    action: CommitAction

    def rollback(self) -> bool:
        """放弃一笔尚未提交的提醒命令。

        Returns:
            事务首次进入回滚态时返回真，已提交或已回滚时返回假。
        """

        return self.action.rollback()


class TimedReminderToolExecutor:
    """校验创建和管理参数，并建立只能在提交后执行的命令事务。

    Args:
        broker: 把正式命令送到主进程并等待 canonical 回执的 Broker。
    """

    def __init__(self, broker: TimedReminderCommandBroker) -> None:
        self._broker = broker
        self._prepared: dict[str, TimedReminderCommandReservation] = {}
        self._lock = RLock()

    def prepare(
        self,
        *,
        tool_call: ForegroundToolCall,
        timed_reminders: tuple[dict[str, object], ...],
        timed_reminder_runtime_id: str = "",
    ) -> dict[str, object]:
        """根据本轮冻结清单准备一笔提醒写命令。

        Args:
            tool_call: Foreground 模型产生的创建或管理调用。
            timed_reminders: Prompt、schema 和执行共用的活跃提醒快照。

        Returns:
            可交给 deferred 通道的准备结果，或不会改变提醒的安全拒绝。
        """

        validated = _validated_command(
            tool_call,
            timed_reminders,
            timed_reminder_runtime_id=timed_reminder_runtime_id,
        )
        if isinstance(validated, dict) and validated.get("state") == "rejected":
            return validated
        operation, arguments, reminder_id = validated
        command_id = self._broker.create_command_id()
        if not command_id:
            return _rejected(
                "timed_reminder_command_unavailable",
                "当前无法提交提醒操作，提醒没有改变。",
                state="failed",
            )

        def execute_after_commit() -> ToolFollowupFact:
            """在工具事件正式提交后执行主进程提醒命令。

            Returns:
                主进程完成后的 canonical 提醒事实或安全失败事实。
            """

            result = self._broker.submit_and_wait(
                command_id=command_id,
                operation=operation,
                arguments=arguments,
            )
            return _followup_fact(
                command_id=command_id,
                operation=operation,
                result=result,
            )

        reservation = TimedReminderCommandReservation(
            command_id=command_id,
            operation=operation,
            reminder_id=reminder_id,
            source="timed_reminder_command",
            action=CommitAction(
                on_accept=lambda: None,
                on_rollback=lambda: None,
                after_commit=execute_after_commit,
                transaction_id=f"timed-reminder-command-{command_id}",
                resource_type="timed_reminder_command",
                resource_id=reminder_id or command_id,
            ),
        )
        with self._lock:
            if command_id in self._prepared:
                return _rejected(
                    "duplicate_timed_reminder_command",
                    "这次提醒操作没有成功准备。",
                    state="failed",
                )
            self._prepared[command_id] = reservation
        result: dict[str, object] = {
            "state": "accepted",
            "resource_id": command_id,
            "operation": operation,
            "safe_message": "提醒操作已准备，等待当前工具事务提交。",
        }
        if reminder_id:
            result["reminder_id"] = reminder_id
        return result

    def take_reservation(
        self,
        command_id: str,
    ) -> TimedReminderCommandReservation | None:
        """按命令编号首次领取已准备的提交事务。

        Args:
            command_id: deferred 准备结果返回的稳定资源编号。

        Returns:
            首次领取时返回事务，不存在或已领取时返回空。
        """

        with self._lock:
            return self._prepared.pop(command_id.strip(), None)

    def rollback_prepared(self, command_id: str) -> bool:
        """释放 generation 在提交前中断时尚未领取的命令事务。

        Args:
            command_id: deferred 准备结果返回的稳定资源编号。

        Returns:
            找到事务并首次回滚时返回真，否则返回假。
        """

        reservation = self.take_reservation(command_id)
        return reservation.rollback() if reservation is not None else False


def _validated_command(
    tool_call: ForegroundToolCall,
    timed_reminders: tuple[dict[str, object], ...],
    *,
    timed_reminder_runtime_id: str,
) -> (
    tuple[TimedReminderCommandOperation, dict[str, object], str]
    | dict[str, object]
):
    """把工具参数收窄成主进程可以再次校验的稳定命令。

    Args:
        tool_call: 模型产生的原始结构化调用。
        timed_reminders: 本轮冻结的活跃提醒快照。

    Returns:
        合法时返回动作、参数和目标编号；非法时返回安全拒绝载荷。
    """

    arguments = dict(tool_call.arguments)
    if tool_call.name == CREATE_TIMED_REMINDER_TOOL:
        if set(arguments) - {"content", "trigger_at", "delay_seconds"}:
            return _rejected("invalid_timed_reminder_arguments", "提醒参数不合法，没有创建提醒。")
        normalized = _validated_mutation_arguments(arguments, require_content=True)
        if isinstance(normalized, dict) and normalized.get("state") == "rejected":
            return normalized
        return "create", normalized, ""

    if tool_call.name != MANAGE_TIMED_REMINDER_TOOL:
        return _rejected("unsupported_timed_reminder_tool", "这次提醒操作不受支持。")
    action = _clean_text(arguments.get("action"))
    reminder_id = _clean_text(arguments.get("reminder_id"))
    reminder = next(
        (
            item
            for item in timed_reminders
            if _clean_text(item.get("reminder_id")) == reminder_id
        ),
        None,
    )
    if action == "dismiss":
        raw_ids = arguments.get("reminder_ids")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or len(raw_ids) > 5
            or set(arguments) != {"action", "reminder_ids"}
        ):
            return _rejected("invalid_timed_reminder_arguments", "关闭提醒的参数不合法。")
        normalized_ids = [
            item.strip() for item in raw_ids if isinstance(item, str) and item.strip()
        ]
        if len(normalized_ids) != len(raw_ids) or len(set(normalized_ids)) != len(normalized_ids):
            return _rejected("invalid_timed_reminder_arguments", "关闭提醒的编号不合法。")
        allowed_ids = {
            _clean_text(item.get("reminder_id"))
            for item in timed_reminders
            if item.get("can_dismiss") is True
        }
        if not set(normalized_ids).issubset(allowed_ids):
            return _rejected(
                "timed_reminder_not_dismissible_in_current_turn",
                "这些提醒不在当前响铃范围内，没有改变提醒。",
            )
        if not timed_reminder_runtime_id.strip():
            return _rejected(
                "timed_reminder_runtime_unavailable",
                "当前无法确认提醒运行期，提醒没有改变。",
                state="failed",
            )
        return "dismiss", {
            "runtime_id": timed_reminder_runtime_id.strip(),
            "reminder_ids": normalized_ids,
        }, ""

    if reminder is None or reminder.get("can_cancel") is not True:
        return _rejected(
            "timed_reminder_not_manageable_in_current_turn",
            "这条提醒不在当前可管理范围内，没有改变提醒。",
        )
    if action == "cancel":
        if set(arguments) != {"action", "reminder_id"}:
            return _rejected("invalid_timed_reminder_arguments", "取消提醒的参数不合法。")
        return "cancel", {"reminder_id": reminder_id}, reminder_id
    if action != "update":
        return _rejected("invalid_timed_reminder_action", "提醒管理动作不合法。")
    if reminder.get("can_modify") is not True:
        return _rejected(
            "timed_reminder_not_modifiable_in_current_turn",
            "这条提醒已经到点，当前不能再修改时间或内容。",
        )
    if set(arguments) - {"action", "reminder_id", "content", "trigger_at", "delay_seconds"}:
        return _rejected("invalid_timed_reminder_arguments", "修改提醒的参数不合法。")
    mutation_arguments = {
        key: value
        for key, value in arguments.items()
        if key in {"content", "trigger_at", "delay_seconds"}
    }
    normalized = _validated_mutation_arguments(
        mutation_arguments,
        require_content=False,
    )
    if isinstance(normalized, dict) and normalized.get("state") == "rejected":
        return normalized
    return "update", {"reminder_id": reminder_id, **normalized}, reminder_id


def _validated_mutation_arguments(
    arguments: dict[str, object],
    *,
    require_content: bool,
) -> dict[str, object]:
    """校验创建或修改共用的正文与时间字段。

    Args:
        arguments: 只含正文和时间候选的参数字典。
        require_content: 创建时是否强制要求正文。

    Returns:
        合法字段字典；不合法时返回带 `state=rejected` 的安全事实。
    """

    content_present = "content" in arguments
    content = _clean_text(arguments.get("content")) if content_present else ""
    if require_content and not content:
        return _rejected("invalid_timed_reminder_content", "提醒内容不能为空。")
    if content_present and (not content or len(content) > MAX_TIMED_REMINDER_CONTENT_LENGTH):
        return _rejected("invalid_timed_reminder_content", "提醒内容必须在五百个字符以内。")

    has_trigger_at = "trigger_at" in arguments
    has_delay_seconds = "delay_seconds" in arguments
    if has_trigger_at and has_delay_seconds:
        return _rejected("invalid_timed_reminder_time", "绝对时间和倒计时不能同时提供。")
    if require_content and has_trigger_at == has_delay_seconds:
        return _rejected("invalid_timed_reminder_time", "创建提醒时必须提供一个明确时间。")
    if not require_content and not content_present and not has_trigger_at and not has_delay_seconds:
        return _rejected("empty_timed_reminder_update", "没有提供需要修改的提醒内容或时间。")

    normalized: dict[str, object] = {}
    if content_present:
        normalized["content"] = content
    if has_trigger_at:
        trigger_at = _aware_datetime_text(arguments.get("trigger_at"))
        if not trigger_at:
            return _rejected("invalid_timed_reminder_time", "提醒时间必须包含明确时区。")
        normalized["trigger_at"] = trigger_at
    if has_delay_seconds:
        delay_seconds = arguments.get("delay_seconds")
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or delay_seconds <= 0:
            return _rejected("invalid_timed_reminder_time", "倒计时秒数必须是正整数。")
        normalized["delay_seconds"] = delay_seconds
    return normalized


def _aware_datetime_text(value: object) -> str:
    """把带时区 ISO 时间清洗成稳定文本。

    Args:
        value: 模型给出的未知时间字段。

    Returns:
        可由主进程解析的带时区时间；非法时返回空字符串。
    """

    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.isoformat()


def _followup_fact(
    *,
    command_id: str,
    operation: TimedReminderCommandOperation,
    result: dict[str, object],
) -> ToolFollowupFact:
    """把主进程回执转换成工具二轮使用的白名单事实。

    Args:
        command_id: 当前命令编号，用于与原始工具调用精确配对。
        operation: 创建、修改或取消动作。
        result: 主进程返回的结构化结果。

    Returns:
        可安全写入工具二轮 Prompt 的 canonical 提醒事实。
    """

    expected_state_by_operation: dict[str, str] = {
        "create": "created",
        "update": "updated",
        "cancel": "cancelled",
        "dismiss": "dismissed",
    }
    reminder = result.get("reminder")
    if operation == "dismiss":
        dismissed_ids = result.get("dismissed_ids")
        if result.get("state") == "dismissed" and isinstance(dismissed_ids, list):
            return ToolFollowupFact(
                kind="timed_reminders_dismissed",
                command_id=command_id,
                reminder_contents=tuple(
                    content.strip()
                    for content in result.get("dismissed_contents", [])
                    if isinstance(content, str) and content.strip()
                ),
            )
    if (
        result.get("state") == expected_state_by_operation[operation]
        and isinstance(reminder, dict)
    ):
        return ToolFollowupFact(
            kind=f"timed_reminder_{expected_state_by_operation[operation]}",
            command_id=command_id,
            reminder_id=_clean_text(reminder.get("reminder_id")),
            reminder_content=_clean_text(reminder.get("content")),
            trigger_at=_clean_text(reminder.get("trigger_at")),
            crosses_local_day=result.get("crosses_local_day") is True,
            runtime_only=result.get("runtime_only") is True,
        )
    safe_message = _clean_text(result.get("safe_message"))
    return ToolFollowupFact(
        kind="timed_reminder_command_failed",
        command_id=command_id,
        safe_message=safe_message or "这次提醒操作没有成功。",
    )


def _rejected(
    reason: str,
    safe_message: str,
    *,
    state: Literal["rejected", "failed"] = "rejected",
) -> dict[str, object]:
    """构造 prepare 阶段不会产生副作用的安全结果。

    Args:
        reason: 供程序判断的稳定原因。
        safe_message: Foreground 可以直接转述的说明。
        state: 参数拒绝或内部失败。

    Returns:
        兼容 Foreground 工具编排器的结果字典。
    """

    return {"state": state, "reason": reason, "safe_message": safe_message}


def _clean_text(value: object) -> str:
    """把动态字段清洗成无首尾空白文本。"""

    return value.strip() if isinstance(value, str) else ""
