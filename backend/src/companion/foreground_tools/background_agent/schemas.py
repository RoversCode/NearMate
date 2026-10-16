"""声明 Foreground 启动自身后台工作的唯一工具。"""

from __future__ import annotations

from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)


START_BACKGROUND_WORK_TOOL = "start_background_work"
BACKGROUND_WORK_FAMILY_ID = "background_work"
BACKGROUND_WORK_DISPLAY_NAME = "后台工作"
START_BACKGROUND_WORK_CAPABILITY_DESCRIPTION = (
    "把目标已经明确的复杂动作交给自己的后台执行能力真实完成：在用户电脑上打开网页、操作应用和窗口、"
    "处理文件、运行代码或命令、播放媒体、联网收集资料，以及制作完整成果；只有一步的动作也算。"
    "纯粹打开一个 Windows 已安装应用本身使用独立同步工具，不属于后台工作；"
    "提交后只会得到已开始、启动失败或拒绝的系统事实；同通道忙时会立即拒绝，且本工具不能当场保证最终完成。"
)


def start_background_work_tool_schemas() -> list[dict[str, object]]:
    """返回启动后台工作的 OpenAI 兼容 schema。

    Returns:
        只包含 `start_background_work` 的工具 schema 列表。
    """

    return [
        {
            "type": "function",
            "function": {
                "name": START_BACKGROUND_WORK_TOOL,
                "description": (
                    "启动你自己的后台执行能力，在用户电脑上真实做事或制作成果：打开用户看得见的网页、"
                    "进入或操作应用程序和窗口、读写与处理文件、运行代码或命令、播放音视频、"
                    "联网检索与收集资料、持续等待、操控系统或改变外部状态，"
                    "以及需要作为结果交付的完整翻译、总结、改写或写作。"
                    "做事不分大小，只有一步的动作（比如打开一个网站）也必须用它完成。"
                    "唯一例外是只打开一个 Windows 已安装应用本身；这种请求使用独立同步工具。"
                    "一旦还包含具体页面、文件、项目或应用内操作，必须把完整目标交给本工具，不能先预开应用。"
                    "goal 必须是你结合当前对话、语音识别文本、材料和现场语境确认后的权威目标；"
                    "关键输入、输出格式或验收条件仍有歧义时先向用户澄清，不得猜测后启动。"
                    "不要用于普通解释、窄范围实时查询，或只需当场理解并口头回答的当前画面问题。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": (
                                "你已经理解清楚、可以直接执行的权威目标。纠正有充分语境依据的语音识别错误；"
                                "无法确认的关键词必须先向用户澄清，不能照抄为任务目标。"
                            ),
                        },
                        "deliverable": {
                            "type": "boolean",
                            "description": "是否需要向用户登记并展示交付结果；文件和完整文本成果都算交付物。",
                        },
                        "requirements": {
                            "type": "string",
                            "description": "可选。只补充格式、语言、命名或验收要求，不重复或改写 goal。",
                        },
                        "include_current_scene": {
                            "type": "boolean",
                            "description": (
                                "任务需要读取本轮冻结的当前屏幕（desktop_view 图像块）时设为 true；"
                                "完整翻译、总结或处理当前屏幕内容时必须设置。"
                                "用户本轮上传的图片材料由系统自动附带，处理它们不需要设置本参数；"
                                "不依赖当前屏幕时省略或设为 false。"
                            ),
                        },
                        "material_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "可选。任务依赖你仍记得的近期本地材料时，逐项抄写 "
                                "recent_materials 中对应 material 的 path；本轮新材料由系统自动附挂，"
                                "不要填写。只允许逐字抄写近期列表中的路径，单次最多 8 项；"
                                "详细规则见 recent_materials 的 usage_policy。"
                            ),
                        },
                        "based_on_activity_id": {
                            "type": "string",
                            "description": (
                                "可选。继续、重做或基于聊天历史中的旧任务衍生时，填写对应 "
                                "<task_memory> 的 activity_id。goal 始终是新任务的权威目标。"
                            ),
                        },
                    },
                    "required": ["goal", "deliverable"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def start_background_work_tool_catalog() -> list[ToolMetadata]:
    """返回后台工作工具的目录元数据。

    Returns:
        派单工具的目录元数据列表。
    """

    return [
        ToolMetadata(
            name=START_BACKGROUND_WORK_TOOL,
            display_name=BACKGROUND_WORK_DISPLAY_NAME,
            family_id=BACKGROUND_WORK_FAMILY_ID,
            family_display_name=BACKGROUND_WORK_DISPLAY_NAME,
            latency_class=ToolLatencyClass.LONG_RUNNING,
            capability_description=START_BACKGROUND_WORK_CAPABILITY_DESCRIPTION,
            self_reflection_safe=True,
        )
    ]


def start_background_work_tool_family_catalog() -> list[ToolFamilyMetadata]:
    """返回后台工作工具族的等待策略元数据。

    Returns:
        后台智能体工具族的等待策略元数据列表。
    """

    return [
        ToolFamilyMetadata(
            family_id=BACKGROUND_WORK_FAMILY_ID,
            display_name=BACKGROUND_WORK_DISPLAY_NAME,
            latency_class=ToolLatencyClass.LONG_RUNNING,
            waiting_feedback_eligible=False,
            default_waiting_enabled=False,
            default_waiting_phrases=[],
        )
    ]


def is_start_background_work_tool(name: str) -> bool:
    """判断工具名是否是后台工作的唯一启动入口。

    Args:
        name: 需要识别的 Foreground 工具名称。

    Returns:
        名称严格等于 `start_background_work` 时返回真。
    """

    return name == START_BACKGROUND_WORK_TOOL
