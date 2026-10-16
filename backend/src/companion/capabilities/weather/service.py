from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from companion.observability.logging_config import get_backend_logger
from companion.observability.structured_logging import log_event
from companion.capabilities.weather.client import QWeatherApiError, QWeatherClient
from companion.capabilities.weather.config import QWeatherConfig
from companion.capabilities.weather.location import LocalLocationResolver
from companion.capabilities.weather.models import LocationCandidate, LocationResolution, WeatherToolResult
from companion.capabilities.weather.normalizers import (
    NormalizedWeatherPayload,
    normalize_air_quality,
    normalize_air_quality_forecast,
    normalize_alerts,
    normalize_forecast,
    normalize_overview,
    normalize_sunrise_sunset,
)
from companion.capabilities.weather.summaries import (
    air_quality_forecast_text,
    air_quality_text,
    alerts_text,
    forecast_text,
    overview_text,
    sunrise_sunset_text,
)


logger = get_backend_logger(__name__)


class WeatherService:
    """面向前台工具的天气业务编排服务。"""

    def __init__(
        self,
        *,
        config: QWeatherConfig,
        client: QWeatherClient | Any,
        resolver: LocalLocationResolver | Any,
        today: Callable[[], date] | None = None,
    ) -> None:
        """初始化天气服务。

        Args:
            config: 和风天气运行时配置。
            client: 和风天气 HTTP 客户端。
            resolver: 地点解析器。
            today: 可注入的本地日期来源，便于测试稳定断言。
        """

        self._config = config
        self._client = client
        self._resolver = resolver
        self._today = today or date.today

    def overview(
        self,
        *,
        location: str,
        adm: str | None = None,
        country: str | None = None,
        lang: str = "zh",
        unit: str = "m",
    ) -> WeatherToolResult:
        """查询实时天气概览。"""

        resolved = self._resolve_or_error(location=location, adm=adm, country=country)
        if isinstance(resolved, WeatherToolResult):
            return resolved

        try:
            payload = self._client.get(
                "/v7/weather/now",
                {"location": resolved.location_id, "lang": lang, "unit": unit},
            )
        except QWeatherApiError as exception:
            return self._api_failure(
                operation="overview",
                resolved=resolved,
                exception=exception,
            )

        normalized = normalize_overview(payload)
        return self._ok_result(
            resolved=resolved,
            normalized=normalized,
            text=overview_text(resolved.name_zh, normalized.data),
            raw_payload=payload,
        )

    def forecast(
        self,
        *,
        location: str,
        days: int = 3,
        adm: str | None = None,
        country: str | None = None,
        lang: str = "zh",
        unit: str = "m",
    ) -> WeatherToolResult:
        """查询逐日天气预报。"""

        resolved = self._resolve_or_error(location=location, adm=adm, country=country)
        if isinstance(resolved, WeatherToolResult):
            return resolved

        requested_days = max(1, min(int(days), 7))
        # 和风逐日预报只支持离散档位；这里选择能覆盖用户请求范围的最小档位。
        api_days = 3 if requested_days <= 3 else 7
        try:
            payload = self._client.get(
                f"/v7/weather/{api_days}d",
                {"location": resolved.location_id, "lang": lang, "unit": unit},
            )
        except QWeatherApiError as exception:
            return self._api_failure(
                operation="forecast",
                resolved=resolved,
                exception=exception,
            )

        normalized = normalize_forecast(payload, days=requested_days)
        return self._ok_result(
            resolved=resolved,
            normalized=normalized,
            text=forecast_text(resolved.name_zh, requested_days, normalized.selected),
            raw_payload=payload,
        )

    def air_quality(
        self,
        *,
        location: str,
        adm: str | None = None,
        country: str | None = None,
        lang: str = "zh",
    ) -> WeatherToolResult:
        """查询实时空气质量。"""

        resolved = self._resolve_or_error(location=location, adm=adm, country=country)
        if isinstance(resolved, WeatherToolResult):
            return resolved

        path = f"/airquality/v1/current/{resolved.latitude}/{resolved.longitude}"
        try:
            payload = self._client.get(
                path,
                {"lang": lang},
                require_business_code=False,
            )
        except QWeatherApiError as exception:
            return self._api_failure(
                operation="air_quality",
                resolved=resolved,
                exception=exception,
            )

        normalized = normalize_air_quality(payload)
        return self._ok_result(
            resolved=resolved,
            normalized=normalized,
            text=air_quality_text(resolved.name_zh, normalized.selected),
            raw_payload=payload,
        )

    def air_quality_forecast(
        self,
        *,
        location: str,
        adm: str | None = None,
        country: str | None = None,
        lang: str = "zh",
    ) -> WeatherToolResult:
        """查询未来 3 天空气质量预报。"""

        resolved = self._resolve_or_error(location=location, adm=adm, country=country)
        if isinstance(resolved, WeatherToolResult):
            return resolved

        path = f"/airquality/v1/daily/{resolved.latitude}/{resolved.longitude}"
        try:
            payload = self._client.get(
                path,
                {"lang": lang, "localTime": "true"},
                require_business_code=False,
            )
        except QWeatherApiError as exception:
            return self._api_failure(
                operation="air_quality_forecast",
                resolved=resolved,
                exception=exception,
            )

        normalized = normalize_air_quality_forecast(payload)
        return self._ok_result(
            resolved=resolved,
            normalized=normalized,
            text=air_quality_forecast_text(
                resolved.name_zh,
                normalized.selected.get("day", {}),
                normalized.selected.get("primary_index", {}),
            ),
            raw_payload=payload,
        )

    def alerts(
        self,
        *,
        location: str,
        adm: str | None = None,
        country: str | None = None,
        lang: str = "zh",
    ) -> WeatherToolResult:
        """查询天气预警。"""

        resolved = self._resolve_or_error(location=location, adm=adm, country=country)
        if isinstance(resolved, WeatherToolResult):
            return resolved

        try:
            payload = self._client.get("/v7/warning/now", {"location": resolved.location_id, "lang": lang})
        except QWeatherApiError as exception:
            return self._api_failure(
                operation="alerts",
                resolved=resolved,
                exception=exception,
            )

        normalized = normalize_alerts(payload)
        return self._ok_result(
            resolved=resolved,
            normalized=normalized,
            text=alerts_text(resolved.name_zh, normalized.data["alerts"]),
            raw_payload=payload,
        )

    def sunrise_sunset(
        self,
        *,
        location: str,
        date_text: str | None = None,
        adm: str | None = None,
        country: str | None = None,
        lang: str = "zh",
    ) -> WeatherToolResult:
        """查询指定日期日出日落。"""

        resolved = self._resolve_or_error(location=location, adm=adm, country=country)
        if isinstance(resolved, WeatherToolResult):
            return resolved

        query_date, display_date = self._normalize_date(date_text)
        try:
            payload = self._client.get(
                "/v7/astronomy/sun",
                {"location": resolved.location_id, "date": query_date, "lang": lang},
            )
        except QWeatherApiError as exception:
            return self._api_failure(
                operation="sunrise_sunset",
                resolved=resolved,
                exception=exception,
            )

        normalized = normalize_sunrise_sunset(payload, display_date=display_date)
        return self._ok_result(
            resolved=resolved,
            normalized=normalized,
            text=sunrise_sunset_text(resolved.name_zh, normalized.data),
            raw_payload=payload,
        )

    def _resolve_or_error(
        self,
        *,
        location: str,
        adm: str | None,
        country: str | None,
    ) -> LocationCandidate | WeatherToolResult:
        """执行通用配置检查和地点解析。"""

        if not self._config.is_ready:
            return WeatherToolResult.fail(
                text="请先在 Console 的服务密钥中配置和风天气 API Host 与 API Key，天气工具才能查询实时数据。",
                reason="weather_config_missing",
            )

        resolution: LocationResolution = self._resolver.resolve(location, adm=adm, country=country)
        if resolution.status == "matched" and resolution.location is not None:
            return resolution.location

        if resolution.status == "ambiguous":
            return WeatherToolResult.fail(
                text="找到多个匹配地点，请补充行政区后再查询。",
                reason="weather_location_ambiguous",
                data={"candidates": [candidate.to_payload() for candidate in resolution.candidates]},
            )

        return WeatherToolResult.fail(
            text=f"未找到地点：{location}。",
            reason="weather_location_not_found",
        )

    def _api_failure(
        self,
        *,
        operation: str,
        resolved: LocationCandidate,
        exception: QWeatherApiError,
    ) -> WeatherToolResult:
        """把已分类 API 异常转换为安全工具结果和单条诊断事件。

        Args:
            operation: 本次天气查询的稳定操作名。
            resolved: 已解析的查询地点。
            exception: 天气客户端产生的预期异常。

        Returns:
            不泄露底层网络细节的失败工具结果。
        """

        log_event(
            logger,
            logging.WARNING,
            "weather_request_failed",
            operation=operation,
            location_id=resolved.location_id,
            reason=exception.reason,
            retryable=exception.retryable,
        )
        return WeatherToolResult.fail(
            text=exception.safe_message,
            reason=f"weather_{exception.reason}",
            retryable=exception.retryable,
            resolved_location=resolved.to_payload(),
        )

    def _normalize_date(self, date_text: str | None) -> tuple[str, str]:
        """把模型日期参数归一成和风查询日期和展示日期。

        Args:
            date_text: 模型传入的日期文本。

        Returns:
            第一项是和风需要的 YYYYMMDD，第二项是面向 LLM 和 TTS 的 YYYY-MM-DD。
        """

        normalized = (date_text or "").strip()
        if not normalized:
            current_date = self._today()
            return current_date.strftime("%Y%m%d"), current_date.strftime("%Y-%m-%d")

        try:
            parsed_date = datetime.strptime(normalized, "%Y-%m-%d").date()
            return parsed_date.strftime("%Y%m%d"), parsed_date.strftime("%Y-%m-%d")
        except ValueError:
            compact_date = normalized.replace("-", "")

        if len(compact_date) == 8 and compact_date.isdigit():
            return compact_date, f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:8]}"
        return compact_date, normalized

    def _ok_result(
        self,
        *,
        resolved: LocationCandidate,
        normalized: NormalizedWeatherPayload,
        text: str,
        raw_payload: dict[str, Any],
    ) -> WeatherToolResult:
        """把规整结果组装成天气工具成功响应。"""

        return WeatherToolResult.ok(
            text=text,
            data=normalized.data,
            resolved_location=resolved.to_payload(),
            observed_at=normalized.observed_at,
            updated_at=normalized.updated_at,
            quality=normalized.quality,
            warnings=normalized.warnings,
            debug_data={"qweather_response": raw_payload},
        )
