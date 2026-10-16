"""读取和校验 NearMate 产品 Skill 的 manifest.json。"""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Any

import yaml

from companion.background_agent.skill_asset_safety import (
    SkillAssetBoundaryError,
    is_linked_or_reparse_path,
    validate_skill_asset_tree,
)
from companion.background_agent.skill_models import (
    SKILL_ID_PATTERN,
    SkillDependencySpec,
    SkillManifest,
    SkillManifestError,
    SkillRegistryIssue,
)


_NUMERIC_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")


def scan_skill_manifests(
    skills_root: Path,
    *,
    asset_root: Path,
) -> tuple[dict[str, SkillManifest], tuple[SkillRegistryIssue, ...]]:
    """扫描产品 Skill 目录，并把坏清单隔离成诊断信息。

    Args:
        skills_root: 只属于 NearMate 的 ``.claude/skills`` 目录。
        asset_root: 包含受管 ``.claude`` 目录的产品资产根。

    Returns:
        按 ID 排序的合法清单和坏 Skill 问题列表。
    """

    manifests: dict[str, SkillManifest] = {}
    issues: list[SkillRegistryIssue] = []
    try:
        root_status = skills_root.lstat()
        root_is_unsafe = is_linked_or_reparse_path(skills_root)
    except FileNotFoundError:
        return {}, ()
    except SkillAssetBoundaryError:
        return {}, ()
    if root_is_unsafe or not stat.S_ISDIR(root_status.st_mode):
        return {}, ()
    for skill_root in sorted(skills_root.iterdir(), key=lambda item: item.name):
        try:
            if is_linked_or_reparse_path(skill_root) or not skill_root.is_dir():
                continue
        except SkillAssetBoundaryError:
            continue
        try:
            manifest = load_skill_manifest(
                skill_root,
                asset_root=asset_root,
                managed_skills_root=skills_root,
            )
        except SkillManifestError as exception:
            issues.append(SkillRegistryIssue(skill_root, str(exception)))
            continue
        if manifest.skill_id in manifests:
            issues.append(SkillRegistryIssue(skill_root, "Skill ID 重复"))
            continue
        manifests[manifest.skill_id] = manifest
    return dict(sorted(manifests.items())), tuple(issues)


def load_skill_manifest(
    skill_root: Path,
    *,
    asset_root: Path,
    managed_skills_root: Path,
) -> SkillManifest:
    """解析并严格校验单个 Skill 目录。

    Args:
        skill_root: 一个候选产品 Skill 目录。
        asset_root: NearMate 产品工作区资产根目录。
        managed_skills_root: 该目录必须直属的 NearMate 受管 Skill 根目录。

    Returns:
        已验证且可以安全物化的产品 Skill 清单。

    Raises:
        SkillManifestError: 清单、目录或依赖声明不合法。
    """

    skill_id = skill_root.name
    if not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise SkillManifestError("目录名不是合法 Skill ID")
    try:
        initial_asset_snapshot = validate_skill_asset_tree(
            skill_root,
            asset_root=asset_root,
            managed_skills_root=managed_skills_root,
        )
    except SkillAssetBoundaryError as exception:
        raise SkillManifestError(str(exception)) from exception

    manifest_path = skill_root / "manifest.json"
    instruction_path = skill_root / "SKILL.md"
    if not manifest_path.is_file():
        raise SkillManifestError("缺少 manifest.json")
    if not instruction_path.is_file():
        raise SkillManifestError("缺少 SKILL.md")

    try:
        instruction_text = instruction_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exception:
        raise SkillManifestError("SKILL.md 必须是 UTF-8 文本") from exception
    except OSError as exception:
        # Registry 问题会直接返回 Console，不能把异常里的产品绝对路径带出去。
        raise SkillManifestError("SKILL.md 无法读取") from exception
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exception:
        raise SkillManifestError("manifest.json 必须是 UTF-8 文本") from exception
    except OSError as exception:
        raise SkillManifestError("manifest.json 无法读取") from exception
    try:
        # 读取期间资产可能被同步工具替换。返回清单前复验，避免把竞态中的
        # 外部文件内容固化为受管 Skill。
        current_asset_snapshot = validate_skill_asset_tree(
            skill_root,
            asset_root=asset_root,
            managed_skills_root=managed_skills_root,
        )
    except SkillAssetBoundaryError as exception:
        raise SkillManifestError(str(exception)) from exception
    if current_asset_snapshot != initial_asset_snapshot:
        raise SkillManifestError("Skill 资产在清单读取期间发生变化")
    try:
        payload = json.loads(manifest_text)
    except json.JSONDecodeError as exception:
        raise SkillManifestError("manifest.json 必须是合法 JSON") from exception
    if not isinstance(payload, dict):
        raise SkillManifestError("manifest.json 根节点必须是对象")
    unexpected_fields = set(payload) - {
        "id",
        "name",
        "summary",
        "category",
        "version",
        "default_enabled",
        "dependencies",
        "trigger_scenarios",
        "inputs",
        "outputs",
    }
    if unexpected_fields:
        raise SkillManifestError(
            f"manifest.json 包含未知字段：{', '.join(sorted(unexpected_fields))}"
        )
    if payload.get("id") != skill_id:
        raise SkillManifestError("manifest.id 必须与 Skill 目录名一致")
    _validate_skill_instruction_frontmatter(instruction_text, skill_id=skill_id)
    default_enabled = payload.get("default_enabled", True)
    if not isinstance(default_enabled, bool):
        raise SkillManifestError("default_enabled 必须是布尔值")
    version = _required_text(payload, "version")
    if not _NUMERIC_VERSION_PATTERN.fullmatch(version):
        raise SkillManifestError("version 必须是纯数字版本号")
    return SkillManifest(
        skill_id=skill_id,
        display_name=_required_text(payload, "name"),
        summary=_required_text(payload, "summary"),
        category=_required_text(payload, "category"),
        version=version,
        default_enabled=default_enabled,
        # 保留已经完成边界校验的词法路径；不能在这里重新跟随潜在 Junction。
        skill_root=skill_root.absolute(),
        asset_snapshot=current_asset_snapshot,
        dependencies=_parse_dependencies(payload.get("dependencies", [])),
        trigger_scenarios=_optional_text_list(payload, "trigger_scenarios"),
        inputs=_optional_text_list(payload, "inputs"),
        outputs=_optional_text_list(payload, "outputs"),
    )


