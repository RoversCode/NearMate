"""持久化启动凭据，并负责把旧记录数据库迁移为纯密钥 schema。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from companion.observability.logging_config import get_backend_logger
from companion.records.corrupt_file_quarantine import quarantine_corrupt_file


logger = get_backend_logger(__name__)


SERVICE_SECRET_KEYS = (
    "llm_config_json",
    "qweather_api_key",
    "qweather_api_host",
    "background_agent_api_key",
    "background_agent_auth_token",
)
REQUIRED_SERVICE_SECRET_KEYS: tuple[str, ...] = ()
SCHEMA_VERSION = 2
RETIRED_RECORD_TABLES = (
    "conversation_messages",
    "event_logs",
    "permission_rows",
    "task_events",
    "task_rows",
)


class BootstrapSecretStore:
    """只保存启动凭据的 SQLite 仓储。"""

    def __init__(self, db_path: str | Path) -> None:
        """保存 SQLite 文件位置，不在构造阶段产生文件。

        Args:
            db_path: 启动凭据数据库路径。
        """

        self.db_path = Path(db_path)

    def initialize(self) -> None:
        """在单一事务内迁移到 V2 并确保密钥表存在；坏库先备份再重建空库。

        Raises:
            RuntimeError: 数据库由更高版本程序创建时抛出。
            sqlite3.DatabaseError: 文件可打开但迁移失败时抛出；只有确认
                文件本身损坏（无法作为 SQLite 打开）时才进入自愈重建。
        """

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._migrate_schema()
        except sqlite3.DatabaseError as exception:
            # 只有文件级损坏才允许重建；合法数据库迁移失败必须原样报错，
            # 否则一次 DDL 冲突就会把用户凭据静默清空。
            if getattr(exception, "sqlite_errorcode", None) not in {
                sqlite3.SQLITE_NOTADB,
                sqlite3.SQLITE_CORRUPT,
            }:
                raise
            # 坏文件靠重试永远无法自愈；改名留证后重建空库，
            # 缺失的凭据由启动初始化流程引导用户重新补齐。
            backup_path = quarantine_corrupt_file(self.db_path)
            logger.warning(
                "启动凭据数据库文件已损坏，已备份为 %s 并重建空库：%s",
                backup_path,
                exception,
            )
            self._migrate_schema()

    def _migrate_schema(self) -> None:
        """打开数据库，在单一事务内迁移到 V2 并确保密钥表存在。

        Raises:
            RuntimeError: 数据库 schema 版本高于当前程序支持版本时抛出。
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {current_version} 高于当前程序支持版本 {SCHEMA_VERSION}"
                )

            # V2 明确放弃跨重启会话、运行日志和旧任务记录，只保留启动凭据。
            if current_version < SCHEMA_VERSION:
                for table_name in RETIRED_RECORD_TABLES:
                    connection.execute(f"DROP TABLE IF EXISTS {table_name}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bootstrap_secrets (
                    config_key TEXT PRIMARY KEY,
                    config_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save_bootstrap_secrets(
        self,
        *,
        qweather_api_key: str | None = None,
        qweather_api_host: str | None = None,
    ) -> None:
        """保存首次初始化使用的服务凭据。

        Args:
            qweather_api_key: 可选和风天气密钥。
            qweather_api_host: 可选和风天气 API Host。
        """

        values = {}
        if qweather_api_key is not None:
            values["qweather_api_key"] = qweather_api_key.strip()
        if qweather_api_host is not None:
            values["qweather_api_host"] = qweather_api_host.strip()
        self._upsert_many(values)

    def save_llm_config_json(self, payload_json: str) -> None:
        """保存完整 LLM 配置 JSON。

        Args:
            payload_json: 已由配置模型验证的 JSON 文本。
        """

        self._upsert_many({"llm_config_json": payload_json})

    def delete_llm_config(self) -> None:
        """只删除已经失效的 LLM 配置。

        其他服务凭据属于独立配置域。旧版或损坏的 LLM JSON 不能连带清除
        天气或 Background Work 凭据。
        """

        with self._connect() as connection:
            connection.execute(
                "DELETE FROM bootstrap_secrets WHERE config_key = ?",
                ("llm_config_json",),
            )

    def save_qweather_api_key(self, *, api_key: str, api_host: str) -> None:
        """保存验证通过的和风天气凭据。

        Args:
            api_key: 和风天气 API Key。
            api_host: 和风天气 API Host。
        """

        self._upsert_many(
            {
                "qweather_api_key": api_key.strip(),
                "qweather_api_host": api_host.strip(),
            }
        )

    def save_background_agent_credential(
        self,
        *,
        credential_type: str,
        credential_value: str,
        clear_credential: bool = False,
    ) -> None:
        """保存后台智能体唯一生效的 Claude Code 凭据。

        Args:
            credential_type: `api_key` 或 `auth_token`。
            credential_value: 新凭据；空字符串表示保留同类型旧值。
            clear_credential: 为真时删除两种后台智能体凭据。

        Raises:
            ValueError: 凭据类型无效，或切换类型时没有提供新凭据。
        """

        key_by_type = {
            "api_key": "background_agent_api_key",
            "auth_token": "background_agent_auth_token",
        }
        selected_key = key_by_type.get(credential_type)
        if selected_key is None:
            raise ValueError("后台智能体凭据类型必须是 api_key 或 auth_token")
        current = self.background_agent_credentials()
        normalized_value = credential_value.strip()
        if not clear_credential and not normalized_value and not current.get(
            "ANTHROPIC_API_KEY" if credential_type == "api_key" else "ANTHROPIC_AUTH_TOKEN"
        ):
            raise ValueError("切换后台智能体凭据类型时必须填写新凭据")

        with self._connect() as connection:
            connection.execute(
                "DELETE FROM bootstrap_secrets WHERE config_key IN (?, ?)",
                ("background_agent_api_key", "background_agent_auth_token"),
            )
            if clear_credential:
                return
            value_to_store = normalized_value or (
                current["ANTHROPIC_API_KEY"]
                if credential_type == "api_key"
                else current["ANTHROPIC_AUTH_TOKEN"]
            )
            connection.execute(
                """
                INSERT INTO bootstrap_secrets (config_key, config_value, updated_at)
                VALUES (?, ?, ?)
                """,
                (selected_key, value_to_store, datetime.now(UTC).isoformat()),
            )

    def background_agent_credentials(self) -> dict[str, str | None]:
        """读取后台智能体的两种官方 Claude Code 凭据。

        Returns:
            官方环境变量名到已保存密钥的映射；未保存时值为空。
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT config_key, config_value
                FROM bootstrap_secrets
                WHERE config_key IN (?, ?)
                """,
                ("background_agent_api_key", "background_agent_auth_token"),
            ).fetchall()
        stored_values = {
            str(row["config_key"]): str(row["config_value"])
            for row in rows
        }
        return {
            "ANTHROPIC_API_KEY": stored_values.get("background_agent_api_key"),
            "ANTHROPIC_AUTH_TOKEN": stored_values.get("background_agent_auth_token"),
        }

    def get_bootstrap_secret(self, key: str) -> str | None:
        """读取白名单中的单个启动凭据。

        Args:
            key: 固定服务凭据键名。

        Returns:
            已保存的值；键名不受支持或尚未保存时返回空。
        """

        if key not in SERVICE_SECRET_KEYS:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT config_value FROM bootstrap_secrets WHERE config_key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["config_value"])

    def snapshot_service_secrets(self) -> dict[str, str | None]:
        """读取全部启动配置的精确持久化快照。

        Returns:
            固定服务键到当前值的映射；未持久化的键保留为空。
        """

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT config_key, config_value
                FROM bootstrap_secrets
                WHERE config_key IN ({','.join('?' for _ in SERVICE_SECRET_KEYS)})
                """,
                SERVICE_SECRET_KEYS,
            ).fetchall()
        stored_values = {
            str(row["config_key"]): str(row["config_value"])
            for row in rows
        }
        return {
            key: stored_values.get(key)
            for key in SERVICE_SECRET_KEYS
        }

    def restore_service_secrets(
        self,
        snapshot: dict[str, str | None],
        *,
        keys: tuple[str, ...] = SERVICE_SECRET_KEYS,
    ) -> None:
        """在单一事务中恢复启动配置的精确持久化快照。

        Args:
            snapshot: `snapshot_service_secrets` 产生的固定服务键快照。
            keys: 本次事务实际拥有、允许恢复的服务键；默认恢复完整快照。

        Raises:
            ValueError: 快照缺少固定服务键、包含未知键，或恢复范围非法。
        """

        if set(snapshot) != set(SERVICE_SECRET_KEYS):
            raise ValueError("bootstrap secret snapshot keys do not match service contract")
        keys_to_restore = tuple(dict.fromkeys(keys))
        if not keys_to_restore or not set(keys_to_restore).issubset(SERVICE_SECRET_KEYS):
            raise ValueError("bootstrap secret restore keys do not match service contract")

        updated_at = datetime.now(UTC).isoformat()
        values_to_restore = [
            (key, value, updated_at)
            for key, value in snapshot.items()
            if key in keys_to_restore and value is not None
        ]
        keys_to_delete = [
            key
            for key, value in snapshot.items()
            if key in keys_to_restore and value is None
        ]
        with self._connect() as connection:
            if keys_to_delete:
                connection.executemany(
                    "DELETE FROM bootstrap_secrets WHERE config_key = ?",
                    [(key,) for key in keys_to_delete],
                )
            if values_to_restore:
                connection.executemany(
                    """
                    INSERT INTO bootstrap_secrets (config_key, config_value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_value = excluded.config_value,
                        updated_at = excluded.updated_at
                    """,
                    values_to_restore,
                )

    def get_bootstrap_status(self) -> dict[str, object]:
        """根据必需凭据是否齐全推导初始化状态。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT config_key, config_value
                FROM bootstrap_secrets
                WHERE config_key IN (?, ?, ?, ?, ?, ?)
                """,
                SERVICE_SECRET_KEYS,
            ).fetchall()
        stored_values = {str(row["config_key"]): str(row["config_value"]) for row in rows}
        missing = [
            key
            for key in REQUIRED_SERVICE_SECRET_KEYS
            if not stored_values.get(key, "").strip()
        ]
        return {"initialized": not missing, "missing": missing}

    def _upsert_many(self, values: dict[str, str]) -> None:
        """在同一事务中更新一组固定凭据。

        Args:
            values: 凭据键值映射。
        """

        updated_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO bootstrap_secrets (config_key, config_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_value = excluded.config_value,
                    updated_at = excluded.updated_at
                """,
                [(key, value, updated_at) for key, value in values.items()],
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """创建具备显式提交、回滚和关闭语义的 SQLite 连接。"""

        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()
