from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime
from pathlib import Path

import pytest

from companion.background_agent.usage_repository import BackgroundAgentUsageRepository
from companion.background_agent.usage_service import (
    BackgroundAgentUsageService,
    BackgroundAgentUsageUnavailableError,
)


@pytest.mark.asyncio
async def test_service_query_observes_previously_queued_terminal_write(
    tmp_path: Path,
) -> None:
    """查询和写入走同一队列，查询必须看到排在它前面的终态。"""

    service = BackgroundAgentUsageService(
        BackgroundAgentUsageRepository(tmp_path / "usage.sqlite3")
    )
    await service.startup()
    service.record_terminal_event(
        "background_agent.completed",
        _terminal_payload(_timestamp_ms(2026, 8, 2)),
    )

    report = await service.query(
        from_date=date(2026, 8, 2),
        to_date=date(2026, 8, 2),
    )
    await service.shutdown()

    assert report.summary.task_count == 1
    assert report.summary.total_input_tokens == 35


@pytest.mark.asyncio
async def test_service_shutdown_drains_accepted_terminal_write(tmp_path: Path) -> None:
    """正常关闭必须在预算内保存已经进入队列的终态。"""

    db_path = tmp_path / "usage.sqlite3"
    service = BackgroundAgentUsageService(BackgroundAgentUsageRepository(db_path))
    await service.startup()
    service.record_terminal_event(
        "background_agent.cancelled",
        _terminal_payload(_timestamp_ms(2026, 8, 2), complete=False),
    )

    await service.shutdown()

    repository = BackgroundAgentUsageRepository(db_path)
    repository.initialize()
    tasks = repository.query_tasks(
        from_date=date(2026, 8, 2),
        to_date=date(2026, 8, 2),
    )
    assert len(tasks) == 1
    assert tasks[0].is_complete is False


@pytest.mark.asyncio
async def test_service_reports_unavailable_before_startup(tmp_path: Path) -> None:
    """生命周期尚未启动时，HTTP 查询不能伪装成合法空统计。"""

    service = BackgroundAgentUsageService(
        BackgroundAgentUsageRepository(tmp_path / "usage.sqlite3")
    )

    with pytest.raises(BackgroundAgentUsageUnavailableError):
        await service.query(
            from_date=date(2026, 8, 2),
            to_date=date(2026, 8, 2),
        )


@pytest.mark.asyncio
async def test_service_keeps_event_loop_free_while_repository_write_blocks() -> None:
    """SQLite 写入阻塞时，提交终态和其它协程仍能立即继续。"""

    repository = BlockingRepository()
    service = BackgroundAgentUsageService(repository)
    await service.startup()

    service.record_terminal_event(
        "background_agent.completed",
        _terminal_payload(_timestamp_ms(2026, 8, 2)),
    )
    assert await _wait_thread_event(repository.write_started)

    heartbeat_completed = False

    async def heartbeat() -> None:
        """证明事件循环没有在同步 SQLite 调用中停住。"""

        nonlocal heartbeat_completed
        heartbeat_completed = True

    await heartbeat()
    assert heartbeat_completed is True

    repository.release_write.set()
    await service.shutdown()


@pytest.mark.asyncio
async def test_service_shutdown_timeout_fails_in_flight_query() -> None:
    """关闭超时取消消费者时，已经出队的查询也必须结束，不能永久悬挂。"""

    repository = BlockingQueryRepository()
    service = BackgroundAgentUsageService(
        repository,
        shutdown_timeout_seconds=0.05,
    )
    await service.startup()
    query_task = asyncio.create_task(
        service.query(
            from_date=date(2026, 8, 2),
            to_date=date(2026, 8, 2),
        )
    )
    assert await _wait_thread_event(repository.query_started)

    try:
        await service.shutdown()
        with pytest.raises(BackgroundAgentUsageUnavailableError):
            await asyncio.wait_for(query_task, timeout=0.2)
    finally:
        repository.release_query.set()
    assert repository.interrupt_called is True


@pytest.mark.asyncio
async def test_service_skips_cancelled_query_waiting_behind_write() -> None:
    """浏览器取消的排队查询不能继续占用 Repository 并挤压后续终态写入。"""

    repository = BlockingRepository()
    service = BackgroundAgentUsageService(repository)
    await service.startup()
    service.record_terminal_event(
        "background_agent.completed",
        _terminal_payload(_timestamp_ms(2026, 8, 2)),
    )
    assert await _wait_thread_event(repository.write_started)
    query_task = asyncio.create_task(
        service.query(
            from_date=date(2026, 8, 2),
            to_date=date(2026, 8, 2),
        )
    )
    await asyncio.sleep(0)

    query_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query_task
    repository.release_write.set()
    await service.shutdown()

    assert repository.query_calls == 0


class BlockingRepository:
    """把写入停在测试可控事件上的 Repository 替身。"""

    def __init__(self) -> None:
        self.write_started = threading.Event()
        self.release_write = threading.Event()
        self.query_calls = 0

    def initialize(self) -> None:
        """测试 Repository 不需要真实 schema。"""

    def record_terminal_usage(self, **_payload: object) -> bool:
        """通知测试写入已开始，并等待显式释放。"""

        self.write_started.set()
        self.release_write.wait(timeout=2)
        return True

    def query_tasks(self, **_payload: object) -> tuple[()]:
        """本测试不执行查询，返回空任务即可。"""

        self.query_calls += 1
        return ()


class BlockingQueryRepository:
    """把查询停在测试可控事件上的 Repository 替身。"""

    def __init__(self) -> None:
        self.query_started = threading.Event()
        self.release_query = threading.Event()
        self.interrupt_called = False

    def initialize(self) -> None:
        """测试 Repository 不需要真实 schema。"""

    def record_terminal_usage(self, **_payload: object) -> bool:
        """本测试不执行写入，保留完整 Repository 端口。"""

        return True

    def query_tasks(self, **_payload: object) -> tuple[()]:
        """通知测试查询已开始，并等待关闭流程触发取消。"""

        self.query_started.set()
        self.release_query.wait(timeout=2)
        return ()

    def interrupt_active_operations(self) -> None:
        """模拟 SQLite interrupt，让专用线程在关闭预算后立即退出。"""

        self.interrupt_called = True
        self.release_query.set()


async def _wait_thread_event(event: threading.Event) -> bool:
    """在线程池外等待测试同步事件。"""

    return await asyncio.to_thread(event.wait, 1)


def _terminal_payload(
    terminal_at_ms: int,
    *,
    complete: bool = True,
) -> dict[str, object]:
    """构造服务测试使用的去内容化终态载荷。"""

    return {
        "activity_id": "11111111-1111-4111-8111-111111111111",
        "created_at": terminal_at_ms,
        "internal_usage": {
            "is_complete": complete,
            "reference_cost_usd": 0.25 if complete else None,
            "by_model": [
                {
                    "model": "deepseek-v4-pro",
                    "non_cached_input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 7,
                }
            ],
        },
    }


def _timestamp_ms(year: int, month: int, day: int) -> int:
    """生成本地中午时间戳，避免测试跨入相邻日期。"""

    return int(datetime(year, month, day, 12).timestamp() * 1000)
