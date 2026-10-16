"""声明基于本轮冻结提醒清单的 Foreground 工具。"""

from __future__ import annotations

from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)


CREATE_TIMED_REMINDER_TOOL = "create_timed_reminder"
MANAGE_TIMED_REMINDER_TOOL = "manage_timed_reminder"


def timed_reminder_tool_schemas(
    timed_reminders: tuple[dict[str, object], ...],
) -> list[dict[str, object]]:
    """按本轮冻结清单声明创建和管理提醒工具。

    Args:
        timed_reminders: 主进程为当前请求冻结的全部活跃提醒。

    Returns:
        始终包含创建工具；存在可修改、可取消或可关闭项目时再包含管理工具。
    """

    schemas = [_create_schema()]
    manageable = [
        reminder
        for reminder in timed_reminders
        if _reminder_id(reminder)
        and (
            reminder.get("can_cancel") is True
            or reminder.get("can_dismiss") is True
        )
    ]
    if manageable:
        schemas.append(_manage_schema(tuple(manageable)))
    return schemas


def timed_reminder_tool_catalog() -> list[ToolMetadata]:
    """返回定时提醒工具的目录元数据。

    Returns:
        创建和管理两项即时、不可供 Self-Reflection 使用的工具元数据。
    """

    return [
        ToolMetadata(
            name=CREATE_TIMED_REMINDER_TOOL,
            display_name="创建定时提醒",
            family_id="timed_reminders",
            family_display_name="定时提醒",
            latency_class=ToolLatencyClass.INSTANT,
            capability_description=(
                "按用户明确要求创建一条当前运行期内的一次性提醒。"
            ),
            self_reflection_safe=False,
        ),
        ToolMetadata(
            name=MANAGE_TIMED_REMINDER_TOOL,
            display_name="管理定时提醒",
            family_id="timed_reminders",
            family_display_name="定时提醒",
            latency_class=ToolLatencyClass.INSTANT,
            capability_description=(
                "按用户明确要求修改、取消或关闭本轮清单中的定时提醒。"
            ),
            self_reflection_safe=False,
        ),
    ]


def timed_reminder_tool_family_catalog() -> list[ToolFamilyMetadata]:
    """返回定时提醒工具族元数据。

    Returns:
        不产生等待话术的即时工具族定义。
    """

    return [
        ToolFamilyMetadata(
            family_id="timed_reminders",
            display_name="定时提醒",
            latency_class=ToolLatencyClass.INSTANT,
            waiting_feedback_eligible=False,
            default_waiting_enabled=False,
            default_waiting_phrases=[],
        )
    ]


def is_timed_reminder_tool(name: str) -> bool:
    """判断工具名是否属于定时提醒写入口。

    Args:
        name: Foreground 模型请求调用的工具名。

    Returns:
        名称与创建或管理工具完全一致时返回真。
    """

    return name in {CREATE_TIMED_REMINDER_TOOL, MANAGE_TIMED_REMINDER_TOOL}


def _create_schema() -> dict[str, object]:
    """构造创建提醒的严格二选一时间结构。

    Returns:
        OpenAI 兼容的函数工具声明。
    """

    return {
        "type": "function",
        "function": {
            "name": CREATE_TIMED_REMINDER_TOOL,
            "description": (
                "用户明确要求在某个时刻或一段时间后提醒时调用。"
                "普通讨论、查看现有提醒或没有明确提醒意图时不要调用。"
                "trigger_at 必须包含明确时区；倒计时直接传 delay_seconds，禁止自己换算绝对时间。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "到点需要自然提醒用户的具体事项。",
                    },
                    "trigger_at": {
                        "type": "string",
                        "format": "date-time",
                        "description": "带明确时区的绝对触发时刻。",
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "从现在开始计算的倒计时秒数。",
                    },
                },
                "required": ["content"],
                "oneOf": [
                    {"required": ["trigger_at"]},
                    {"required": ["delay_seconds"]},
                ],
                "additionalProperties": False,
            },
        },
    }


