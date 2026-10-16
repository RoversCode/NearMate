from __future__ import annotations

import logging
from typing import Any

from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)
from companion.foreground_tools.cognitive.schemas import (
    is_cognitive_tool,
    cognitive_tool_catalog,
    cognitive_tool_family_catalog,
    cognitive_tool_schemas,
)
from companion.foreground_tools.background_agent.schemas import (
    start_background_work_tool_catalog,
    start_background_work_tool_family_catalog,
    start_background_work_tool_schemas,
)
from companion.foreground_tools.background_agent.interaction_response import (
    respond_to_work_interaction_tool_catalog,
    respond_to_work_interaction_tool_schemas,
)
from companion.foreground_tools.task_memory.schemas import (
    is_recall_task_memory_tool,
    recall_task_memory_tool_catalog,
    recall_task_memory_tool_family_catalog,
    recall_task_memory_tool_schemas,
)
from companion.foreground_tools.activity_correction.schemas import (
    activity_correction_tool_catalog,
    activity_correction_tool_schemas,
    is_activity_correction_tool,
)
from companion.foreground_tools.activity_correction.policy import (
    CorrectableActivityContext,
)
from companion.foreground_tools.activity_control.policy import (
    CancellableActivityContext,
)
from companion.foreground_tools.activity_control.schemas import (
    activity_control_tool_catalog,
    activity_control_tool_family_catalog,
    activity_control_tool_schemas,
    is_cancel_activity_tool,
)
from companion.foreground_tools.timed_reminders.schemas import (
    is_timed_reminder_tool,
    timed_reminder_tool_catalog,
    timed_reminder_tool_family_catalog,
    timed_reminder_tool_schemas,
)
from companion.foreground_self_reflection.tool_policy import (
    SelfReflectionToolDefinition,
    SelfReflectionToolPolicy,
)
from companion.foreground_tools.external.windows_application_tool import (
    WindowsApplicationForegroundTool,
    is_windows_application_tool,
)
from companion.foreground_tools.recent_image.schemas import (
    load_recent_image_tool_catalog,
    load_recent_image_tool_family_catalog,
    load_recent_image_tool_schemas,
)
from companion.foreground_tools.inline_execution import InlineExecutionContext
from companion.observability.logging_config import get_backend_logger
from companion.observability.structured_logging import log_event


logger = get_backend_logger(__name__)


