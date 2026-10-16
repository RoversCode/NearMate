"""定义不包含任务正文的后台工作用量模型。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentModelTokenUsage:
    """保存一个模型在一项后台工作中的四类 Token。

    Args:
        model: SDK 报告的模型标识。
        non_cached_input_tokens: 没有来自缓存的输入 Token。
        cache_creation_input_tokens: 本次写入缓存的输入 Token。
        cache_read_input_tokens: 本次从缓存读取的输入 Token。
        output_tokens: 模型生成的输出 Token。
    """

    model: str
    non_cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        """返回包含缓存创建与读取的总输入 Token。"""

        return (
            self.non_cached_input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def to_payload(self) -> dict[str, object]:
        """转换为可跨进程传递的去内容化字典。

        Returns:
            只包含模型名和四类 Token 的字典。
        """

        return {
            "model": self.model,
            "non_cached_input_tokens": self.non_cached_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_payload(cls, value: object) -> AgentModelTokenUsage:
        """校验并还原一条模型用量。

        Args:
            value: 跨进程终态中的未知对象。

        Returns:
            已通过非负整数校验的模型用量。

        Raises:
            ValueError: 模型名或任一计数不合法。
        """

        if not isinstance(value, dict):
            raise ValueError("模型用量必须是对象")
        model = value.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("模型用量缺少模型名")
        return cls(
            model=model.strip(),
            non_cached_input_tokens=_require_non_negative_int(
                value.get("non_cached_input_tokens"),
                "非缓存输入 Token",
            ),
            cache_creation_input_tokens=_require_non_negative_int(
                value.get("cache_creation_input_tokens"),
                "缓存创建 Token",
            ),
            cache_read_input_tokens=_require_non_negative_int(
                value.get("cache_read_input_tokens"),
                "缓存读取 Token",
            ),
            output_tokens=_require_non_negative_int(
                value.get("output_tokens"),
                "输出 Token",
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentTaskUsageSnapshot:
    """保存一项后台工作终止时可确认的用量快照。

    Args:
        by_model: 按模型合并后的四类 Token。
        is_complete: 是否收到 SDK 的权威 ``model_usage`` 终态汇总。
        reference_cost_usd: SDK 提供的美元参考成本；缺失时为空。
    """

    by_model: tuple[AgentModelTokenUsage, ...]
    is_complete: bool
    reference_cost_usd: float | None = None

    def to_payload(self) -> dict[str, object]:
        """转换为内部终态事件使用的去内容化字典。

        Returns:
            可安全跨进程传递的用量字段。
        """

        return {
            "is_complete": self.is_complete,
            "reference_cost_usd": self.reference_cost_usd,
            "by_model": [usage.to_payload() for usage in self.by_model],
        }

    @classmethod
    def from_payload(cls, value: object) -> AgentTaskUsageSnapshot:
        """校验并还原内部终态事件中的用量快照。

        Args:
            value: 终态事件携带的未知对象。

        Returns:
            已完成严格数值校验的用量快照。

        Raises:
            ValueError: 完整性、成本或模型用量不合法。
        """

        if not isinstance(value, dict):
            raise ValueError("任务用量必须是对象")
        is_complete = value.get("is_complete")
        if not isinstance(is_complete, bool):
            raise ValueError("任务用量完整性必须是布尔值")
        raw_cost = value.get("reference_cost_usd")
        reference_cost_usd = _optional_non_negative_float(raw_cost)
        raw_by_model = value.get("by_model")
        if not isinstance(raw_by_model, list):
            raise ValueError("任务用量模型列表必须是数组")
        by_model = tuple(AgentModelTokenUsage.from_payload(item) for item in raw_by_model)
        if len({item.model for item in by_model}) != len(by_model):
            raise ValueError("任务用量不能重复同一个模型")
        return cls(
            by_model=by_model,
            is_complete=is_complete,
            reference_cost_usd=reference_cost_usd,
        )


def _require_non_negative_int(value: object, field_label: str) -> int:
    """读取一个不接受布尔值的非负整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_label} 必须是非负整数")
    return value


def _optional_non_negative_float(value: object) -> float | None:
    """读取可空的非负有限浮点数。"""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("SDK 参考成本必须是非负有限数或空值")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("SDK 参考成本必须是非负有限数或空值")
    return normalized
