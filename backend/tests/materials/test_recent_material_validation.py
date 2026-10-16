"""验证近期材料共享机械状态检查器的稳定优先级。"""

from __future__ import annotations

from pathlib import Path

import pytest

from companion.filesystem.safety import FileSystemNodeSnapshot
from companion.materials import recent_validation as validation_module
from companion.materials.models import MaterialNodeKind
from companion.materials.recent_validation import inspect_recent_material_path


@pytest.mark.parametrize("node_kind", ["file", "directory"])
def test_real_symbolic_link_is_unsafe_before_type_change(
    tmp_path: Path,
    node_kind: MaterialNodeKind,
) -> None:
    """真实文件或目录链接的 lstat 类型即使是 other，也必须优先归为 unsafe。"""

    target = tmp_path / f"target-{node_kind}"
    if node_kind == "file":
        target.write_text("content", encoding="utf-8")
    else:
        target.mkdir()
    linked_path = tmp_path / f"linked-{node_kind}"
    try:
        linked_path.symlink_to(target, target_is_directory=node_kind == "directory")
    except OSError as exception:
        pytest.skip(f"当前平台不允许创建测试符号链接：{exception}")

    status = inspect_recent_material_path(
        linked_path,
        expected_node_kind=node_kind,
    )

    assert status == "unsafe"


def test_reparse_snapshot_is_unsafe_before_kind_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Junction 或重解析点即使报告 other，也不能退化成 type_changed。"""

    monkeypatch.setattr(
        validation_module,
        "inspect_filesystem_node",
        lambda _path: FileSystemNodeSnapshot(
            node_kind="other",
            is_linked_or_reparse=True,
            size=0,
        ),
    )

    assert inspect_recent_material_path(
        tmp_path / "junction",
        expected_node_kind="directory",
    ) == "unsafe"


@pytest.mark.parametrize(
    ("expected_kind", "create_current"),
    [
        ("file", lambda path: path.mkdir()),
        ("directory", lambda path: path.write_text("content", encoding="utf-8")),
    ],
)
def test_non_link_file_directory_swap_is_type_changed(
    tmp_path: Path,
    expected_kind: MaterialNodeKind,
    create_current,
) -> None:
    """只有真实普通文件与目录互换时才返回 type_changed。"""

    candidate = tmp_path / "same-name"
    create_current(candidate)

    assert inspect_recent_material_path(
        candidate,
        expected_node_kind=expected_kind,
    ) == "type_changed"
