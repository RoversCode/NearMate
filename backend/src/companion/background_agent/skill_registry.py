"""管理 NearMate 自己拥有的后台 Skill 及其执行快照。

本模块只协调产品清单、用户偏好和依赖状态。清单解析与外部命令探测分别由专门
模块负责，避免 Skill 数量增长后 Registry 退化成难以维护的大杂烩。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Mapping

from companion.background_agent.skill_asset_safety import (
    SkillAssetBoundaryError,
    is_linked_or_reparse_path,
    validate_skill_asset_tree,
)
from companion.background_agent.skill_dependencies import SkillDependencyChecker
from companion.background_agent.skill_manifest import scan_skill_manifests
from companion.background_agent.skill_models import (
    SkillExecutionSnapshot,
    SkillManifest,
    SkillManifestError,
    SkillNotFoundError,
    SkillRegistryError,
    SkillRegistryIssue,
    SkillRegistrySnapshot,
    SkillRevisionConflictError,
    SkillStatus,
    normalize_skill_preferences,
)


class SkillRegistry:
    """扫描和检查 NearMate 产品 Skill，绝不读取本机 Claude Code Skill。

    Args:
        asset_root: 产品工作区资产根目录；没有 ``.claude/skills`` 时返回空库。
        dependency_timeout_seconds: 单次外部命令版本检查的最长等待时间。
        dependency_cache_ttl_seconds: 依赖状态在当前 Registry 内复用的秒数。
        dependency_clock: 返回单调秒数的时钟；测试可注入可控实现。

    Raises:
        ValueError: 依赖检查超时或缓存有效期不是正数。
    """

    def __init__(
        self,
        *,
        asset_root: str | Path,
        dependency_timeout_seconds: float = 2.0,
        dependency_cache_ttl_seconds: float = 5.0,
        dependency_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建一个绑定产品资产根目录的 Registry。

        Args:
            asset_root: 产品工作区资产根目录。
            dependency_timeout_seconds: 单次依赖版本检查的超时时间。
            dependency_cache_ttl_seconds: 依赖状态在当前 Registry 内复用的秒数。
            dependency_clock: 返回单调秒数的时钟；测试可注入可控实现。

        Raises:
            ValueError: 依赖检查超时或缓存有效期不是正数。
        """

        self.asset_root = Path(asset_root).absolute()
        # 受管目录本身可能在扫描前被替换成 Junction。这里必须保留词法路径，
        # 否则 resolve() 会先跟随到产品目录外，后续安全检查再也看不到重解析点。
        self.skills_root = self.asset_root / ".claude" / "skills"
        self._dependency_checker = SkillDependencyChecker(
            timeout_seconds=dependency_timeout_seconds,
            cache_ttl_seconds=dependency_cache_ttl_seconds,
            clock=dependency_clock,
        )
        self._manifests: dict[str, SkillManifest] = {}
        self._issues: tuple[SkillRegistryIssue, ...] = ()
        self.refresh()

    def refresh(self) -> tuple[SkillManifest, ...]:
        """重新扫描产品 Skill 目录，并隔离清单错误。

        Returns:
            按 Skill ID 排序的合法清单元组。
        """

        # `skills` 自身正常不代表路径安全：上级 `.claude` 也可能被替换成
        # Junction。扫描前逐级检查产品资产路径，任何一级重定向都按空库处理。
        managed_path = self.asset_root
        try:
            for path_part in (managed_path, managed_path / ".claude", self.skills_root):
                if path_part.exists() and is_linked_or_reparse_path(path_part):
                    self._manifests, self._issues = {}, ()
                    self._dependency_checker.clear_cache()
                    return self.manifests
        except SkillAssetBoundaryError:
            self._manifests, self._issues = {}, ()
            self._dependency_checker.clear_cache()
            return self.manifests

        self._manifests, self._issues = scan_skill_manifests(
            self.skills_root,
            asset_root=self.asset_root,
        )
        self._dependency_checker.clear_cache()
        return self.manifests

    @property
    def manifests(self) -> tuple[SkillManifest, ...]:
        """返回最近一次扫描得到的合法清单。

        Returns:
            按 Skill ID 排序的合法清单。
        """

        return tuple(self._manifests.values())

    @property
    def issues(self) -> tuple[SkillRegistryIssue, ...]:
        """返回被隔离的坏 Skill 清单。

        Returns:
            每个坏 Skill 的安全错误摘要。
        """

        return self._issues

    def get_manifest(self, skill_id: str) -> SkillManifest:
        """按稳定 ID 获取清单。

        Args:
            skill_id: 用户或任务请求的 Skill ID。

        Returns:
            对应的产品 Skill 清单。

        Raises:
            SkillNotFoundError: ID 不存在或对应清单无效。
        """

        normalized_id = skill_id.strip()
        manifest = self._manifests.get(normalized_id)
        if manifest is None:
            raise SkillNotFoundError(f"找不到产品 Skill：{normalized_id}")
        return manifest

    def status_snapshot(
        self,
        preferences: Mapping[str, object] | None = None,
        *,
        revision: int | None = None,
    ) -> SkillRegistrySnapshot:
        """读取所有合法 Skill 的短 TTL 依赖状态，并合并用户开关偏好。

        Args:
            preferences: ``skill_id -> enabled`` 的本地偏好；非法项会被忽略。
            revision: 调用方已有的设置版本；为空时按偏好和清单内容计算稳定版本。

        Returns:
            可直接交给 Console 的完整 Skill 状态快照。
        """

        normalized_preferences = self._known_preferences(preferences)
        manifests = self.manifests
        statuses = self._build_statuses(
            manifests,
            normalized_preferences,
        )
        resolved_revision = (
            revision
            if revision is not None
            else self.revision_for_preferences(normalized_preferences)
        )
        return SkillRegistrySnapshot(statuses, resolved_revision, self._issues)

    def execution_snapshot(
        self,
        preferences: Mapping[str, object] | None = None,
        *,
        revision: int | None = None,
    ) -> SkillExecutionSnapshot:
        """冻结下一项后台任务实际可以加载的 Skill。

        Args:
            preferences: 用户保存的 Skill 开关偏好。
            revision: 可选的外部设置版本。

        Returns:
            只含开启且依赖就绪 Skill ID 的不可变快照。
        """

        status_snapshot = self.status_snapshot(preferences, revision=revision)
        return SkillExecutionSnapshot(
            enabled_skill_ids=tuple(
                status.manifest.skill_id
                for status in status_snapshot.statuses
                if status.usable
            ),
            revision=status_snapshot.revision,
        )

    def detail(
        self,
        skill_id: str,
        preferences: Mapping[str, object] | None = None,
        *,
        revision: int | None = None,
    ) -> dict[str, object]:
        """返回一个 Skill 的详情和可安全展示的 SKILL.md 文本。

        Args:
            skill_id: 要查看的稳定 Skill ID。
            preferences: 当前用户保存的 Skill 偏好。
            revision: 调用方希望附带的外部设置版本。

        Returns:
            包含状态、说明文本和相对文件名的详情对象；读取失败时说明为稳定提示。

        Raises:
            SkillNotFoundError: Skill 不存在或清单无效。
        """

        normalized_id = skill_id.strip()
        manifest = self.get_manifest(normalized_id)
        normalized_preferences = self._known_preferences(preferences)
        status = self._build_statuses(
            (manifest,),
            normalized_preferences,
        )[0]
        resolved_revision = (
            revision
            if revision is not None
            else self.revision_for_preferences(normalized_preferences)
        )
        asset_snapshot = validate_skill_asset_tree(
            status.manifest.skill_root,
            asset_root=self.asset_root,
            managed_skills_root=self.skills_root,
        )
        if asset_snapshot != status.manifest.asset_snapshot:
            raise SkillAssetBoundaryError("Skill 资产已在清单扫描后发生变化")
        try:
            instruction = status.manifest.instruction_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # 扫描完成后文件仍可能被外部程序替换。详情接口必须把这种竞态
            # 收敛成稳定提示，不能让一次坏编码升级成 Console 的 500。
            instruction = "Skill 说明不是有效的 UTF-8 文本。"
        except OSError:
            # 文件可能在列表和详情两次读取之间被安全软件占用。错误文本不能
            # 把仓库绝对路径带进 Console，因此只返回稳定的产品提示。
            instruction = "无法读取 Skill 说明。"
        # 详情读取前后的资产都必须属于同一安全边界。若中途被替换，调用方会
        # 得到稳定冲突，而不是看到可能来自产品目录外的说明或文件列表。
        current_asset_snapshot = validate_skill_asset_tree(
            status.manifest.skill_root,
            asset_root=self.asset_root,
            managed_skills_root=self.skills_root,
        )
        if (
            current_asset_snapshot != asset_snapshot
            or current_asset_snapshot != status.manifest.asset_snapshot
        ):
            raise SkillAssetBoundaryError("Skill 资产在读取期间发生变化")
        return {
            **status.to_payload(),
            "instruction": instruction,
            "files": _relative_skill_files(
                status.manifest.skill_root,
                asset_snapshot.files,
            ),
            "revision": resolved_revision,
        }

    def update_enabled(
        self,
        *,
        preferences: Mapping[str, object] | None,
        skill_id: str,
        enabled: bool,
        expected_revision: int,
    ) -> tuple[dict[str, bool], SkillStatus, int]:
        """校验一次开关修改，但不直接写用户设置文件。

        调用方持有自己的设置事务锁后，把返回的偏好字典写入设置即可。这样 Registry
        不会偷偷绕过主进程的 JSON、广播和回滚协议。

        Args:
            preferences: 当前本地 Skill 开关偏好。
            skill_id: 要修改的 Skill ID。
            enabled: 新的用户开关值。
            expected_revision: Console 读取列表时看到的版本。

        Returns:
            新偏好、修改后的 Skill 状态和新版本号。

        Raises:
            SkillRevisionConflictError: 当前设置已经被另一请求修改。
            SkillNotFoundError: Skill ID 不存在。
            ValueError: enabled 不是布尔值。
        """

        if not isinstance(enabled, bool):
            raise ValueError("Skill enabled 必须是布尔值")
        current_preferences = self._known_preferences(preferences)
        current_revision = self.revision_for_preferences(current_preferences)
        if expected_revision != current_revision:
            raise SkillRevisionConflictError(expected_revision, current_revision)
        manifest = self.get_manifest(skill_id)
        target_status = self._build_statuses(
            (manifest,),
            current_preferences,
        )[0]
        updated_preferences = dict(current_preferences)
        updated_preferences[manifest.skill_id] = enabled
        updated_revision = self.revision_for_preferences(updated_preferences)
        # 一次 PATCH 内依赖事实没有变化，只切换用户偏好即可，不能为了响应再
        # 启动第二轮外部命令检查。
        updated_status = SkillStatus(
            manifest=manifest,
            enabled=enabled,
            dependencies=target_status.dependencies,
        )
        return updated_preferences, updated_status, updated_revision

    def revision_for_preferences(self, preferences: Mapping[str, object] | None) -> int:
        """根据清单和偏好计算稳定的非密钥版本号。

        Args:
            preferences: 当前用户保存的 Skill 偏好。

        Returns:
            可以安全经过浏览器 JSON 往返的 48 bit 整数。
        """

        normalized_preferences = self._known_preferences(preferences)
        manifest_fingerprint = [
            {
                "id": manifest.skill_id,
                "version": manifest.version,
                "default_enabled": manifest.default_enabled,
                "dependencies": [item.to_payload() for item in manifest.dependencies],
            }
            for manifest in self.manifests
        ]
        encoded = json.dumps(
            {"manifests": manifest_fingerprint, "preferences": normalized_preferences},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        # JSON 数字会经过浏览器的 IEEE-754 Number。只取 48 bit，确保 Console
        # 原样回传 expected_revision 时不会因为整数精度丢失制造假冲突。
        return int.from_bytes(hashlib.sha256(encoded).digest()[:6], "big")

    def _known_preferences(
        self,
        preferences: Mapping[str, object] | None,
    ) -> dict[str, bool]:
        """丢弃已经不属于当前产品清单的历史开关。

        Args:
            preferences: 尚未信任的 Skill 偏好。

        Returns:
            只包含当前合法产品 Skill 的偏好。
        """

        normalized = normalize_skill_preferences(preferences)
        return {
            skill_id: enabled
            for skill_id, enabled in normalized.items()
            if skill_id in self._manifests
        }

    def _build_statuses(
        self,
        manifests: tuple[SkillManifest, ...],
        preferences: Mapping[str, bool],
    ) -> tuple[SkillStatus, ...]:
        """批量构建指定 Skill 的状态，并共享相同依赖检查。

        Args:
            manifests: 本次调用真正需要读取状态的 Skill 清单。
            preferences: 已过滤到当前 Registry 的用户开关。

        Returns:
            与输入清单顺序一致的 Skill 状态。
        """

        dependency_statuses = self._dependency_checker.check_many(
            dependency
            for manifest in manifests
            for dependency in manifest.dependencies
        )
        return tuple(
            SkillStatus(
                manifest=manifest,
                enabled=preferences.get(
                    manifest.skill_id,
                    manifest.default_enabled,
                ),
                dependencies=tuple(
                    dependency_statuses[dependency]
                    for dependency in manifest.dependencies
                ),
            )
            for manifest in manifests
        )


def _relative_skill_files(
    skill_root: Path,
    skill_files: tuple[Path, ...],
) -> list[str]:
    """列出 Skill 内部文件相对路径，不把本机绝对路径交给 Console。

    Args:
        skill_root: 已经通过边界校验的 Skill 根目录。
        skill_files: 同一次安全遍历得到的普通文件。

    Returns:
        排序、去绝对路径的文件名列表。
    """

    files: list[str] = []
    for path in skill_files:
        if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        files.append(path.relative_to(skill_root).as_posix())
    return files


__all__ = [
    "SkillExecutionSnapshot",
    "SkillAssetBoundaryError",
    "SkillManifest",
    "SkillManifestError",
    "SkillNotFoundError",
    "SkillRegistry",
    "SkillRegistryError",
    "SkillRegistryIssue",
    "SkillRegistrySnapshot",
    "SkillRevisionConflictError",
    "SkillStatus",
]
