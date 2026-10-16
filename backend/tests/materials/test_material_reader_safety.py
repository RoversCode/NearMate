from pathlib import Path

from companion.filesystem.safety import (
    FileSystemNodeSnapshot,
    inspect_filesystem_node,
)
from companion.materials.readers.registry import MaterialReaderRegistry


def test_material_reader_does_not_enter_reparse_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """验证材料 Reader 不会把 Junction 形状的子目录当成普通目录读取。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的函数替换工具。
    """

    folder = tmp_path / "bundle"
    unsafe_directory = folder / "linked-project"
    unsafe_directory.mkdir(parents=True)
    (unsafe_directory / "secret.txt").write_text("secret", encoding="utf-8")
    (folder / "visible.txt").write_text("visible", encoding="utf-8")

    def inspect_with_reparse_directory(path: Path) -> FileSystemNodeSnapshot:
        snapshot = inspect_filesystem_node(path)
        if path == unsafe_directory:
            return FileSystemNodeSnapshot(
                node_kind="directory",
                is_linked_or_reparse=True,
                size=0,
            )
        return snapshot

    monkeypatch.setattr(
        "companion.materials.readers.registry.inspect_filesystem_node",
        inspect_with_reparse_directory,
    )

    result = MaterialReaderRegistry.default().read(folder)

    assert result.local_file_count == 1
    assert [part.display_name for part in result.parts] == ["visible.txt"]
    assert result.ignored_reasons == {"missing_or_symlink": 1}