def _validate_skill_instruction_frontmatter(
    instruction_text: str,
    *,
    skill_id: str,
) -> None:
    """验证 Claude Code 用来识别 Skill 的 YAML frontmatter。

    Args:
        instruction_text: 已经按 UTF-8 解码的完整 SKILL.md。
        skill_id: 目录名和 manifest.id 共同确认的稳定 Skill ID。

    Returns:
        无返回值。

    Raises:
        SkillManifestError: frontmatter 缺失、YAML 无效或身份字段不合法。
    """

    lines = instruction_text.splitlines()
    if not lines or lines[0] != "---":
        raise SkillManifestError("SKILL.md 缺少 YAML frontmatter")
    try:
        closing_delimiter_index = lines.index("---", 1)
    except ValueError as exception:
        raise SkillManifestError("SKILL.md 缺少 YAML frontmatter 结束分隔线") from exception
    frontmatter_text = "\n".join(lines[1:closing_delimiter_index])
    try:
        metadata = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exception:
        raise SkillManifestError("SKILL.md YAML frontmatter 无法解析") from exception
    if not isinstance(metadata, dict):
        raise SkillManifestError("SKILL.md YAML frontmatter 必须是对象")
    if metadata.get("name") != skill_id:
        raise SkillManifestError("SKILL.md frontmatter.name 必须与 Skill ID 一致")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillManifestError(
            "SKILL.md frontmatter.description 必须是非空字符串"
        )


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    """读取一个必须存在且非空的清单文本字段。

    Args:
        payload: 已确认根节点为对象的清单。
        field_name: 要读取的字段名。

    Returns:
        去除首尾空白的非空文本。

    Raises:
        SkillManifestError: 字段缺失、类型错误或内容为空。
    """

    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise SkillManifestError(f"{field_name} 必须是非空字符串")
    return value.strip()


def _optional_text_list(
    payload: dict[str, Any],
    field_name: str,
) -> tuple[str, ...]:
    """读取可选、去重且不含空白项的展示文本列表。

    Args:
        payload: 已确认根节点为对象的 Skill 清单。
        field_name: 要读取的元数据字段名。

    Returns:
        保持清单顺序的唯一非空文本元组；字段缺失时返回空元组。

    Raises:
        SkillManifestError: 字段不是字符串数组、含空项或重复项时抛出。
    """

    value = payload.get(field_name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SkillManifestError(f"{field_name} 必须是字符串数组")
    normalized = tuple(item.strip() for item in value)
    if any(not item for item in normalized):
        raise SkillManifestError(f"{field_name} 不能包含空白项")
    if len(set(normalized)) != len(normalized):
        raise SkillManifestError(f"{field_name} 不能包含重复项")
    return normalized


def _parse_dependencies(value: object) -> tuple[SkillDependencySpec, ...]:
    """解析 V1 executable 依赖声明。

    Args:
        value: manifest.json 中尚未信任的 dependencies 字段。

    Returns:
        去重并验证过的外部命令依赖。

    Raises:
        SkillManifestError: 依赖结构、字段或版本不合法。
    """

    if not isinstance(value, list):
        raise SkillManifestError("dependencies 必须是数组")
    dependencies: list[SkillDependencySpec] = []
    dependency_names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise SkillManifestError("每个依赖必须是对象")
        unexpected_fields = set(item) - {"kind", "name", "minimum_version"}
        if unexpected_fields:
            raise SkillManifestError(
                f"依赖包含未知字段：{', '.join(sorted(unexpected_fields))}"
            )
        kind = item.get("kind")
        name = item.get("name")
        minimum_version = item.get("minimum_version")
        if kind != "executable":
            raise SkillManifestError("V1 只支持 executable 依赖")
        if not isinstance(name, str) or not name.strip():
            raise SkillManifestError("依赖 name 必须是非空字符串")
        if minimum_version is not None and (
            not isinstance(minimum_version, str) or not minimum_version.strip()
        ):
            raise SkillManifestError("minimum_version 必须是非空字符串或空值")
        if isinstance(minimum_version, str) and not _NUMERIC_VERSION_PATTERN.fullmatch(
            minimum_version.strip()
        ):
            raise SkillManifestError("minimum_version 必须是纯数字版本号")
        normalized_name = name.strip()
        if normalized_name in dependency_names:
            raise SkillManifestError(f"依赖 {normalized_name} 重复声明")
        dependency_names.add(normalized_name)
        dependencies.append(
            SkillDependencySpec(
                kind="executable",
                name=normalized_name,
                minimum_version=(
                    minimum_version.strip() if isinstance(minimum_version, str) else None
                ),
            )
        )
    return tuple(dependencies)
