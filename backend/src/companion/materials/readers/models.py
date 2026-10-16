from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ReadMaterialPart:
    """描述 Reader 产出的一个有序 Knowledge 材料片段。"""

    kind: Literal["text", "image", "pdf", "audio", "video"]
    display_name: str
    text: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class ReadMaterialResult:
    """描述单个入口递归读取后的材料与工作量统计。"""

    parts: list[ReadMaterialPart] = field(default_factory=list)
    local_file_count: int = 0
    visual_unit_count: int = 0
    local_total_bytes: int = 0
    ignored_file_count: int = 0
    ignored_reasons: dict[str, int] = field(default_factory=dict)
    readable: bool = False
