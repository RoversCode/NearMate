from __future__ import annotations

from dataclasses import dataclass
from typing import Any


WEATHER_LIFE_INDEX_LEVELS: dict[str, dict[int, str]] = {
    "运动指数": {1: "适宜", 2: "较适宜", 3: "较不宜"},
    "洗车指数": {1: "适宜", 2: "较适宜", 3: "较不宜", 4: "不宜"},
    "穿衣指数": {
        1: "寒冷",
        2: "冷",
        3: "较冷",
        4: "较舒适",
        5: "舒适",
        6: "热",
        7: "炎热",
    },
    "钓鱼指数": {1: "适宜", 2: "较适宜", 3: "不宜"},
    "紫外线指数": {1: "最弱", 2: "弱", 3: "中等", 4: "强", 5: "很强"},
    "旅游指数": {1: "适宜", 2: "较适宜", 3: "一般", 4: "较不宜", 5: "不适宜"},
    "花粉过敏指数": {1: "极不易发", 2: "不易发", 3: "较易发", 4: "易发", 5: "极易发"},
    "舒适度指数": {
        1: "舒适",
        2: "较舒适",
        3: "较不舒适",
        4: "很不舒适",
        5: "极不舒适",
        6: "不舒适",
        7: "非常不舒适",
    },
    "感冒指数": {1: "少发", 2: "较易发", 3: "易发", 4: "极易发"},
    "空气污染扩散条件指数": {1: "优", 2: "良", 3: "中", 4: "较差", 5: "很差"},
    "空调开启指数": {
        1: "长时间开启",
        2: "部分时间开启",
        3: "较少开启",
        4: "开启制暖空调",
    },
    "太阳镜指数": {1: "不需要", 2: "需要", 3: "必要", 4: "很必要", 5: "非常必要"},
    "化妆指数": {
        1: "保湿",
        2: "保湿防晒",
        3: "去油防晒",
        4: "防脱水防晒",
        5: "去油",
        6: "防脱水",
        7: "防晒",
        8: "滋润保湿",
    },
    "晾晒指数": {
        1: "极适宜",
        2: "适宜",
        3: "基本适宜",
        4: "不太适宜",
        5: "不宜",
        6: "不适宜",
    },
    "交通指数": {1: "良好", 2: "较好", 3: "一般", 4: "较差", 5: "很差"},
    "防晒指数": {1: "弱", 2: "较弱", 3: "中等", 4: "强", 5: "极强"},
}

WEATHER_LIFE_INDEX_API_TYPES: dict[str, str] = {
    "1": "运动指数",
    "2": "洗车指数",
    "3": "穿衣指数",
    "4": "钓鱼指数",
    "5": "紫外线指数",
    "6": "旅游指数",
    "7": "花粉过敏指数",
    "8": "舒适度指数",
    "9": "感冒指数",
    "10": "空气污染扩散条件指数",
    "11": "空调开启指数",
    "12": "太阳镜指数",
    "13": "化妆指数",
    "14": "晾晒指数",
    "15": "交通指数",
    "16": "防晒指数",
}


@dataclass(frozen=True)
class AirQualityBand:
    """空气质量指数区间。

    Args:
        minimum: 区间最小值，包含端点。
        maximum: 区间最大值，包含端点；None 表示无上限。
        category: 区间对应类别。
    """

    minimum: float
    maximum: float | None
    category: str

    def contains(self, value: float) -> bool:
        """判断数值是否落在当前区间。

        Args:
            value: 待判断的 AQI 数值。

        Returns:
            落在区间内时返回 True。
        """

        if value < self.minimum:
            return False
        if self.maximum is None:
            return True
        return value <= self.maximum


