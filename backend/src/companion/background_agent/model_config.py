"""装配 Claude Code 官方环境变量契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from companion.background_agent.settings import AgentEffortLevel, AgentPermissionMode
from companion.background_agent.skill_models import SkillExecutionSnapshot


_ENVIRONMENT_KEY_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")


@dataclass(frozen=True, slots=True)
class AgentModelConfig:
    """保存后台智能体启动时需要的模型环境。

    Args:
        environment: Console 按 Claude Code 官方契约保存的环境变量。
        config_dir: NearMate 私有 Claude 配置目录。
        model: 后台工作明确使用的主模型。
        effort_level: 主模型推理强度；为空时交给模型默认值。
    """

    environment: Mapping[str, str]
    config_dir: Path
    model: str
    effort_level: AgentEffortLevel | None = None

    def to_sdk_environment(self) -> dict[str, str]:
        """生成 SDK 环境变量，并强制覆盖产品私有配置目录。

        Returns:
            只包含允许透传的模型环境变量和产品强制隔离变量。
        """

        result: dict[str, str] = {}
        for key, value in self.environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            normalized_key = key.strip().upper()
            if not normalized_key.startswith(_ENVIRONMENT_KEY_PREFIXES):
                continue
            if value.strip():
                result[normalized_key] = value
        # WorkspaceManager 已验证这条词法路径。这里不能 resolve()，否则
        # prepare 后被替换的 Junction 会在最终环境变量中伪装成外部目标。
        result["CLAUDE_CONFIG_DIR"] = str(self.config_dir.absolute())
        return result


@dataclass(frozen=True, slots=True)
class AgentExecutionConfig:
    """冻结一项新任务实际使用的 Claude Code 配置。

    Args:
        model_config: 本任务要传给 SDK 的环境变量和私有配置目录。
        permission_mode: 由可信用户设置决定的权限模式。
        max_turns: 本任务最多允许的 Agent 轮数；0 表示不限制。
        skill_snapshot: 任务准备时冻结的产品 Skill 名单。
    """

    model_config: AgentModelConfig
    permission_mode: AgentPermissionMode
    max_turns: int
    skill_snapshot: SkillExecutionSnapshot = field(default_factory=SkillExecutionSnapshot)
