from __future__ import annotations

import sqlite3
import threading
from time import monotonic
from datetime import date, datetime
from pathlib import Path

import pytest

from companion.background_agent.usage_models import (
    AgentModelTokenUsage,
    AgentTaskUsageSnapshot,
)
from companion.background_agent.usage_repository import (
    BackgroundAgentUsageRepository,
    SCHEMA_VERSION,
)


def test_repository_is_idempotent_and_survives_restart(tmp_path: Path) -> None:
    """同一 Activity 重放不会重复计数，重新构造 Repository 后仍能查询。"""

    db_path = tmp_path / "background-agent-usage.sqlite3"
    repository = BackgroundAgentUsageRepository(db_path)
    repository.initialize()
    terminal_at_ms = _timestamp_ms(2026, 8, 2)
    snapshot = _usage_snapshot(cost=0.5, is_complete=True)

    assert repository.record_terminal_usage(
        activity_id="11111111-1111-4111-8111-111111111111",
        terminal_at_ms=terminal_at_ms,
        usage=snapshot,
    ) is True
    assert repository.record_terminal_usage(
        activity_id="11111111-1111-4111-8111-111111111111",
        terminal_at_ms=terminal_at_ms,
        usage=snapshot,
    ) is False

    restarted_repository = BackgroundAgentUsageRepository(db_path)
    restarted_repository.initialize()
    tasks = restarted_repository.query_tasks(
        from_date=date(2026, 8, 2),
        to_date=date(2026, 8, 2),
    )

    assert len(tasks) == 1
    assert tasks[0].reference_cost_usd == 0.5
    assert [usage.model for usage in tasks[0].by_model] == [
        "claude-haiku-4-5",
        "deepseek-v4-pro",
    ]


