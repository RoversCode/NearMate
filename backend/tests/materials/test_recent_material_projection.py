from __future__ import annotations

from pathlib import Path

import pytest

from companion.filesystem.safety import FileSystemNodeSnapshot
from companion.materials.recent_projection import (
    inspect_recent_material_status,
    project_recent_materials,
)
from companion.materials.recent_registry import RecentMaterial
from companion.materials import recent_validation as validation_module


def _record(path: Path, *, node_kind: str = "file") -> RecentMaterial:
    """构造 Desktop 状态投影使用的近期材料记录。"""

    return RecentMaterial(
        path=str(path),
        display_name=path.name,
        material_kind="dragged_folder" if node_kind == "directory" else "dragged_file",
        node_kind=node_kind,  # type: ignore[arg-type]
        media_class="text" if node_kind == "file" else "mixed_or_document",
        last_used_sequence=1,
    )


def test_projection_reports_available_file_and_directory(tmp_path: Path) -> None:
    """普通文件和目录根可读时应投影为 available。"""

    material_file = tmp_path / "app.py"
    material_file.write_text("", encoding="utf-8")
    material_directory = tmp_path / "project"
    material_directory.mkdir()

    payload = project_recent_materials(
        (
            _record(material_file),
            _record(material_directory, node_kind="directory"),
        )
    )

    assert [item["status"] for item in payload] == ["available", "available"]
    assert payload[0] == {
        "display_name": "app.py",
        "path": str(material_file),
        "material_kind": "dragged_file",
        "node_kind": "file",
        "media_class": "text",
        "status": "available",
    }


def test_projection_distinguishes_missing_and_type_change(tmp_path: Path) -> None:
    """缺失节点和文件变目录必须使用不同稳定状态。"""

    missing = tmp_path / "missing.txt"
    changed = tmp_path / "report.pdf"
    changed.mkdir()

    assert inspect_recent_material_status(_record(missing)) == "missing"
    assert inspect_recent_material_status(_record(changed)) == "type_changed"


def test_projection_reports_unsafe_and_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """链接根和无法打开的普通节点应精确区分安全与可读状态。"""

    material_file = tmp_path / "report.txt"
    material_file.write_text("content", encoding="utf-8")
    record = _record(material_file)
    monkeypatch.setattr(
        validation_module,
        "inspect_filesystem_node",
        lambda _path: FileSystemNodeSnapshot(
            node_kind="file",
            is_linked_or_reparse=True,
            size=1,
        ),
    )
    assert inspect_recent_material_status(record) == "unsafe"

    monkeypatch.setattr(
        validation_module,
        "inspect_filesystem_node",
        lambda _path: FileSystemNodeSnapshot(
            node_kind="file",
            is_linked_or_reparse=False,
            size=1,
        ),
    )

    def reject_read(_path: Path, *, node_kind: str) -> None:
        """模拟节点元数据可见但内容不可打开。"""

        del node_kind
        raise PermissionError("denied")

    monkeypatch.setattr(
        validation_module,
        "ensure_filesystem_node_readable",
        reject_read,
    )
    assert inspect_recent_material_status(record) == "unreadable"
