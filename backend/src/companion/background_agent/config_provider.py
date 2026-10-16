"""从现有用户设置文件和启动密钥库读取新任务配置。"""

from __future__ import annotations

import json
from pathlib import Path

from companion.background_agent.config_commit_lock import (
    BackgroundAgentConfigCommitLock,
)
from companion.background_agent.model_config import (
    AgentExecutionConfig,
    AgentModelConfig,
)
from companion.background_agent.settings import (
    BackgroundAgentSettings,
    load_background_agent_settings_payload,
)
from companion.background_agent.skill_models import SkillExecutionSnapshot
from companion.background_agent.skill_registry import SkillRegistry
from companion.records.bootstrap_secret_store import BootstrapSecretStore


class BackgroundAgentNotConfiguredError(RuntimeError):
    """表示新任务开始时 Claude Code 端点或凭据尚未配齐。

    Args:
        message: 面向调用方的配置缺失原因。
    """


class BackgroundAgentConfigProvider:
    """在每次新任务准备时读取用户最近保存的智能体配置。

    Args:
        user_settings_path: NearMate 现有 `user-settings.json` 路径。
        secret_store: NearMate 现有 SQLite 启动密钥仓储。
        config_dir: 产品私有 `CLAUDE_CONFIG_DIR`。
        skill_registry: NearMate 产品 Skill 的唯一来源；为空时任务不加载 Skill。
        defaults: 用户尚未保存配置时使用的非密钥默认值。
        config_commit_lock: 与主进程配置写事务共用的跨进程锁。
    """

    def __init__(
        self,
        *,
        user_settings_path: str | Path,
        secret_store: BootstrapSecretStore,
        config_dir: str | Path,
        skill_registry: SkillRegistry | None = None,
        defaults: BackgroundAgentSettings | None = None,
        config_commit_lock: BackgroundAgentConfigCommitLock | None = None,
    ) -> None:
        """初始化新任务配置读取器。

        Args:
            user_settings_path: `user-settings.json` 文件路径。
            secret_store: 保存 Claude Code 凭据的 SQLite 仓储。
            config_dir: 产品私有 `CLAUDE_CONFIG_DIR`。
            skill_registry: 只扫描 NearMate 产品资产的 Skill Registry。
            defaults: 用户尚未保存配置时使用的默认值。
            config_commit_lock: 与主进程配置写事务共用的跨进程锁。
        """

        self._user_settings_path = Path(user_settings_path)
        self._secret_store = secret_store
        self._config_dir = Path(config_dir)
        self._skill_registry = skill_registry
        self._defaults = defaults or BackgroundAgentSettings()
        self._config_commit_lock = config_commit_lock or BackgroundAgentConfigCommitLock(
            self._user_settings_path
        )

    def snapshot(
        self,
        *,
        inherited_skill_snapshot: SkillExecutionSnapshot | None = None,
    ) -> AgentExecutionConfig:
        """冻结下一项任务使用的完整配置。

        Args:
            inherited_skill_snapshot: 恢复父会话时必须沿用的任务 Skill 快照；
                传入后不会重新检查当前全局 Skill 依赖。

        Returns:
            已合并非密钥设置、SQLite 凭据、产品私有目录和 Skill 快照的任务配置。

        Raises:
            BackgroundAgentNotConfiguredError: 模型、端点或凭据尚未配置完整。
        """

        # 非密钥设置和凭据分属 JSON 与 SQLite。连续读取期间必须持有写侧同一把
        # 跨进程锁，否则新任务可能冻结尚未越过持久化提交点的半套配置。
        with self._config_commit_lock.hold():
            settings = self._load_user_settings()
            credentials = self._secret_store.background_agent_credentials()
            if not settings.is_runtime_configured() or not any(credentials.values()):
                raise BackgroundAgentNotConfiguredError("后台工作配置尚未完成")
            environment = dict(settings.environment)
            for key, value in credentials.items():
                if value:
                    environment[key] = value

        # 外部依赖预检不属于设置事务。先复制好用户偏好再释放文件锁，避免未来
        # 某项带版本探测的 Skill 在两秒超时窗口内阻塞 Console 保存设置。
        if inherited_skill_snapshot is not None:
            skill_snapshot = inherited_skill_snapshot
        elif self._skill_registry is not None:
            skill_snapshot = self._skill_registry.execution_snapshot(settings.skills)
        else:
            skill_snapshot = SkillExecutionSnapshot()
        return AgentExecutionConfig(
            model_config=AgentModelConfig(
                environment=environment,
                config_dir=self._config_dir,
                model=settings.model,
                effort_level=settings.effort_level,
            ),
            permission_mode=settings.permission_mode,
            max_turns=settings.max_turns,
            skill_snapshot=skill_snapshot,
        )

    def _load_user_settings(self) -> BackgroundAgentSettings:
        """读取用户设置文件中的 `background_agent` 段。

        Returns:
            文件不存在、损坏或旧版本没有该段时返回进程启动时冻结的默认值。
        """

        if not self._user_settings_path.exists():
            return self._defaults.model_copy(deep=True)
        try:
            payload = json.loads(self._user_settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._defaults.model_copy(deep=True)
        if not isinstance(payload, dict):
            return self._defaults.model_copy(deep=True)
        return load_background_agent_settings_payload(
            payload.get("background_agent"),
            self._defaults,
        )
