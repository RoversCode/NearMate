"""准备 Background Activity 的配置快照、工作区和不可变任务输入。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from companion.background_agent.config_provider import (
    BackgroundAgentConfigProvider,
    BackgroundAgentNotConfiguredError,
)
from companion.background_agent.contracts import AgentMaterial, AgentTask
from companion.background_agent.model_config import AgentExecutionConfig
from companion.background_agent.skill_models import SkillExecutionSnapshot
from companion.background_agent.workspace import WorkspaceManager, WorkspaceTaskPaths
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)

BACKGROUND_AGENT_UNCONFIGURED_MESSAGE = (
    "后台工作尚未配置。请先打开 Console 的“后台工作”设置，填写 "
    "ANTHROPIC_BASE_URL，并配置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN。"
)
BACKGROUND_AGENT_PREPARE_FAILED_MESSAGE = "后台任务没有成功建立。"


@dataclass(frozen=True, slots=True)
class PreparedActivityResources:
    """保存 Runner 启动前已经冻结的一项任务资源。

    Args:
        task: 已写入工作区路径、材料目录和执行身份的不可变任务。
        execution_config: prepare 时读取的可信后台执行配置快照。
    """

    task: AgentTask
    execution_config: AgentExecutionConfig


@dataclass(frozen=True, slots=True)
class ActivityResourcePreparation:
    """保存一次资源准备的成功结果或安全拒绝事实。

    Args:
        resources: 成功时可以交给 Runner 的完整资源；失败时为空。
        reason: 失败时供代码分支使用的稳定原因。
        safe_message: 失败时可以直接告诉用户的安全说明。
    """

    resources: PreparedActivityResources | None = None
    reason: str = ""
    safe_message: str = ""

    @property
    def accepted(self) -> bool:
        """判断本次是否已经完整建立任务资源。

        Returns:
            配置、工作区和 AgentTask 都准备完成时返回真。
        """

        return self.resources is not None


class BackgroundActivityResourceManager:
    """集中管理 Background Activity 启动前资源的创建与清理。

    该对象不拥有 Activity 槽位、Runner 状态或事件账本。它只负责把一次
    prepare 所需的配置、工作区、材料和任务输入冻结在一起，并清理从未启动
    的工作区，避免通道状态机同时承担文件系统组装职责。
    """

    def __init__(
        self,
        *,
        config_provider: BackgroundAgentConfigProvider,
        workspace_manager: WorkspaceManager,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        """初始化资源管理器。

        Args:
            config_provider: 每项任务读取可信执行配置快照的来源。
            workspace_manager: 创建和清理受管任务目录的工作区管理器。
            id_factory: 可替换的 Activity 编号生成器。
        """

        self._config_provider = config_provider
        self._workspace_manager = workspace_manager
        self._id_factory = id_factory or (lambda _prefix: str(uuid.uuid4()))

    def prepare_new_task(
        self,
        *,
        interaction_session_id: str,
        goal: str,
        deliverable: bool,
        requirements: str,
        materials: tuple[AgentMaterial, ...],
        current_scene_bytes: bytes | None,
        based_on_activity_id: str | None,
        parent_task_context: dict[str, object] | None,
        agent_session_id: str | None,
    ) -> ActivityResourcePreparation:
        """为普通新任务或终态派生任务建立完整启动前资源。

        Args:
            interaction_session_id: 发起任务的 Foreground 会话编号。
            goal: 已去除首尾空白的权威任务目标。
            deliverable: 是否需要登记并展示最终成果。
            requirements: Foreground 补充的成果要求。
            materials: 本轮正式授权的材料。
            current_scene_bytes: 可选的冻结现场图片字节。
            based_on_activity_id: 可选父 Activity 编号。
            parent_task_context: 已脱敏的父任务事实。
            agent_session_id: 可选的产品级 AgentSession 编号。

        Returns:
            完整资源，或不泄漏异常正文的安全失败事实。
        """

        execution_config = self._snapshot_config()
        if isinstance(execution_config, ActivityResourcePreparation):
            return execution_config
        activity_id = self._new_activity_id()
        if not activity_id:
            return _preparation_failed("background_activity_identity_missing")
        workspace = self._prepare_workspace(
            activity_id,
            agent_config_base=execution_config.model_config.config_dir,
            current_scene_bytes=current_scene_bytes,
            inherited_agent_config_root=None,
        )
        if workspace is None:
            return _preparation_failed()
        task_paths, current_scene_path = workspace
        normalized_parent_id = _optional_text(based_on_activity_id)
        task = AgentTask(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id.strip(),
            goal=goal,
            deliverable=deliverable,
            requirements=requirements.strip(),
            turn_materials=tuple(materials),
            material_catalog=tuple(materials),
            workspace_root=str(task_paths.workspace_root),
            agent_config_dir=str(task_paths.agent_config_root),
            task_output_dir=str(task_paths.output_root),
            permission_mode=execution_config.permission_mode,
            agent_session_id=_optional_text(agent_session_id),
            current_scene_path=(
                str(current_scene_path) if current_scene_path is not None else None
            ),
            based_on_activity_id=normalized_parent_id,
            parent_task_context=(
                dict(parent_task_context)
                if isinstance(parent_task_context, dict)
                else None
            ),
            task_mode="task_derivation" if normalized_parent_id else "new_task",
            skill_snapshot=execution_config.skill_snapshot,
        )
        return ActivityResourcePreparation(
            resources=PreparedActivityResources(
                task,
                replace(
                    execution_config,
                    model_config=replace(
                        execution_config.model_config,
                        config_dir=task_paths.agent_config_root,
                    ),
                ),
            )
        )

    def prepare_correction_task(
        self,
        *,
        parent_task: AgentTask,
        interaction_session_id: str,
        goal: str,
        deliverable: bool,
        requirements: str,
        materials: tuple[AgentMaterial, ...],
        current_scene_bytes: bytes | None,
        agent_session_id: str,
        expected_sdk_session_id: str,
    ) -> ActivityResourcePreparation:
        """在现有 AgentSession 工作区中准备一项活跃纠正替代任务。

        Args:
            parent_task: 正在被用户纠正的父任务。
            interaction_session_id: 当前 Foreground 会话编号。
            goal: 纠正后的权威目标。
            deliverable: 是否需要登记并展示最终成果。
            requirements: 纠正轮补充的成果要求。
            materials: 纠正轮新增或重新授权的材料。
            current_scene_bytes: 可选的纠正轮冻结现场图片。
            agent_session_id: 必须继续使用的产品级 AgentSession 编号。
            expected_sdk_session_id: Runner 恢复时必须匹配的 SDK 会话编号。

        Returns:
            完整替代任务资源，或安全失败事实。
        """

        execution_config = self._snapshot_config(
            inherited_skill_snapshot=parent_task.skill_snapshot
        )
        if isinstance(execution_config, ActivityResourcePreparation):
            return execution_config
        activity_id = self._new_activity_id()
        if not activity_id:
            return _preparation_failed("background_activity_identity_missing")
        workspace = self._prepare_workspace(
            activity_id,
            agent_config_base=execution_config.model_config.config_dir,
            current_scene_bytes=current_scene_bytes,
            inherited_agent_config_root=Path(parent_task.agent_config_dir),
        )
        if workspace is None:
            return _preparation_failed()
        task_paths, current_scene_path = workspace
        task = AgentTask(
            activity_id=activity_id,
            interaction_session_id=interaction_session_id.strip(),
            goal=goal,
            deliverable=deliverable,
            requirements=requirements.strip(),
            turn_materials=tuple(materials),
            material_catalog=merge_agent_materials(
                parent_task.material_catalog,
                materials,
            ),
            workspace_root=str(task_paths.workspace_root),
            agent_config_dir=str(task_paths.agent_config_root),
            task_output_dir=str(task_paths.output_root),
            permission_mode=execution_config.permission_mode,
            cua_capability_granted=parent_task.cua_capability_granted,
            agent_session_id=agent_session_id.strip(),
            expected_sdk_session_id=expected_sdk_session_id.strip(),
            current_scene_path=(
                str(current_scene_path) if current_scene_path is not None else None
            ),
            based_on_activity_id=parent_task.activity_id,
            parent_task_context=None,
            task_mode="activity_correction",
            correction_instruction=_correction_instruction(goal),
            skill_snapshot=parent_task.skill_snapshot,
        )
        return ActivityResourcePreparation(
            resources=PreparedActivityResources(
                task,
                replace(
                    execution_config,
                    model_config=replace(
                        execution_config.model_config,
                        config_dir=task_paths.agent_config_root,
                    ),
                ),
            )
        )

    def configured(self) -> bool:
        """判断当前配置是否足以准备新任务。

        Returns:
            配置提供器能够生成可信快照时返回真。
        """

        try:
            self._config_provider.snapshot()
        except Exception:
            return False
        return True

    def can_start(self, task: AgentTask) -> bool:
        """在 commit 时复核配置和工作区仍然可用。

        Args:
            task: prepare 阶段已经冻结的任务。

        Returns:
            当前配置可读且任务根目录、输出目录仍存在时返回真。
        """

        if not self.configured():
            return False
        try:
            task_paths = self._workspace_manager.validate_prepared_task(
                task.activity_id,
                agent_config_root=Path(task.agent_config_dir),
                require_agent_config=False,
            )
        except (OSError, RuntimeError, ValueError):
            return False
        return bool(
            task_paths.workspace_root == Path(task.workspace_root).resolve()
            and task_paths.output_root == Path(task.task_output_dir).resolve()
        )

    def materialize_task_config(
        self,
        task: AgentTask,
        *,
        skill_snapshot: SkillExecutionSnapshot,
    ) -> None:
        """按启动时最终 Skill 快照物化普通 Activity 的私有配置。

        Args:
            task: prepare 阶段已经创建任务目录的 Activity。
            skill_snapshot: Grant 裁决后真正可见的 Skill 名单。

        Returns:
            无返回值；配置原子提交成功后才交给 Runner。

        Raises:
            RuntimeError: prepare 目录已经失效。
            OSError: 物化固定资产或 Skill 失败。
        """

        task_paths = self._workspace_manager.validate_prepared_task(
            task.activity_id,
            agent_config_root=Path(task.agent_config_dir),
            require_agent_config=False,
        )
        self._workspace_manager.materialize_agent_config(
            task_paths,
            skill_snapshot=skill_snapshot,
        )

    def discard_unstarted(self, task: AgentTask) -> None:
        """清理一项从未交给 Runner 的受管工作区。

        Args:
            task: 需要删除未启动目录和独占配置的完整任务身份。

        Returns:
            无。
        """

        self._workspace_manager.discard_unstarted_task(
            task.activity_id,
            agent_config_root=Path(task.agent_config_dir),
        )

    def _snapshot_config(
        self,
        *,
        inherited_skill_snapshot: SkillExecutionSnapshot | None = None,
    ) -> AgentExecutionConfig | ActivityResourcePreparation:
        """读取配置并把可预期失败归一化为安全事实。

        Args:
            inherited_skill_snapshot: 纠正任务必须沿用的父任务 Skill 快照。

        Returns:
            完整执行配置，或不泄漏配置内容的安全失败事实。
        """

        try:
            return self._config_provider.snapshot(
                inherited_skill_snapshot=inherited_skill_snapshot
            )
        except BackgroundAgentNotConfiguredError:
            return ActivityResourcePreparation(
                reason="background_agent_unconfigured",
                safe_message=BACKGROUND_AGENT_UNCONFIGURED_MESSAGE,
            )
        except Exception as exception:
            logger.warning(
                "后台工作配置快照读取失败 error_type=%s",
                type(exception).__name__,
            )
            return _preparation_failed()

    def _new_activity_id(self) -> str:
        """生成并清洗一项新的 Activity 编号。

        Returns:
            去除首尾空白的 Activity 编号；生成器无效时返回空字符串。
        """

        return str(self._id_factory("background_agent") or "").strip()

    def _prepare_workspace(
        self,
        activity_id: str,
        *,
        agent_config_base: Path,
        current_scene_bytes: bytes | None,
        inherited_agent_config_root: Path | None,
    ) -> tuple[WorkspaceTaskPaths, Path | None] | None:
        """创建工作区并在部分失败时原地回滚本次目录。

        Args:
            activity_id: 本项任务的 Activity 编号。
            agent_config_base: 新任务私有配置使用的产品运行目录根。
            skill_snapshot: 新工作区需要物化的产品 Skill 快照。
            current_scene_bytes: 可选的冻结现场图片字节。
            inherited_agent_config_root: 纠正任务需要复用的父 AgentSession 配置。

        Returns:
            工作区路径和可选现场路径；任何步骤失败时返回空。
        """

        workspace_created = False
        try:
            task_paths = (
                self._workspace_manager.prepare_correction_task(
                    activity_id,
                    agent_config_root=inherited_agent_config_root,
                )
                if inherited_agent_config_root is not None
                else self._workspace_manager.prepare_task(
                    activity_id,
                    agent_config_base=agent_config_base,
                )
            )
            workspace_created = True
            current_scene_path = (
                self._workspace_manager.write_current_scene(
                    task_paths,
                    current_scene_bytes,
                )
                if current_scene_bytes is not None
                else None
            )
            return task_paths, current_scene_path
        except Exception as exception:
            logger.warning(
                "后台工作区准备失败 activity_id=%s error_type=%s",
                activity_id,
                type(exception).__name__,
            )
            if workspace_created:
                try:
                    self._workspace_manager.discard_unstarted_task(
                        activity_id,
                        agent_config_root=task_paths.agent_config_root,
                    )
                except Exception:
                    logger.exception(
                        "后台未启动工作区清理失败 activity_id=%s",
                        activity_id,
                    )
            return None


def merge_agent_materials(
    inherited: tuple[AgentMaterial, ...],
    current_turn: tuple[AgentMaterial, ...],
) -> tuple[AgentMaterial, ...]:
    """合并父任务与当前轮材料，让同编号的新授权覆盖旧记录。

    Args:
        inherited: 重新确认仍有效的父任务正式材料。
        current_turn: 当前轮新增或重新授权的正式材料。

    Returns:
        父任务顺序稳定、同编号使用最新记录的去重材料清单。
    """

    latest_by_id = {material.material_id: material for material in inherited}
    ordered_ids = [material.material_id for material in inherited]
    for material in current_turn:
        if material.material_id not in latest_by_id:
            ordered_ids.append(material.material_id)
        latest_by_id[material.material_id] = material
    return tuple(latest_by_id[material_id] for material_id in ordered_ids)


def _preparation_failed(
    reason: str = "background_prepare_failed",
) -> ActivityResourcePreparation:
    """构造不泄漏异常正文的资源准备失败事实。

    Args:
        reason: 供调用方分支使用的稳定失败原因。

    Returns:
        带统一安全说明的失败结果。
    """

    return ActivityResourcePreparation(
        reason=reason,
        safe_message=BACKGROUND_AGENT_PREPARE_FAILED_MESSAGE,
    )


def _optional_text(value: str | None) -> str | None:
    """把可选文本清洗为空或去除首尾空白后的值。

    Args:
        value: 可能为空或只有空白的文本。

    Returns:
        非空清洗文本；没有有效内容时返回空。
    """

    return value.strip() if isinstance(value, str) and value.strip() else None


def _correction_instruction(goal: str) -> str:
    """生成文件清理边界明确的后台纠正指令。

    Args:
        goal: 用户纠正后的权威后台目标。

    Returns:
        要求尽力清理、保留不确定项且禁止承诺完整回滚的任务说明。
    """

    return (
        "用户已经纠正了旧任务目标。新的权威目标是："
        f"{goal.strip()}\n"
        "请先检查旧任务实际做过的分析、工具调用和文件操作。"
        "保留对新目标仍有用的变更；删除旧目标新增且对新目标无用的文件；"
        "还原旧目标修改且对新目标无用的文件。无法确认归属时保留并报告不确定项。"
        "这只是尽力清理，不得声称已经完整回滚；Bash、目录操作和外部系统副作用可能无法覆盖。"
    )
