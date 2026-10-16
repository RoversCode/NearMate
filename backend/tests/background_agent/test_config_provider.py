from __future__ import annotations

import json
from pathlib import Path

import pytest

from companion.background_agent.config_provider import (
    BackgroundAgentConfigProvider,
    BackgroundAgentNotConfiguredError,
)
from companion.background_agent.settings import BackgroundAgentSettings
from companion.background_agent.skill_models import SkillExecutionSnapshot
from companion.background_agent.skill_registry import SkillRegistry
from companion.records.bootstrap_secret_store import BootstrapSecretStore


def test_config_provider_reads_latest_user_settings_for_each_new_task(
    tmp_path: Path,
) -> None:
    """保存后的非密钥设置只影响之后冻结的新任务。"""

    user_settings_path = tmp_path / "user-settings.json"
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="secret-value",
    )
    provider = BackgroundAgentConfigProvider(
        user_settings_path=user_settings_path,
        secret_store=secret_store,
        config_dir=tmp_path / "runtime",
    )
    _write_settings(
        user_settings_path,
        base_url="https://first.example.com",
        permission_mode="acceptEdits",
    )

    first = provider.snapshot()
    _write_settings(
        user_settings_path,
        base_url="https://second.example.com",
        permission_mode="bypassPermissions",
    )
    second = provider.snapshot()

    assert first.model_config.environment["ANTHROPIC_BASE_URL"] == "https://first.example.com"
    assert first.model_config.model == "claude-sonnet-4-5"
    assert first.permission_mode == "acceptEdits"
    assert second.model_config.environment["ANTHROPIC_BASE_URL"] == "https://second.example.com"
    assert second.permission_mode == "bypassPermissions"
    assert second.model_config.environment["ANTHROPIC_API_KEY"] == "secret-value"


def test_config_provider_rejects_task_until_endpoint_and_credential_exist(
    tmp_path: Path,
) -> None:
    """端点或凭据缺失时，任务快照必须在 prepare 前明确失败。"""

    user_settings_path = tmp_path / "user-settings.json"
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    provider = BackgroundAgentConfigProvider(
        user_settings_path=user_settings_path,
        secret_store=secret_store,
        config_dir=tmp_path / "runtime",
        defaults=BackgroundAgentSettings(),
    )

    with pytest.raises(BackgroundAgentNotConfiguredError):
        provider.snapshot()


def test_background_agent_settings_strip_secrets_and_product_fixed_environment() -> None:
    """用户 JSON 不能绕过 SQLite 密钥边界或覆盖产品私有目录。"""

    settings = BackgroundAgentSettings(
        model="claude-sonnet-4-5",
        effort_level="high",
        environment={
            "anthropic_base_url": " https://endpoint.example.com ",
            "ANTHROPIC_API_KEY": "must-not-persist",
            "ANTHROPIC_AUTH_TOKEN": "must-not-persist",
            "CLAUDE_CONFIG_DIR": "C:/unsafe",
            "CLAUDE_CODE_EFFORT_LEVEL": "high",
            "ANTHROPIC_MODEL": "must-not-stay-in-environment",
            "CLAUDE_CODE_SUBAGENT_MODEL": "must-not-stay-in-environment",
            "UNRELATED": "ignored",
        }
    )

    assert settings.environment == {
        "ANTHROPIC_BASE_URL": "https://endpoint.example.com",
    }
    assert settings.model == "claude-sonnet-4-5"
    assert settings.effort_level == "high"


def test_config_provider_migrates_legacy_model_and_effort_settings(
    tmp_path: Path,
) -> None:
    """旧环境变量只迁移成本地数据，不继续进入 SDK 环境。"""

    user_settings_path = tmp_path / "user-settings.json"
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="auth_token",
        credential_value="legacy-secret",
    )
    user_settings_path.write_text(
        json.dumps(
            {
                "background_agent": {
                    "environment": {
                        "ANTHROPIC_BASE_URL": "https://legacy.example.com",
                        "ANTHROPIC_MODEL": "legacy-model",
                        "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
                        "CLAUDE_CODE_SUBAGENT_MODEL": "unused-model",
                    },
                    "max_turns": 0,
                    "max_budget_usd": 12.5,
                    "permission_mode": "acceptEdits",
                }
            }
        ),
        encoding="utf-8",
    )
    provider = BackgroundAgentConfigProvider(
        user_settings_path=user_settings_path,
        secret_store=secret_store,
        config_dir=tmp_path / "runtime",
    )

    snapshot = provider.snapshot()

    assert snapshot.model_config.model == "legacy-model"
    assert snapshot.model_config.effort_level == "xhigh"
    assert snapshot.max_turns == 0
    assert snapshot.model_config.environment == {
        "ANTHROPIC_BASE_URL": "https://legacy.example.com",
        "ANTHROPIC_AUTH_TOKEN": "legacy-secret",
    }


