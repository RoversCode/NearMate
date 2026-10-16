from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from companion.capabilities.weather.models import WeatherResultQuality
from companion.capabilities.weather.payload import list_field, object_field, object_items, text_field
from companion.capabilities.weather.selectors import (
    compact_air_quality_index,
    first_air_quality_forecast_day,
    first_explainable_air_quality_index,
    first_useful_forecast,
)


@dataclass(frozen=True)
class NormalizedWeatherPayload:
    """天气工具轻量业务载荷。

    Args:
        data: 可以进入 LLM tool message 的轻量结构化数据。
        quality: 数据完整度标记。
        warnings: 短警告码列表。
        observed_at: 数据观测时间。
        updated_at: 数据更新时间。
        selected: 生成摘要时需要的首个有效条目。
    """

    data: dict[str, Any]
    quality: WeatherResultQuality
    warnings: list[str] = field(default_factory=list)
    observed_at: str | None = None
    updated_at: str | None = None
    selected: dict[str, Any] = field(default_factory=dict)


def normalize_overview(payload: dict[str, Any]) -> NormalizedWeatherPayload:
    """规整实时天气响应。

    Args:
        payload: 和风实时天气响应。

    Returns:
        不包含原始响应的轻量实时天气载荷。
    """

    now = object_field(payload, "now")
    data = {
        "text": text_field(now, "text"),
        "temp": text_field(now, "temp"),
        "feels_like": text_field(now, "feelsLike"),
        "humidity": text_field(now, "humidity"),
        "wind_dir": text_field(now, "windDir"),
        "wind_scale": text_field(now, "windScale"),
        "precip": text_field(now, "precip"),
    }
    useful_values = [value for value in data.values() if value]
    if not now or not useful_values:
        return NormalizedWeatherPayload(
            data=data,
            quality="empty",
            warnings=["missing_now"] if not now else ["missing_now_data"],
            observed_at=text_field(now, "obsTime") or None,
            updated_at=text_field(payload, "updateTime") or None,
        )

    missing_core = [field_name for field_name in ("text", "temp", "humidity") if not data[field_name]]
    return NormalizedWeatherPayload(
        data=data,
        quality="partial" if missing_core else "complete",
        warnings=[f"missing_{field_name}" for field_name in missing_core],
        observed_at=text_field(now, "obsTime") or None,
        updated_at=text_field(payload, "updateTime") or None,
    )


def normalize_forecast(payload: dict[str, Any], *, days: int) -> NormalizedWeatherPayload:
    """规整逐日天气预报响应。

    Args:
        payload: 和风逐日预报响应。
        days: 调用方请求的预报天数。

    Returns:
        不包含原始响应的轻量预报载荷。
    """

    daily_items = list_field(payload, "daily")
    # 上游可能为了匹配 API 档位多取数据，这里只把用户请求范围内的预报交给 LLM。
    forecasts = [
        {
            "date": text_field(item, "fxDate"),
            "text_day": text_field(item, "textDay"),
            "temp_min": text_field(item, "tempMin"),
            "temp_max": text_field(item, "tempMax"),
            "precip": text_field(item, "precip"),
        }
        for item in object_items(daily_items)[:days]
    ]
    first = first_useful_forecast(forecasts)
    data = {"days": days, "forecast": forecasts}
    if not first:
        return NormalizedWeatherPayload(
            data=data,
            quality="empty",
            warnings=["missing_daily"] if not daily_items else ["missing_forecast"],
            updated_at=text_field(payload, "updateTime") or None,
            selected={},
        )

    missing_temperature = not first.get("temp_min") and not first.get("temp_max")
    return NormalizedWeatherPayload(
        data=data,
        quality="partial" if missing_temperature else "complete",
        warnings=["missing_temperature"] if missing_temperature else [],
        updated_at=text_field(payload, "updateTime") or None,
        selected=first,
    )


def normalize_air_quality(payload: dict[str, Any]) -> NormalizedWeatherPayload:
    """规整实时空气质量响应。

    Args:
        payload: 和风实时空气质量响应。

    Returns:
        不包含原始响应的轻量空气质量载荷。
    """

    indexes = object_items(list_field(payload, "indexes"))
    primary_index = first_explainable_air_quality_index(indexes)
    pollutants = [_compact_pollutant(item) for item in object_items(list_field(payload, "pollutants"))[:6]]
    if not primary_index:
        return NormalizedWeatherPayload(
            data={"primary_index": {}, "pollutants": pollutants},
            quality="empty",
            warnings=["missing_indexes"] if not indexes else ["invalid_air_quality_index"],
            updated_at=text_field(payload, "updateTime") or None,
            selected={},
        )

    compact_index = compact_air_quality_index(primary_index)
    return NormalizedWeatherPayload(
        data={"primary_index": compact_index, "pollutants": pollutants},
        quality="complete",
        updated_at=text_field(payload, "updateTime") or None,
        selected=compact_index,
    )


