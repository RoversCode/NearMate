from __future__ import annotations

from typing import Any

from companion.capabilities.weather.knowledge import describe_air_quality_index
from companion.capabilities.weather.payload import text_field


def first_useful_forecast(items: list[dict[str, Any]]) -> dict[str, Any]:
    """选择首个包含天气预报业务信息的条目。

    Args:
        items: 已规整的逐日预报条目列表。

    Returns:
        首个至少包含日期、天气、温度或降水信息的条目；没有命中时返回空字典。
    """

    for item in items:
        if any(text_field(item, key) for key in ("date", "text_day", "temp_min", "temp_max", "precip")):
            return item
    return {}


def first_explainable_air_quality_index(items: list[dict[str, Any]]) -> dict[str, Any]:
    """选择首个能解释空气质量含义的指数条目。

    Args:
        items: 和风空气质量 indexes 条目列表。

    Returns:
        首个可直接提供类别或能通过指数代码和值推导类别的条目；没有命中时返回空字典。
    """

    for item in items:
        if air_quality_category(item):
            return item
    return {}


def first_air_quality_forecast_day(days: list[dict[str, Any]]) -> dict[str, Any]:
    """选择首个包含可解释空气质量指数的预报日。

    Args:
        days: 已规整的空气质量预报日列表。

    Returns:
        首个包含有效指数的预报日；没有命中时返回空字典。
    """

    for day in days:
        indexes = day.get("indexes")
        if isinstance(indexes, list) and first_explainable_air_quality_index(indexes):
            return day
    return {}


def air_quality_category(index: dict[str, Any]) -> str:
    """提取或推导空气质量指数类别。

    Args:
        index: 和风空气质量 indexes 条目。

    Returns:
        API 直接返回或按指数代码推导出的类别；无法可信解释时返回空字符串。
    """

    category = text_field(index, "category")
    if category:
        return category

    index_code = text_field(index, "code") or text_field(index, "indexCode") or text_field(index, "type")
    value = text_field(index, "aqiDisplay") or text_field(index, "aqi") or text_field(index, "value")
    if not index_code or not value:
        return ""
    return describe_air_quality_index(index_code, value)


def compact_air_quality_index(index: dict[str, Any]) -> dict[str, Any]:
    """生成适合 LLM 阅读的轻量空气质量指数。

    Args:
        index: 和风空气质量 indexes 条目。

    Returns:
        只保留名称、代码、数值、类别和健康建议的轻量字典。
    """

    health = index.get("health", {})
    advice = health.get("advice", {}) if isinstance(health, dict) else {}
    if isinstance(advice, dict):
        advice_parts = (
            text_field(advice, "generalPopulation"),
            text_field(advice, "sensitivePopulation"),
        )
    else:
        advice_parts = ()
    health_advice = "；".join(part for part in advice_parts if part)
    primary_pollutant = _pollutant_name(index.get("primaryPollutant"))
    return {
        "name": text_field(index, "name"),
        "code": (
            text_field(index, "code")
            or text_field(index, "indexCode")
            or text_field(index, "type")
        ),
        "aqi": text_field(index, "aqi"),
        "aqi_display": text_field(index, "aqiDisplay") or text_field(index, "aqi"),
        "category": air_quality_category(index),
        "primary_pollutant": primary_pollutant,
        "health_advice": health_advice,
    }


def _pollutant_name(value: Any) -> str:
    """提取污染物名称。"""

    if isinstance(value, dict):
        return text_field(value, "name")
    return str(value or "").strip()

