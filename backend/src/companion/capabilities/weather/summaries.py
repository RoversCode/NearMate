from __future__ import annotations

from typing import Any


def overview_text(location: str, data: dict[str, Any]) -> str:
    """生成实时天气摘要。

    Args:
        location: 已解析地点名。
        data: 规整后的实时天气字段。

    Returns:
        只包含可信字段的中文摘要。
    """

    parts: list[str] = []
    if data["text"]:
        parts.append(data["text"])
    if data["temp"]:
        parts.append(temperature_text(data["temp"]))
    if data["feels_like"]:
        parts.append(f"体感{temperature_text(data['feels_like'])}")
    if data["humidity"]:
        parts.append(f"湿度{percent_text(data['humidity'])}")
    if data["wind_dir"] or data["wind_scale"]:
        wind_text = f"{data['wind_dir']}{data['wind_scale'] + '级' if data['wind_scale'] else ''}"
        parts.append(wind_text)
    if data["precip"]:
        parts.append(f"降水量{millimeter_text(data['precip'])}")

    if not parts:
        return f"{location}当前天气暂无完整实时数据。"
    return f"{location}当前天气：" + "，".join(parts) + "。"


def forecast_text(location: str, days: int, first: dict[str, Any]) -> str:
    """生成逐日预报摘要。

    Args:
        location: 已解析地点名。
        days: 查询天数。
        first: 首个有效预报条目。

    Returns:
        只包含可信字段的中文摘要。
    """

    if not first:
        return f"{location}未来 {days} 天天气预报暂无数据。"

    title = " ".join(part for part in (first.get("date", ""), first.get("text_day", "")) if part) or "首日"
    temperature = temperature_range_text(str(first.get("temp_min", "")), str(first.get("temp_max", "")))
    return f"{location}未来 {days} 天天气预报：{title}，{temperature}。"


def air_quality_text(location: str, primary_index: dict[str, Any]) -> str:
    """生成实时空气质量摘要。

    Args:
        location: 已解析地点名。
        primary_index: 规整后的首要空气质量指数。

    Returns:
        适合 LLM 和 TTS 的中文摘要。
    """

    if not primary_index:
        return f"{location}当前暂无完整空气质量数据。"

    category = primary_index.get("category") or "未知"
    aqi = primary_index.get("aqi") or primary_index.get("aqi_display")
    return f"{location}当前空气质量{category}" + (f"，空气质量指数{aqi}。" if aqi else "。")


def air_quality_forecast_text(location: str, first_day: dict[str, Any], primary_index: dict[str, Any]) -> str:
    """生成空气质量预报摘要。

    Args:
        location: 已解析地点名。
        first_day: 首个有效空气质量预报日。
        primary_index: 首个有效空气质量指数。

    Returns:
        适合 LLM 和 TTS 的中文摘要。
    """

    if not first_day:
        return f"{location}未来 3 天暂无空气质量预报数据。"
    if not primary_index:
        return f"{location}未来 3 天暂无完整空气质量预报数据。"

    index_name = air_index_name(primary_index.get("name", "AQI"))
    aqi_display = primary_index.get("aqi_display") or primary_index.get("aqi") or "暂无数据"
    category = primary_index.get("category") or "未知"
    primary_pollutant = primary_index.get("primary_pollutant", "")
    health_advice = str(primary_index.get("health_advice", "")).strip()

    text = f"{location}未来 3 天空气质量预报：{index_name}{aqi_display}，{category}"
    if primary_pollutant:
        text += f"，首要污染物 {primary_pollutant}"
    if health_advice:
        text += f"。健康建议：{health_advice.rstrip('。.!！')}"
    return text + "。"


def alerts_text(location: str, alerts: list[dict[str, Any]]) -> str:
    """生成天气预警摘要。

    Args:
        location: 已解析地点名。
        alerts: 规整后的天气预警列表。

    Returns:
        天气预警中文摘要。
    """

    if not alerts:
        return f"{location}当前暂无天气预警。"

    first = alerts[0]
    return f"{location}当前有 {len(alerts)} 条天气预警：{first.get('title') or '预警信息'}。"


def sunrise_sunset_text(location: str, data: dict[str, Any]) -> str:
    """生成日出日落摘要。

    Args:
        location: 已解析地点名。
        data: 规整后的日出日落字段。

    Returns:
        使用可读日期的中文摘要。
    """

    return f"{location}{data['date']} 日出{data['sunrise']}，日落{data['sunset']}。"


def temperature_text(value: str) -> str:
    """生成摄氏度表达。"""

    return f"{value}摄氏度" if value else "暂无温度数据"


def temperature_range_text(temp_min: str, temp_max: str) -> str:
    """生成温度范围表达。"""

    if temp_min and temp_max:
        return f"{temp_min}到{temp_max}摄氏度"
    if temp_min:
        return f"最低{temp_min}摄氏度"
    if temp_max:
        return f"最高{temp_max}摄氏度"
    return "温度暂无数据"


def percent_text(value: str) -> str:
    """生成百分比中文表达。"""

    return f"百分之{value}" if value else "未知"


def millimeter_text(value: str) -> str:
    """生成毫米单位中文表达。"""

    return f"{value}毫米" if value else "未知毫米"


def air_index_name(value: Any) -> str:
    """生成空气质量指数名称。"""

    normalized_value = str(value or "").strip()
    if normalized_value.upper() == "AQI":
        return "空气质量指数"
    return normalized_value or "空气质量指数"
