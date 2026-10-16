from __future__ import annotations

from pathlib import Path
from typing import Any

from companion.core.runtime_paths import BACKEND_ROOT
from companion.foreground_tools.catalog.metadata import ToolFamilyMetadata, ToolLatencyClass, ToolMetadata
from companion.capabilities.weather.auth import ApiKeyAuthProvider, MissingAuthProvider
from companion.capabilities.weather.client import QWeatherClient
from companion.capabilities.weather.config import QWeatherConfig
from companion.capabilities.weather.location import LocalLocationResolver
from companion.capabilities.weather.service import WeatherService


WEATHER_OVERVIEW_TOOL = "weather_overview"
WEATHER_FORECAST_TOOL = "weather_forecast"
WEATHER_AIR_QUALITY_TOOL = "weather_air_quality"
WEATHER_AIR_QUALITY_FORECAST_TOOL = "weather_air_quality_forecast"
WEATHER_ALERTS_TOOL = "weather_alerts"
WEATHER_SUNRISE_SUNSET_TOOL = "weather_sunrise_sunset"
DEFAULT_CITY_CSV_PATH = BACKEND_ROOT / "ckpt" / "LocationList" / "China-City-List-latest.csv"
WEATHER_FAMILY_ID = "weather"
WEATHER_FAMILY_DISPLAY_NAME = "天气"
WEATHER_DEFAULT_WAITING_PHRASES = ["好，我查一下天气。"]
WEATHER_TOOL_DISPLAY_NAMES = {
    WEATHER_OVERVIEW_TOOL: "实时天气",
    WEATHER_FORECAST_TOOL: "天气预报",
    WEATHER_AIR_QUALITY_TOOL: "实时空气质量",
    WEATHER_AIR_QUALITY_FORECAST_TOOL: "空气质量预报",
    WEATHER_ALERTS_TOOL: "天气预警",
    WEATHER_SUNRISE_SUNSET_TOOL: "日出日落",
}
WEATHER_TOOL_DESCRIPTIONS = {
    WEATHER_OVERVIEW_TOOL: "查询指定地点的实时天气概览，包括温度、体感、湿度、风、降水和观测时间。",
    WEATHER_FORECAST_TOOL: "查询指定地点未来 1 到 7 天逐日天气预报。",
    WEATHER_AIR_QUALITY_TOOL: "查询指定地点当前空气质量。",
    WEATHER_AIR_QUALITY_FORECAST_TOOL: "查询指定地点未来 3 天空气质量预报。",
    WEATHER_ALERTS_TOOL: "查询指定地点当前天气预警；无预警时返回明确空结果。",
    WEATHER_SUNRISE_SUNSET_TOOL: "查询指定地点指定日期的日出日落时间。",
}


