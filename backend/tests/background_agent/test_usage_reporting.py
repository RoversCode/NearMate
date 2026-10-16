from __future__ import annotations

from datetime import date

from companion.background_agent.usage_models import AgentModelTokenUsage
from companion.background_agent.usage_reporting import build_usage_report
from companion.background_agent.usage_repository import StoredUsageTask


def test_reporting_combines_tokens_tasks_and_partial_cost_coverage() -> None:
    """总览、模型与日期维度必须使用同一套 Token 和覆盖公式。"""

    tasks = (
        _task(
            activity_id="task-a",
            local_date=date(2026, 8, 1),
            complete=True,
            cost=0.25,
            token_usage=AgentModelTokenUsage(
                model="deepseek-v4-pro",
                non_cached_input_tokens=10,
                cache_creation_input_tokens=5,
                cache_read_input_tokens=20,
                output_tokens=7,
            ),
        ),
        _task(
            activity_id="task-b",
            local_date=date(2026, 8, 2),
            complete=False,
            cost=None,
            token_usage=AgentModelTokenUsage(
                model="deepseek-v4-pro",
                non_cached_input_tokens=2,
                output_tokens=1,
            ),
        ),
    )

    report = build_usage_report(
        tasks=tasks,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 2),
    )

    assert report.summary.task_count == 2
    assert report.summary.complete_task_count == 1
    assert report.summary.incomplete_task_count == 1
    assert report.summary.total_input_tokens == 37
    assert report.summary.cache_hit_tokens == 20
    assert report.summary.cache_hit_rate == 20 / 37
    assert report.summary.reference_cost_usd == 0.25
    assert report.summary.reference_cost_task_count == 1
    assert report.by_model[0].task_count == 2
    assert [row.date for row in report.by_day] == [date(2026, 8, 1), date(2026, 8, 2)]


def test_reporting_returns_null_hit_rate_without_input_tokens() -> None:
    """没有输入 Token 时缓存命中率必须为空而不是零。"""

    report = build_usage_report(
        tasks=(
            _task(
                activity_id="task-a",
                local_date=date(2026, 8, 2),
                complete=False,
                cost=None,
                token_usage=None,
            ),
        ),
        from_date=date(2026, 8, 2),
        to_date=date(2026, 8, 2),
    )

    assert report.summary.total_input_tokens == 0
    assert report.summary.cache_hit_rate is None
    assert report.by_model == ()


def _task(
    *,
    activity_id: str,
    local_date: date,
    complete: bool,
    cost: float | None,
    token_usage: AgentModelTokenUsage | None,
) -> StoredUsageTask:
    """构造 Reporting 测试使用的固定 Repository 记录。"""

    return StoredUsageTask(
        activity_id=activity_id,
        terminal_at_ms=1,
        terminal_local_date=local_date,
        is_complete=complete,
        reference_cost_usd=cost,
        by_model=(token_usage,) if token_usage is not None else (),
    )
