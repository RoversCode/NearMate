"""按 Tool Catalog 静态元数据构造自我反思最小工具白名单。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from companion.foreground_tools.catalog.metadata import (
    ToolLatencyClass,
    ToolMetadata,
)


SelfReflectionToolExecutionChannel = Literal["inline", "background"]

_BACKGROUND_TOOL_NAME = "start_background_work"
_SAFE_INLINE_FAMILY_IDS = frozenset({"weather", "recent_image_mount"})


@dataclass(frozen=True, slots=True)
class SelfReflectionToolDefinition:
    """保存一项经过静态安全策略确认的恢复工具。

    Args:
        name: 实际注册给模型的函数工具名。
        schema: 与执行器一致的 OpenAI 兼容工具声明副本。
        capability_description: 模型无需理解函数名也能读懂的权威能力说明。
        execution_channel: 工具在本轮使用 Inline 还是 Background 提交边界。
    """

    name: str
    schema: dict[str, object]
    capability_description: str
    execution_channel: SelfReflectionToolExecutionChannel

    def request_schema(self) -> dict[str, object]:
        """返回不会反向修改策略结果的工具声明副本。

        Returns:
            可直接放入模型请求的独立字典。
        """

        return deepcopy(self.schema)


class SelfReflectionToolPolicy:
    """只按开发者静态标记和能力族生成最小恢复白名单。"""

    def select(
        self,
        *,
        schemas: list[dict[str, object]],
        metadata: list[ToolMetadata],
    ) -> tuple[SelfReflectionToolDefinition, ...]:
        """从目录中选出稳定后台入口和显式安全的天气类 Inline 工具。

        这个接口故意不接收用户正文、失败原因、模型路由、配置可用性或队列容量。
        白名单只决定“允许模型考虑哪些能力”，真实适用性与瞬时接纳仍由 Thinking
        Foreground 和各工具的 ``prepare`` 边界裁决。

        Args:
            schemas: Tool Catalog 当前稳定登记的函数工具声明。
            metadata: 与工具声明同源的静态元数据。

        Returns:
            按原 schema 顺序排列的最小工具定义元组。

        Raises:
            ValueError: 目录有重复名称、说明为空或 schema 结构无效。
        """

        metadata_by_name: dict[str, ToolMetadata] = {}
        for item in metadata:
            if item.name in metadata_by_name:
                raise ValueError(f"工具元数据名称重复：{item.name}")
            if not item.capability_description.strip():
                raise ValueError(
                    f"{item.name} 的 capability_description 不能为空"
                )
            metadata_by_name[item.name] = item

        schema_by_name: dict[str, dict[str, object]] = {}
        schema_order: list[str] = []
        for schema in schemas:
            name = _schema_name(schema)
            if name in schema_by_name:
                raise ValueError(f"工具 schema 名称重复：{name}")
            schema_by_name[name] = schema
            schema_order.append(name)

        selected: list[SelfReflectionToolDefinition] = []
        for name in schema_order:
            item = metadata_by_name.get(name)
            if item is None:
                continue
            channel = _allowed_execution_channel(item)
            if channel is None:
                continue
            selected.append(
                SelfReflectionToolDefinition(
                    name=name,
                    schema=deepcopy(schema_by_name[name]),
                    capability_description=item.capability_description,
                    execution_channel=channel,
                )
            )
        return tuple(selected)


def _allowed_execution_channel(
    metadata: ToolMetadata,
) -> SelfReflectionToolExecutionChannel | None:
    """按固定后台入口和静态 Inline 安全标记决定执行通道。

    Args:
        metadata: 单项工具的权威静态元数据。

    Returns:
        允许时返回执行通道，不在白名单时返回空。
    """

    if metadata.name == _BACKGROUND_TOOL_NAME:
        return "background"
    if not metadata.self_reflection_safe:
        return None
    if metadata.family_id not in _SAFE_INLINE_FAMILY_IDS:
        return None
    if metadata.latency_class is ToolLatencyClass.LONG_RUNNING:
        return None
    return "inline"


def _schema_name(schema: dict[str, object]) -> str:
    """从 OpenAI 函数工具声明中读取并验证稳定名称。

    Args:
        schema: 待验证的工具声明。

    Returns:
        去除空白后的函数工具名。

    Raises:
        ValueError: 声明不是函数工具或缺少名称。
    """

    if schema.get("type") != "function":
        raise ValueError("自我反思只接受 function 工具 schema")
    function = schema.get("function")
    if not isinstance(function, dict):
        raise ValueError("工具 schema 缺少 function 对象")
    name_value = function.get("name")
    name = name_value.strip() if isinstance(name_value, str) else ""
    if not name:
        raise ValueError("工具 schema 缺少 function.name")
    return name
