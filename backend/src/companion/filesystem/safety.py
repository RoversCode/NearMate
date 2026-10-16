"""集中识别普通文件、目录以及会重定向访问范围的文件系统节点。"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


FileSystemNodeKind = Literal["file", "directory", "other"]

_WINDOWS_REPARSE_POINT_ATTRIBUTE = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0,
)


class FileSystemNodeInspectionError(RuntimeError):
    """表示节点存在，但系统无法可靠读取它的安全元数据。"""


class UnsafeFileSystemPathError(RuntimeError):
    """表示访问路径会经过链接、Junction 或其它重解析点。"""


@dataclass(frozen=True, slots=True)
class FileSystemNodeSnapshot:
    """保存一次不跟随链接读取到的节点事实。

    Args:
        node_kind: 节点当前是普通文件、目录还是其它特殊类型。
        is_linked_or_reparse: 节点是否会把访问重定向到另一个位置。
        size: 普通文件的当前字节数；目录和特殊节点固定为零。
    """

    node_kind: FileSystemNodeKind
    is_linked_or_reparse: bool
    size: int


def inspect_filesystem_node(path: Path) -> FileSystemNodeSnapshot:
    """不跟随链接读取一个本地节点的类型与安全属性。

    Args:
        path: 需要检查的词法路径。

    Returns:
        节点类型、重定向属性和普通文件大小。

    Raises:
        FileNotFoundError: 路径当前不存在。
        FileSystemNodeInspectionError: 操作系统拒绝或无法可靠读取节点元数据。
    """

    try:
        path_status = path.lstat()
        is_junction = path.is_junction() if hasattr(path, "is_junction") else False
    except FileNotFoundError:
        raise
    except OSError as exception:
        raise FileSystemNodeInspectionError("文件系统节点状态无法读取") from exception

    file_attributes = getattr(path_status, "st_file_attributes", 0)
    is_linked_or_reparse = (
        stat.S_ISLNK(path_status.st_mode)
        or is_junction
        or bool(file_attributes & _WINDOWS_REPARSE_POINT_ATTRIBUTE)
    )
    if stat.S_ISREG(path_status.st_mode):
        node_kind: FileSystemNodeKind = "file"
    elif stat.S_ISDIR(path_status.st_mode):
        node_kind = "directory"
    else:
        node_kind = "other"
    return FileSystemNodeSnapshot(
        node_kind=node_kind,
        is_linked_or_reparse=is_linked_or_reparse,
        size=path_status.st_size if node_kind == "file" else 0,
    )


def is_linked_or_reparse_path(path: Path) -> bool:
    """判断路径自身是否会把文件访问重定向到其它位置。

    Args:
        path: 只检查自身、不跟随到目标位置的文件或目录。

    Returns:
        软链接、Windows Junction 或其它 Windows 重解析点返回真。

    Raises:
        FileNotFoundError: 路径当前不存在。
        FileSystemNodeInspectionError: 节点状态无法可靠读取。
    """

    return inspect_filesystem_node(path).is_linked_or_reparse


def ensure_filesystem_node_readable(
    path: Path,
    *,
    node_kind: Literal["file", "directory"],
) -> None:
    """轻量确认普通文件或目录根当前可以读取。

    调用方必须先用 ``inspect_filesystem_node`` 确认节点类型且不是链接或重解析点。
    文件只读取一个字节，因此空文件也算可读；目录只打开一次枚举，不递归扫描内容。

    Args:
        path: 已完成根节点安全检查的本地路径。
        node_kind: 调用方已经确认的普通文件或目录类型。

    Raises:
        OSError: 操作系统拒绝打开或读取当前节点。
        ValueError: 节点类型不是受支持的文件或目录。
    """

    if node_kind == "file":
        with path.open("rb") as stream:
            stream.read(1)
        return
    if node_kind == "directory":
        with os.scandir(path) as entries:
            next(entries, None)
        return
    raise ValueError("只支持检查普通文件或目录的可读性")


def lexical_absolute_path(path: Path) -> Path:
    """生成不解析链接目标的绝对词法路径。

    Args:
        path: 需要展开用户目录和相对段的本地路径。

    Returns:
        已消除 ``.``、``..`` 和相对前缀，但没有跟随链接的绝对路径。
    """

    return Path(os.path.abspath(os.path.expanduser(str(path))))


def ensure_path_segments_without_links(
    *,
    root: Path,
    candidate: Path,
) -> None:
    """确认候选路径位于目录根内且现有父链不经过重定向节点。

    不存在的尾部可用于创建新文件；一旦遇到首个不存在节点，后续节点也不可能
    独立存在，因此停止元数据检查。该函数不读取文件内容。

    Args:
        root: 已授权的目录材料根。
        candidate: 准备访问或创建的根内路径。

    Raises:
        UnsafeFileSystemPathError: 候选越界或现有父链包含链接、Junction、重解析点。
        FileSystemNodeInspectionError: 现有节点元数据无法可靠读取。
    """

    normalized_root = lexical_absolute_path(root)
    normalized_candidate = lexical_absolute_path(candidate)
    try:
        relative_parts = normalized_candidate.relative_to(normalized_root).parts
    except ValueError as exception:
        raise UnsafeFileSystemPathError("候选路径超出授权目录根") from exception

    current = normalized_root
    for part in ("", *relative_parts):
        if part:
            current = current / part
        try:
            snapshot = inspect_filesystem_node(current)
        except FileNotFoundError:
            break
        if snapshot.is_linked_or_reparse:
            raise UnsafeFileSystemPathError("访问路径包含链接或重解析点")


def ensure_directory_tree_without_links(root: Path) -> None:
    """不跟随链接遍历目录元数据，拒绝包含重定向节点的递归读取。

    Args:
        root: Glob 或 Grep 准备递归读取的目录根。

    Raises:
        UnsafeFileSystemPathError: 根或任一后代是链接、Junction、重解析点。
        FileSystemNodeInspectionError: 任一节点元数据无法可靠读取。
        OSError: 目录无法枚举。
        ValueError: 根节点不是目录。
    """

    normalized_root = lexical_absolute_path(root)
    root_snapshot = inspect_filesystem_node(normalized_root)
    if root_snapshot.node_kind != "directory":
        raise ValueError("递归文件工具只能以目录为扫描根")
    if root_snapshot.is_linked_or_reparse:
        raise UnsafeFileSystemPathError("递归目录根是链接或重解析点")

    pending_directories = [normalized_root]
    while pending_directories:
        current_directory = pending_directories.pop()
        with os.scandir(current_directory) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                snapshot = inspect_filesystem_node(entry_path)
                if snapshot.is_linked_or_reparse:
                    raise UnsafeFileSystemPathError(
                        "递归目录树包含链接或重解析点"
                    )
                if snapshot.node_kind == "directory":
                    pending_directories.append(entry_path)


__all__ = [
    "FileSystemNodeInspectionError",
    "FileSystemNodeKind",
    "FileSystemNodeSnapshot",
    "UnsafeFileSystemPathError",
    "ensure_directory_tree_without_links",
    "ensure_filesystem_node_readable",
    "ensure_path_segments_without_links",
    "inspect_filesystem_node",
    "is_linked_or_reparse_path",
    "lexical_absolute_path",
]