AIR_QUALITY_INDEX_BANDS: dict[str, tuple[AirQualityBand, ...]] = {
    "ca-eccc": (
        AirQualityBand(1, 3, "低风险"),
        AirQualityBand(4, 6, "中风险"),
        AirQualityBand(7, 10, "高风险"),
        AirQualityBand(10.0001, None, "极高风险"),
    ),
    "cn-mee": (
        AirQualityBand(0, 50, "优"),
        AirQualityBand(51, 100, "良"),
        AirQualityBand(101, 150, "轻度污染"),
        AirQualityBand(151, 200, "中度污染"),
        AirQualityBand(201, 300, "重度污染"),
        AirQualityBand(301, 500, "严重污染"),
    ),
    "cn-mee-1h": (
        AirQualityBand(0, 50, "优"),
        AirQualityBand(51, 100, "良"),
        AirQualityBand(101, 150, "轻度污染"),
        AirQualityBand(151, 200, "中度污染"),
        AirQualityBand(201, 300, "重度污染"),
        AirQualityBand(301, 500, "严重污染"),
    ),
    "eu-eea": (
        AirQualityBand(1, 1, "优"),
        AirQualityBand(2, 2, "良"),
        AirQualityBand(3, 3, "中"),
        AirQualityBand(4, 4, "差"),
        AirQualityBand(5, 5, "很差"),
        AirQualityBand(6, 6, "极差"),
    ),
    "fr-atmo": (
        AirQualityBand(1, 1, "好"),
        AirQualityBand(2, 2, "一般"),
        AirQualityBand(3, 3, "不好"),
        AirQualityBand(4, 4, "差"),
        AirQualityBand(5, 5, "很差"),
        AirQualityBand(6, 6, "极差"),
    ),
    "gb-defra": (
        AirQualityBand(1, 3, "低"),
        AirQualityBand(4, 6, "中"),
        AirQualityBand(7, 9, "高"),
        AirQualityBand(10, 10, "严重"),
    ),
    "hk-epd": (
        AirQualityBand(1, 3, "低"),
        AirQualityBand(4, 6, "中"),
        AirQualityBand(7, 7, "高"),
        AirQualityBand(8, 10, "甚高"),
        AirQualityBand(10.0001, None, "严重"),
    ),
    "jp-moe": (
        AirQualityBand(1, 1, "蓝色"),
        AirQualityBand(2, 2, "青色"),
        AirQualityBand(3, 3, "绿色"),
        AirQualityBand(4, 4, "黄色/注意"),
        AirQualityBand(5, 5, "橙色/警报"),
        AirQualityBand(6, 6, "红色/严重警报"),
    ),
    "kr-moe": (
        AirQualityBand(0, 50, "好"),
        AirQualityBand(51, 100, "中等"),
        AirQualityBand(101, 250, "不健康"),
        AirQualityBand(251, 500, "非常不健康"),
    ),
    "mo-smg": (
        AirQualityBand(0, 50, "良好"),
        AirQualityBand(51, 100, "普通"),
        AirQualityBand(101, 200, "不良"),
        AirQualityBand(201, 300, "非常不良"),
        AirQualityBand(301, 400, "严重"),
        AirQualityBand(401, 500, "有害"),
    ),
    "qaqi": (
        AirQualityBand(0, 2.0, "优"),
        AirQualityBand(2.1, 4.0, "良"),
        AirQualityBand(4.1, 5.0, "中等"),
        AirQualityBand(5.1, 7.0, "差"),
        AirQualityBand(7.1, 9.0, "很差"),
        AirQualityBand(9.1, 10.0, "极差"),
    ),
    "sg-nea": (
        AirQualityBand(0, 50, "良好水平"),
        AirQualityBand(51, 100, "适中水平"),
        AirQualityBand(101, 200, "不健康水平"),
        AirQualityBand(201, 300, "非常不健康水平"),
        AirQualityBand(301, 500, "危险水平"),
    ),
    "sg-nea-pm1h": (
        AirQualityBand(0, 55, "等级1正常"),
        AirQualityBand(56, 150, "等级2偏高"),
        AirQualityBand(151, 250, "等级3高"),
        AirQualityBand(251, None, "等级4非常高"),
    ),
    "th-pcd": (
        AirQualityBand(0, 25, "优秀"),
        AirQualityBand(25.0001, 50, "良好"),
        AirQualityBand(51, 100, "中等"),
        AirQualityBand(101, 200, "不健康"),
        AirQualityBand(201, None, "非常不健康"),
    ),
    "tw-me": (
        AirQualityBand(0, 50, "良好"),
        AirQualityBand(51, 100, "普通"),
        AirQualityBand(101, 150, "对敏感人群不健康"),
        AirQualityBand(151, 200, "对所有人群不健康"),
        AirQualityBand(201, 300, "非常不健康"),
        AirQualityBand(301, 500, "危害"),
    ),
    "tw-me-1h": (
        AirQualityBand(0, 50, "良好"),
        AirQualityBand(51, 100, "普通"),
        AirQualityBand(101, 150, "对敏感人群不健康"),
        AirQualityBand(151, 200, "对所有人群不健康"),
        AirQualityBand(201, 300, "非常不健康"),
        AirQualityBand(301, 500, "危害"),
    ),
    "us-epa": (
        AirQualityBand(0, 50, "好"),
        AirQualityBand(51, 100, "中等"),
        AirQualityBand(101, 150, "不适于敏感人群"),
        AirQualityBand(151, 200, "不健康"),
        AirQualityBand(201, 300, "非常不健康"),
        AirQualityBand(301, 500, "危险"),
    ),
    "us-epa-nc": (
        AirQualityBand(0, 50, "好"),
        AirQualityBand(51, 100, "中等"),
        AirQualityBand(101, 150, "不适于敏感人群"),
        AirQualityBand(151, 200, "不健康"),
        AirQualityBand(201, 300, "非常不健康"),
        AirQualityBand(301, 500, "危险"),
    ),
}


def describe_weather_life_index(index_type: Any, level: Any) -> str:
    """查询天气生活指数等级名称。

    Args:
        index_type: 指数中文名或和风 API 类型值。
        level: 指数等级数值。

    Returns:
        匹配到的等级名称；未知输入返回空字符串。
    """

    normalized_index_type = str(index_type or "").strip()
    index_name = WEATHER_LIFE_INDEX_API_TYPES.get(
        normalized_index_type, normalized_index_type
    )
    normalized_level = _parse_int(level)
    if normalized_level is None:
        return ""
    return WEATHER_LIFE_INDEX_LEVELS.get(index_name, {}).get(normalized_level, "")


def describe_air_quality_index(index_code: Any, value: Any) -> str:
    """查询空气质量指数类别。

    Args:
        index_code: 和风空气质量指数代码，例如 cn-mee、us-epa。
        value: AQI 数值或 10+ 这类开放上限文本。

    Returns:
        匹配到的类别名称；未知输入返回空字符串。
    """

    normalized_index_code = str(index_code or "").strip().lower()
    normalized_value = _parse_air_quality_value(value)
    if normalized_value is None:
        return ""

    for band in AIR_QUALITY_INDEX_BANDS.get(normalized_index_code, ()):
        if band.contains(normalized_value):
            return band.category
    return ""


def _parse_int(value: Any) -> int | None:
    """把外部等级值解析为整数。"""

    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_air_quality_value(value: Any) -> float | None:
    """把空气质量指数值解析为可比较数值。"""

    normalized_value = str(value or "").strip()
    if not normalized_value:
        return None
    if normalized_value.endswith("+"):
        normalized_value = normalized_value.rstrip("+")
        try:
            return float(normalized_value) + 0.0001
        except ValueError:
            return None
    try:
        return float(normalized_value)
    except ValueError:
        return None
