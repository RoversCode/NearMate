from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class LocationCandidate(BaseModel):
    """标准化地点候选。

    Args:
        location_id: 和风天气 LocationID。
        name_zh: 中文地点名。
        name_en: 英文地点名。
        country_code: ISO 国家或地区代码。
        country_name_zh: 中文国家或地区名。
        adm1_zh: 一级行政区中文名。
        adm2_zh: 二级行政区中文名。
        timezone: 地点时区。
        latitude: 纬度。
        longitude: 经度。
    """

    location_id: str
    name_zh: str
    name_en: str = ""
    country_code: str = ""
    country_name_zh: str = ""
    adm1_zh: str = ""
    adm2_zh: str = ""
    timezone: str = ""
    latitude: float
    longitude: float

    def display_name(self) -> str:
        """返回适合展示给模型和用户的地点名称。

        Returns:
            包含行政区上下文的中文地点名。
        """

        parts = [self.adm1_zh, self.adm2_zh, self.name_zh]
        normalized_parts = [part for part in parts if part]
        if not normalized_parts:
            return self.name_en or self.location_id
        return " / ".join(dict.fromkeys(normalized_parts))

    def to_payload(self) -> dict[str, Any]:
        """转换为工具结果中的结构化地点载荷。

        Returns:
            可 JSON 序列化的地点字典。
        """

        return {
            "location_id": self.location_id,
            "name_zh": self.name_zh,
            "name_en": self.name_en,
            "country_code": self.country_code,
            "country_name_zh": self.country_name_zh,
            "adm1_zh": self.adm1_zh,
            "adm2_zh": self.adm2_zh,
            "timezone": self.timezone,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


class LocationResolution(BaseModel):
    """地点解析结果。"""

    status: Literal["matched", "ambiguous", "not_found"]
    location: LocationCandidate | None = None
    candidates: list[LocationCandidate] = Field(default_factory=list)

    @classmethod
    def matched(cls, location: LocationCandidate) -> LocationResolution:
        """创建单一命中的地点解析结果。"""

        return cls(status="matched", location=location, candidates=[location])

    @classmethod
    def ambiguous(cls, candidates: list[LocationCandidate]) -> LocationResolution:
        """创建需要用户进一步确认的地点解析结果。"""

        return cls(status="ambiguous", candidates=candidates)

    @classmethod
    def not_found(cls) -> LocationResolution:
        """创建未命中的地点解析结果。"""

        return cls(status="not_found", candidates=[])


WeatherResultQuality = Literal["complete", "partial", "empty"]


class WeatherToolResult:
    """天气工具统一返回结构。"""

    def __init__(
        self,
        *,
        ok: bool,
        text: str,
        data: dict[str, Any] | None = None,
        source: str = "qweather",
        resolved_location: dict[str, Any] | None = None,
        observed_at: str | None = None,
        updated_at: str | None = None,
        quality: WeatherResultQuality = "complete",
        warnings: list[str] | None = None,
        debug_data: dict[str, Any] | None = None,
        reason: str = "",
        retryable: bool = False,
    ) -> None:
        """初始化天气工具结果。

        Args:
            ok: 工具是否成功完成。
            text: 给 LLM 直接阅读的中文摘要。
            data: 标准化结构化数据。
            source: 数据来源标识。
            resolved_location: 标准化地点信息。
            observed_at: 数据观测时间。
            updated_at: 数据更新时间。
            quality: 成功响应的数据完整度标记。
            warnings: 只面向模型和监控的短警告码。
            debug_data: 调试专用原始数据，不进入 LLM tool message。
            reason: 失败时供编排层判断的稳定原因码。
            retryable: 失败是否可能通过稍后重试恢复。
        """

        self.ok = ok
        self.text = text
        self.data = data or {}
        self.source = source
        self.resolved_location = resolved_location
        self.observed_at = observed_at
        self.updated_at = updated_at
        self.quality = quality
        self.warnings = warnings or []
        self.debug_data = debug_data or {}
        self.reason = reason
        self.retryable = retryable

    @classmethod
    def ok(
        cls,
        *,
        text: str,
        data: dict[str, Any] | None = None,
        resolved_location: dict[str, Any] | None = None,
        observed_at: str | None = None,
        updated_at: str | None = None,
        quality: WeatherResultQuality = "complete",
        warnings: list[str] | None = None,
        debug_data: dict[str, Any] | None = None,
    ) -> WeatherToolResult:
        """创建成功工具结果。"""

        return cls(
            ok=True,
            text=text,
            data=data or {},
            resolved_location=resolved_location,
            observed_at=observed_at,
            updated_at=updated_at,
            quality=quality,
            warnings=warnings,
            debug_data=debug_data,
        )

    @classmethod
    def fail(
        cls,
        *,
        text: str,
        reason: str,
        retryable: bool = False,
        data: dict[str, Any] | None = None,
        resolved_location: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        debug_data: dict[str, Any] | None = None,
    ) -> WeatherToolResult:
        """创建失败工具结果。"""

        return cls(
            ok=False,
            text=text,
            data=data or {},
            resolved_location=resolved_location,
            quality="empty",
            warnings=warnings,
            debug_data=debug_data,
            reason=reason,
            retryable=retryable,
        )

    def to_tool_payload(self) -> dict[str, Any]:
        """转换为前台工具链路使用的字典结果。"""

        payload: dict[str, Any] = {
            "ok": self.ok,
            "state": "completed" if self.ok else "failed",
            "text": self.text,
            "data": self.data,
            "source": self.source,
            "quality": self.quality,
            "warnings": self.warnings,
        }
        if not self.ok:
            payload["reason"] = self.reason
            payload["retryable"] = self.retryable
        if self.resolved_location is not None:
            payload["resolved_location"] = self.resolved_location
        if self.observed_at is not None:
            payload["observed_at"] = self.observed_at
        if self.updated_at is not None:
            payload["updated_at"] = self.updated_at
        return payload
