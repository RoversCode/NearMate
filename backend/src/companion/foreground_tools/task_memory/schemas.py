"""声明按 Activity 编号精确召回任务详情的工具。"""

from __future__ import annotations

from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)


RECALL_TASK_MEMORY_TOOL = "recall_task_memory"
TASK_MEMORY_FAMILY_ID = "task_memory"


def recall_task_memory_tool_schemas() -> list[dict[str, object]]:
    """返回任务记忆召回工具声明。

    Returns:
        只包含精确 activity_id 参数的 OpenAI 兼容 schema。
    """

    return [
        {
            "type": "function",
            "function": {
                "name": RECALL_TASK_MEMORY_TOOL,
                "description": (
                    "当用户追问聊天历史中某条 <task_memory> 的具体结果、失败事实、"
                    "输出文件或当时输入时，按其中明确的 activity_id 读取详情。"
                    "不能按自然语言猜任务编号，也不能用它修改或重做旧任务；"
                    "继续、重做或衍生旧任务应在对应执行工具中填写 based_on_activity_id。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "activity_id": {
                            "type": "string",
                            "description": "聊天历史 <task_memory> 中明确出现的 Activity 编号。",
                        }
                    },
                    "required": ["activity_id"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def recall_task_memory_tool_catalog() -> list[ToolMetadata]:
    """返回任务记忆工具元数据。

    Returns:
        当前轮同步等待主进程详情回执的工具记录。
    """

    return [
        ToolMetadata(
            name=RECALL_TASK_MEMORY_TOOL,
            display_name="读取任务详情",
            family_id=TASK_MEMORY_FAMILY_ID,
            family_display_name="任务记忆",
            latency_class=ToolLatencyClass.INSTANT,
            capability_description=(
                "读取一项已经登记的任务事实，供用户明确追问旧任务时回忆；"
                "它不会执行、恢复、取消或修改任何任务。"
            ),
            self_reflection_safe=False,
        )
    ]


def recall_task_memory_tool_family_catalog() -> list[ToolFamilyMetadata]:
    """返回任务记忆工具族元数据。

    Returns:
        不产生固定等待句的同步召回工具族。
    """

    return [
        ToolFamilyMetadata(
            family_id=TASK_MEMORY_FAMILY_ID,
            display_name="任务记忆",
            latency_class=ToolLatencyClass.INSTANT,
            waiting_feedback_eligible=False,
            default_waiting_enabled=False,
            default_waiting_phrases=[],
        )
    ]


def is_recall_task_memory_tool(name: str) -> bool:
    """判断工具名是否为任务记忆召回入口。

    Args:
        name: 待检查工具名。

    Returns:
        名称完全匹配时返回真。
    """

    return name == RECALL_TASK_MEMORY_TOOL
