"""声明 Activity 纠正所需的两类 Foreground 工具。"""

from __future__ import annotations

from companion.foreground_tools.catalog.metadata import (
    ToolLatencyClass,
    ToolMetadata,
)
from companion.foreground_tools.activity_correction.policy import (
    COGNITIVE_TOOL_TYPES,
    CorrectableActivityContext,
)


CORRECT_COGNITIVE_ACTIVITY_TOOL = "correct_cognitive_activity"
CORRECT_BACKGROUND_WORK_TOOL = "correct_background_work"
ACTIVITY_CORRECTION_TOOL_NAMES = {
    CORRECT_COGNITIVE_ACTIVITY_TOOL,
    CORRECT_BACKGROUND_WORK_TOOL,
}


def activity_correction_tool_schemas(
    correctable_activities: tuple[CorrectableActivityContext, ...],
) -> list[dict[str, object]]:
    """按活跃任务来源返回对应的纠正工具声明。

    Args:
        correctable_activities: 本轮从实时快照冻结出的可纠正现场。

    Returns:
        可直接注册给 Foreground 模型的零个、一个或两个函数工具 schema。
    """

    cognitive_contexts = tuple(
        context for context in correctable_activities if context.source == "cognitive"
    )
    background_contexts = tuple(
        context
        for context in correctable_activities
        if context.source == "background_agent"
    )
    schemas: list[dict[str, object]] = []

    if cognitive_contexts:
        cognitive_id_schema = {
            "type": "string",
            "enum": [context.activity_id for context in cognitive_contexts],
            "description": "body_status 中当前仍可纠正的认知任务编号。",
        }
        schemas.append(
            _cognitive_correction_schema(
                activity_id_schema=cognitive_id_schema,
                contexts=cognitive_contexts,
            )
        )

    if background_contexts:
        background_id_schema = {
            "type": "string",
            "enum": [context.activity_id for context in background_contexts],
            "description": "body_status 中当前仍可纠正的后台工作任务编号。",
        }
        schemas.append(
            _background_correction_schema(activity_id_schema=background_id_schema)
        )
    return schemas


def _cognitive_correction_schema(
    *,
    activity_id_schema: dict[str, object],
    contexts: tuple[CorrectableActivityContext, ...],
) -> dict[str, object]:
    """构造只接受 Cognitive Activity 编号的纠正工具声明。

    Args:
        activity_id_schema: 当前轮冻结的 Cognitive Activity 编号约束。
        contexts: 当前轮冻结的 Cognitive Activity 现场。

    Returns:
        Cognitive 纠正工具 schema。
    """

    return {
        "type": "function",
        "function": {
            "name": CORRECT_COGNITIVE_ACTIVITY_TOOL,
            "description": (
                "替换当前尚未结束的认知任务，包括事实核查和深度分析。"
                "仅当用户明确表示原任务目标错误，并给出停止、替换或改做的新目标时调用。"
                "新目标必须写成完整目标。普通补充资料、追问进度、结果不满意或要求重试时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_id": activity_id_schema,
                    "correction_goal": {
                        "type": "string",
                        "description": (
                            "纠正后要完整执行的新认知目标。需要保留的旧目标必须重新写入；"
                            "未写入的旧目标不会自动保留。"
                        ),
                    },
                    **_additional_cognitive_types_property(contexts),
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选的 HTTP(S) 核查来源。",
                    },
                },
                "required": ["activity_id", "correction_goal"],
                "additionalProperties": False,
            },
        },
    }


def _background_correction_schema(
    *,
    activity_id_schema: dict[str, object],
) -> dict[str, object]:
    """构造只接受 Background Activity 编号的纠正工具声明。

    Args:
        activity_id_schema: 当前轮冻结的 Background Activity 编号约束。

    Returns:
        Background Work 纠正工具 schema。
    """

    return {
        "type": "function",
        "function": {
            "name": CORRECT_BACKGROUND_WORK_TOOL,
            "description": (
                "替换当前尚未结束的后台工作，包括打开网页或应用等电脑操作、文件处理、脚本或命令执行、开放式研究和完整成果制作。"
                "仅当用户明确表示当前后台工作目标错误，并给出停止、替换或改做的新目标时调用。"
                "新目标必须写成完整的后台工作目标。普通补充资料、追问进度、结果不满意或要求重试时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_id": activity_id_schema,
                    "goal": {
                        "type": "string",
                        "description": "纠正后要完整执行的权威后台工作目标。",
                    },
                    "deliverable": {
                        "type": "boolean",
                        "description": "是否登记并展示完整交付结果。",
                    },
                    "requirements": {
                        "type": "string",
                        "description": "可选的格式、语言、命名或验收要求，不要重写 goal。",
                    },
                    "include_current_scene": {
                        "type": "boolean",
                        "description": "是否把本轮冻结屏幕作为任务输入。",
                    },
                },
                "required": ["activity_id", "goal", "deliverable"],
                "additionalProperties": False,
            },
        },
    }


def _additional_cognitive_types_property(
    correctable_activities: tuple[CorrectableActivityContext, ...],
) -> dict[str, object]:
    """返回纠正轮可追加的 Cognitive 类型声明。

    同一轮若存在多个可能的 Cognitive 父 Activity，schema 不能按 activity_id
    做条件分支，因此保留全部类型并由执行阶段按父 Activity 实时校验。
    """

    cognitive_contexts = correctable_activities
    if len(cognitive_contexts) == 1:
        inherited_types = set(cognitive_contexts[0].cognitive_tool_types)
        available_types = tuple(
            tool_type for tool_type in COGNITIVE_TOOL_TYPES if tool_type not in inherited_types
        )
    else:
        available_types = tuple(COGNITIVE_TOOL_TYPES)
    if not available_types:
        return {}
    return {
        "additional_cognitive_types": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": list(available_types),
            },
            "uniqueItems": True,
            "description": (
                "可选。只填写纠正后的新目标明确新增、且当前 Activity 尚未包含的认知能力。"
                "旧 Activity 的已有能力由系统继承。"
            ),
        }
    }


def activity_correction_tool_catalog() -> list[ToolMetadata]:
    """返回 Activity 纠正工具目录。

    Returns:
        分别归入 Cognitive 和 Background Work 工具族的元数据。
    """

    return [
        ToolMetadata(
            name=CORRECT_COGNITIVE_ACTIVITY_TOOL,
            display_name="纠正认知活动",
            family_id="cognitive",
            family_display_name="认知任务",
            latency_class=ToolLatencyClass.LONG_RUNNING,
            capability_description=(
                "按用户当前明确纠正替换一项尚未结束的认知任务；会改变现有任务，"
                "不能用于内部恢复或在用户没有新授权时自行调用。"
            ),
            self_reflection_safe=False,
        ),
        ToolMetadata(
            name=CORRECT_BACKGROUND_WORK_TOOL,
            display_name="纠正后台工作",
            family_id="background_work",
            family_display_name="后台工作",
            latency_class=ToolLatencyClass.LONG_RUNNING,
            capability_description=(
                "按用户当前明确纠正替换一项尚未结束的后台工作；会取消或改动现有工作，"
                "不能用于内部恢复或在用户没有新授权时自行调用。"
            ),
            self_reflection_safe=False,
        ),
    ]


def is_activity_correction_tool(name: str) -> bool:
    """判断工具名是否属于 Activity 纠正入口。

    Args:
        name: Foreground 模型请求调用的工具名。

    Returns:
        名称是两种显式纠正工具之一时返回真。
    """

    return name in ACTIVITY_CORRECTION_TOOL_NAMES
