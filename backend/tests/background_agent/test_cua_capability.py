from __future__ import annotations

import threading

import pytest

from companion.background_agent.cua_capability import (
    CuaCapabilityBusyError,
    CuaCapabilitySlot,
)


def test_capability_slot_exposes_only_content_free_availability() -> None:
    """快照只能说明是否空闲，不能暴露 owner Activity。"""

    slot = CuaCapabilitySlot()
    assert slot.snapshot().available is True

    slot.acquire("activity-a")

    snapshot = slot.snapshot()
    assert snapshot.available is False
    assert not hasattr(snapshot, "owner_activity_id")


def test_two_threads_cannot_receive_the_same_global_grant() -> None:
    """两条 Background lane 同时申请时只能有一项成功。"""

    slot = CuaCapabilitySlot()
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, bool]] = []

    def acquire(activity_id: str) -> None:
        """让两个测试线程在同一时刻竞争能力槽位。"""

        barrier.wait()
        try:
            slot.acquire(activity_id)
        except CuaCapabilityBusyError:
            outcomes.append((activity_id, False))
        else:
            outcomes.append((activity_id, True))

    threads = [
        threading.Thread(target=acquire, args=("activity-a",)),
        threading.Thread(target=acquire, args=("activity-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert sorted(granted for _activity_id, granted in outcomes) == [False, True]


def test_release_is_precise_and_late_parent_cannot_clear_replacement() -> None:
    """错误或迟到清理不能释放当前 replacement 的 Grant。"""

    slot = CuaCapabilitySlot()
    slot.acquire("activity-a")

    assert slot.release("activity-b") is False
    assert slot.transfer("activity-a", "activity-b") is True
    assert slot.release("activity-a") is False
    assert slot.snapshot().available is False
    assert slot.release("activity-b") is True


def test_transfer_is_atomic_and_requires_exact_parent() -> None:
    """纠正转移只能由当前父所有者一次性完成。"""

    slot = CuaCapabilitySlot()
    slot.acquire("activity-a")

    assert slot.transfer("activity-x", "activity-b") is False
    assert slot.transfer("activity-a", "activity-b") is True
    with pytest.raises(CuaCapabilityBusyError):
        slot.acquire("activity-c")


@pytest.mark.parametrize("activity_id", ["", "   "])
def test_blank_activity_id_cannot_acquire_grant(activity_id: str) -> None:
    """空身份不能成为无法精确释放的 Grant owner。"""

    with pytest.raises(ValueError, match="Activity ID"):
        CuaCapabilitySlot().acquire(activity_id)
