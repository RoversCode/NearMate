from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from companion.foreground_tools.background_activity_runtime import BackgroundActivityRuntime


class ImmediateExecutor:
    """在 submit 调用内同步执行工作。"""

    def submit(self, function: Any, *args: object) -> None:
        """立即调用给定工作函数。"""

        function(*args)


class DelayedSubmitReturnExecutor(ThreadPoolExecutor):
    """真实投递工作后，延迟把 Future 交还给 Runtime。"""

    def __init__(self) -> None:
        """初始化单线程执行器与竞态控制信号。"""

        super().__init__(max_workers=1, thread_name_prefix="activity-runtime-submit-race")
        self.submitted = threading.Event()
        self.release_submit = threading.Event()

    def submit(self, function: Any, *args: object, **kwargs: object) -> Any:
        """投递工作并暂停 Future 返回，放大关闭竞态窗口。"""

        future = super().submit(function, *args, **kwargs)
        self.submitted.set()
        self.release_submit.wait(timeout=0.3)
        return future


def test_runtime_preserves_event_when_notifier_fails() -> None:
    """旁路唤醒失败不能撤销已经成功入账的事件。"""

    runtime = BackgroundActivityRuntime(
        executor=ImmediateExecutor(),
        thread_name_prefix="test-activity",
        event_notifier=lambda: (_ for _ in ()).throw(RuntimeError("notify failed")),
    )

    assert runtime.append_event({"type": "activity.started", "payload": {}}) is True
    assert runtime.drain_events() == [{"type": "activity.started", "payload": {}}]


def test_runtime_rejects_submission_after_shutdown() -> None:
    """关闭边界之后不得再把工作交给底层执行器。"""

    runtime = BackgroundActivityRuntime(
        executor=ImmediateExecutor(),
        thread_name_prefix="test-activity",
    )
    runtime.shutdown()

    with pytest.raises(RuntimeError, match="Runtime 已关闭"):
        runtime.submit(lambda: None)
    assert runtime.append_event({"type": "activity.completed", "payload": {}}) is False


def test_shutdown_does_not_wait_for_future_registration_race() -> None:
    """Future 尚未返回时，关闭流程也不能越过给定等待预算。"""

    executor = DelayedSubmitReturnExecutor()
    runtime = BackgroundActivityRuntime(
        executor=executor,
        thread_name_prefix="test-activity",
    )
    work_release = threading.Event()
    submit_thread = threading.Thread(
        target=lambda: runtime.submit(lambda: work_release.wait(timeout=0.3)),
        daemon=True,
    )
    submit_thread.start()
    assert executor.submitted.wait(timeout=1.0) is True

    started_at = time.monotonic()
    runtime.shutdown(timeout=0.02)
    elapsed = time.monotonic() - started_at

    executor.release_submit.set()
    work_release.set()
    submit_thread.join(timeout=1.0)
    executor.shutdown(wait=True, cancel_futures=True)
    assert elapsed < 0.2
