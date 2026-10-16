"""校验受管 Skill 资产没有通过文件系统链接越过产品边界。"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from companion.filesystem.safety import (
    FileSystemNodeInspectionError,
    is_linked_or_reparse_path as _is_linked_or_reparse_path,
)


class SkillAssetBoundaryError(RuntimeError):
    """表示 Skill 资产含有链接、重解析点或无法安全读取的节点。

    Args:
        message: 不包含本机绝对路径、可以安全返回给本地控制端的原因。
    """


@dataclass(frozen=True, slots=True)
class SkillAssetTreeSnapshot:
    """记录一次安全遍历看到的文件和节点身份。

    Args:
        files: 按相对路径稳定排序的普通文件绝对路径。
        node_fingerprints: 根目录和全部后代的相对路径、类型、身份与修改信息。
    """

    files: tuple[Path, ...]
    node_fingerprints: tuple[tuple[str, int, int, int, int, int, int], ...]


def is_linked_or_reparse_path(path: Path) -> bool:
    """判断路径自身是否会把文件访问重定向到其它位置。

    Args:
        path: 只检查自身、不跟随到目标位置的文件或目录。

    Returns:
        软链接、Windows Junction 或其它 Windows 重解析点返回真。

    Raises:
        SkillAssetBoundaryError: 路径状态无法可靠读取时抛出。
    """

    try:
        return _is_linked_or_reparse_path(path)
    except (FileNotFoundError, FileSystemNodeInspectionError) as exception:
        raise SkillAssetBoundaryError("Skill 资产状态无法读取") from exception


def validate_skill_asset_tree(
    skill_root: Path,
    *,
    asset_root: Path,
    managed_skills_root: Path,
) -> SkillAssetTreeSnapshot:
    """验证并列出一个受管 Skill 内的普通文件。

    目录遍历使用不跟随链接的系统入口。这样 Windows Junction 不会在检查过程
    中先被展开，也不会让清单校验读取到产品 Skill 根目录外的资产。

    Args:
        skill_root: 包含 manifest.json、SKILL.md 和可选资产的 Skill 根目录。
        asset_root: NearMate 产品工作区资产根目录。
        managed_skills_root: NearMate 唯一允许扫描的受管 Skill 根目录。

    Returns:
        包含普通文件列表和节点身份的不可变快照。

    Raises:
        SkillAssetBoundaryError: 根目录、任一后代不安全或遍历失败时抛出。
    """

    try:
        skill_root.relative_to(managed_skills_root)
    except ValueError as exception:
        raise SkillAssetBoundaryError("Skill 目录不属于受管资产根") from exception

    # 不能只检查 Skill 目录自身。扫描完成后，任一级祖先都可能被替换成
    # Junction，使同一个词法路径转而指向产品目录外。
    expected_managed_skills_root = asset_root / ".claude" / "skills"
    if managed_skills_root != expected_managed_skills_root:
        raise SkillAssetBoundaryError("Skill 受管资产根不合法")

    return validate_managed_asset_tree(skill_root, asset_root=asset_root)


def validate_managed_asset_tree(
    tree_root: Path,
    *,
    asset_root: Path,
    ignored_names: frozenset[str] = frozenset(),
) -> SkillAssetTreeSnapshot:
    """验证产品目录中的普通资产，并且全程不跟随链接或重解析点。

    Args:
        tree_root: 需要读取或复制的产品目录子树。
        asset_root: 受管产品资产的词法根目录。
        ignored_names: 只在树根层级跳过的直接子节点名称。

    Returns:
        包含普通文件列表和节点身份的不可变快照。

    Raises:
        SkillAssetBoundaryError: 目录越过产品根、含危险节点或无法读取时抛出。
    """

    try:
        relative_root = tree_root.relative_to(asset_root)
    except ValueError as exception:
        raise SkillAssetBoundaryError("产品资产不属于受管资产根") from exception

    current_path = asset_root
    path_chain = [current_path]
    for relative_part in relative_root.parts:
        current_path /= relative_part
        path_chain.append(current_path)
    if any(is_linked_or_reparse_path(path) for path in path_chain):
        raise SkillAssetBoundaryError("产品资产不能包含链接或重解析点")
    try:
        root_status = tree_root.stat(follow_symlinks=False)
    except OSError as exception:
        raise SkillAssetBoundaryError("产品资产目录无法读取") from exception
    if not stat.S_ISDIR(root_status.st_mode):
        raise SkillAssetBoundaryError("产品资产根路径必须是目录")

    files: list[Path] = []
    node_fingerprints = [_node_fingerprint(tree_root, root_status, tree_root)]
    pending_directories = [tree_root]
    while pending_directories:
        current_directory = pending_directories.pop()
        # 子目录在入队后仍可能被其它进程替换成 Junction。真正打开目录前再验
        # 一次自身，避免 scandir 跟随刚发生的替换越过产品边界。
        if is_linked_or_reparse_path(current_directory):
            raise SkillAssetBoundaryError("产品资产不能包含链接或重解析点")
        try:
            with os.scandir(current_directory) as entries:
                sorted_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError as exception:
            raise SkillAssetBoundaryError("产品资产目录无法读取") from exception

        for entry in sorted_entries:
            if current_directory == tree_root and entry.name in ignored_names:
                continue
            entry_path = Path(entry.path)
            if is_linked_or_reparse_path(entry_path):
                raise SkillAssetBoundaryError("产品资产不能包含链接或重解析点")
            try:
                entry_status = entry.stat(follow_symlinks=False)
            except OSError as exception:
                raise SkillAssetBoundaryError("产品资产状态无法读取") from exception
            node_fingerprints.append(
                _node_fingerprint(entry_path, entry_status, tree_root)
            )
            if stat.S_ISDIR(entry_status.st_mode):
                pending_directories.append(entry_path)
                continue
            if stat.S_ISREG(entry_status.st_mode):
                files.append(entry_path)
                continue
            raise SkillAssetBoundaryError("产品资产目录只能包含普通文件和目录")

    # 遍历完成后再次检查祖先链，防止读取期间受管根被整体替换。
    if any(is_linked_or_reparse_path(path) for path in path_chain):
        raise SkillAssetBoundaryError("产品资产不能包含链接或重解析点")

    return SkillAssetTreeSnapshot(
        files=tuple(sorted(
            files,
            key=lambda path: path.relative_to(tree_root).as_posix(),
        )),
        node_fingerprints=tuple(sorted(node_fingerprints)),
    )


def _node_fingerprint(
    path: Path,
    path_status: os.stat_result,
    skill_root: Path,
) -> tuple[str, int, int, int, int, int, int]:
    """构造用于识别遍历期间资产替换的稳定节点摘要。

    Args:
        path: 当前普通文件或目录。
        path_status: 不跟随链接读取到的文件状态。
        skill_root: 用于生成相对路径的 Skill 根目录。

    Returns:
        相对路径、类型、设备、节点编号、大小、修改时间和元数据变更时间。
    """

    relative_path = "." if path == skill_root else path.relative_to(skill_root).as_posix()
    is_regular_file = stat.S_ISREG(path_status.st_mode)
    return (
        relative_path,
        stat.S_IFMT(path_status.st_mode),
        path_status.st_dev,
        path_status.st_ino,
        path_status.st_size if is_regular_file else 0,
        path_status.st_mtime_ns if is_regular_file else 0,
        path_status.st_ctime_ns if is_regular_file else 0,
    )


__all__ = [
    "SkillAssetBoundaryError",
    "SkillAssetTreeSnapshot",
    "is_linked_or_reparse_path",
    "validate_managed_asset_tree",
    "validate_skill_asset_tree",
]
