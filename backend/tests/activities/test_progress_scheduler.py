from __future__ import annotations

import pytest

from companion.activities.progress_scheduler import (
    ACTIVITY_PROGRESS_INTERVALS_S,
    TASK_RESULT_PROGRESS_INTERVALS_S,
    ActivityProgressScheduler,
    ProgressIntervalPolicy,
)


def test_interval_policy_uses_bounded_backoff() -> None:
    """退避策略超过已配置档位后应稳定保持最大间隔。"""

    activity = ProgressIntervalPolicy(ACTIVITY_PROGRESS_INTERVALS_S)
    task_result = ProgressIntervalPolicy(TASK_RESULT_PROGRESS_INTERVALS_S)

    assert [activity.interval_for(index) for index in range(6)] == [30.0, 60.0, 60.0, 60.0, 60.0, 60.0]
    assert [task_result.interval_for(index) for index in range(6)] == [25.0, 30.0, 60.0, 90.0, 90.0, 90.0]


def test_scheduler_runs_due_callbacks_and_honors_cancellation() -> None:
    """fake clock 推进后只执行到期且没有取消的回调。"""

    current_time = [10.0]
    scheduler = ActivityProgressScheduler(
        monotonic_now=lambda: current_time[0],
        autostart=False,
    )
    calls: list[str] = []
    scheduler.schedule(5.0, lambda: calls.append("first"))
    cancelled = scheduler.schedule(5.0, lambda: calls.append("cancelled"))
    scheduler.schedule(8.0, lambda: calls.append("later"))
    cancelled.cancel()

    current_time[0] = 15.0
    assert scheduler.run_due() == 1
    assert calls == ["first"]

    current_time[0] = 18.0
    assert scheduler.run_due() == 1
    assert calls == ["first", "later"]


def test_scheduler_shutdown_joins_thread_and_rejects_new_deadlines() -> None:
    """关闭必须等待共享线程退出，并阻止活动在旧生命周期中重新登记。"""

    scheduler = ActivityProgressScheduler()
    scheduler_thread = scheduler._thread

    scheduler.shutdown(timeout=1.0)

    assert scheduler_thread is not None
    assert scheduler_thread.is_alive() is False
    with pytest.raises(RuntimeError, match="已经停止"):
        scheduler.schedule(1.0, lambda: None)