def normalize_air_quality_forecast(payload: dict[str, Any]) -> NormalizedWeatherPayload:
    """规整空气质量预报响应。

    Args:
        payload: 和风空气质量预报响应。

    Returns:
        不包含原始响应的轻量空气质量预报载荷。
    """

    raw_days = object_items(list_field(payload, "days"))
    days = [
        {
            "forecast_start_time": text_field(item, "forecastStartTime"),
            "forecast_end_time": text_field(item, "forecastEndTime"),
            "indexes": object_items(list_field(item, "indexes")),
            "pollutants": [_compact_pollutant(pollutant) for pollutant in object_items(list_field(item, "pollutants"))[:6]],
        }
        for item in raw_days
    ]
    first_day = first_air_quality_forecast_day(days)
    if not first_day:
        return NormalizedWeatherPayload(
            data={"days": []},
            quality="empty",
            warnings=["missing_days"] if not raw_days else ["invalid_air_quality_index"],
            selected={},
        )

    primary_index = first_explainable_air_quality_index(first_day.get("indexes", []))
    compact_index = compact_air_quality_index(primary_index) if primary_index else {}
    compact_days = [
        {
            "forecast_start_time": day["forecast_start_time"],
            "forecast_end_time": day["forecast_end_time"],
            "primary_index": compact_air_quality_index(index) if (index := first_explainable_air_quality_index(day.get("indexes", []))) else {},
            "pollutants": day["pollutants"],
        }
        for day in days[:3]
    ]
    return NormalizedWeatherPayload(
        data={"days": compact_days},
        quality="complete" if compact_index else "empty",
        warnings=[] if compact_index else ["invalid_air_quality_index"],
        selected={"day": first_day, "primary_index": compact_index},
    )


def normalize_alerts(payload: dict[str, Any]) -> NormalizedWeatherPayload:
    """规整天气预警响应。

    Args:
        payload: 和风天气预警响应。

    Returns:
        不包含原始响应的轻量预警载荷。
    """

    raw_alerts = object_items(list_field(payload, "warning"))
    alerts = [
        {
            "id": text_field(item, "id"),
            "title": text_field(item, "title"),
            "type": text_field(item, "typeName") or text_field(item, "type"),
            "level": text_field(item, "level"),
            "severity": text_field(item, "severity"),
            "status": text_field(item, "status"),
            "start_time": text_field(item, "startTime"),
            "end_time": text_field(item, "endTime"),
        }
        for item in raw_alerts
    ]
    if not alerts:
        return NormalizedWeatherPayload(
            data={"alerts": []},
            quality="empty",
            warnings=["missing_warning"],
            updated_at=text_field(payload, "updateTime") or None,
        )

    return NormalizedWeatherPayload(
        data={"alerts": alerts},
        quality="complete",
        updated_at=text_field(payload, "updateTime") or None,
        selected=alerts[0],
    )


def normalize_sunrise_sunset(payload: dict[str, Any], *, display_date: str) -> NormalizedWeatherPayload:
    """规整日出日落响应。

    Args:
        payload: 和风日出日落响应。
        display_date: 面向 LLM 和 TTS 的可读日期。

    Returns:
        不包含原始响应的轻量日出日落载荷。
    """

    sunrise = text_field(payload, "sunrise")
    sunset = text_field(payload, "sunset")
    warnings: list[str] = []
    if not sunrise:
        warnings.append("missing_sunrise")
    if not sunset:
        warnings.append("missing_sunset")

    data = {
        "date": display_date,
        "sunrise": sunrise or "暂无数据",
        "sunset": sunset or "暂无数据",
    }
    return NormalizedWeatherPayload(
        data=data,
        quality="empty" if len(warnings) == 2 else "partial" if warnings else "complete",
        warnings=warnings,
    )


def _compact_pollutant(item: dict[str, Any]) -> dict[str, Any]:
    """生成轻量污染物条目。

    Args:
        item: 和风空气质量 pollutants 条目。

    Returns:
        只保留污染物名称、代码和浓度的轻量字典。
    """

    concentration = object_field(item, "concentration")
    concentration_value = text_field(concentration, "value") or text_field(
        item,
        "value",
    )
    concentration_unit = text_field(concentration, "unit") or text_field(
        item,
        "unit",
    )
    return {
        "name": text_field(item, "name"),
        "code": text_field(item, "code"),
        "concentration": concentration_value,
        "unit": concentration_unit,
    }