class WeatherForegroundTool:
    """把天气能力适配成 foreground LLM 可调用工具。"""

    def __init__(self, service: WeatherService | Any) -> None:
        """初始化天气 foreground 工具适配器。

        Args:
            service: 天气业务服务。

        Returns:
            无。
        """

        self._service = service

    @classmethod
    def from_config(
        cls,
        config: QWeatherConfig,
        *,
        city_csv_path: Path | None = None,
    ) -> WeatherForegroundTool:
        """从配置构造默认天气 foreground 工具适配器。

        Args:
            config: 和风天气配置。
            city_csv_path: 可选城市表路径。

        Returns:
            可注册到 ForegroundToolService 的天气工具适配器。
        """

        auth_provider = ApiKeyAuthProvider(config.api_key) if config.api_key else MissingAuthProvider()
        client = QWeatherClient(config=config, auth_provider=auth_provider)
        resolver = LocalLocationResolver(city_csv_path or DEFAULT_CITY_CSV_PATH)
        return cls(WeatherService(config=config, client=client, resolver=resolver))

    def tool_schemas(self) -> list[dict[str, Any]]:
        """返回天气工具声明列表。

        Returns:
            OpenAI 兼容天气工具 schema 列表。
        """

        return [
            _schema(
                name=WEATHER_OVERVIEW_TOOL,
                description=WEATHER_TOOL_DESCRIPTIONS[WEATHER_OVERVIEW_TOOL],
                properties=_common_location_properties(include_unit=True),
                required=["location"],
            ),
            _schema(
                name=WEATHER_FORECAST_TOOL,
                description=WEATHER_TOOL_DESCRIPTIONS[WEATHER_FORECAST_TOOL],
                properties={
                    **_common_location_properties(include_unit=True),
                    "days": {
                        "type": "integer",
                        "description": "预报天数，默认 3，最大 7。",
                        "minimum": 1,
                        "maximum": 7,
                    },
                },
                required=["location"],
            ),
            _schema(
                name=WEATHER_AIR_QUALITY_TOOL,
                description=WEATHER_TOOL_DESCRIPTIONS[WEATHER_AIR_QUALITY_TOOL],
                properties=_common_location_properties(include_unit=False),
                required=["location"],
            ),
            # _schema(
            #     name=WEATHER_AIR_QUALITY_FORECAST_TOOL,
            #     description="查询指定地点未来 3 天空气质量 AQI 预报、污染物浓度值和健康建议。",
            #     properties=_common_location_properties(include_unit=False),
            #     required=["location"],
            # ),
            # _schema(
            #     name=WEATHER_ALERTS_TOOL,
            #     description="查询指定地点当前天气预警；无预警时返回明确空结果。",
            #     properties=_common_location_properties(include_unit=False),
            #     required=["location"],
            # ),
            _schema(
                name=WEATHER_SUNRISE_SUNSET_TOOL,
                description=WEATHER_TOOL_DESCRIPTIONS[WEATHER_SUNRISE_SUNSET_TOOL],
                properties={
                    **_common_location_properties(include_unit=False),
                    "date": {
                        "type": "string",
                        "description": "查询日期，格式 YYYY-MM-DD；不传则使用本地今天。",
                    },
                },
                required=["location"],
            ),
        ]

    def tool_catalog(self) -> list[ToolMetadata]:
        """返回天气工具元数据目录。

        Returns:
            天气工具元数据列表。
        """

        return [
            ToolMetadata(
                name=schema["function"]["name"],
                display_name=WEATHER_TOOL_DISPLAY_NAMES[schema["function"]["name"]],
                family_id=WEATHER_FAMILY_ID,
                family_display_name=WEATHER_FAMILY_DISPLAY_NAME,
                latency_class=ToolLatencyClass.EXTERNAL_API,
                capability_description=(
                    f"{WEATHER_TOOL_DESCRIPTIONS[schema['function']['name']]}"
                    "只读取真实天气服务结果，不操作文件、不改变其它任务，也不能保证第三方服务始终可用。"
                ),
                self_reflection_safe=True,
            )
            for schema in self.tool_schemas()
        ]

    def tool_family_catalog(self) -> list[ToolFamilyMetadata]:
        """返回天气工具族元数据。

        Returns:
            天气工具族元数据列表。
        """

        return [
            ToolFamilyMetadata(
                family_id=WEATHER_FAMILY_ID,
                display_name=WEATHER_FAMILY_DISPLAY_NAME,
                latency_class=ToolLatencyClass.EXTERNAL_API,
                waiting_feedback_eligible=True,
                default_waiting_enabled=True,
                default_waiting_phrases=list(WEATHER_DEFAULT_WAITING_PHRASES),
            )
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行天气工具并返回统一字典结果。

        Args:
            name: foreground 工具名。
            arguments: 模型传入的工具参数。

        Returns:
            可写回模型的天气工具结果。
        """

        if name == WEATHER_OVERVIEW_TOOL:
            return self._service.overview(**_location_args(arguments, include_unit=True)).to_tool_payload()

        if name == WEATHER_FORECAST_TOOL:
            args = _location_args(arguments, include_unit=True)
            args["days"] = _int_arg(arguments.get("days"), default=3)
            return self._service.forecast(**args).to_tool_payload()

        if name == WEATHER_AIR_QUALITY_TOOL:
            return self._service.air_quality(**_location_args(arguments, include_unit=False)).to_tool_payload()

        if name == WEATHER_AIR_QUALITY_FORECAST_TOOL:
            return self._service.air_quality_forecast(**_location_args(arguments, include_unit=False)).to_tool_payload()

        if name == WEATHER_ALERTS_TOOL:
            return self._service.alerts(**_location_args(arguments, include_unit=False)).to_tool_payload()

        if name == WEATHER_SUNRISE_SUNSET_TOOL:
            args = _location_args(arguments, include_unit=False)
            args["date_text"] = _optional_str(arguments.get("date"))
            return self._service.sunrise_sunset(**args).to_tool_payload()

        return {
            "ok": False,
            "text": f"不支持的天气工具：{name}。",
            "source": "qweather",
        }


def _schema(
    *,
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    """创建 OpenAI 兼容工具声明。"""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _common_location_properties(*, include_unit: bool) -> dict[str, Any]:
    """创建天气工具共享地点参数声明。"""

    properties: dict[str, Any] = {
        "location": {"type": "string", "description": "需要查询的地点名称。"},
        "adm": {"type": "string", "description": "可选行政区，用于消除重名地点歧义。"},
        "country": {"type": "string", "description": "可选国家或地区代码，例如 CN。"},
        "lang": {"type": "string", "description": "返回语言，默认 zh。"},
    }
    if include_unit:
        properties["unit"] = {"type": "string", "description": "单位制，默认 m 表示公制。"}
    return properties


def _location_args(arguments: dict[str, Any], *, include_unit: bool) -> dict[str, Any]:
    """归一化天气工具共享地点参数。"""

    args: dict[str, Any] = {
        "location": _str_arg(arguments.get("location"), default="当前位置"),
        "adm": _optional_str(arguments.get("adm")),
        "country": _optional_str(arguments.get("country")),
        "lang": _str_arg(arguments.get("lang"), default="zh"),
    }
    if include_unit:
        args["unit"] = _str_arg(arguments.get("unit"), default="m")
    return args


def _optional_str(value: Any) -> str | None:
    """裁剪可选字符串参数。"""

    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def _str_arg(value: Any, *, default: str) -> str:
    """裁剪字符串参数并提供默认值。"""

    normalized = str(value).strip() if value is not None else ""
    return normalized or default


def _int_arg(value: Any, *, default: int) -> int:
    """把模型传入值转换成整数参数。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default
