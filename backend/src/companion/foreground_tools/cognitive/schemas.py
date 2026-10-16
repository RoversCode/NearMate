from __future__ import annotations

from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)


LOOKUP_CURRENT_FACT_TOOL = "lookup_current_fact"
ANALYZE_DEEPLY_TOOL = "analyze_deeply"
COGNITIVE_TOOL_NAMES = {
    LOOKUP_CURRENT_FACT_TOOL,
    ANALYZE_DEEPLY_TOOL,
}
COGNITIVE_FAMILY_ID = "cognitive"
COGNITIVE_FAMILY_DISPLAY_NAME = "认知任务"

LOOKUP_BRIEF_DESCRIPTION = (
    "需要联网核查的时效性、版本敏感性或外部证据问题。"
    "稳定定义、基础玩法、常识解释和提示词已有事实直接回答；用户明确要求外部核验时例外。"
    "只写清要确认什么，不要写回答。"
)
ANALYZE_BRIEF_DESCRIPTION = (
    "需要深入分析、推演或讨论的具体问题。不要要求读取整份附件或材料正文。"
)
SOURCE_URLS_DESCRIPTION = (
    "可选。填写需要由网页能力读取或核查的完整 HTTP(S) URL。"
)
RESPONSE_GOAL_DESCRIPTION = (
    "可选。说明核查完成后需要借此回答用户的讨论、比较、决策或解释目的。"
    "brief 已经足以直接回答用户时不要填写；这里只写回答目的，不写预设结论。"
)


def cognitive_tool_schemas() -> list[dict[str, object]]:
    """返回 Foreground 可调用的认知意图工具声明。

    Returns:
        lookup 和 analyze 两个函数工具 schema。
    """

    return [
        _cognitive_tool_schema(
            name=LOOKUP_CURRENT_FACT_TOOL,
            description=(
                "只用于时效性、版本敏感性或需要外部核验的事实。"
                "当前版本、最新发布、价格、政策、排名、赛程、天气、尚在测试中的角色上线状态与定位，"
                "或用户明确要求外部核验时应调用；"
                "稳定定义、基础玩法、常识解释和提示词已有事实直接回答。"
                "例如‘王者荣耀是什么类型’和‘MOBA 怎么玩’直接回答；"
                "‘王者荣耀目前最新英雄’和‘某个角色当前是否已上线、定位是否有变化’调用本工具。"
                "支持使用本轮代码自动附带的图片作为外部身份核验的证据。"
            ),
            brief_description=LOOKUP_BRIEF_DESCRIPTION,
            optional_properties={
                "source_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": SOURCE_URLS_DESCRIPTION,
                },
            },
        ),
        _cognitive_tool_schema(
            name=ANALYZE_DEEPLY_TOOL,
            description=(
                "深度分析工具。仅在用户明确要求深入分析、推演或深度讨论时调用；"
                "不适用于整份网页、文档或附件的总结、翻译、提取和材料问答，"
                "此类请求必须遵守 system prompt 中的 material_action_policy。"
            ),
            brief_description=ANALYZE_BRIEF_DESCRIPTION,
            optional_properties={},
        ),
    ]


def cognitive_tool_catalog() -> list[ToolMetadata]:
    """返回认知工具元数据。

    Returns:
        两个认知意图的目录记录。
    """

    return [
        ToolMetadata(
            name=LOOKUP_CURRENT_FACT_TOOL,
            display_name="当前事实校准",
            family_id=COGNITIVE_FAMILY_ID,
            family_display_name=COGNITIVE_FAMILY_DISPLAY_NAME,
            latency_class=ToolLatencyClass.LONG_RUNNING,
            capability_description=(
                "联网核查时效性、版本敏感或需要外部证据的事实，并形成可供回答的分析结果；"
                "不能操作本地文件、改变外部状态或交付新文件。"
            ),
            self_reflection_safe=False,
        ),
        ToolMetadata(
            name=ANALYZE_DEEPLY_TOOL,
            display_name="深度分析",
            family_id=COGNITIVE_FAMILY_ID,
            family_display_name=COGNITIVE_FAMILY_DISPLAY_NAME,
            latency_class=ToolLatencyClass.LONG_RUNNING,
            capability_description=(
                "对明确问题做深入分析、推演或讨论，并形成可供回答的结论；"
                "不能执行程序、改变外部状态或交付新生成文件。"
            ),
            self_reflection_safe=False,
        ),
    ]


def cognitive_tool_family_catalog() -> list[ToolFamilyMetadata]:
    """返回认知工具族等待策略默认值。

    Returns:
        认知工具族元数据。
    """

    return [
        ToolFamilyMetadata(
            family_id=COGNITIVE_FAMILY_ID,
            display_name=COGNITIVE_FAMILY_DISPLAY_NAME,
            latency_class=ToolLatencyClass.LONG_RUNNING,
            waiting_feedback_eligible=False,
            default_waiting_enabled=False,
            default_waiting_phrases=[],
        )
    ]


def is_cognitive_tool(name: str) -> bool:
    """判断工具名是否属于认知意图。

    Args:
        name: 待判断工具名。

    Returns:
        匹配任一认知工具时返回真。
    """

    return name in COGNITIVE_TOOL_NAMES


def _cognitive_tool_schema(
    *,
    name: str,
    description: str,
    brief_description: str,
    optional_properties: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造统一的认知工具 schema。

    Args:
        name: 工具函数名。
        description: 工具整体用途说明。
        brief_description: brief 参数的专属语义。
        optional_properties: 工具额外接受的可选参数。

    Returns:
        可直接暴露给模型的函数工具声明。
    """

    properties: dict[str, object] = {
        "brief": {"type": "string", "description": brief_description},
        "response_goal": {
            "type": "string",
            "description": RESPONSE_GOAL_DESCRIPTION,
        },
        "based_on_activity_id": {
            "type": "string",
            "description": (
                "可选。继续、重做或基于聊天历史中的旧任务衍生时，填写对应 "
                "<task_memory> 的 activity_id。当前 brief 始终是新任务的权威目标。"
            ),
        },
    }
    properties.update(optional_properties or {})
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": ["brief"],
                "additionalProperties": False,
            },
        },
    }
