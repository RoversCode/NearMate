"""统一判断近期材料路径当前的机械可用状态。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from companion.filesystem.safety import (
    FileSystemNodeInspectionError,
    ensure_filesystem_node_readable,
    inspect_filesystem_node,
)
from companion.materials.models import MaterialNodeKind


RecentMaterialStatus = Literal[
    "available",
    "missing",
    "type_changed",
    "unsafe",
    "unreadable",
]


def inspect_recent_material_path(
    path: Path,
    *,
    expected_node_kind: MaterialNodeKind,
) -> RecentMaterialStatus:
    """按固定优先级判断近期文件或目录能否继续作为路径能力。

    链接和重解析点必须先于类型变化判断。真实符号链接的 ``lstat`` 类型通常是
    ``other``，如果先比较类型，就会把安全风险错误归类成普通形状变化。

    Args:
        path: 用户此前正式提供的本地词法路径。
        expected_node_kind: 登记时确认的普通文件或目录形状。

    Returns:
        Background 准入和 Desktop 共同使用的五种稳定状态之一。
    """

    try:
        snapshot = inspect_filesystem_node(path)
    except FileNotFoundError:
        return "missing"
    except (FileSystemNodeInspectionError, OSError, RuntimeError, ValueError):
        return "unreadable"

    if snapshot.is_linked_or_reparse:
        return "unsafe"
    if snapshot.node_kind != expected_node_kind:
        return "type_changed"
    try:
        ensure_filesystem_node_readable(
            path,
            node_kind=expected_node_kind,
        )
    except (OSError, RuntimeError, ValueError):
        return "unreadable"
    return "available"


__all__ = ["RecentMaterialStatus", "inspect_recent_material_path"]
