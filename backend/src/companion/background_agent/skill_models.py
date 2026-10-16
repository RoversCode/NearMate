"""定义 NearMate 产品 Skill 的稳定领域模型和错误类型。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from companion.background_agent.skill_asset_safety import SkillAssetTreeSnapshot


SkillDependencyKind = Literal["executable"]
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class SkillRegistryError(RuntimeError):
    """表示 Skill Registry 无法提供可靠的产品 Skill 信息。

    Args:
        message: 面向产品调用方的稳定错误原因。
    """


class SkillManifestError(SkillRegistryError):
    """表示单个 Skill 的清单或资产结构不合法。

    Args:
        message: 清单或资产不合法的稳定原因。
    """


class SkillNotFoundError(SkillRegistryError):
    """表示调用方请求了 Registry 中不存在的 Skill。

    Args:
        message: 找不到目标 Skill 的稳定原因。
    """


class SkillRevisionConflictError(SkillRegistryError):
    """表示用户正在基于过期的 Skill 设置版本提交修改。"""

    def __init__(self, expected_revision: int, actual_revision: int) -> None:
        """保存冲突双方的版本号。

        Args:
            expected_revision: Console 提交时携带的旧版本。
            actual_revision: Backend 当前权威版本。
        """

        super().__init__(
            f"Skill 设置版本已变化，提交版本={expected_revision}，当前版本={actual_revision}"
        )
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


@dataclass(frozen=True, slots=True)
class SkillDependencySpec:
    """描述一个 Skill 启动前必须具备的外部命令。

    Args:
        kind: V1 只支持 executable，表示从系统 PATH 查找命令。
        name: 命令名称，例如 ffmpeg。
        minimum_version: 可选的最低版本号；为空时只检查命令是否存在。
    """

    kind: SkillDependencyKind
    name: str
    minimum_version: str | None = None

    def to_payload(self) -> dict[str, object]:
        """转换成可直接返回给 Console 的依赖描述。

        Returns:
            不包含运行检查结果的依赖声明。
        """

        payload: dict[str, object] = {"kind": self.kind, "name": self.name}
        if self.minimum_version is not None:
            payload["minimum_version"] = self.minimum_version
        return payload


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """保存一个产品 Skill 的展示元数据和受管资产路径。

    Args:
        skill_id: 对 SDK 和用户设置都稳定的 Skill 标识。
        display_name: Console 展示名称。
        summary: 一句话说明 Skill 能做什么。
        category: Console 用于筛选的分类。
        version: 产品 Skill 自己的版本号。
        default_enabled: 用户没有保存偏好时是否启用。
        skill_root: 包含 SKILL.md 和脚本的受管目录。
        asset_snapshot: 清单被信任时完整资产树的身份快照。
        dependencies: 运行该 Skill 前需要检查的依赖。
        trigger_scenarios: 适合调用这项 Skill 的用户场景。
        inputs: 完成工作时通常需要的输入。
        outputs: 完成工作后通常生成的结果。
    """

    skill_id: str
    display_name: str
    summary: str
    category: str
    version: str
    default_enabled: bool
    skill_root: Path
    asset_snapshot: SkillAssetTreeSnapshot
    dependencies: tuple[SkillDependencySpec, ...] = ()
    trigger_scenarios: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    @property
    def instruction_path(self) -> Path:
        """返回 Skill 的唯一说明文件路径。

        Returns:
            当前 Skill 根目录下的 SKILL.md 路径。
        """

        return self.skill_root / "SKILL.md"

    def to_payload(self) -> dict[str, object]:
        """返回不暴露绝对本机路径的清单信息。

        Returns:
            Console 列表可直接使用的清单字段。
        """

        return {
            "id": self.skill_id,
            "name": self.display_name,
            "summary": self.summary,
            "category": self.category,
            "version": self.version,
            "default_enabled": self.default_enabled,
            "source": "builtin",
            "dependencies": [item.to_payload() for item in self.dependencies],
            "trigger_scenarios": list(self.trigger_scenarios),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
        }


@dataclass(frozen=True, slots=True)
class SkillDependencyStatus:
    """保存一次依赖预检的可解释结果。

    Args:
        specification: Registry 清单中声明的依赖。
        ready: 当前依赖是否满足要求。
        executable_path: 找到的命令绝对路径；找不到时为空。
        detected_version: 从命令版本输出中解析出的版本；无法解析时为空。
        reason: 给用户看的失败原因；依赖正常时为空。
    """

    specification: SkillDependencySpec
    ready: bool
    executable_path: str | None = None
    detected_version: str | None = None
    reason: str | None = None

    def to_payload(self) -> dict[str, object]:
        """转换成 Console 可展示的依赖状态。

        Returns:
            清单声明和本次检查结果组成的依赖对象。
        """

        return {
            **self.specification.to_payload(),
            "ready": self.ready,
            "path": self.executable_path,
            "detected_version": self.detected_version,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SkillStatus:
    """把用户开关和短 TTL 依赖快照合成一个 Skill 状态。

    Args:
        manifest: Skill 的产品清单。
        enabled: 用户偏好是否开启该 Skill。
        dependencies: 这次检查得到的依赖结果。
    """

    manifest: SkillManifest
    enabled: bool
    dependencies: tuple[SkillDependencyStatus, ...] = ()

    @property
    def ready(self) -> bool:
        """返回所有必需依赖都已满足的结果。

        Returns:
            没有失败依赖时返回真。
        """

        return all(item.ready for item in self.dependencies)

    @property
    def usable(self) -> bool:
        """返回既开启又可运行的结果。

        Returns:
            用户开启且依赖就绪时返回真。
        """

        return self.enabled and self.ready

    @property
    def unavailable_reason(self) -> str | None:
        """返回第一个依赖失败原因，供列表行直接展示。

        Returns:
            第一个失败原因；全部依赖正常时为空。
        """

        for dependency in self.dependencies:
            if not dependency.ready:
                return dependency.reason or "运行依赖不可用"
        return None

    def to_payload(self) -> dict[str, object]:
        """转换成 Skill 列表或详情接口使用的 JSON 对象。

        Returns:
            清单、用户开关和依赖状态组成的完整对象。
        """

        return {
            **self.manifest.to_payload(),
            "enabled": self.enabled,
            "ready": self.ready,
            "usable": self.usable,
            "unavailable_reason": self.unavailable_reason,
            "dependency_status": [item.to_payload() for item in self.dependencies],
        }


@dataclass(frozen=True, slots=True)
class SkillExecutionSnapshot:
    """冻结一次后台任务实际可以加载的 Skill 名单。

    Args:
        enabled_skill_ids: 已开启且依赖就绪的 Skill ID，顺序稳定且不重复。
        revision: 生成名单时清单和用户偏好共同计算的版本；纠正任务会原样继承它。
    """

    enabled_skill_ids: tuple[str, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        """校验快照不可被恶意构造成重复或不稳定的名单。

        Raises:
            ValueError: Skill ID 非法、重复或版本为负数。
        """

        normalized = tuple(item.strip() for item in self.enabled_skill_ids)
        if any(not item or not SKILL_ID_PATTERN.fullmatch(item) for item in normalized):
            raise ValueError("Skill 执行快照包含非法 Skill ID")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Skill 执行快照不能包含重复 Skill ID")
        if normalized != self.enabled_skill_ids:
            object.__setattr__(self, "enabled_skill_ids", normalized)
        if self.revision < 0:
            raise ValueError("Skill 执行快照版本不能为负数")

    @property
    def skill_ids(self) -> tuple[str, ...]:
        """返回 SDK 和工作区物化共用的 Skill ID 名单。

        Returns:
            任务准备时冻结的稳定 Skill ID 元组。
        """

        return self.enabled_skill_ids

    def to_payload(self) -> dict[str, object]:
        """转换成任务详情或诊断事件使用的安全载荷。

        Returns:
            Skill ID 列表和快照版本。
        """

        return {
            "enabled_skill_ids": list(self.enabled_skill_ids),
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class SkillRegistryIssue:
    """记录一个被隔离的坏 Skill，而不是阻断整个后台工作能力。

    Args:
        path: 坏 Skill 的产品目录；对外载荷只使用最后一级目录名。
        message: 已去除敏感路径的稳定校验原因。
    """

    path: Path
    message: str

    def to_payload(self) -> dict[str, object]:
        """返回不泄露仓库绝对路径的错误摘要。

        Returns:
            只包含目录名和校验原因的安全对象。
        """

        return {"name": self.path.name, "message": self.message}


@dataclass(frozen=True, slots=True)
class SkillRegistrySnapshot:
    """保存一次完整 Skill 列表和对应执行版本。

    Args:
        statuses: 按稳定 ID 排序的 Skill 状态。
        revision: 清单与用户偏好共同计算出的版本。
        issues: 本次扫描隔离的坏 Skill 问题。
    """

    statuses: tuple[SkillStatus, ...]
    revision: int
    issues: tuple[SkillRegistryIssue, ...] = ()

    def to_payload(self) -> dict[str, object]:
        """转换成 Skill 库列表接口的安全载荷。

        Returns:
            Skill 列表、版本、清单问题和汇总数字。
        """

        return {
            "skills": [item.to_payload() for item in self.statuses],
            "revision": self.revision,
            "issues": [item.to_payload() for item in self.issues],
            "summary": {
                "total": len(self.statuses),
                "enabled": sum(item.enabled for item in self.statuses),
                "ready": sum(item.ready for item in self.statuses),
                "usable": sum(item.usable for item in self.statuses),
            },
        }


def normalize_skill_preferences(value: Mapping[str, object] | None) -> dict[str, bool]:
    """只保留合法的 Skill ID 和真实布尔开关。

    Args:
        value: 尚未信任的偏好映射。

    Returns:
        可用于状态合并和版本计算的偏好字典。
    """

    if not isinstance(value, Mapping):
        return {}
    return {
        key.strip(): item
        for key, item in value.items()
        if isinstance(key, str)
        and SKILL_ID_PATTERN.fullmatch(key.strip())
        and type(item) is bool
    }
