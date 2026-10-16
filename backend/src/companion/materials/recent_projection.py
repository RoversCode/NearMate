"""把近期材料记录投影成 Desktop 可展示的轻量状态。"""

from __future__ import annotations

from pathlib import Path

from companion.materials.recent_registry import RecentMaterial
from companion.materials.recent_validation import (
    RecentMaterialStatus,
    inspect_recent_material_path,
)


def project_recent_materials(
    records: tuple[RecentMaterial, ...],
) -> list[dict[str, str]]:
    """生成不持续扫描磁盘的近期材料 Desktop 快照。

    Args:
        records: 注册表按 LRU 顺序冻结的近期路径记录。

    Returns:
        只包含展示字段、路径、节点类型和当前轻量状态的列表。
    """

    return [
        {
            "display_name": record.display_name,
            "path": record.path,
            "material_kind": record.material_kind,
            "node_kind": record.node_kind,
            "media_class": record.media_class,
            "status": inspect_recent_material_status(record),
        }
        for record in records
    ]


def inspect_recent_material_status(record: RecentMaterial) -> RecentMaterialStatus:
    """检查一项近期路径当前是否仍满足根节点机械条件。

    Args:
        record: 用户正式提供时冻结的路径与节点类型。

    Returns:
        Desktop 使用的五种稳定状态之一。
    """

    return inspect_recent_material_path(
        Path(record.path),
        expected_node_kind=record.node_kind,
    )


__all__ = [
    "RecentMaterialStatus",
    "inspect_recent_material_status",
    "project_recent_materials",
]
