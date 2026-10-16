"""维护 AgentSession 级别的累计材料目录和读取能力。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import RLock

from companion.background_agent.contracts import AgentMaterial
from companion.filesystem.safety import (
    FileSystemNodeInspectionError,
    UnsafeFileSystemPathError,
    ensure_directory_tree_without_links,
    ensure_filesystem_node_readable,
    ensure_path_segments_without_links,
    inspect_filesystem_node,
    lexical_absolute_path,
)


def readable_material_path(material: AgentMaterial) -> str | None:
    """解析一份材料当前真实可读取的绝对路径。

    Args:
        material: 材料目录里保留的正式材料事实。

    Returns:
        节点仍符合冻结类型且当前可读时返回词法绝对路径；否则返回空。
    """

    path = material.local_path
    if not isinstance(path, str) or not path.strip():
        return None
    try:
        candidate = lexical_absolute_path(Path(path))
        snapshot = inspect_filesystem_node(candidate)
        if (
            snapshot.node_kind != material.node_kind
            or snapshot.is_linked_or_reparse
        ):
            return None
        ensure_filesystem_node_readable(
            candidate,
            node_kind=material.node_kind,
        )
    except (
        FileNotFoundError,
        FileSystemNodeInspectionError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        return None
    return str(candidate)


def material_limitations(materials: tuple[AgentMaterial, ...]) -> tuple[str, ...]:
    """现场计算完整材料目录中当前无法读取的材料名称。

    Args:
        materials: 需要生成终态审计快照的完整材料目录。

    Returns:
        没有可用本地文件的材料显示名称，顺序与目录一致。
    """

    return tuple(
        material.display_name
        for material in materials
        if readable_material_path(material) is None
    )


def resolve_allowed_task_path(
    candidate: str,
    workspace_root: str,
    material_capabilities: tuple[AgentMaterial, ...],
    *,
    recursive: bool,
    allow_file_material: bool = True,
    read_only_roots: tuple[str, ...] = (),
) -> Path | None:
    """解析一个属于任务工作区、Skill 配置或冻结材料能力的绝对路径。

    Args:
        candidate: 工具准备访问的绝对或相对路径。
        workspace_root: 当前任务工作区根目录。
        material_capabilities: 当前 Turn 仍然有效的材料能力。
        recursive: 是否会递归读取候选目录。
        allow_file_material: 是否允许命中单文件材料；下载目录等场景应关闭。
        read_only_roots: 只能读取、不能写入的额外目录，例如当前 Activity 的
            私有 Skill 配置目录。

    Returns:
        路径没有越界、没有借链接逃逸且能力形态匹配时返回词法绝对路径；
        不满足任务能力时返回空值。
    """

    try:
        normalized_workspace_root = Path(workspace_root).resolve(strict=False)
        lexical_candidate = _resolve_task_path(candidate, normalized_workspace_root)
        resolved_candidate = lexical_candidate.resolve(strict=False)
        if (
            resolved_candidate == normalized_workspace_root
            or normalized_workspace_root in resolved_candidate.parents
        ):
            if recursive and lexical_candidate.is_dir():
                ensure_directory_tree_without_links(lexical_candidate)
            return lexical_candidate

        # Claude Code 需要读取 Skill 引用的 WINDOWS.md/BROWSER.md 等伴随文件。
        # 这些文件属于当前 Activity 私有配置，不是用户工作区材料；只给读类工具
        # 额外根目录，调用方不会把它传给 Write/Edit。
        for read_only_root in read_only_roots:
            normalized_read_only_root = lexical_absolute_path(Path(read_only_root))
            try:
                lexical_candidate.relative_to(normalized_read_only_root)
            except ValueError:
                continue
            ensure_path_segments_without_links(
                root=normalized_read_only_root,
                candidate=lexical_candidate,
            )
            if recursive and lexical_candidate.is_dir():
                ensure_directory_tree_without_links(lexical_candidate)
            return lexical_candidate

        for material in material_capabilities:
            readable_path = readable_material_path(material)
            if readable_path is None:
                continue
            material_root = lexical_absolute_path(Path(readable_path))
            if material.node_kind == "file":
                if allow_file_material and lexical_candidate == material_root and not recursive:
                    return lexical_candidate
                continue
            try:
                lexical_candidate.relative_to(material_root)
            except ValueError:
                continue
            ensure_path_segments_without_links(
                root=material_root,
                candidate=lexical_candidate,
            )
            if recursive and lexical_candidate.is_dir():
                ensure_directory_tree_without_links(lexical_candidate)
            return lexical_candidate
        return None
    except (
        FileNotFoundError,
        FileSystemNodeInspectionError,
        UnsafeFileSystemPathError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        return None


def _resolve_task_path(candidate: str, workspace_root: Path) -> Path:
    """按任务工作区解释相对路径，并保留词法所有权边界。

    Args:
        candidate: 工具输入中的绝对或相对路径。
        workspace_root: 已规范化的任务工作区根目录。

    Returns:
        不要求目标已经存在的词法绝对路径。
    """

    candidate_path = Path(candidate).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = workspace_root / candidate_path
    return lexical_absolute_path(candidate_path)


class AgentSessionMaterialCapability:
    """保存会话累计材料，并冻结当前仍符合节点类型的路径能力。

    初始材料和用户后续补充材料共享同一份会话能力。PreToolUse 每次调用都读取
    最新快照，因此不需要重建 Claude SDK client 或重新发送第一轮任务上下文。

    Args:
        materials: 会话第一次绑定 Turn 时已经确认的材料目录。
    """

    def __init__(self, materials: tuple[AgentMaterial, ...]) -> None:
        """初始化会话累计材料能力。

        Args:
            materials: 第一轮已经授权给任务的材料。
        """

        self._materials: dict[str, AgentMaterial] = {}
        self._lock = RLock()
        self.extend(materials)

    def extend(self, materials: tuple[AgentMaterial, ...]) -> None:
        """把正式材料加入会话目录，并让同编号的新授权覆盖旧记录。

        Args:
            materials: 已由 NearMate 材料系统接纳的新增材料。

        Returns:
            无返回值。
        """

        with self._lock:
            for material in materials:
                if not material.material_id.strip():
                    continue
                self._materials[material.material_id] = material

    def catalog(self) -> tuple[AgentMaterial, ...]:
        """返回包含失效材料事实的完整目录。

        Returns:
            按首次出现顺序排列的材料目录。
        """

        with self._lock:
            return tuple(self._materials.values())

    def capabilities(self) -> tuple[AgentMaterial, ...]:
        """读取当前允许访问的稳定材料能力快照。

        Returns:
            按规范路径去重且携带冻结节点类型的材料目录。
        """

        with self._lock:
            materials = tuple(self._materials.values())

        capabilities: list[AgentMaterial] = []
        normalized_paths: set[str] = set()
        for material in materials:
            readable_path = readable_material_path(material)
            if readable_path is None:
                continue
            normalized_path = readable_path.casefold()
            if normalized_path in normalized_paths:
                continue
            normalized_paths.add(normalized_path)
            capabilities.append(replace(material, local_path=readable_path))
        return tuple(capabilities)

    def limitations(self) -> tuple[str, ...]:
        """返回目录中当前没有可读取文件的材料名称。

        Returns:
            没有本地路径或路径已经失效的材料名称。
        """

        with self._lock:
            materials = tuple(self._materials.values())
        return material_limitations(materials)