def test_config_provider_recovers_valid_fields_when_legacy_effort_is_invalid(
    tmp_path: Path,
) -> None:
    """子进程读取配置时也只能回退非法字段，不能丢弃整段配置。"""

    user_settings_path = tmp_path / "user-settings.json"
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="secret-value",
    )
    user_settings_path.write_text(
        json.dumps(
            {
                "background_agent": {
                    "environment": {
                        "ANTHROPIC_BASE_URL": "https://legacy.example.com",
                        "anthropic_model": "legacy-model",
                        "claude_code_effort_level": "unsupported",
                    },
                    "max_turns": 30,
                    "permission_mode": "acceptEdits",
                }
            }
        ),
        encoding="utf-8",
    )
    provider = BackgroundAgentConfigProvider(
        user_settings_path=user_settings_path,
        secret_store=secret_store,
        config_dir=tmp_path / "runtime",
    )

    snapshot = provider.snapshot()

    assert snapshot.model_config.model == "legacy-model"
    assert snapshot.model_config.effort_level is None
    assert snapshot.model_config.environment["ANTHROPIC_BASE_URL"] == (
        "https://legacy.example.com"
    )


def test_config_provider_freezes_latest_ready_skill_preferences(
    tmp_path: Path,
) -> None:
    """每项新任务读取最新 Skill 偏好，已关闭的 Skill 不进入执行快照。"""

    asset_root = tmp_path / "assets"
    skill_root = asset_root / ".claude" / "skills" / "document-read"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: document-read\ndescription: 测试 Skill。\n---\n\n说明",
        encoding="utf-8",
    )
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "document-read",
                "name": "文档读取",
                "summary": "读取文档",
                "category": "文档",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": [],
            }
        ),
        encoding="utf-8",
    )
    user_settings_path = tmp_path / "user-settings.json"
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="secret-value",
    )
    provider = BackgroundAgentConfigProvider(
        user_settings_path=user_settings_path,
        secret_store=secret_store,
        config_dir=tmp_path / "runtime",
        skill_registry=SkillRegistry(asset_root=asset_root),
    )
    _write_settings(
        user_settings_path,
        base_url="https://example.com",
        permission_mode="acceptEdits",
        skills={"document-read": False},
    )

    first = provider.snapshot()
    _write_settings(
        user_settings_path,
        base_url="https://example.com",
        permission_mode="acceptEdits",
        skills={"document-read": True},
    )
    second = provider.snapshot()

    assert first.skill_snapshot.enabled_skill_ids == ()
    assert second.skill_snapshot.enabled_skill_ids == ("document-read",)
    assert first.skill_snapshot.revision != second.skill_snapshot.revision


def test_config_provider_reuses_parent_skill_snapshot_without_dependency_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """恢复父会话时直接继承父 Skill 快照，不能重新扫描当前全局依赖。"""

    asset_root = tmp_path / "assets"
    skill_root = asset_root / ".claude" / "skills" / "document-read"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: document-read\ndescription: 测试 Skill。\n---\n\n说明",
        encoding="utf-8",
    )
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "document-read",
                "name": "文档读取",
                "summary": "读取文档",
                "category": "文档",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": [
                    {"kind": "executable", "name": "document-tool"}
                ],
            }
        ),
        encoding="utf-8",
    )
    user_settings_path = tmp_path / "user-settings.json"
    secret_store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    secret_store.initialize()
    secret_store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="secret-value",
    )
    provider = BackgroundAgentConfigProvider(
        user_settings_path=user_settings_path,
        secret_store=secret_store,
        config_dir=tmp_path / "runtime",
        skill_registry=SkillRegistry(asset_root=asset_root),
    )
    _write_settings(
        user_settings_path,
        base_url="https://example.com",
        permission_mode="acceptEdits",
        skills={"document-read": True},
    )
    inherited_snapshot = SkillExecutionSnapshot(
        enabled_skill_ids=("document-read",),
        revision=9,
    )

    def fail_if_dependency_is_checked(_name: str) -> None:
        """在纠正任务错误地重查依赖时立即让测试失败。

        Args:
            _name: 错误探测的外部命令名称。

        Raises:
            AssertionError: 本路径不允许执行任何依赖探测。
        """

        raise AssertionError("纠正任务不应重新检查全局 Skill 依赖")

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        fail_if_dependency_is_checked,
    )

    execution_config = provider.snapshot(
        inherited_skill_snapshot=inherited_snapshot,
    )

    assert execution_config.skill_snapshot is inherited_snapshot


def _write_settings(
    path: Path,
    *,
    base_url: str,
    permission_mode: str,
    skills: dict[str, bool] | None = None,
) -> None:
    """写入测试需要的用户设置文件。"""

    path.write_text(
        json.dumps(
            {
                "background_agent": {
                    "environment": {"ANTHROPIC_BASE_URL": base_url},
                    "model": "claude-sonnet-4-5",
                    "effort_level": "high",
                    "max_turns": 30,
                    "permission_mode": permission_mode,
                    "skills": skills or {},
                }
            }
        ),
        encoding="utf-8",
    )
