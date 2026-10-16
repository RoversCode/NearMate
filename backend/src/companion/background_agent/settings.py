"""定义后台智能体可由用户保存的非密钥设置。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from companion.background_agent.skill_models import SKILL_ID_PATTERN


AgentPermissionMode = Literal["acceptEdits", "bypassPermissions"]
AgentEffortLevel = Literal["low", "medium", "high", "xhigh", "max"]

SECRET_ENVIRONMENT_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
}
FIXED_ENVIRONMENT_KEYS = {"CLAUDE_CONFIG_DIR"}
RESERVED_ENVIRONMENT_KEYS = {
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
}
ALLOWED_ENVIRONMENT_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
_RETIRED_CONTROL_SKILL_IDS = frozenset({"browser-use", "computer-use"})
_OFFICIAL_CUA_SKILL_ID = "cua-driver"


class BackgroundAgentSettings(BaseModel):
    """保存 Console 可编辑、可以明文写入用户设置文件的智能体配置。

    Args:
        environment: Claude Code 官方非密钥环境变量；键名保持官方大写名称。
        model: 后台工作明确使用的主模型。
        effort_level: Claude Code 主模型使用的推理强度；为空时使用模型默认值。
        max_turns: 单任务最多执行的 Agent 轮数。
        permission_mode: 普通审批模式或用户显式开启的全权限模式。
        skills: 用户对产品 Skill 保存的启停偏好；未出现的 Skill 使用清单默认值。
    """

    model_config = ConfigDict(extra="forbid")

    environment: dict[str, str] = Field(default_factory=dict)
    model: str = ""
    effort_level: AgentEffortLevel | None = None
    max_turns: StrictInt = Field(default=30, ge=0, le=200)
    permission_mode: AgentPermissionMode = "acceptEdits"
    skills: dict[str, bool] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        """裁剪后台工作主模型名称。

        Args:
            value: Console 或本地设置文件提供的模型名。

        Returns:
            去掉首尾空白后的模型名；空字符串表示尚未配置。
        """

        return normalize_background_agent_model(value)

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, str]:
        """只保留 Claude Code 官方前缀下的非密钥字符串变量。

        Args:
            value: 用户设置文件或 HTTP 请求中的原始环境变量对象。

        Returns:
            已统一大写、裁剪值并移除密钥与产品固定变量的字典。
        """

        return normalize_background_agent_environment(value)

    @field_validator("skills", mode="before")
    @classmethod
    def normalize_skills(cls, value: Any) -> dict[str, bool]:
        """只保留合法 Skill ID 和真实布尔开关。

        Args:
            value: 用户设置文件或 Console 提交的原始 Skill 偏好。

        Returns:
            已裁剪 ID 且只含布尔值的 Skill 开关字典。

        Raises:
            ValueError: 原始值不是对象。
        """

        return normalize_background_agent_skill_preferences(value)

    def is_runtime_configured(self) -> bool:
        """判断用户是否已经填写后台工作端点和主模型。

        Returns:
            Base URL 与主模型都非空时返回真。
        """

        return bool(
            self.environment.get("ANTHROPIC_BASE_URL", "").strip()
            and self.model
        )


def migrate_background_agent_settings_payload(value: Any) -> Any:
    """把旧版后台工作本地设置转换成当前结构。

    这项迁移只服务本机持久化数据，不让旧 HTTP 字段继续成为公开接口。旧主模型、
    推理强度和两项自研控制 Skill 会转换成当前字段，退役数据随后删除。

    Args:
        value: `user-settings.json` 中的旧版或当前版后台工作设置。

    Returns:
        可交给 `BackgroundAgentSettings` 校验的当前版字典；非对象原样返回。
    """

    if not isinstance(value, dict):
        return value
    migrated = dict(value)
    raw_skills = migrated.get("skills")
    if isinstance(raw_skills, dict):
        migrated_skills = dict(raw_skills)
        legacy_items = [
            (raw_skill_id, enabled)
            for raw_skill_id, enabled in raw_skills.items()
            if isinstance(raw_skill_id, str)
            and raw_skill_id.strip() in _RETIRED_CONTROL_SKILL_IDS
        ]
        has_current_cua_preference = any(
            isinstance(raw_skill_id, str)
            and raw_skill_id.strip() == _OFFICIAL_CUA_SKILL_ID
            for raw_skill_id in raw_skills
        )
        if legacy_items and not has_current_cua_preference:
            legacy_preferences = {
                raw_skill_id.strip(): enabled
                for raw_skill_id, enabled in legacy_items
            }
            # 新 Skill 同时开放桌面和网页。只有用户过去明确开启两项能力时才能
            # 自动开启；旧选择不一致或缺项都回到关闭，避免迁移扩大授权范围。
            migrated_skills[_OFFICIAL_CUA_SKILL_ID] = all(
                legacy_preferences.get(retired_skill_id) is True
                for retired_skill_id in _RETIRED_CONTROL_SKILL_IDS
            )
        for raw_skill_id, _enabled in legacy_items:
            migrated_skills.pop(raw_skill_id, None)
        migrated["skills"] = migrated_skills
    raw_environment = migrated.get("environment", {})
    # 非对象 environment 不是“没有配置”，而是损坏数据。必须原样保留给严格模型
    # 拒绝整次迁移，不能在回写时静默变成空对象并清除用户端点。
    if not isinstance(raw_environment, dict):
        migrated["environment"] = raw_environment
        migrated.pop("max_budget_usd", None)
        return migrated
    environment = _normalize_environment_entries(raw_environment)
    if "model" not in migrated:
        migrated["model"] = environment.get("ANTHROPIC_MODEL", "")
    if "effort_level" not in migrated:
        effort = environment.get("CLAUDE_CODE_EFFORT_LEVEL")
        migrated["effort_level"] = (
            effort if isinstance(effort, str) and effort.strip() else None
        )
    migrated["environment"] = {
        key: environment_value
        for key, environment_value in environment.items()
        if key not in SECRET_ENVIRONMENT_KEYS
        and key not in FIXED_ENVIRONMENT_KEYS
        and key not in RESERVED_ENVIRONMENT_KEYS
    }
    migrated.pop("max_budget_usd", None)
    return migrated


def background_agent_settings_payload_needs_migration(value: Any) -> bool:
    """判断本地后台工作设置是否还包含已经退役的字段。

    Args:
        value: ``user-settings.json`` 中尚未校验的后台工作设置。

    Returns:
        存在旧预算、旧模型环境变量或旧控制 Skill ID 时返回真，否则返回假。
    """

    if not isinstance(value, dict):
        return False
    if "max_budget_usd" in value:
        return True
    raw_skills = value.get("skills")
    if isinstance(raw_skills, dict) and any(
        isinstance(raw_skill_id, str)
        and raw_skill_id.strip() in _RETIRED_CONTROL_SKILL_IDS
        for raw_skill_id in raw_skills
    ):
        return True
    raw_environment = value.get("environment")
    if not isinstance(raw_environment, dict):
        return False
    normalized_keys = {
        raw_key.strip().upper()
        for raw_key in raw_environment
        if isinstance(raw_key, str)
    }
    return bool(normalized_keys & RESERVED_ENVIRONMENT_KEYS)


def load_background_agent_settings_payload(
    value: Any,
    fallback: BackgroundAgentSettings,
) -> BackgroundAgentSettings:
    """从本地文件按字段恢复后台工作设置。

    本地文件可能来自旧版本或被手工编辑。这里保留每个仍然合法的字段，只让非法字段
    回到启动默认值，避免一个拼错的推理强度清空有效端点和模型。

    Args:
        value: `user-settings.json` 中尚未信任的后台工作设置。
        fallback: 当前进程启动时冻结的默认设置。

    Returns:
        一定能通过当前领域模型校验的后台工作设置。
    """

    migrated = migrate_background_agent_settings_payload(value)
    if not isinstance(migrated, dict):
        return fallback.model_copy(deep=True)

    raw_persisted_environment = value.get("environment")
    raw_environment = migrated.get("environment")
    environment = (
        normalize_background_agent_environment(raw_environment)
        if isinstance(raw_persisted_environment, dict)
        else dict(fallback.environment)
    )

    raw_model = migrated.get("model")
    model = (
        normalize_background_agent_model(raw_model)
        if isinstance(raw_model, str)
        else fallback.model
    )

    raw_effort = migrated.get("effort_level", fallback.effort_level)
    effort_level = (
        raw_effort.strip()
        if isinstance(raw_effort, str)
        and raw_effort.strip() in {"low", "medium", "high", "xhigh", "max"}
        else None if raw_effort is None else fallback.effort_level
    )

    raw_max_turns = migrated.get("max_turns", fallback.max_turns)
    max_turns = (
        raw_max_turns
        if type(raw_max_turns) is int and 0 <= raw_max_turns <= 200
        else fallback.max_turns
    )

    raw_permission_mode = migrated.get("permission_mode", fallback.permission_mode)
    permission_mode = (
        raw_permission_mode
        if raw_permission_mode in {"acceptEdits", "bypassPermissions"}
        else fallback.permission_mode
    )
    raw_skills = migrated.get("skills", fallback.skills)
    skills = (
        normalize_background_agent_skill_preferences(raw_skills)
        if isinstance(raw_skills, dict)
        else dict(fallback.skills)
    )
    return BackgroundAgentSettings(
        environment=environment,
        model=model,
        effort_level=effort_level,
        max_turns=max_turns,
        permission_mode=permission_mode,
        skills=skills,
    )


def normalize_background_agent_model(value: str) -> str:
    """裁剪后台工作模型名称。

    Args:
        value: 用户输入或本地文件中的模型名称。

    Returns:
        去掉首尾空白后的模型名称。
    """

    return value.strip()


def normalize_background_agent_environment(value: Any) -> dict[str, str]:
    """归一化可公开保存的 Claude Code 环境变量。

    Args:
        value: Console 或本地文件提供的环境变量对象。

    Returns:
        只包含官方前缀、非密钥且非一等配置字段的环境变量。
    """

    if not isinstance(value, dict):
        raise ValueError("后台工作环境变量必须是对象")
    return {
        key: environment_value
        for key, environment_value in _normalize_environment_entries(value).items()
        if key not in SECRET_ENVIRONMENT_KEYS
        and key not in FIXED_ENVIRONMENT_KEYS
        and key not in RESERVED_ENVIRONMENT_KEYS
    }


def normalize_background_agent_skill_preferences(value: Any) -> dict[str, bool]:
    """归一化产品 Skill 启停偏好。

    Args:
        value: 本地设置或 Console 提交的原始字典。

    Returns:
        只包含安全 Skill ID 和真实布尔值的偏好字典。

    Raises:
        ValueError: 公开提交值不是对象时抛出。
    """

    if not isinstance(value, dict):
        raise ValueError("后台 Skill 偏好必须是对象")
    normalized: dict[str, bool] = {}
    for raw_skill_id, enabled in value.items():
        if not isinstance(raw_skill_id, str) or type(enabled) is not bool:
            continue
        skill_id = raw_skill_id.strip()
        if not SKILL_ID_PATTERN.fullmatch(skill_id):
            continue
        normalized[skill_id] = enabled
    return normalized


def _normalize_environment_entries(value: dict[object, object]) -> dict[str, str]:
    """先统一环境变量键名，供迁移提取和公开过滤共用。

    Args:
        value: 尚未信任的环境变量对象。

    Returns:
        键名已裁剪并转成大写、值已裁剪的官方前缀变量。
    """

    normalized: dict[str, str] = {}
    canonical_sources: dict[str, bool] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        key = raw_key.strip().upper()
        value_text = raw_value.strip()
        if not key.startswith(ALLOWED_ENVIRONMENT_PREFIXES) or not value_text:
            continue

        # 同一个键可能被手工写出多个大小写版本。规范的大写键优先，避免字典顺序
        # 决定最终模型；没有规范键时保留最先出现的有效值。
        is_canonical_source = raw_key == key
        if key in normalized and (
            canonical_sources[key] or not is_canonical_source
        ):
            continue
        normalized[key] = value_text
        canonical_sources[key] = is_canonical_source
    return normalized
