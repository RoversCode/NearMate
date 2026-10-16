"""把 Windows 已安装应用启动能力适配成 Foreground Inline Tool。"""

from __future__ import annotations

from typing import Any

from companion.capabilities.windows_applications import (
    WindowsApplicationGateway,
    WindowsApplicationLaunchService,
)
from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)
from companion.foreground_tools.inline_execution import InlineExecutionContext


OPEN_INSTALLED_WINDOWS_APP_TOOL = "open_installed_windows_app"
WINDOWS_APPLICATION_FAMILY_ID = "windows_application_launch"
WINDOWS_APPLICATION_FAMILY_DISPLAY_NAME = "应用启动"
WINDOWS_APPLICATION_TOOL_DESCRIPTION = (
    "只在用户要求打开一个 Windows 已安装应用本身时调用。"
    "application_name 只能是应用名称，不能是 URL、网站、文件、目录、项目、设置页面或启动参数。"
    "如果用户还要求进入具体页面、读取或操作应用内容、播放指定媒体，必须把完整目标交给后台工作，"
    "不得先调用本工具预开应用。用户一次只要求打开多个纯应用时，为每个应用分别调用一次。"
    "用户只说‘浏览器’时会打开 Windows 默认浏览器，但不会打开任何网页。"
)
_INLINE_RESULT_MARGIN_SECONDS = 0.2


class WindowsApplicationForegroundTool:
    """声明并执行 Windows 已安装应用同步启动 Tool。"""

    def __init__(self, service: WindowsApplicationLaunchService | None) -> None:
        """初始化 Foreground Adapter。

        Args:
            service: 真实应用启动服务；只展示目录元数据时允许为空。
        """

        self._service = service

    @classmethod
    def from_system(
        cls,
        *,
        prewarm: bool = True,
    ) -> WindowsApplicationForegroundTool:
        """使用当前 Windows 系统能力构造生产 Adapter。

        Args:
            prewarm: 是否在 Assistant Runtime 启动时预热应用目录。

        Returns:
            绑定真实 Windows Gateway 的 Tool Adapter。
        """

        service = WindowsApplicationLaunchService(
            gateway=WindowsApplicationGateway(),
        )
        if prewarm:
            service.prewarm()
        return cls(service)

    def tool_schemas(self) -> list[dict[str, Any]]:
        """返回稳定的 OpenAI 兼容 Tool Schema。

        Returns:
            只包含单应用启动入口的 Schema 列表。
        """

        return [
            {
                "type": "function",
                "function": {
                    "name": OPEN_INSTALLED_WINDOWS_APP_TOOL,
                    "description": WINDOWS_APPLICATION_TOOL_DESCRIPTION,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "application_name": {
                                "type": "string",
                                "description": (
                                    "要打开的单个 Windows 已安装应用名称；"
                                    "可以填写‘浏览器’表示系统默认浏览器。"
                                ),
                            },
                            "launch_mode": {
                                "type": "string",
                                "enum": ["reuse_existing", "new_instance"],
                                "default": "reuse_existing",
                                "description": (
                                    "默认 reuse_existing，优先唤起现有窗口；"
                                    "只有用户明确要求再开一个或新开实例时才使用 new_instance。"
                                ),
                            },
                        },
                        "required": ["application_name"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def tool_catalog(self) -> list[ToolMetadata]:
        """返回应用启动 Tool 的目录元数据。

        Returns:
            标记为即时、不可用于 Self-Reflection 的单项元数据。
        """

        return [
            ToolMetadata(
                name=OPEN_INSTALLED_WINDOWS_APP_TOOL,
                display_name="打开已安装应用",
                family_id=WINDOWS_APPLICATION_FAMILY_ID,
                family_display_name=WINDOWS_APPLICATION_FAMILY_DISPLAY_NAME,
                latency_class=ToolLatencyClass.INSTANT,
                capability_description=(
                    f"{WINDOWS_APPLICATION_TOOL_DESCRIPTION}"
                    "该动作立即生效，不创建 Activity，也不会因后续回复取消而回滚。"
                ),
                self_reflection_safe=False,
            )
        ]

    def tool_family_catalog(self) -> list[ToolFamilyMetadata]:
        """返回不可配置等待句的应用启动工具族。

        Returns:
            不进入等待策略设置页的单项工具族元数据。
        """

        return [
            ToolFamilyMetadata(
                family_id=WINDOWS_APPLICATION_FAMILY_ID,
                display_name=WINDOWS_APPLICATION_FAMILY_DISPLAY_NAME,
                latency_class=ToolLatencyClass.INSTANT,
                waiting_feedback_eligible=False,
                default_waiting_enabled=False,
                default_waiting_phrases=[],
            )
        ]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        inline_execution_context: InlineExecutionContext | None = None,
    ) -> dict[str, object]:
        """同步执行单个 Windows 应用启动请求。

        Args:
            name: Foreground 模型选择的 Tool 名称。
            arguments: 只包含应用名称和可选启动模式的参数。
            inline_execution_context: 编排层从提交时冻结的截止时间和取消信号。

        Returns:
            结构化完成、拒绝或失败结果。
        """

        if name != OPEN_INSTALLED_WINDOWS_APP_TOOL:
            return {
                "state": "failed",
                "reason": "unsupported_windows_application_tool",
                "safe_message": "不支持这项应用启动操作。",
            }
        if self._service is None:
            return {
                "state": "failed",
                "reason": "windows_application_launcher_unavailable",
                "safe_message": "Windows 应用启动能力当前不可用。",
            }
        application_name = arguments.get("application_name")
        launch_mode = arguments.get("launch_mode", "reuse_existing")
        # 给结构化结果回到编排层预留 0.2 秒，避免刚产生本地副作用就被外层判成超时。
        side_effect_deadline = None
        cancellation_requested = None
        if inline_execution_context is not None:
            if inline_execution_context.completion_deadline is not None:
                side_effect_deadline = (
                    inline_execution_context.completion_deadline
                    - _INLINE_RESULT_MARGIN_SECONDS
                )
            cancellation_requested = (
                inline_execution_context.cancellation_requested
            )
        return self._service.open_application(
            application_name=(
                application_name if isinstance(application_name, str) else ""
            ),
            launch_mode=(launch_mode if isinstance(launch_mode, str) else ""),
            side_effect_deadline=side_effect_deadline,
            cancellation_requested=cancellation_requested,
        ).to_tool_payload()


def is_windows_application_tool(name: str) -> bool:
    """判断工具名是否是已安装应用同步启动入口。

    Args:
        name: 待识别的 Foreground Tool 名称。

    Returns:
        名称严格匹配时返回真。
    """

    return name == OPEN_INSTALLED_WINDOWS_APP_TOOL
