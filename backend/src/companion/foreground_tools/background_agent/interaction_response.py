"""声明只在后台工作等待用户时开放的统一回答工具。"""

from __future__ import annotations

from companion.foreground_tools.background_agent.schemas import (
    BACKGROUND_WORK_DISPLAY_NAME,
    BACKGROUND_WORK_FAMILY_ID,
)
from companion.foreground_tools.catalog.metadata import ToolLatencyClass, ToolMetadata


RESPOND_TO_WORK_INTERACTION_TOOL = "respond_to_work_interaction"


def respond_to_work_interaction_tool_schemas() -> list[dict[str, object]]:
    """返回恢复当前后台工作的统一交互工具声明。

    Returns:
        包含请求编号、明确决定和可选资料答案的工具 schema。
    """

    return [
        {
            "type": "function",
            "function": {
                "name": RESPOND_TO_WORK_INTERACTION_TOOL,
                "description": (
                    "仅当本轮用户明确回应了 body_status 中当前工作正在等待的权限确认或资料问题时，"
                    "把允许、拒绝、取消或资料答案交回同一项工作。与等待事项无关或含义不明确时不要调用；"
                    "模糊的‘继续’‘可以吧’不能视为授权，应先自然澄清。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                            "description": "动态环境中当前等待事项的请求编号。",
                        },
                        "outcome": {
                            "type": "string",
                            "enum": ["allow", "deny", "answer", "cancel"],
                            "description": (
                                "权限确认只能使用 allow、deny 或 cancel；资料问题只能使用 answer 或 cancel。"
                            ),
                        },
                        "answers": {
                            "type": "object",
                            "description": (
                                "资料问题使用：以问题原文为键、用户明确答案为值的完整映射；"
                                "权限确认和取消时省略。"
                            ),
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["request_id", "outcome"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def respond_to_work_interaction_tool_catalog() -> list[ToolMetadata]:
    """返回回答工具的等待策略元数据。

    Returns:
        归入后台智能体工具族的瞬时工具元数据。
    """

    return [
        ToolMetadata(
            name=RESPOND_TO_WORK_INTERACTION_TOOL,
            display_name="回应当前工作",
            family_id=BACKGROUND_WORK_FAMILY_ID,
            family_display_name=BACKGROUND_WORK_DISPLAY_NAME,
            latency_class=ToolLatencyClass.INSTANT,
            capability_description=(
                "只把用户对当前后台工作权限问题或资料问题的明确回答交回原任务；"
                "不能创建新任务，也不能用于回答与等待事项无关的内容。"
            ),
            self_reflection_safe=False,
        )
    ]


def is_respond_to_work_interaction_tool(name: str) -> bool:
    """判断工具名是否是当前工作统一回答入口。

    Args:
        name: 待识别工具名称。

    Returns:
        名称严格匹配时返回真。
    """

    return name == RESPOND_TO_WORK_INTERACTION_TOOL
