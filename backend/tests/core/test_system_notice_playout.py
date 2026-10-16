from __future__ import annotations

from companion.realtime.system_notice_playout import SystemNoticePlayoutTracker


def test_resolving_permission_returns_only_its_system_notice_request() -> None:
    """权限解决后只能定位承载它的 System Notice，不能误伤其他请求。"""

    tracker = SystemNoticePlayoutTracker()
    tracker.register_request(
        request_id="system-notice-a",
        interaction_request_ids=("permission-a",),
    )
    tracker.register_request(
        request_id="system-notice-b",
        interaction_request_ids=("permission-b",),
    )

    assert tracker.resolve_interaction("permission-a") == ("system-notice-a",)
    assert tracker.resolve_interaction("permission-a") == ()
    assert tracker.resolve_interaction("permission-b") == ("system-notice-b",)


def test_resolving_one_interaction_releases_the_whole_mixed_request() -> None:
    """一条语音含多项权限时，任一事实失效都会收口整条语音映射。"""

    tracker = SystemNoticePlayoutTracker()
    tracker.register_request(
        request_id="system-notice-a",
        interaction_request_ids=("permission-a", "permission-b"),
    )

    assert tracker.resolve_interaction("permission-a") == ("system-notice-a",)
    assert tracker.resolve_interaction("permission-b") == ()


def test_finished_request_cannot_be_cancelled_by_late_interaction_event() -> None:
    """语音已正常收口后，迟到的权限事件不能再命中过期请求。"""

    tracker = SystemNoticePlayoutTracker()
    tracker.register_request(
        request_id="system-notice-a",
        interaction_request_ids=("permission-a",),
    )

    tracker.finish_request("system-notice-a")

    assert tracker.resolve_interaction("permission-a") == ()
