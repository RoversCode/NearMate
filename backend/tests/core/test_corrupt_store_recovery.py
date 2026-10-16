"""验证损坏的本地存储文件会被备份留证，启动路径按空内容安全重建。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from companion.core.settings import Settings
from companion.core.user_settings_store import UserSettingsStore
from companion.records.bootstrap_secret_store import (
    SCHEMA_VERSION,
    BootstrapSecretStore,
)


def test_corrupt_bootstrap_database_is_backed_up_and_rebuilt(tmp_path: Path) -> None:
    """坏掉的 SQLite 文件必须改名留证，并重建出可正常读写的空库。"""

    db_path = tmp_path / "companion.sqlite3"
    db_path.write_bytes(b"this is not a sqlite database at all")

    store = BootstrapSecretStore(db_path)
    store.initialize()

    backups = list(tmp_path.glob("companion.sqlite3.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"this is not a sqlite database at all"

    # 重建后的空库必须立即可用：schema 版本已推进，凭据可以正常读写。
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store.save_qweather_api_key(api_key="weather-key", api_host="weather-host")
    assert store.get_bootstrap_secret("qweather_api_key") == "weather-key"


def test_corrupt_bootstrap_database_backup_never_overwrites_evidence(tmp_path: Path) -> None:
    """同一秒内重复隔离坏库时，每份备份都必须保留。"""

    db_path = tmp_path / "companion.sqlite3"
    db_path.write_bytes(b"first corrupt copy")
    BootstrapSecretStore(db_path).initialize()

    db_path.write_bytes(b"second corrupt copy")
    BootstrapSecretStore(db_path).initialize()

    backups = sorted(tmp_path.glob("companion.sqlite3.corrupt-*"))
    assert {backup.read_bytes() for backup in backups} == {
        b"first corrupt copy",
        b"second corrupt copy",
    }


def test_valid_database_with_failed_migration_is_not_destroyed(tmp_path: Path) -> None:
    """合法数据库的迁移冲突必须继续报错，绝不能被自愈逻辑清空。"""

    db_path = tmp_path / "valid-but-unmigratable.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE permission_rows (request_id TEXT PRIMARY KEY);
            CREATE VIEW task_rows AS SELECT 'task-dev' AS task_id;
            INSERT INTO permission_rows VALUES ('permission-dev');
            """
        )

    store = BootstrapSecretStore(db_path)
    try:
        store.initialize()
    except sqlite3.OperationalError:
        pass
    else:  # pragma: no cover - 迁移冲突静默成功即回归
        raise AssertionError("合法数据库的迁移冲突必须抛出 OperationalError")

    assert list(tmp_path.glob("valid-but-unmigratable.sqlite3.corrupt-*")) == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT request_id FROM permission_rows"
        ).fetchone()[0] == "permission-dev"


def test_corrupt_user_settings_file_is_backed_up_and_falls_back_to_defaults(
    tmp_path: Path,
) -> None:
    """非法 JSON 设置文件必须改名留证，load 按从未保存回落到默认设置。"""

    settings_path = tmp_path / "user-settings.json"
    settings_path.write_text("{这不是合法 JSON", encoding="utf-8")
    settings = Settings(user_settings_path=str(tmp_path / "fallback.json"))

    snapshot = UserSettingsStore(settings_path).load(settings)

    assert snapshot is None
    backups = list(tmp_path.glob("user-settings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{这不是合法 JSON"

    # 回落后再次保存与读取必须恢复正常往返。
    store = UserSettingsStore(settings_path)
    store.save(
        {
            "settings_revision": 0,
            **{
                field_name: getattr(settings, field_name)
                for field_name in (
                    "show_speech_bubbles",
                    "followup_toggle_shortcut_enabled",
                    "followup_toggle_shortcut",
                    "avatar_visibility_shortcut_enabled",
                    "avatar_visibility_shortcut",
                    "selection_capture_shortcut_enabled",
                    "selection_capture_shortcut",
                    "selected_live2d_model_id",
                    "user_self_intro",
                    "user_extra_notes",
                    "streamvox_device",
                    "streamvox_llama_backend",
                    "speaker_verification_enabled",
                    "speaker_whitelist_audio_paths",
                    "audio_input_device_id",
                    "audio_input_device_name",
                    "audio_output_reference_device_id",
                    "audio_output_reference_device_name",
                    "audio_device_mode",
                )
            },
            "background_agent": settings.background_agent.model_dump(mode="json"),
        }
    )
    restored = store.load(settings)
    assert restored is not None
    assert restored["settings_revision"] == 0


def test_user_settings_file_with_non_object_root_is_backed_up(tmp_path: Path) -> None:
    """根节点不是 JSON 对象的设置文件同样按损坏处理。"""

    settings_path = tmp_path / "user-settings.json"
    settings_path.write_text(json.dumps(["不是对象"]), encoding="utf-8")
    settings = Settings(user_settings_path=str(tmp_path / "fallback.json"))

    snapshot = UserSettingsStore(settings_path).load(settings)

    assert snapshot is None
    assert len(list(tmp_path.glob("user-settings.json.corrupt-*"))) == 1


def test_user_settings_file_with_invalid_utf8_is_backed_up(tmp_path: Path) -> None:
    """无法按 UTF-8 解码的设置文件必须改名留证并回落默认设置。"""

    settings_path = tmp_path / "user-settings.json"
    settings_path.write_bytes(b"\xff\xfe\x00")
    settings = Settings(user_settings_path=str(tmp_path / "fallback.json"))

    snapshot = UserSettingsStore(settings_path).load(settings)

    assert snapshot is None
    backups = list(tmp_path.glob("user-settings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"\xff\xfe\x00"


def test_user_settings_io_failure_preserves_original_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """普通读取 I/O 错误不能被误判为损坏并隔离合法设置文件。"""

    settings_path = tmp_path / "user-settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    settings = Settings(user_settings_path=str(tmp_path / "fallback.json"))
    original_read_text = Path.read_text

    def fail_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == settings_path:
            raise PermissionError("settings file is temporarily locked")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    with pytest.raises(PermissionError, match="temporarily locked"):
        UserSettingsStore(settings_path).load(settings)

    assert original_read_text(settings_path, encoding="utf-8") == "{}"
    assert list(tmp_path.glob("user-settings.json.corrupt-*")) == []
