from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from companion.records.bootstrap_secret_store import BootstrapSecretStore, SCHEMA_VERSION


LEGACY_TABLES = {"task_rows", "task_events", "permission_rows"}


def test_initialize_drops_all_runtime_records_and_preserves_secrets(tmp_path: Path) -> None:
    """V1 升级必须删除历史记录和旧任务表，同时完整保留启动凭据。"""

    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE task_rows (task_id TEXT PRIMARY KEY);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY);
            CREATE TABLE permission_rows (request_id TEXT PRIMARY KEY);
            CREATE TABLE conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE event_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE bootstrap_secrets (
                config_key TEXT PRIMARY KEY,
                config_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO task_rows VALUES ('task-dev');
            INSERT INTO task_events VALUES (1);
            INSERT INTO permission_rows VALUES ('permission-dev');
            INSERT INTO conversation_messages (role, text, created_at)
            VALUES ('user', '保留会话', '2026-07-13T00:00:00+00:00');
            INSERT INTO event_logs (event_type, payload_json, recorded_at)
            VALUES ('kept.event', '{}', '2026-07-13T00:00:00+00:00');
            INSERT INTO bootstrap_secrets (config_key, config_value, updated_at)
            VALUES ('streamvox_key', 'secret', '2026-07-13T00:00:00+00:00');
            PRAGMA user_version = 1;
            """
        )

    store = BootstrapSecretStore(db_path)
    store.initialize()
    store.initialize()

    with sqlite3.connect(db_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert LEGACY_TABLES.isdisjoint(table_names)
        assert "conversation_messages" not in table_names
        assert "event_logs" not in table_names
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT config_value FROM bootstrap_secrets").fetchone()[0] == "secret"


def test_failed_migration_rolls_back_and_stops_initialization(tmp_path: Path) -> None:
    """验证迁移失败会回滚已执行 DDL，并把异常传播到启动边界。"""

    db_path = tmp_path / "broken.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE permission_rows (request_id TEXT PRIMARY KEY);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY);
            CREATE VIEW task_rows AS SELECT 'task-dev' AS task_id;
            INSERT INTO permission_rows VALUES ('permission-dev');
            INSERT INTO task_events VALUES (1);
            """
        )

    with pytest.raises(sqlite3.OperationalError):
        BootstrapSecretStore(db_path).initialize()

    with sqlite3.connect(db_path) as connection:
        objects = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT name, type FROM sqlite_master WHERE name IN ('permission_rows', 'task_events', 'task_rows')"
            )
        }
        assert objects == {
            ("permission_rows", "table"),
            ("task_events", "table"),
            ("task_rows", "view"),
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_future_schema_version_is_rejected_without_mutation(tmp_path: Path) -> None:
    """验证旧程序不会修改由未来版本创建的数据库。"""

    db_path = tmp_path / "future.sqlite3"
    future_version = SCHEMA_VERSION + 1
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE future_records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_records VALUES ('keep-me')")
        connection.execute(f"PRAGMA user_version = {future_version}")

    with pytest.raises(RuntimeError, match="高于当前程序支持版本"):
        BootstrapSecretStore(db_path).initialize()

    with sqlite3.connect(db_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == future_version
        assert connection.execute("SELECT value FROM future_records").fetchone()[0] == "keep-me"
        assert "conversation_messages" not in table_names
        assert "event_logs" not in table_names
        assert "bootstrap_secrets" not in table_names


def test_service_secret_snapshot_restores_values_and_missing_keys(tmp_path: Path) -> None:
    """配置事务回滚必须同时恢复旧值，并删除请求前不存在的新密钥。"""

    store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    store.initialize()
    store.save_llm_config_json("old-llm")
    store.save_qweather_api_key(api_key="old-weather", api_host="old-host")
    snapshot = store.snapshot_service_secrets()

    store.save_bootstrap_secrets(
        qweather_api_key="new-weather",
        qweather_api_host="new-host",
    )
    store.save_llm_config_json("new-llm")
    store.restore_service_secrets(snapshot)

    assert store.snapshot_service_secrets() == {
        "llm_config_json": "old-llm",
        "qweather_api_key": "old-weather",
        "qweather_api_host": "old-host",
        "background_agent_api_key": None,
        "background_agent_auth_token": None,
    }


def test_background_agent_credential_keeps_only_selected_official_variable(
    tmp_path: Path,
) -> None:
    """后台智能体两种官方凭据只能有一种进入新任务环境。"""

    store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    store.initialize()

    store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="api-secret",
    )
    assert store.background_agent_credentials() == {
        "ANTHROPIC_API_KEY": "api-secret",
        "ANTHROPIC_AUTH_TOKEN": None,
    }

    store.save_background_agent_credential(
        credential_type="auth_token",
        credential_value="token-secret",
    )
    assert store.background_agent_credentials() == {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": "token-secret",
    }


def test_scoped_secret_restore_does_not_overwrite_other_service_updates(
    tmp_path: Path,
) -> None:
    """配置回滚只能恢复本事务拥有的密钥，不能覆盖其它服务的新提交。"""

    store = BootstrapSecretStore(tmp_path / "bootstrap.sqlite3")
    store.initialize()
    store.save_llm_config_json("old-llm")
    store.save_background_agent_credential(
        credential_type="api_key",
        credential_value="old-agent",
    )
    snapshot = store.snapshot_service_secrets()

    store.save_llm_config_json("new-llm")
    store.save_background_agent_credential(
        credential_type="auth_token",
        credential_value="new-agent",
    )
    store.restore_service_secrets(
        snapshot,
        keys=("background_agent_api_key", "background_agent_auth_token"),
    )

    restored = store.snapshot_service_secrets()
    assert restored["llm_config_json"] == "new-llm"
    assert restored["background_agent_api_key"] == "old-agent"
    assert restored["background_agent_auth_token"] is None
