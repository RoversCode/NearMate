"""管理 V1 默认工作区和产品 Skill 资产。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from companion.background_agent.skill_asset_safety import (
    SkillAssetBoundaryError,
    is_linked_or_reparse_path,
    validate_managed_asset_tree,
    validate_skill_asset_tree,
)
from companion.background_agent.skill_models import SkillExecutionSnapshot
from companion.background_agent.skill_registry import SkillRegistry
from companion.background_agent.workspace_location import resolve_default_workspace_root


@dataclass(frozen=True, slots=True)
class WorkspaceTaskPaths:
    """保存一次 Activity 使用的工作目录、私有配置和交付目录。

    Args:
        workspace_root: Claude Code 本项任务的工作区根目录。
        task_root: 当前 Activity 的任务目录。
        agent_config_root: 当前 AgentSession 独占的 Claude 配置目录。
        input_root: Backend 冻结现场输入的只读目录。
        output_root: Backend 会登记交付文件的输出目录。
    """

    workspace_root: Path
    task_root: Path
    agent_config_root: Path
    input_root: Path
    output_root: Path


class WorkspaceManager:
    """为每项任务准备共享业务工作区和独占 Agent 配置。

    Args:
        asset_root: 仓库内的产品工作区资产目录。
        workspace_root: 测试或未来工作区功能指定的根目录。
        skill_registry: 只允许物化产品 Skill 的 Registry。
    """

    def __init__(
        self,
        *,
        asset_root: Path,
        workspace_root: Path | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        """初始化工作区管理器。

        Args:
            asset_root: 仓库内的 `.claude` Skill 和 CLAUDE.md 模板目录。
            workspace_root: 测试或未来工作区管理功能传入的根目录。
            skill_registry: 只扫描 `asset_root` 的产品 Skill Registry。
        """

        # 产品资产路径必须保留词法身份；resolve() 会先跟随 Junction，使后续
        # 安全校验无法判断目录是否已经越过安装边界。
        self._asset_root = asset_root.absolute()
        # 业务任务目录同样是清理边界，必须保留词法身份才能识别后来出现的
        # Junction；共享工作区中的其它用户文件仍按原业务语义对 Agent 可见。
        self._workspace_root = (
            workspace_root or resolve_default_workspace_root()
        ).absolute()
        self._skill_registry = skill_registry or SkillRegistry(asset_root=self._asset_root)

    @property
    def workspace_root(self) -> Path:
        """返回 V1 默认工作区绝对路径。

        Returns:
            当前管理器使用的工作区绝对路径。
        """

        return self._workspace_root

    def prepare_task(
        self,
        activity_id: str,
        *,
        agent_config_base: Path,
    ) -> WorkspaceTaskPaths:
        """只创建任务目录并预留本 Activity 独占的 Claude 配置路径。

        Args:
            activity_id: Activity 编号，只允许作为目录名使用。
            agent_config_base: 产品私有配置根；每项 Activity 会在其下独占目录。
        Returns:
            包含业务工作区、尚未物化的私有配置路径、输入目录和交付目录的路径对象。

        Raises:
            ValueError: Activity 编号为空或包含路径分隔符。
            FileExistsError: Activity 编号已经拥有任务目录或私有配置。
        """

        normalized_activity_id = _normalize_activity_id(activity_id)
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        # 配置路径是后续 SDK 信任边界，必须保留调用方给出的词法身份。
        # resolve() 会跟随 Junction，把受管路径悄悄改写成外部目标。
        agent_config_base_root = agent_config_base.absolute()
        agent_config_activities_root = agent_config_base_root / "activities"
        agent_config_base_root.mkdir(parents=True, exist_ok=True)
        agent_config_activities_root.mkdir(exist_ok=True)
        agent_config_root = agent_config_activities_root / normalized_activity_id
        _validate_agent_config_boundary(agent_config_root, require_config_root=False)
        if agent_config_root.is_symlink() or agent_config_root.is_junction() or agent_config_root.exists():
            raise FileExistsError("Activity 私有 Agent 配置已经存在")
        task_paths = self._create_task_directories(
            normalized_activity_id,
            agent_config_root=agent_config_root,
        )
        return task_paths

    def materialize_agent_config(
        self,
        task_paths: WorkspaceTaskPaths,
        *,
        skill_snapshot: SkillExecutionSnapshot,
    ) -> None:
        """按启动时最终裁决的 Skill 快照原子物化私有 Agent 配置。

        Args:
            task_paths: prepare 阶段已经创建的任务目录和配置路径。
            skill_snapshot: CUA Grant 裁决后真正可以加载的 Skill 名单。

        Returns:
            无返回值；配置目录提交成功后才允许 Runner 接管任务。

        Raises:
            FileExistsError: 配置目录已被其它任务占用。
            OSError: 固定资产或 Skill 复制失败。
        """

        self._materialize_agent_config(
            task_paths.agent_config_root,
            skill_snapshot=skill_snapshot,
        )
        try:
            _validate_agent_config_boundary(task_paths.agent_config_root)
        except BaseException:
            if task_paths.agent_config_root.is_symlink() or task_paths.agent_config_root.exists():
                _remove_workspace_asset(task_paths.agent_config_root)
            raise

    def prepare_correction_task(
        self,
        activity_id: str,
        *,
        agent_config_root: Path,
    ) -> WorkspaceTaskPaths:
        """为纠正任务创建目录，并复用父 AgentSession 的私有配置。

        后台纠正会恢复父 Claude 会话，因此配置、Skill 和 Session 存储必须
        继续使用父任务目录；这里只为替代 Activity 新建输入与输出目录。

        Args:
            activity_id: Activity 编号，只允许作为目录名使用。
            agent_config_root: 父任务已经物化并仍在使用的私有配置目录。

        Returns:
            包含共享业务工作区、父私有配置和新任务目录的路径对象。

        Raises:
            ValueError: Activity 编号不安全。
            RuntimeError: 工作区或父私有配置已经失效。
            FileExistsError: Activity 编号已经拥有任务目录。
        """

        normalized_activity_id = _normalize_activity_id(activity_id)
        lexical_agent_config_root = _validate_agent_config_boundary(agent_config_root)
        return self._create_task_directories(
            normalized_activity_id,
            agent_config_root=lexical_agent_config_root,
        )

    def _create_task_directories(
        self,
        activity_id: str,
        *,
        agent_config_root: Path,
    ) -> WorkspaceTaskPaths:
        """原子地建立一项全新 Activity 的输入与输出目录。

        Args:
            activity_id: 已验证可安全用作目录名的 Activity 编号。
            agent_config_root: 新任务独占或从父任务继承的私有配置目录。

        Returns:
            已创建任务目录的完整路径。

        Raises:
            FileExistsError: 相同 Activity 目录已经存在。
            OSError: 目录创建失败。
        """

        lexical_workspace_root = _validate_workspace_root(self._workspace_root)
        tasks_root = lexical_workspace_root / "tasks"
        tasks_root.mkdir(exist_ok=True)
        task_root = tasks_root / activity_id
        _validate_task_boundary(task_root, require_task_root=False)
        if task_root.is_symlink() or task_root.is_junction() or task_root.exists():
            raise FileExistsError("Activity 任务目录已经存在")
        input_root = task_root / "inputs"
        output_root = task_root / "outputs"
        try:
            input_root.mkdir(parents=True)
            output_root.mkdir()
            _validate_task_boundary(task_root)
        except BaseException:
            # 外层只有在本方法完整返回后才拿得到目录身份，因此部分创建必须由
            # 所有者原地回滚，不能把半个任务目录留给后续同编号请求。
            _remove_task_root_when_parent_is_safe(task_root)
            raise
        return WorkspaceTaskPaths(
            workspace_root=self._workspace_root,
            task_root=task_root,
            agent_config_root=agent_config_root,
            input_root=input_root,
            output_root=output_root,
        )

    def _materialize_agent_config(
        self,
        agent_config_root: Path,
        *,
        skill_snapshot: SkillExecutionSnapshot | None,
    ) -> None:
        """把固定规则和冻结 Skill 安全复制到一项任务的私有配置目录。

        Args:
            agent_config_root: 当前 Activity 独占且尚不存在的正式配置目录。
            skill_snapshot: 本任务冻结的可用 Skill 名单；为空时不物化 Skill。

        Returns:
            无返回值。

        Raises:
            FileExistsError: 正式配置目录已经存在，不能覆盖另一项任务。
            SkillAssetBoundaryError: 产品资产越界或在复制期间变化。
            OSError: 文件复制或正式提交失败。
        """

        if agent_config_root.is_symlink() or agent_config_root.exists():
            raise FileExistsError("Activity 私有 Agent 配置已经存在")
        resolved_snapshot = skill_snapshot or SkillExecutionSnapshot()
        temporary_config_root = agent_config_root.parent / (
            f".{agent_config_root.name}.nearmate-{uuid4().hex}.tmp"
        )
        product_claude_root = self._asset_root / ".claude"
        try:
            # 固定规则和 Skill 都先进入临时树。源资产在复制前后必须保持同一
            # 安全快照，避免同步工具或 Junction 在检查后偷换实际内容。
            if product_claude_root.is_symlink() or product_claude_root.exists():
                fixed_asset_snapshot_before_copy = validate_managed_asset_tree(
                    product_claude_root,
                    asset_root=self._asset_root,
                    ignored_names=frozenset({"skills"}),
                )
                shutil.copytree(
                    product_claude_root,
                    temporary_config_root,
                    ignore=shutil.ignore_patterns("skills"),
                )
                fixed_asset_snapshot_after_copy = validate_managed_asset_tree(
                    product_claude_root,
                    asset_root=self._asset_root,
                    ignored_names=frozenset({"skills"}),
                )
                if fixed_asset_snapshot_after_copy != fixed_asset_snapshot_before_copy:
                    raise SkillAssetBoundaryError("产品固定资产在复制期间发生变化")
            else:
                temporary_config_root.mkdir()
            temporary_skills_root = temporary_config_root / "skills"
            temporary_skills_root.mkdir()
            for skill_id in resolved_snapshot.enabled_skill_ids:
                manifest = self._skill_registry.get_manifest(skill_id)
                asset_snapshot_before_copy = validate_skill_asset_tree(
                    manifest.skill_root,
                    asset_root=self._skill_registry.asset_root,
                    managed_skills_root=self._skill_registry.skills_root,
                )
                if asset_snapshot_before_copy != manifest.asset_snapshot:
                    raise SkillAssetBoundaryError("Skill 资产已在清单扫描后发生变化")
                shutil.copytree(
                    manifest.skill_root,
                    temporary_skills_root / manifest.skill_id,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                )
                asset_snapshot_after_copy = validate_skill_asset_tree(
                    manifest.skill_root,
                    asset_root=self._skill_registry.asset_root,
                    managed_skills_root=self._skill_registry.skills_root,
                )
                if asset_snapshot_after_copy != asset_snapshot_before_copy:
                    raise SkillAssetBoundaryError("Skill 资产在复制期间发生变化")
            temporary_config_root.replace(agent_config_root)
        finally:
            if temporary_config_root.is_symlink() or temporary_config_root.exists():
                _remove_workspace_asset(temporary_config_root)

    def validate_prepared_task(
        self,
        activity_id: str,
        *,
        agent_config_root: Path,
        require_agent_config: bool = True,
    ) -> WorkspaceTaskPaths:
        """确认 prepare 生成的任务目录和私有配置仍可供 Runner 使用。

        Args:
            activity_id: 已经由本管理器创建的 Activity 编号。
            agent_config_root: 任务冻结的私有 Agent 配置目录。
            require_agent_config: 是否要求启动前已经完成配置物化。

        Returns:
            全部目录存在时返回规范路径对象。

        Raises:
            RuntimeError: 业务工作区、任务目录或 Agent 配置已经失效。
        """

        normalized_activity_id = _normalize_activity_id(activity_id)
        task_root = self._workspace_root / "tasks" / normalized_activity_id
        lexical_task_root = _validate_task_boundary(task_root)
        input_root = lexical_task_root / "inputs"
        output_root = lexical_task_root / "outputs"
        lexical_agent_config_root = _validate_agent_config_boundary(
            agent_config_root,
            require_config_root=require_agent_config,
        )
        if not all(
            path.is_dir()
            for path in (
                self._workspace_root,
                lexical_task_root,
                input_root,
                output_root,
                *( (lexical_agent_config_root,) if require_agent_config else () ),
            )
        ):
            raise RuntimeError("后台任务准备资源已经失效")
        return WorkspaceTaskPaths(
            workspace_root=self._workspace_root,
            task_root=lexical_task_root,
            agent_config_root=lexical_agent_config_root,
            input_root=input_root,
            output_root=output_root,
        )

    def discard_unstarted_task(
        self,
        activity_id: str,
        *,
        agent_config_root: Path,
    ) -> bool:
        """删除从未启动的任务目录及其独占 Agent 配置。

        纠正任务继承父配置，因此只有配置目录本身以当前 Activity 编号命名时
        才能删除；回滚替代任务绝不能破坏仍属于父会话的配置和 Session。

        Args:
            activity_id: 已经由本管理器创建、但 Runner 未接管的 Activity 编号。
            agent_config_root: 新任务独占或纠正任务继承的私有配置目录。

        Returns:
            任一受管目录存在并成功删除时返回真；都不存在时返回假。
        """

        normalized_activity_id = _normalize_activity_id(activity_id)
        tasks_root = self._workspace_root / "tasks"
        task_root = tasks_root / normalized_activity_id
        lexical_agent_config_root = agent_config_root.absolute()
        removable_paths: list[Path] = []
        try:
            lexical_task_root = _validate_task_boundary(
                task_root,
                require_task_root=False,
            )
        except RuntimeError:
            # 父链失信时最终词法路径可能已指向外部同名目录。此时宁可留下
            # 原业务目录，也不能把外部普通目录误当成当前 Activity 递归删除。
            pass
        else:
            removable_paths.append(lexical_task_root)
        if (
            lexical_agent_config_root.name == normalized_activity_id
            and lexical_agent_config_root.parent.name == "activities"
        ):
            try:
                _validate_agent_config_boundary(
                    lexical_agent_config_root,
                    require_config_root=False,
                )
            except RuntimeError:
                # task 目录仍归当前 Activity，但配置父链已经无法证明所有权。
                # 此时不能沿危险父目录删除词法同名路径。
                pass
            else:
                removable_paths.append(lexical_agent_config_root)
        removed = False
        first_error: Exception | None = None
        for path in removable_paths:
            if not (path.is_symlink() or path.is_junction() or path.exists()):
                continue
            try:
                _remove_workspace_asset(path)
                removed = True
            except Exception as exception:
                # 两份目录互不包含。一个清理失败时仍继续尝试另一份，尽量避免
                # 同时遗留业务输入和私有凭据目录；最后再把首个异常交给调用方。
                first_error = first_error or exception
        if first_error is not None:
            raise first_error
        return removed

    @staticmethod
    def write_current_scene(
        task_paths: WorkspaceTaskPaths,
        screenshot_bytes: bytes,
    ) -> Path:
        """把本轮冻结截图原样写入任务输入目录。

        Args:
            task_paths: 已经为当前 Activity 创建好的任务目录。
            screenshot_bytes: Desktop 本轮冻结的 PNG 原始字节。

        Returns:
            执行内核可读取的截图绝对路径。

        Raises:
            ValueError: 截图内容为空。
        """

        if not screenshot_bytes:
            raise ValueError("当前现场截图不能为空")
        current_scene_path = task_paths.input_root / "current-scene.png"
        current_scene_path.write_bytes(screenshot_bytes)
        return current_scene_path.resolve()

    def collect_outputs(
        self,
        activity_id: str,
    ) -> tuple[str, ...]:
        """只读收集任务交付目录中的实际文件路径。

        Args:
            activity_id: Activity 编号。

        Returns:
            按路径排序的文件绝对路径元组。

        Raises:
            RuntimeError: 任务输出目录已经失效。
        """

        normalized_activity_id = _normalize_activity_id(activity_id)
        output_root = (
            self._workspace_root / "tasks" / normalized_activity_id / "outputs"
        )
        if not output_root.is_dir():
            raise RuntimeError("后台任务输出目录已经失效")
        return tuple(
            str(path.resolve())
            for path in sorted(output_root.rglob("*"))
            if path.is_file()
        )


def _normalize_activity_id(activity_id: str) -> str:
    """校验 Activity 编号可以安全用作单层目录名。

    Args:
        activity_id: 外层执行器生成的 Activity 编号。

    Returns:
        去除首尾空白后的安全目录名。

    Raises:
        ValueError: 编号为空、包含路径分隔符或试图引用父目录。
    """

    normalized_activity_id = activity_id.strip()
    if (
        not normalized_activity_id
        or Path(normalized_activity_id).name != normalized_activity_id
    ):
        raise ValueError("Activity 编号不能作为安全的任务目录名")
    return normalized_activity_id


def _validate_workspace_root(workspace_root: Path) -> Path:
    """确认共享业务工作区根没有被链接或重解析点接管。

    Args:
        workspace_root: WorkspaceManager 持有的词法工作区根目录。

    Returns:
        不跟随链接的词法绝对工作区路径。

    Raises:
        RuntimeError: 工作区不存在、已被重定向或状态无法安全读取。
    """

    lexical_workspace_root = workspace_root.absolute()
    try:
        if (
            is_linked_or_reparse_path(lexical_workspace_root)
            or not lexical_workspace_root.is_dir()
        ):
            raise RuntimeError("后台任务业务工作区已经失效或越界")
        # 检查与使用之间仍可能发生替换。返回前再检查一次节点自身，缩小把
        # 已重定向路径交给后续任务目录操作的竞态窗口。
        if is_linked_or_reparse_path(lexical_workspace_root):
            raise RuntimeError("后台任务业务工作区已经失效或越界")
    except SkillAssetBoundaryError as exception:
        raise RuntimeError("后台任务业务工作区状态无法安全确认") from exception
    return lexical_workspace_root


def _validate_task_boundary(
    task_root: Path,
    *,
    require_task_root: bool = True,
) -> Path:
    """验证 Activity 任务目录仍位于未重定向的业务父链中。

    Args:
        task_root: 词法结构应为 `workspace/tasks/<activity_id>` 的路径。
        require_task_root: 创建前只验证 workspace 和 tasks 两级父目录。

    Returns:
        不跟随链接的词法绝对任务路径。

    Raises:
        RuntimeError: 路径结构、目录状态或任一关键节点不再可信。
    """

    lexical_task_root = task_root.absolute()
    tasks_root = lexical_task_root.parent
    workspace_root = tasks_root.parent
    if tasks_root.name != "tasks":
        raise RuntimeError("Activity 任务目录不属于业务工作区")
    _validate_workspace_root(workspace_root)
    path_chain = [workspace_root, tasks_root]
    if require_task_root:
        path_chain.append(lexical_task_root)
    try:
        for path in path_chain:
            if is_linked_or_reparse_path(path) or not path.is_dir():
                raise RuntimeError("Activity 任务目录已经失效或越界")
        # 父目录若在第一次检查后被换成 Junction，最终节点会伪装成外部普通
        # 目录；返回前复查整条关键链，不能只盯着 Activity 最终节点。
        if any(is_linked_or_reparse_path(path) for path in path_chain):
            raise RuntimeError("Activity 任务目录已经失效或越界")
    except SkillAssetBoundaryError as exception:
        raise RuntimeError("Activity 任务目录状态无法安全确认") from exception
    return lexical_task_root


def _remove_task_root_when_parent_is_safe(task_root: Path) -> bool:
    """只在 workspace/tasks 父链可信时删除具体 Activity 节点。

    Args:
        task_root: 当前 Activity 的词法任务目录。

    Returns:
        实际删除了目录或链接节点时返回真；父链危险或节点不存在时返回假。
    """

    try:
        lexical_task_root = _validate_task_boundary(
            task_root,
            require_task_root=False,
        )
    except RuntimeError:
        return False
    if not (
        lexical_task_root.is_symlink()
        or lexical_task_root.is_junction()
        or lexical_task_root.exists()
    ):
        return False
    _remove_workspace_asset(lexical_task_root)
    return True


def _validate_agent_config_boundary(
    agent_config_root: Path,
    *,
    require_config_root: bool = True,
) -> Path:
    """验证 Activity 私有配置仍位于未重定向的三层受管路径中。

    Args:
        agent_config_root: 词法结构应为 `runtime/activities/<activity_id>` 的路径。
        require_config_root: 正式配置尚未物化时只校验 runtime 和 activities。

    Returns:
        不跟随链接的词法绝对配置路径。

    Raises:
        RuntimeError: 路径结构、目录状态或任一关键节点不再可信。
    """

    lexical_agent_config_root = agent_config_root.absolute()
    agent_config_activities_root = lexical_agent_config_root.parent
    agent_runtime_root = agent_config_activities_root.parent
    if agent_config_activities_root.name != "activities":
        raise RuntimeError("Activity 私有 Agent 配置不属于产品运行目录")
    path_chain = [agent_runtime_root, agent_config_activities_root]
    if require_config_root:
        path_chain.append(lexical_agent_config_root)
    try:
        for path in path_chain:
            if is_linked_or_reparse_path(path) or not path.is_dir():
                raise RuntimeError("Activity 私有 Agent 配置已经失效或越界")
        # 目录状态读取期间仍可能发生替换。返回前再看一次节点自身，避免把
        # 已经重定向的词法身份交给 Runner 和 CLAUDE_CONFIG_DIR。
        if any(is_linked_or_reparse_path(path) for path in path_chain):
            raise RuntimeError("Activity 私有 Agent 配置已经失效或越界")
    except SkillAssetBoundaryError as exception:
        raise RuntimeError("Activity 私有 Agent 配置状态无法安全确认") from exception
    return lexical_agent_config_root


def _remove_workspace_asset(path: Path) -> None:
    """删除 NearMate 自己创建的具体目录、文件或重解析节点。

    Args:
        path: 调用方已经确认所有权的具体资产路径。

    Returns:
        无返回值。
    """

    if path.is_junction():
        path.rmdir()
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.exists():
        shutil.rmtree(path)
