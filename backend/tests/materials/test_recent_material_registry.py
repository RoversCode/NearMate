from __future__ import annotations

from companion.materials.models import MaterialAttachment, MaterialNodeKind, normalize_material_path
from companion.materials.recent_registry import RecentMaterialRegistry


def _attachment(
    material_id: str,
    path: str,
    *,
    display_name: str | None = None,
    node_kind: MaterialNodeKind = "file",
    managed_asset_id: str | None = None,
) -> MaterialAttachment:
    """构造一项无需读取真实磁盘的近期注册表测试材料。

    Args:
        material_id: 当前 Turn 内部材料编号。
        path: 用户正式提供的本地路径。
        display_name: 可选展示名称，默认取路径末段。
        node_kind: 登记时冻结的普通文件或目录类型。
        managed_asset_id: 可选 Desktop 受管资产编号。

    Returns:
        字段完整的正式材料。
    """

    return MaterialAttachment(
        material_id=material_id,
        turn_id="turn-a",
        kind="dragged_folder" if node_kind == "directory" else "dragged_file",
        source="drag_drop",
        display_name=display_name or path.replace("\\", "/").rsplit("/", 1)[-1],
        path=path,
        created_at="2026-08-23T10:00:00+08:00",
        node_kind=node_kind,
        media_class="mixed_or_document",
        managed_asset_id=managed_asset_id,
    )


def test_registry_remembers_only_user_owned_paths() -> None:
    """受管副本会随材料上下文释放，不能成为近期路径能力。"""

    registry = RecentMaterialRegistry()

    changed = registry.remember([
        _attachment("material-a", "C:/recordings/meeting.m4a"),
        _attachment(
            "material-b",
            "C:/managed/clipboard.png",
            managed_asset_id="asset-a",
        ),
    ])

    assert changed is True
    assert [item.path for item in registry.snapshot()] == ["C:/recordings/meeting.m4a"]


def test_same_path_updates_metadata_and_refreshes_lru_order() -> None:
    """同路径再次正式提供时只保留最新元数据，并移动到最近使用位置。"""

    registry = RecentMaterialRegistry(capacity=3)
    registry.remember([
        _attachment("material-a", "C:/docs/a.txt", display_name="旧名称.txt"),
        _attachment("material-b", "C:/docs/b.txt"),
    ])

    registry.remember([
        _attachment("material-c", "C:/docs/a.txt", display_name="新名称.txt"),
    ])

    assert [(item.path, item.display_name) for item in registry.snapshot()] == [
        ("C:/docs/b.txt", "b.txt"),
        ("C:/docs/a.txt", "新名称.txt"),
    ]


def test_successful_touch_changes_the_next_lru_eviction() -> None:
    """成功重挂会刷新仍在列表中的路径，使真正最久未用项先淘汰。"""

    registry = RecentMaterialRegistry(capacity=3)
    registry.remember([
        _attachment("material-a", "C:/docs/a.txt"),
        _attachment("material-b", "C:/docs/b.txt"),
        _attachment("material-c", "C:/docs/c.txt"),
    ])

    assert registry.touch(["C:/docs/a.txt"]) is True
    registry.remember([_attachment("material-d", "C:/docs/d.txt")])

    assert [item.path for item in registry.snapshot()] == [
        "C:/docs/c.txt",
        "C:/docs/a.txt",
        "C:/docs/d.txt",
    ]


def test_late_touch_never_recreates_a_forgotten_path() -> None:
    """手动忘记后的迟到跨进程刷新只能被忽略，不能恢复授权。"""

    registry = RecentMaterialRegistry()
    registry.remember([_attachment("material-a", "C:/docs/a.txt")])
    assert registry.forget("C:/docs/a.txt") is True

    assert registry.touch(["C:/docs/a.txt"]) is False
    assert registry.snapshot() == ()


def test_clear_removes_every_recent_path() -> None:
    """明确清空会一次性撤销未来新任务的全部近期路径资格。"""

    registry = RecentMaterialRegistry()
    registry.remember([
        _attachment("material-a", "C:/docs/a.txt"),
        _attachment("material-b", "C:/projects/demo", node_kind="directory"),
    ])

    assert registry.clear() is True
    assert registry.clear() is False
    assert registry.snapshot() == ()


def test_normalize_material_path_collapses_separators_and_rejects_blank() -> None:
    """路径键必须消除分隔符差异，非法输入不能进入注册表。"""

    assert normalize_material_path("C:/docs//a.txt") == normalize_material_path("C:/docs/a.txt")
    assert normalize_material_path("  ") == ""
    assert normalize_material_path(None) == ""