def test_repository_uses_inclusive_frozen_date_boundaries(tmp_path: Path) -> None:
    """查询直接使用固化日期，两端日期都包含且不重新解释时间戳。"""

    db_path = tmp_path / "usage.sqlite3"
    repository = BackgroundAgentUsageRepository(db_path)
    repository.initialize()
    repository.record_terminal_usage(
        activity_id="11111111-1111-4111-8111-111111111111",
        terminal_at_ms=_timestamp_ms(2026, 8, 3),
        usage=_usage_snapshot(cost=None, is_complete=False),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE background_agent_usage_tasks
            SET terminal_local_date = ?
            WHERE activity_id = ?
            """,
            ("2026-08-02", "11111111-1111-4111-8111-111111111111"),
        )

    selected = repository.query_tasks(
        from_date=date(2026, 8, 2),
        to_date=date(2026, 8, 2),
    )
    excluded = repository.query_tasks(
        from_date=date(2026, 8, 3),
        to_date=date(2026, 8, 3),
    )

    assert len(selected) == 1
    assert selected[0].terminal_local_date == date(2026, 8, 2)
    assert excluded == ()


def test_repository_keeps_tasks_without_model_rows(tmp_path: Path) -> None:
    """不完整任务即使尚无模型 Token，也必须保留在任务统计中。"""

    repository = BackgroundAgentUsageRepository(tmp_path / "usage.sqlite3")
    repository.initialize()
    repository.record_terminal_usage(
        activity_id="11111111-1111-4111-8111-111111111111",
        terminal_at_ms=_timestamp_ms(2026, 8, 2),
        usage=AgentTaskUsageSnapshot(by_model=(), is_complete=False),
    )

    tasks = repository.query_tasks(
        from_date=date(2026, 8, 2),
        to_date=date(2026, 8, 2),
    )

    assert len(tasks) == 1
    assert tasks[0].by_model == ()
    assert tasks[0].is_complete is False


@pytest.mark.parametrize("unsupported_version", [1, 2, SCHEMA_VERSION + 1])
def test_repository_rejects_unsupported_schema_without_migration(
    tmp_path: Path,
    unsupported_version: int,
) -> None:
    """开发阶段不兼容旧或未来 schema，也不复制其中的数据。"""

    db_path = tmp_path / f"unsupported-v{unsupported_version}.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"PRAGMA user_version = {unsupported_version}")

    with pytest.raises(RuntimeError, match="不是当前开发版本"):
        BackgroundAgentUsageRepository(db_path).initialize()

    with sqlite3.connect(db_path) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == unsupported_version


def test_repository_rejects_inverted_date_range(tmp_path: Path) -> None:
    """Repository 自身也必须保护日期查询边界。"""

    repository = BackgroundAgentUsageRepository(tmp_path / "usage.sqlite3")
    repository.initialize()

    with pytest.raises(ValueError, match="结束日期不能早于开始日期"):
        repository.query_tasks(
            from_date=date(2026, 8, 3),
            to_date=date(2026, 8, 2),
        )


def test_repository_query_stays_consistent_during_second_connection_commit(
    tmp_path: Path,
) -> None:
    """并发提交前后查询都只能看到完整任务，不能出现孤立模型行。"""

    db_path = tmp_path / "usage.sqlite3"
    repository = BackgroundAgentUsageRepository(db_path)
    repository.initialize()
    repository.record_terminal_usage(
        activity_id="11111111-1111-4111-8111-111111111111",
        terminal_at_ms=_timestamp_ms(2026, 8, 2),
        usage=_usage_snapshot(cost=0.25, is_complete=True),
    )
    start_write = threading.Event()
    write_finished = threading.Event()

    def write_second_task() -> None:
        """通过第二条 SQLite 连接提交一项完整任务。"""

        start_write.wait(timeout=1)
        BackgroundAgentUsageRepository(db_path).record_terminal_usage(
            activity_id="22222222-2222-4222-8222-222222222222",
            terminal_at_ms=_timestamp_ms(2026, 8, 2),
            usage=_usage_snapshot(cost=None, is_complete=False),
        )
        write_finished.set()

    writer = threading.Thread(target=write_second_task)
    writer.start()
    start_write.set()
    snapshot = repository.query_tasks(
        from_date=date(2026, 8, 2),
        to_date=date(2026, 8, 2),
    )
    writer.join(timeout=2)

    assert write_finished.is_set()
    assert len(snapshot) in {1, 2}
    assert all(len(task.by_model) == 2 for task in snapshot)


def test_repository_lock_wait_is_bounded_to_short_timeout(tmp_path: Path) -> None:
    """外部独占锁不能让一次用量 I/O 沿用 SQLite 默认五秒等待。"""

    db_path = tmp_path / "usage.sqlite3"
    repository = BackgroundAgentUsageRepository(db_path)
    repository.initialize()
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    started_at = monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            repository.record_terminal_usage(
                activity_id="11111111-1111-4111-8111-111111111111",
                terminal_at_ms=_timestamp_ms(2026, 8, 2),
                usage=_usage_snapshot(cost=None, is_complete=False),
            )
    finally:
        blocker.rollback()
        blocker.close()

    assert monotonic() - started_at < 1.0


def _usage_snapshot(
    *,
    cost: float | None,
    is_complete: bool,
) -> AgentTaskUsageSnapshot:
    """构造包含两个模型的固定任务用量。"""

    return AgentTaskUsageSnapshot(
        by_model=(
            AgentModelTokenUsage(
                model="deepseek-v4-pro",
                non_cached_input_tokens=10,
                cache_creation_input_tokens=5,
                cache_read_input_tokens=20,
                output_tokens=7,
            ),
            AgentModelTokenUsage(
                model="claude-haiku-4-5",
                output_tokens=1,
            ),
        ),
        is_complete=is_complete,
        reference_cost_usd=cost,
    )


def _timestamp_ms(year: int, month: int, day: int) -> int:
    """生成本地中午时间戳，避免测试跨入相邻日期。"""

    return int(datetime(year, month, day, 12).timestamp() * 1000)
