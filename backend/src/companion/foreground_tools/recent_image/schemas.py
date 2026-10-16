"""声明近期图片挂载工具的 OpenAI 兼容协议。"""

from __future__ import annotations

from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)


LOAD_RECENT_IMAGE_FOR_FOREGROUND_TOOL = "load_recent_image_for_foreground"
RECENT_IMAGE_FAMILY_ID = "recent_image_mount"
RECENT_IMAGE_FAMILY_DISPLAY_NAME = "近期图片挂载"


def load_recent_image_tool_schemas() -> list[dict[str, object]]:
    """返回近期图片挂载工具的 OpenAI 兼容 Schema。

    Returns:
        只包含近期图片挂载函数的工具声明。
    """

    return [
        {
            "type": "function",
            "function": {
                "name": LOAD_RECENT_IMAGE_FOR_FOREGROUND_TOOL,
                "description": (
                    "只把用户明确指向的一张近期图片临时挂载到下一次 Foreground 输入中，"
                    "供 Foreground 自己理解图片。只能逐字填写 recent_materials 中已经列出的图片路径；"
                    "不能填写任意本地路径，不能用于文档、目录或其它材料，也不能返回图片内容。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "material_path": {
                            "type": "string",
                            "description": (
                                "recent_materials 中与用户指代唯一对应的图片路径，必须逐字填写。"
                            ),
                        }
                    },
                    "required": ["material_path"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def load_recent_image_tool_catalog() -> list[ToolMetadata]:
    """返回近期图片挂载工具的目录元数据。

    Returns:
        Inline 工具目录记录。
    """

    return [
        ToolMetadata(
            name=LOAD_RECENT_IMAGE_FOR_FOREGROUND_TOOL,
            display_name=RECENT_IMAGE_FAMILY_DISPLAY_NAME,
            family_id=RECENT_IMAGE_FAMILY_ID,
            family_display_name=RECENT_IMAGE_FAMILY_DISPLAY_NAME,
            latency_class=ToolLatencyClass.INSTANT,
            capability_description=(
                "从近期材料白名单中重新校验并临时挂载一张图片给 Foreground；"
                "不读取任意路径，不创建 Activity，不保存图片编码。"
            ),
            self_reflection_safe=True,
        )
    ]


def load_recent_image_tool_family_catalog() -> list[ToolFamilyMetadata]:
    """返回近期图片挂载工具族的等待策略元数据。

    Returns:
        不产生固定等待句的即时工具族记录。
    """

    return [
        ToolFamilyMetadata(
            family_id=RECENT_IMAGE_FAMILY_ID,
            display_name=RECENT_IMAGE_FAMILY_DISPLAY_NAME,
            latency_class=ToolLatencyClass.INSTANT,
            waiting_feedback_eligible=False,
            default_waiting_enabled=False,
            default_waiting_phrases=[],
        )
    ]


def is_load_recent_image_for_foreground_tool(name: str) -> bool:
    """判断工具名是否为近期图片挂载工具。

    Args:
        name: 待判断的工具名称。

    Returns:
        名称完全匹配时返回真。
    """

    return name == LOAD_RECENT_IMAGE_FOR_FOREGROUND_TOOL
