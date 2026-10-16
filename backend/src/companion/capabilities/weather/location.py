from __future__ import annotations

import csv
from pathlib import Path

from companion.capabilities.weather.models import LocationCandidate, LocationResolution


class LocalLocationResolver:
    """基于本地和风城市表解析地点。"""

    def __init__(self, city_csv_path: Path) -> None:
        """初始化本地地点解析器。

        Args:
            city_csv_path: 和风城市列表 CSV 文件路径。
        """

        self._city_csv_path = city_csv_path
        self._locations = self._read_locations()

    def resolve(self, location: str, *, adm: str | None = None, country: str | None = None) -> LocationResolution:
        """解析用户输入地点。

        Args:
            location: 用户或模型传入的地点名。
            adm: 可选行政区约束。
            country: 可选国家或地区约束。

        Returns:
            地点解析结果；可能是命中、歧义或未命中。
        """

        normalized_location = _normalize_text(location)
        if not normalized_location:
            return LocationResolution.not_found()

        locations = self._load_locations()
        normalized_adm = _normalize_text(adm or "")
        normalized_country = _normalize_text(country or "")
        candidates = [
            item
            for item in locations
            if normalized_location in {
                _normalize_text(item.name_zh),
                _normalize_text(item.name_en),
                _normalize_text(item.location_id),
            }
        ]

        if normalized_country:
            candidates = [
                item
                for item in candidates
                if normalized_country
                in {
                    _normalize_text(item.country_code),
                    _normalize_text(item.country_name_zh),
                }
            ]

        if normalized_adm:
            narrowed_candidates = [
                item
                for item in candidates
                if normalized_adm in {_normalize_text(item.adm1_zh), _normalize_text(item.adm2_zh)}
            ]
            if narrowed_candidates:
                candidates = narrowed_candidates

        if not candidates:
            return LocationResolution.not_found()

        if len(candidates) == 1:
            return LocationResolution.matched(candidates[0])

        return LocationResolution.ambiguous(candidates)

    def _load_locations(self) -> list[LocationCandidate]:
        """返回初始化阶段已加载的本地城市表。"""

        return self._locations

    def _read_locations(self) -> list[LocationCandidate]:
        """在初始化阶段读取本地城市表。"""

        if not self._city_csv_path.exists():
            return []

        with self._city_csv_path.open("r", encoding="utf-8-sig", newline="") as file:
            # 和风 LocationList 第一行是版本信息，第二行才是 CSV 表头。
            next(file, None)
            reader = csv.DictReader(file)
            return [_candidate_from_row(row) for row in reader if row.get("Location_ID")]


def _candidate_from_row(row: dict[str, str]) -> LocationCandidate:
    """把和风城市表行转换为内部地点候选。"""

    return LocationCandidate(
        location_id=row.get("Location_ID", "").strip(),
        name_zh=row.get("Location_Name_ZH", "").strip(),
        name_en=row.get("Location_Name_EN", "").strip(),
        country_code=row.get("ISO_3166_1", "").strip(),
        country_name_zh=row.get("Country_Region_ZH", "").strip(),
        adm1_zh=row.get("Adm1_Name_ZH", "").strip(),
        adm2_zh=row.get("Adm2_Name_ZH", "").strip(),
        timezone=row.get("Timezone", "").strip(),
        latitude=float(row.get("Latitude") or 0.0),
        longitude=float(row.get("Longitude") or 0.0),
    )


def _normalize_text(value: str) -> str:
    """归一化用于地点匹配的短文本。"""

    return value.strip().lower()

