"""在独立 SQLite 中保存并读取去内容化的后台工作用量事实。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Iterator

from companion.background_agent.usage_models import (
    AgentModelTokenUsage,
    AgentTaskUsageSnapshot,
)


SCHEMA_VERSION = 3
SQLITE_LOCK_TIMEOUT_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class StoredUsageTask:
    """表示 Repository 从同一个查询快照还原的一项任务。

    Args:
        activity_id: 用于幂等去重的 Activity 编号。
        terminal_at_ms: 任务终态发生的 Unix 毫秒时间。
        terminal_local_date: 写入时固化的 Backend 本地日期。
        is_complete: 是否收到 SDK 权威终态汇总。
        reference_cost_usd: SDK 提供的可选参考成本。
        by_model: 这项任务按模型拆分的四类 Token。
    """

    activity_id: str
    terminal_at_ms: int
    terminal_local_date: date
    is_complete: bool
    reference_cost_usd: float | None
    by_model: tuple[AgentModelTokenUsage, ...]


@dataclass(slots=True)
class _TaskRowBuilder:
    """在单次 JOIN 结果中暂存一项任务及其模型行。"""

    activity_id: str
    terminal_at_ms: int
    terminal_local_date: date
    is_complete: bool
    reference_cost_usd: float | None
    by_model: list[AgentModelTokenUsage]


class BackgroundAgentUsageRepository:
    """只负责后台工作用量数据库的 schema、事务和事实读写。"""

    def __init__(self, db_path: str | Path) -> None:
        """保存独立用量数据库路径，不在构造阶段创建文件。

        Args:
            db_path: 后台工作用量 SQLite 文件路径。
        """

        self.db_path = Path(db_path)
        self._active_connections_lock = Lock()
        self._active_connections: set[sqlite3.Connection] = set()

    def interrupt_active_operations(self) -> None:
        """中断当前仍在执行的 SQLite 读写，供应用关闭超时使用。"""

        with self._active_connections_lock:
            active_connections = tuple(self._active_connections)
        for connection in active_connections:
            # sqlite3.Connection.interrupt 明确允许跨线程调用，用于让专用执行器
            # 尽快离开长查询；连接自己的 finally 仍负责回滚和关闭。
            connection.interrupt()

    def initialize(self) -> None:
        """创建当前用量 schema，并拒绝其它已存在版本。"""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if current_version not in {0, SCHEMA_VERSION}:
                    raise RuntimeError(
                        "用量数据库 schema 版本 "
                        f"{current_version} 不是当前开发版本 {SCHEMA_VERSION}，"
                        "旧用量数据不再兼容，请删除数据库后重新启动"
                    )
                if current_version == 0:
                    self._create_schema(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                foreign_key_errors = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if foreign_key_errors:
                    raise RuntimeError("后台工作用量数据库外键校验失败")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_terminal_usage(
        self,
        *,
        activity_id: str,
        terminal_at_ms: int,
        usage: AgentTaskUsageSnapshot,
    ) -> bool:
        """幂等保存一项后台工作的终态用量。

        Args:
            activity_id: 公开 Activity 的稳定编号，只用于去重关联。
            terminal_at_ms: 任务终态发生的 Unix 毫秒时间。
            usage: 子进程提供的去内容化用量快照。

        Returns:
            首次写入返回真；同一 Activity 已存在时返回假。

        Raises:
            ValueError: Activity 编号或时间不合法。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            raise ValueError("后台工作用量缺少 Activity 编号")
        if (
            isinstance(terminal_at_ms, bool)
            or not isinstance(terminal_at_ms, int)
            or terminal_at_ms < 0
        ):
            raise ValueError("后台工作终态时间必须是非负整数")
        terminal_local_date = _local_date_from_ms(terminal_at_ms)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO background_agent_usage_tasks (
                        activity_id,
                        terminal_at_ms,
                        terminal_local_date,
                        is_complete,
                        reference_cost_usd
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_activity_id,
                        terminal_at_ms,
                        terminal_local_date.isoformat(),
                        int(usage.is_complete),
                        usage.reference_cost_usd,
                    ),
                )
                if cursor.rowcount == 0:
                    connection.commit()
                    return False
                connection.executemany(
                    """
                    INSERT INTO background_agent_usage_models (
                        activity_id,
                        model,
                        non_cached_input_tokens,
                        cache_creation_input_tokens,
                        cache_read_input_tokens,
                        output_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            normalized_activity_id,
                            item.model,
                            item.non_cached_input_tokens,
                            item.cache_creation_input_tokens,
                            item.cache_read_input_tokens,
                            item.output_tokens,
                        )
                        for item in usage.by_model
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def query_tasks(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> tuple[StoredUsageTask, ...]:
        """按固化日期读取一个包含两端日期的任务快照。

        Args:
            from_date: 查询包含的首日。
            to_date: 查询包含的末日。

        Returns:
            从同一条 LEFT JOIN 查询还原的任务及模型用量。

        Raises:
            ValueError: 结束日期早于开始日期。
        """

        if to_date < from_date:
            raise ValueError("用量查询结束日期不能早于开始日期")

        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                rows = connection.execute(
                    """
                    SELECT
                        task_usage.activity_id,
                        task_usage.terminal_at_ms,
                        task_usage.terminal_local_date,
                        task_usage.is_complete,
                        task_usage.reference_cost_usd,
                        model_usage.model,
                        model_usage.non_cached_input_tokens,
                        model_usage.cache_creation_input_tokens,
                        model_usage.cache_read_input_tokens,
                        model_usage.output_tokens
                    FROM background_agent_usage_tasks AS task_usage
                    LEFT JOIN background_agent_usage_models AS model_usage
                        ON model_usage.activity_id = task_usage.activity_id
                    WHERE task_usage.terminal_local_date >= ?
                      AND task_usage.terminal_local_date <= ?
                    ORDER BY
                        task_usage.terminal_local_date,
                        task_usage.terminal_at_ms,
                        task_usage.activity_id,
                        model_usage.model
                    """,
                    (from_date.isoformat(), to_date.isoformat()),
                ).fetchall()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _restore_tasks(rows)

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        """创建当前任务表、模型表和日期查询索引。

        Args:
            connection: 已开启写事务的 SQLite 连接。
        """

        connection.execute(
            """
            CREATE TABLE background_agent_usage_tasks (
                activity_id TEXT PRIMARY KEY,
                terminal_at_ms INTEGER NOT NULL CHECK (terminal_at_ms >= 0),
                terminal_local_date TEXT NOT NULL CHECK (
                    terminal_local_date GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                ),
                is_complete INTEGER NOT NULL CHECK (is_complete IN (0, 1)),
                reference_cost_usd REAL CHECK (
                    reference_cost_usd IS NULL OR reference_cost_usd >= 0
                )
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE background_agent_usage_models (
                activity_id TEXT NOT NULL,
                model TEXT NOT NULL,
                non_cached_input_tokens INTEGER NOT NULL CHECK (non_cached_input_tokens >= 0),
                cache_creation_input_tokens INTEGER NOT NULL CHECK (cache_creation_input_tokens >= 0),
                cache_read_input_tokens INTEGER NOT NULL CHECK (cache_read_input_tokens >= 0),
                output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
                PRIMARY KEY (activity_id, model),
                FOREIGN KEY (activity_id)
                    REFERENCES background_agent_usage_tasks(activity_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_background_agent_usage_terminal_date
            ON background_agent_usage_tasks(terminal_local_date)
            """
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """创建使用短锁等待和显式事务的 SQLite 连接。"""

        connection = sqlite3.connect(
            self.db_path,
            timeout=SQLITE_LOCK_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {int(SQLITE_LOCK_TIMEOUT_SECONDS * 1000)}"
        )
        with self._active_connections_lock:
            self._active_connections.add(connection)
        try:
            yield connection
        finally:
            with self._active_connections_lock:
                self._active_connections.discard(connection)
            connection.close()


def _restore_tasks(rows: list[sqlite3.Row]) -> tuple[StoredUsageTask, ...]:
    """把单次 JOIN 的重复任务列合并为类型化记录。

    Args:
        rows: 同一 SQLite 查询快照返回的任务与可选模型行。

    Returns:
        每项任务只出现一次的不可变记录。
    """

    builders: dict[str, _TaskRowBuilder] = {}
    for row in rows:
        activity_id = str(row["activity_id"])
        builder = builders.get(activity_id)
        if builder is None:
            raw_cost = row["reference_cost_usd"]
            builder = _TaskRowBuilder(
                activity_id=activity_id,
                terminal_at_ms=int(row["terminal_at_ms"]),
                terminal_local_date=date.fromisoformat(
                    str(row["terminal_local_date"])
                ),
                is_complete=bool(row["is_complete"]),
                reference_cost_usd=(
                    float(raw_cost) if raw_cost is not None else None
                ),
                by_model=[],
            )
            builders[activity_id] = builder
        if row["model"] is None:
            continue
        builder.by_model.append(
            AgentModelTokenUsage(
                model=str(row["model"]),
                non_cached_input_tokens=int(row["non_cached_input_tokens"]),
                cache_creation_input_tokens=int(
                    row["cache_creation_input_tokens"]
                ),
                cache_read_input_tokens=int(row["cache_read_input_tokens"]),
                output_tokens=int(row["output_tokens"]),
            )
        )
    return tuple(
        StoredUsageTask(
            activity_id=builder.activity_id,
            terminal_at_ms=builder.terminal_at_ms,
            terminal_local_date=builder.terminal_local_date,
            is_complete=builder.is_complete,
            reference_cost_usd=builder.reference_cost_usd,
            by_model=tuple(builder.by_model),
        )
        for builder in builders.values()
    )


def _local_date_from_ms(terminal_at_ms: int) -> date:
    """按写入当时的 Backend 系统时区固化终态日期。

    Args:
        terminal_at_ms: 任务终态发生的 Unix 毫秒时间。

    Returns:
        写入当时本地时区下的日期。

    Raises:
        ValueError: 时间戳超出系统日期支持范围。
    """

    try:
        return datetime.fromtimestamp(terminal_at_ms / 1000.0).date()
    except (OSError, OverflowError, ValueError) as exception:
        raise ValueError("后台工作终态时间超出系统支持范围") from exception
