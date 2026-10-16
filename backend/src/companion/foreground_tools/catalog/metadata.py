from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ToolLatencyClass(StrEnum):
    """描述工具的等待体感类型。"""

    INSTANT = "instant"
    EXTERNAL_API = "external_api"
    LONG_RUNNING = "long_running"


@dataclass(frozen=True)
class ToolMetadata:
    """描述一个可供前台模型调用的微工具。

    Args:
        name: 工具函数名，必须与 OpenAI tools schema 中的 function.name 一致。
        display_name: 控制台展示名。
        family_id: 工具族标识，用于批量管理等待反馈策略。
        family_display_name: 工具族展示名。
        latency_class: 工具体感耗时类型。
        capability_description: 不依赖函数名也能理解的完整业务能力与限制说明。
        self_reflection_safe: 开发者是否静态允许自我反思考虑这项工具。
    """

    name: str
    display_name: str
    family_id: str
    family_display_name: str
    latency_class: ToolLatencyClass
    capability_description: str
    self_reflection_safe: bool

    def __post_init__(self) -> None:
        """规范化目录文本并拒绝含糊或类型错误的安全元数据。"""

        for field_name in (
            "name",
            "display_name",
            "family_id",
            "family_display_name",
            "capability_description",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
            object.__setattr__(self, field_name, value.strip())
        if not isinstance(self.latency_class, ToolLatencyClass):
            raise ValueError("latency_class 必须是 ToolLatencyClass")
        if not isinstance(self.self_reflection_safe, bool):
            raise ValueError("self_reflection_safe 必须是布尔值")

    def to_payload(self) -> dict[str, object]:
        """转换为控制台 API 可直接返回的字典。

        Returns:
            包含工具身份、延迟等级、能力说明和恢复安全标记的字典。
        """

        return {
            "name": self.name,
            "display_name": self.display_name,
            "family_id": self.family_id,
            "family_display_name": self.family_display_name,
            "latency_class": self.latency_class.value,
            "capability_description": self.capability_description,
            "self_reflection_safe": self.self_reflection_safe,
        }


@dataclass(frozen=True)
class ToolFamilyMetadata:
    """描述一组语义相近工具的默认等待反馈策略。

    Args:
        family_id: 工具族标识。
        display_name: 控制台展示名。
        latency_class: family 默认体感耗时类型。
        waiting_feedback_eligible: 是否允许为该工具族配置 Inline 等待反馈。
        default_waiting_enabled: 是否默认开启等待反馈。
        default_waiting_phrases: 默认等待句列表。
    """

    family_id: str
    display_name: str
    latency_class: ToolLatencyClass
    waiting_feedback_eligible: bool
    default_waiting_enabled: bool
    default_waiting_phrases: list[str]

    def to_payload(self) -> dict[str, object]:
        """转换为控制台 API 可直接返回的字典。"""

        return {
            "family_id": self.family_id,
            "display_name": self.display_name,
            "latency_class": self.latency_class.value,
            "waiting_feedback_eligible": self.waiting_feedback_eligible,
            "default_waiting_enabled": self.default_waiting_enabled,
            "default_waiting_phrases": list(self.default_waiting_phrases),
        }
