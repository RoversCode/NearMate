"""声明 Foreground 精确取消 Activity 的动态工具。"""

from __future__ import annotations

from companion.foreground_tools.activity_control.policy import (
    CancellableActivityContext,
)
from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)


CANCEL_ACTIVITY_TOOL = "cancel_activity"


def activity_control_tool_schemas(
    cancellable_activities: tuple[CancellableActivityContext, ...],
) -> list[dict[str, object]]:
    """按本轮真实候选返回精确取消工具声明。

    Args:
        cancellable_activities: 当前会话仍可取消的 Activity 现场。

    Returns:
        有候选时返回一项动态枚举工具，否则返回空列表。
    """

    if not cancellable_activities:
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": CANCEL_ACTIVITY_TOOL,
                "description": (
                    "停止一项当前仍在执行或等待用户的认知任务或后台工作。"
                    "只有用户明确要求停止且目标唯一，或明确指出了具体目标时调用。"
                    "有多个候选但无法确定用户指的是哪一项时，必须先澄清。"
                    "取消只阻止后续执行，不承诺撤销已经发生的文件、命令或外部副作用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "activity_id": {
                            "type": "string",
                            "enum": [
                                context.activity_id
                                for context in cancellable_activities
                            ],
                            "description": (
                                "body_status 中当前会话仍可取消的 Activity 编号。"
                            ),
                        }
                    },
                    "required": ["activity_id"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def activity_control_tool_catalog() -> list[ToolMetadata]:
    """返回精确取消工具的目录元数据。

    Returns:
        一项不允许 Self-Reflection 使用的 Activity 控制工具元数据。
    """

    return [
        ToolMetadata(
            name=CANCEL_ACTIVITY_TOOL,
            display_name="取消活动",
            family_id="activity_control",
            family_display_name="活动控制",
            latency_class=ToolLatencyClass.INSTANT,
            capability_description=(
                "按用户当前明确要求停止一项仍可取消的认知任务或后台工作；"
                "只提交取消请求，不承诺回滚已经发生的副作用。"
            ),
            self_reflection_safe=False,
        )
    ]


def activity_control_tool_family_catalog() -> list[ToolFamilyMetadata]:
    """返回 Activity 控制工具族的目录元数据。

    Returns:
        不产生额外等待话术的工具族定义。
    """

    return [
        ToolFamilyMetadata(
            family_id="activity_control",
            display_name="活动控制",
            latency_class=ToolLatencyClass.INSTANT,
            waiting_feedback_eligible=False,
            default_waiting_enabled=False,
            default_waiting_phrases=[],
        )
    ]


def is_cancel_activity_tool(name: str) -> bool:
    """判断工具名是否是精确 Activity 取消入口。

    Args:
        name: Foreground 模型请求调用的工具名。

    Returns:
        名称与 `cancel_activity` 完全一致时返回真。
    """

    return name == CANCEL_ACTIVITY_TOOL