def _manage_schema(
    timed_reminders: tuple[dict[str, object], ...],
) -> dict[str, object]:
    """按真实可操作编号构造修改与取消分支。

    Args:
        timed_reminders: 当前仍可取消的提醒快照。

    Returns:
        action 为 update、cancel 或 dismiss 的互斥管理工具声明。
    """

    cancellable_ids = [
        _reminder_id(reminder)
        for reminder in timed_reminders
        if reminder.get("can_cancel") is True
    ]
    dismissible_ids = [
        _reminder_id(reminder)
        for reminder in timed_reminders
        if reminder.get("can_dismiss") is True
    ]
    modifiable_ids = [
        _reminder_id(reminder)
        for reminder in timed_reminders
        if reminder.get("can_modify") is True
    ]
    action_schemas: list[dict[str, object]] = []
    if modifiable_ids:
        action_schemas.append(_update_action_schema(modifiable_ids))
    if cancellable_ids:
        action_schemas.append(_cancel_action_schema(cancellable_ids))
    if dismissible_ids:
        action_schemas.append(_dismiss_action_schema(dismissible_ids))
    return {
        "type": "function",
        "function": {
            "name": MANAGE_TIMED_REMINDER_TOOL,
            "description": (
                "用户明确要求修改或取消等待中的提醒，或对响铃中的提醒表示知道了、别再提醒时调用。"
                "编号只能逐字选用当前 body_status 中的值；没有具体指代的关闭意图应传入全部响铃中编号。"
            ),
            "parameters": {
                "type": "object",
                "oneOf": action_schemas,
            },
        },
    }


def _update_action_schema(reminder_ids: list[str]) -> dict[str, object]:
    """构造只允许修改等待中提醒的参数分支。

    Args:
        reminder_ids: 当前确实允许修改的提醒编号。

    Returns:
        至少修改正文或时间、且两种时间不能并存的 JSON Schema。
    """

    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "const": "update"},
            "reminder_id": {"type": "string", "enum": reminder_ids},
            "content": {"type": "string", "minLength": 1, "maxLength": 500},
            "trigger_at": {"type": "string", "format": "date-time"},
            "delay_seconds": {"type": "integer", "minimum": 1},
        },
        "required": ["action", "reminder_id"],
        "allOf": [
            {
                "anyOf": [
                    {"required": ["content"]},
                    {"required": ["trigger_at"]},
                    {"required": ["delay_seconds"]},
                ]
            },
            {"not": {"required": ["trigger_at", "delay_seconds"]}},
        ],
        "additionalProperties": False,
    }


def _cancel_action_schema(reminder_ids: list[str]) -> dict[str, object]:
    """构造取消任一等待中提醒的参数分支。

    Args:
        reminder_ids: 当前确实允许取消的提醒编号。

    Returns:
        只接收 action 与 reminder_id 的 JSON Schema。
    """

    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "const": "cancel"},
            "reminder_id": {"type": "string", "enum": reminder_ids},
        },
        "required": ["action", "reminder_id"],
        "additionalProperties": False,
    }


def _dismiss_action_schema(reminder_ids: list[str]) -> dict[str, object]:
    """构造关闭当前一项或多项响铃任务的参数分支。

    Args:
        reminder_ids: 本轮 Prompt 中确实处于响铃状态的提醒编号。

    Returns:
        只允许选择当前冻结响铃编号的批次关闭 JSON Schema。
    """

    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "const": "dismiss"},
            "reminder_ids": {
                "type": "array",
                "items": {"type": "string", "enum": reminder_ids},
                "minItems": 1,
                "maxItems": len(reminder_ids),
                "uniqueItems": True,
            },
        },
        "required": ["action", "reminder_ids"],
        "additionalProperties": False,
    }


def _reminder_id(reminder: dict[str, object]) -> str:
    """读取一条动态快照中的有效提醒编号。

    Args:
        reminder: 尚未信任的提醒字典。

    Returns:
        去除首尾空白的编号；字段无效时返回空字符串。
    """

    value = reminder.get("reminder_id")
    return value.strip() if isinstance(value, str) else ""