class ForegroundToolService:
    """管理 foreground LLM 可调用工具的声明、目录和同步执行入口。"""

    def __init__(
        self,
        *,
        weather_tools: Any | None = None,
        windows_application_tools: WindowsApplicationForegroundTool | None = None,
    ) -> None:
        """初始化 foreground 工具服务。

        Args:
            weather_tools: 可选天气工具集合，负责天气类工具声明与执行。
            windows_application_tools: 可选真实 Windows 应用启动 Adapter；为空时仍保留稳定 Schema。

        Returns:
            无。
        """

        self._weather_tools = weather_tools
        self._windows_application_tools = (
            windows_application_tools or WindowsApplicationForegroundTool(service=None)
        )

    def tool_schemas(
        self,
        *,
        has_pending_work_interaction: bool = False,
        correctable_activities: tuple[CorrectableActivityContext, ...] = (),
        cancellable_activities: tuple[CancellableActivityContext, ...] = (),
        timed_reminders: tuple[dict[str, object], ...] = (),
    ) -> list[dict[str, Any]]:
        """返回允许暴露给 foreground 模型的工具声明。

        Args:
            has_pending_work_interaction: 当前轮是否存在可由用户回答的工作请求。
            correctable_activities: 当前轮从实时快照冻结出的可纠正现场。
            cancellable_activities: 当前轮从实时快照冻结出的可取消现场。
            timed_reminders: 当前轮从主进程快照冻结出的活跃提醒。

        Returns:
            OpenAI 兼容工具 schema 列表。
        """

        schemas: list[dict[str, Any]] = []
        schemas.extend(cognitive_tool_schemas())
        schemas.extend(activity_correction_tool_schemas(correctable_activities))
        schemas.extend(activity_control_tool_schemas(cancellable_activities))
        schemas.extend(timed_reminder_tool_schemas(timed_reminders))
        schemas.extend(recall_task_memory_tool_schemas())
        schemas.extend(load_recent_image_tool_schemas())
        # 工具存在表示产品具备这项稳定能力。配置缺失和槽位占用都由 prepare 返回
        # 可解释的安全事实，不能让模型靠工具忽隐忽现来猜运行状态。
        schemas.extend(start_background_work_tool_schemas())
        if has_pending_work_interaction:
            schemas.extend(respond_to_work_interaction_tool_schemas())
        if self._weather_tools is not None:
            schemas.extend(self._weather_tools.tool_schemas())
        schemas.extend(self._windows_application_tools.tool_schemas())
        return schemas

    def tool_catalog(self) -> list[ToolMetadata]:
        """返回 foreground 工具元数据目录。

        Returns:
            工具元数据列表。
        """

        catalog: list[ToolMetadata] = cognitive_tool_catalog()
        catalog.extend(recall_task_memory_tool_catalog())
        catalog.extend(load_recent_image_tool_catalog())
        catalog.extend(activity_correction_tool_catalog())
        catalog.extend(activity_control_tool_catalog())
        catalog.extend(timed_reminder_tool_catalog())
        catalog.extend(start_background_work_tool_catalog())
        if self._weather_tools is not None:
            catalog.extend(self._weather_tools.tool_catalog())
        catalog.extend(self._windows_application_tools.tool_catalog())
        return catalog

    def self_reflection_tools(self) -> tuple[SelfReflectionToolDefinition, ...]:
        """返回自我反思专用的静态最小工具集合。

        这里不调用普通 ``tool_schemas``，因此动态纠正工具、任务召回、工作交互和
        Cognitive 工具从未进入恢复请求的候选集合。配置缺失或后台队列瞬时忙碌也
        不会隐藏稳定入口，真实接纳结果仍由工具执行事务裁决。

        Returns:
            固定后台入口与静态安全 Inline 工具的完整声明和能力说明。
        """

        schemas = start_background_work_tool_schemas()
        catalog = start_background_work_tool_catalog()
        schemas.extend(load_recent_image_tool_schemas())
        catalog.extend(load_recent_image_tool_catalog())
        if self._weather_tools is not None:
            schemas.extend(self._weather_tools.tool_schemas())
            catalog.extend(self._weather_tools.tool_catalog())
        return SelfReflectionToolPolicy().select(
            schemas=schemas,
            metadata=catalog,
        )

    def tool_family_catalog(self) -> list[ToolFamilyMetadata]:
        """返回工具族元数据目录。

        Returns:
            工具族元数据列表，按 family_id 去重。
        """

        families: list[ToolFamilyMetadata] = cognitive_tool_family_catalog()
        families.extend(recall_task_memory_tool_family_catalog())
        families.extend(load_recent_image_tool_family_catalog())
        families.extend(activity_control_tool_family_catalog())
        families.extend(timed_reminder_tool_family_catalog())
        families.extend(start_background_work_tool_family_catalog())
        if self._weather_tools is not None:
            families.extend(self._weather_tools.tool_family_catalog())
        families.extend(self._windows_application_tools.tool_family_catalog())
        return _deduplicate_families(families)

    def tool_metadata(self, name: str) -> ToolMetadata:
        """按工具名返回元数据。

        Args:
            name: 工具函数名。

        Returns:
            匹配的工具元数据。

        Raises:
            KeyError: 工具名未注册。
        """

        for metadata in self.tool_catalog():
            if metadata.name == name:
                return metadata
        for metadata in respond_to_work_interaction_tool_catalog():
            if metadata.name == name:
                return metadata
        raise KeyError(f"unknown foreground tool metadata: {name}")

    def tool_family_metadata(self, family_id: str) -> ToolFamilyMetadata:
        """按 family_id 返回工具族元数据。

        Args:
            family_id: 工具族标识。

        Returns:
            匹配的工具族元数据。

        Raises:
            KeyError: 工具族未注册。
        """

        for metadata in self.tool_family_catalog():
            if metadata.family_id == family_id:
                return metadata
        raise KeyError(f"unknown foreground tool family metadata: {family_id}")

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        inline_execution_context: InlineExecutionContext | None = None,
    ) -> dict[str, Any]:
        """执行指定 foreground 同步工具并返回可写回模型的结果。

        Args:
            name: 工具函数名。
            arguments: 模型传入的工具参数。
            inline_execution_context: 从线程池提交时开始计时的取消与截止时间门禁。

        Returns:
            工具执行结果；长任务工具或未知工具会返回拒绝载荷。
        """

        # 工具参数可能携带用户材料或 URL，只记录稳定工具名，避免进入持久化 JSONL。
        log_event(logger, logging.INFO, "foreground_tool_execute", tool_name=name)
        if is_cognitive_tool(name):
            return {
                "ok": False,
                "text": "认知工具需要 foreground 运行时上下文，不能通过通用 foreground 工具服务执行。",
            }
        if is_activity_correction_tool(name):
            return {
                "ok": False,
                "text": "Activity 纠正需要当前 turn 上下文，不能通过通用 foreground 工具服务执行。",
            }
        if is_cancel_activity_tool(name):
            return {
                "ok": False,
                "text": "Activity 取消需要当前 turn 上下文，不能通过通用 foreground 工具服务执行。",
            }
        if is_timed_reminder_tool(name):
            return {
                "ok": False,
                "text": "定时提醒写操作需要主进程权威服务，不能通过通用 foreground 工具服务执行。",
            }
        if name == "start_background_work":
            return {
                "ok": False,
                "text": "后台工作需要当前 turn 上下文，不能通过通用 foreground 工具服务执行。",
            }
        if is_recall_task_memory_tool(name):
            return {
                "state": "failed",
                "reason": "task_memory_broker_required",
                "safe_message": "任务详情当前无法读取。",
            }

        if self._weather_tools is not None:
            weather_tool_names = {
                schema["function"]["name"]
                for schema in self._weather_tools.tool_schemas()
                if schema["type"] == "function"
            }
            if name in weather_tool_names:
                return self._weather_tools.execute(name, arguments)

        if is_windows_application_tool(name):
            return self._windows_application_tools.execute(
                name,
                arguments,
                inline_execution_context=inline_execution_context,
            )

        return {
            "ok": False,
            "text": f"不支持的 foreground 工具：{name}。",
        }

    def execution_timeout_seconds(self, name: str) -> float | None:
        """返回 Inline 工具的前台等待时限。

        Args:
            name: 工具函数名。

        Returns:
            天气工具为五秒；其它未知工具使用一秒保护时限。
        """

        try:
            metadata = self.tool_metadata(name)
        except KeyError:
            return 1.0
        if metadata.family_id == "weather":
            return 5.0
        if is_windows_application_tool(name):
            return 4.0
        if metadata.latency_class == ToolLatencyClass.INSTANT:
            return 1.0
        return None


def _deduplicate_families(families: list[ToolFamilyMetadata]) -> list[ToolFamilyMetadata]:
    """按 family_id 去重并保持首次出现顺序。

    Args:
        families: 原始工具族元数据列表。

    Returns:
        去重后的工具族元数据列表。
    """

    seen: set[str] = set()
    result: list[ToolFamilyMetadata] = []
    for family in families:
        if family.family_id in seen:
            continue
        seen.add(family.family_id)
        result.append(family)
    return result
