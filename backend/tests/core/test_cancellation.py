from __future__ import annotations

from threading import Event, Thread

from companion.realtime.cancellation import CancellationToken, CombinedCancellationSignal


def test_combined_signal_wait_wakes_from_secondary_without_waiting_for_primary() -> None:
    """辅助取消触发时，组合信号必须立刻唤醒，不得卡在主信号的等待里。"""

    primary = CancellationToken()
    secondary = CancellationToken()
    primary_wait_started = Event()
    release_primary_wait = Event()

    def blocking_primary_wait(timeout: float | None = None) -> bool:
        """模拟一个不会及时返回的 provider 主取消等待。"""

        _ = timeout
        primary_wait_started.set()
        release_primary_wait.wait(timeout=2.0)
        return primary.is_set()

    # 旧实现会直接调用 primary.wait；这个替身让该错误变成确定性的阻塞，
    # 新实现则只注册两个 token 的回调，因此不会触发这里的等待。
    primary.wait = blocking_primary_wait  # type: ignore[method-assign]
    signal = CombinedCancellationSignal(primary, secondary)
    result: list[bool] = []
    returned = Event()

    worker = Thread(
        target=lambda: (result.append(signal.wait(timeout=1.0)), returned.set()),
        daemon=True,
    )
    worker.start()
    try:
        secondary.set("deadline")
        assert returned.wait(timeout=0.5) is True
        assert result == [True]
        assert primary_wait_started.is_set() is False
    finally:
        release_primary_wait.set()
        worker.join(timeout=1.0)

