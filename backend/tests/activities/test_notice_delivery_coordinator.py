from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import NotRequired, get_origin, get_type_hints

from companion.activities.notice.delivery_coordinator import (
    NoticeBatchLease,
    NoticeDeliveryCoordinator,
)
from companion.activities.notice.models import (
    ActivityInteractionNoticeInvalidation,
    ActivityEventApplyResult,
    ActivityLifecycleEvent,
    ActivityProgressNoticeIntent,
    ActivityProgressNoticeInvalidation,
    NoticeDeliveryCommand,
    NoticeDeliveryOutcome,
    NoticeSnapshot,
    WorkResultNoticeSnapshot,
)


RUNTIME_EPOCH = 1


def test_activity_fact_notice_contract_allows_projected_result_context() -> None:
    """强提醒契约必须允许 System Notice 临时投影答案产物正文。"""

    result_context_type = get_type_hints(
        WorkResultNoticeSnapshot,
        include_extras=True,
    )["result_context"]

    assert get_origin(result_context_type) is NotRequired


def test_delivery_command_returns_explicit_accepted_result() -> None:
    """交付边界必须返回请求身份和归一化结果，不能用空值暗示成功。"""

    coordinator = NoticeDeliveryCoordinator()
    result = coordinator.apply_delivery(
        NoticeDeliveryCommand(
            request_id=" request-a ",
            outcome="request_enqueued",
        )
    )

    assert result.accepted is True
    assert result.request_id == "request-a"
    assert result.outcome == "request_enqueued"
    assert result.released_terminal_activity_keys == ()


def test_delivery_result_releases_terminal_progress_only_after_final_disposition() -> None:
    """强终态重排时保留 frozen Progress，完成表达后才返回明确清理键。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(
        coordinator,
        _terminal(activity_id="task-result-a", source="background_agent"),
    )
    first = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    assert first["notice_batch"][0]["activity_id"] == "task-result-a"
    coordinator.apply_delivery(
        NoticeDeliveryCommand(request_id="request-a", outcome="request_enqueued")
    )

    cancelled = coordinator.apply_delivery(
        NoticeDeliveryCommand(request_id="request-a", outcome="delivery_cancelled")
    )
    assert cancelled.released_terminal_activity_keys == ()

    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )
    coordinator.apply_delivery(
        NoticeDeliveryCommand(request_id="request-b", outcome="request_enqueued")
    )
    completed = coordinator.apply_delivery(
        NoticeDeliveryCommand(request_id="request-b", outcome="llm_output_completed")
    )

    assert completed.released_terminal_activity_keys == (
        ("background_agent", "task-result-a"),
    )


def _apply(
    coordinator: NoticeDeliveryCoordinator,
    event: (
        ActivityLifecycleEvent
        | ActivityProgressNoticeIntent
        | ActivityProgressNoticeInvalidation
        | ActivityInteractionNoticeInvalidation
    ),
    runtime_epoch: int = RUNTIME_EPOCH,
) -> ActivityEventApplyResult:
    """以显式运行时世代向提醒协调器提交测试事件。"""

    return coordinator.apply_event(event, runtime_epoch)


def _deliver(
    coordinator: NoticeDeliveryCoordinator,
    request_id: str,
    outcome: NoticeDeliveryOutcome,
) -> None:
    """通过唯一交付命令入口推进测试请求状态。"""

    coordinator.apply_delivery(
        NoticeDeliveryCommand(
            request_id=request_id,
            outcome=outcome,
        )
    )


def _terminal(
    *,
    activity_id: str,
    source: str,
    terminal_state: str = "completed",
    message: str = "",
    suppress_notice: bool = False,
) -> ActivityLifecycleEvent:
    """构造测试使用的强终态事件。"""

    return ActivityLifecycleEvent(
        lifecycle="terminal",
        activity_id=activity_id,
        interaction_session_id="session-a",
        source=source,
        category="认知任务" if source == "cognitive" else "材料处理",
        goal=f"目标-{activity_id}",
        intent=(
            "cognitive_result"
            if terminal_state == "completed" and source == "cognitive"
            else "work_result"
            if terminal_state == "completed"
            else "terminal_status"
        ),
        result_context=(
            message or "认知结果已完成。"
            if terminal_state == "completed" and source == "cognitive"
            else ""
        ),
        status_context=message if terminal_state != "completed" else "",
        terminal_state=terminal_state,  # type: ignore[arg-type]
        suppress_notice=suppress_notice,
    )


def _progress(
    *,
    activity_id: str,
    source: str,
    sequence: int,
) -> ActivityProgressNoticeIntent:
    """构造不携带进度正文的弱提醒意图。"""

    return ActivityProgressNoticeIntent(
        activity_id=activity_id,
        interaction_session_id="session-a",
        source=source,
        sequence=sequence,
    )


def _started(*, activity_id: str, source: str) -> ActivityLifecycleEvent:
    """构造测试使用的活动开始事件。"""

    return ActivityLifecycleEvent(
        lifecycle="started",
        activity_id=activity_id,
        interaction_session_id="session-a",
        source=source,
        category="认知任务" if source == "cognitive" else "材料处理",
        goal=f"目标-{activity_id}",
    )


def _user_intervention(
    *,
    activity_id: str,
    source: str,
    lifecycle: str = "waiting_user",
) -> ActivityLifecycleEvent:
    """构造需要用户介入的强事实事件。

    Args:
        activity_id: 事件对应的活动编号。
        source: 活动所属的来源车道。
        lifecycle: 旧测试语义提示；显式意图始终使用资料请求。

    Returns:
        可提交给 Notice 协调器的活动生命周期事件。
    """

    return ActivityLifecycleEvent(
        lifecycle="waiting_user",
        activity_id=activity_id,
        interaction_session_id="session-a",
        source=source,
        category="认知任务" if source == "cognitive" else "材料处理",
        goal=f"目标-{activity_id}",
        intent="information_request",
        interaction_request_id=f"request-{activity_id}-{lifecycle}",
        questions=(
            {
                "question": "请补充完成这项工作所需的信息。",
                "header": "补充信息",
                "options": [],
                "multiSelect": False,
            },
        ),
    )


def test_system_notice_reserves_strong_notice_before_cross_source_progress() -> None:
    """同一空闲时刻应先领取强提醒，不能混入其他来源的弱进度。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))

    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    assert [notice["intent"] for notice in context["notice_batch"]] == [
        "work_result",
    ]
    assert [notice["source"] for notice in context["notice_batch"]] == [
        "task_result",
    ]
    assert set(context) == {"notice_batch"}
    assert coordinator.has_active_system_notice("session-a") is False
    _deliver(coordinator, "request-a", "request_enqueued")
    assert coordinator.has_active_system_notice("session-a") is True


def test_runtime_interruption_closes_active_notice_state_without_new_notice() -> None:
    """运行时中断应废止在途进度、封住迟到事件且不创建终态提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _started(activity_id="task_result-a", source="task_result"))
    _apply(coordinator,
        _progress(activity_id="task_result-a", source="task_result", sequence=1)
    )
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    result = coordinator.interrupt_active(RUNTIME_EPOCH)
    _apply(coordinator,
        _progress(activity_id="task_result-a", source="task_result", sequence=2)
    )

    assert result.superseded_request_ids == ("request-a",)
    assert coordinator.has_pending_notice("session-a") is False
    assert coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )["notice_batch"] == []


def test_runtime_interruption_notify_creates_one_strong_terminal_per_activity() -> None:
    """意外中断必须为每项真实活动补一次有目标与类别的强终态提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _started(activity_id="task_result-a", source="task_result"))
    _apply(coordinator, _started(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator,
        _progress(activity_id="task_result-a", source="task_result", sequence=1)
    )
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    result = coordinator.interrupt_active(RUNTIME_EPOCH, "notify")

    assert result.superseded_request_ids == ("request-a",)
    context = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="request-b",
    )
    terminals = [
        notice
        for notice in context["notice_batch"]
        if notice["intent"] == "terminal_status"
    ]
    assert [notice["activity_id"] for notice in terminals] == ["cognitive-a"]

    # task_result 的旧进度请求尚未确认停止输出，因此其中断终态必须继续等待；
    # 旧请求完成 supersede 对账后，车道才允许下一请求领取终态。
    _deliver(coordinator, "request-a", "superseded")
    later = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="request-c",
    )
    later_terminals = [
        notice
        for notice in later["notice_batch"]
        if notice["intent"] == "terminal_status"
    ]
    assert [notice["activity_id"] for notice in later_terminals] == ["task_result-a"]

    for notice in [*terminals, *later_terminals]:
        assert notice["terminal_state"] == "interrupted"
        assert notice["goal"] == f"目标-{notice['activity_id']}"
        assert "意外中断" in str(notice["status_context"])


def test_runtime_interruption_notify_seals_late_events() -> None:
    """notify 中断同样封住迟到事件，且每项活动只生成一次终态提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _started(activity_id="task_result-a", source="task_result"))

    coordinator.interrupt_active(RUNTIME_EPOCH, "notify")
    coordinator.interrupt_active(RUNTIME_EPOCH, "notify")
    _apply(coordinator,
        _progress(activity_id="task_result-a", source="task_result", sequence=2)
    )
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))

    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )
    terminals = [
        notice
        for notice in context["notice_batch"]
        if notice["intent"] == "terminal_status"
    ]
    assert len(terminals) == 1
    assert terminals[0]["terminal_state"] == "interrupted"


def test_old_runtime_interruption_preserves_new_runtime_notice() -> None:
    """旧世代清场不能废止热替换后已经进入账本的新进度。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(
        coordinator,
        _progress(activity_id="task_result-new", source="task_result", sequence=1),
        runtime_epoch=2,
    )

    coordinator.interrupt_active(1)
    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-new",
    )

    assert [notice["activity_id"] for notice in context["notice_batch"]] == [
        "task_result-new"
    ]


def test_notice_batch_lease_is_exactly_once_for_enqueue_or_rollback() -> None:
    """提醒批次只能在入队提交与入队前回滚之间二选一。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    lease = NoticeBatchLease(request_id="request-a", coordinator=coordinator)

    assert lease.rollback() is True
    assert lease.rollback() is False
    assert lease.commit_enqueued() is False
    assert coordinator.has_active_system_notice("session-a") is False

    retry_context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )
    assert len(retry_context["notice_batch"]) == 1
    retry_lease = NoticeBatchLease(request_id="request-b", coordinator=coordinator)
    assert retry_lease.commit_enqueued() is True
    assert retry_lease.commit_enqueued() is False
    assert retry_lease.rollback() is False
    assert coordinator.has_active_system_notice("session-a") is True


def test_cancelled_strong_notice_restores_terminal_without_consuming_progress() -> None:
    """用户打断强请求时应归还终态，并保留尚未领取的弱进度。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    first = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    assert [notice["intent"] for notice in first["notice_batch"]] == [
        "work_result"
    ]

    _deliver(coordinator, "request-a", "delivery_cancelled")
    second = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert [(notice["intent"], notice["source"]) for notice in second["notice_batch"]] == [
        ("work_result", "task_result")
    ]

    _deliver(coordinator, "request-b", "llm_output_completed")
    third = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-c",
    )
    assert [(notice["intent"], notice["source"]) for notice in third["notice_batch"]] == [
        ("progress_report", "cognitive")
    ]

    _deliver(coordinator, "request-c", "delivery_cancelled")
    assert coordinator.has_pending_notice("session-a") is False


def test_output_unavailable_drops_strong_notice_and_releases_all_request_state() -> None:
    """永久输出失败必须终止强提醒，不能像普通取消一样重新排队。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _deliver(coordinator, "request-a", "request_enqueued")

    _deliver(coordinator, "request-a", "output_unavailable")
    _deliver(coordinator, "request-a", "output_unavailable")

    assert coordinator.has_active_system_notice("session-a") is False
    assert coordinator.has_pending_notice("session-a") is False
    assert coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )["notice_batch"] == []


def test_late_cancellation_after_llm_completion_cannot_restore_terminal() -> None:
    """LLM 正常完成后 Notice 已结束，迟到的取消补偿不能让强终态复活。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _deliver(coordinator, "request-a", "request_enqueued")

    _deliver(coordinator, "request-a", "llm_output_completed")
    _deliver(coordinator, "request-a", "delivery_cancelled")

    assert coordinator.has_active_system_notice("session-a") is False
    assert coordinator.has_pending_notice("session-a") is False
    assert coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )["notice_batch"] == []


def test_system_notice_llm_completion_releases_terminal_and_output_lease() -> None:
    """LLM 正常输出完毕后立即删除强终态并释放独立播报占用。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _deliver(coordinator, "request-a", "request_enqueued")

    _deliver(coordinator, "request-a", "llm_output_completed")

    assert coordinator.has_active_system_notice("session-a") is False
    assert coordinator.has_pending_notice("session-a") is False
    assert coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )["notice_batch"] == []


def test_strong_notice_completion_leaves_weak_progress_for_next_request() -> None:
    """强提醒完成后，弱进度应留给下一次独立播报。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _deliver(coordinator, "request-a", "request_enqueued")

    _deliver(coordinator, "request-a", "llm_output_completed")

    assert coordinator.has_active_system_notice("session-a") is False
    assert coordinator.has_pending_notice("session-a") is True
    next_context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )
    assert [notice["intent"] for notice in next_context["notice_batch"]] == [
        "progress_report"
    ]


def test_real_user_request_only_attaches_strong_notices() -> None:
    """真实用户请求只能顺带消费必须表达的强提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(coordinator,
        _terminal(
            activity_id="task_result-a",
            source="task_result",
            terminal_state="failed",
        )
    )

    context = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="request-a",
    )

    assert [(notice["intent"], notice["terminal_state"]) for notice in context["notice_batch"]] == [
        ("terminal_status", "failed")
    ]
    _deliver(coordinator, "request-a", "llm_output_completed")
    assert coordinator.has_pending_notice("session-a") is True
    retry = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )
    assert [(notice["intent"], notice["activity_id"]) for notice in retry["notice_batch"]] == [
        ("progress_report", "cognitive-a")
    ]


def test_terminal_marks_reserved_progress_superseded_without_breaking_lease() -> None:
    """终态应标记旧进度请求，但在条件取消前保持原输出租约。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="progress-request",
    )

    result = _apply(coordinator,
        _terminal(
            activity_id="cognitive-a",
            source="cognitive",
            message="最终答案",
        )
    )

    assert result.superseded_request_ids == ("progress-request",)
    blocked = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="terminal-request",
    )
    assert blocked["notice_batch"] == []


def test_successful_supersede_restores_other_batch_items() -> None:
    """条件取消旧批次后，只丢弃过期项并恢复同批仍有效提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(coordinator,
        _progress(activity_id="task_result-a", source="task_result", sequence=1)
    )
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    result = _apply(coordinator,
        _progress(activity_id="cognitive-a", source="cognitive", sequence=2)
    )
    assert result.superseded_request_ids == ("request-a",)
    _deliver(coordinator, "request-a", "superseded")
    rebuilt = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert [(notice["activity_id"], notice["sequence"]) for notice in rebuilt["notice_batch"]] == [
        ("task_result-a", 1),
        ("cognitive-a", 2),
    ]


def test_weak_progress_update_does_not_supersede_active_strong_request() -> None:
    """弱进度更新不得取消只包含强提醒的独立请求。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(
        coordinator,
        _progress(activity_id="background-a", source="background_agent", sequence=1),
    )
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    strong_context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="strong-request",
    )

    progress_result = _apply(
        coordinator,
        _progress(activity_id="background-a", source="background_agent", sequence=2),
    )

    assert [notice["intent"] for notice in strong_context["notice_batch"]] == [
        "cognitive_result"
    ]
    assert progress_result.superseded_request_ids == ()

    _deliver(coordinator, "strong-request", "llm_output_completed")
    weak_context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="weak-request",
    )
    assert [notice["sequence"] for notice in weak_context["notice_batch"]] == [2]


def test_superseded_strong_batch_restores_other_strong_notice() -> None:
    """强批中一项失效后，应删除该项并归还其他仍有效的强提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    _apply(
        coordinator,
        ActivityLifecycleEvent(
            lifecycle="waiting_user",
            activity_id="background-a",
            interaction_session_id="session-a",
            source="background_agent",
            category="后台工作",
            goal="整理资料",
            intent="permission_request",
            interaction_request_id="permission-a",
            action_summary="运行完成这项工作所需的命令",
            grant_scope="activity",
        ),
    )
    first_context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="strong-request-a",
    )

    invalidation_result = _apply(
        coordinator,
        ActivityInteractionNoticeInvalidation(
            activity_id="background-a",
            interaction_session_id="session-a",
            source="background_agent",
            interaction_request_id="permission-a",
        ),
    )

    assert [notice["intent"] for notice in first_context["notice_batch"]] == [
        "cognitive_result",
        "permission_request",
    ]
    assert invalidation_result.superseded_request_ids == ("strong-request-a",)

    _deliver(coordinator, "strong-request-a", "superseded")
    rebuilt_context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="strong-request-b",
    )
    assert [notice["intent"] for notice in rebuilt_context["notice_batch"]] == [
        "cognitive_result"
    ]


def test_latest_pending_progress_replaces_older_progress() -> None:
    """同一活动只保留最高序号的尚未表达进度。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator,
        _progress(activity_id="task_result-a", source="task_result", sequence=1)
    )
    _apply(coordinator,
        _progress(activity_id="task_result-a", source="task_result", sequence=2)
    )
    _apply(coordinator,
        _progress(activity_id="task_result-a", source="task_result", sequence=1)
    )

    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    assert [notice["sequence"] for notice in context["notice_batch"]] == [2]


def test_same_source_keeps_only_latest_pending_candidate_across_activities() -> None:
    """同一来源的不同活动也只能保留一条最新 pending 进度。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(coordinator, _progress(activity_id="cognitive-b", source="cognitive", sequence=1))

    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    assert [(notice["activity_id"], notice["sequence"]) for notice in context["notice_batch"]] == [
        ("cognitive-b", 1)
    ]


def test_same_source_terminal_precedes_progress_in_separate_batches() -> None:
    """同来源终态应先领取，弱进度留到下一次交付机会。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator, _progress(activity_id="cognitive-b", source="cognitive", sequence=1))

    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    assert [(notice["intent"], notice["activity_id"]) for notice in context["notice_batch"]] == [
        ("cognitive_result", "cognitive-a"),
    ]

    _deliver(coordinator, "request-a", "llm_output_completed")
    later = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert [(notice["intent"], notice["activity_id"]) for notice in later["notice_batch"]] == [
        ("progress_report", "cognitive-b"),
    ]


def test_same_source_terminals_are_delivered_fifo_across_requests() -> None:
    """旧终态占用来源车道时，新终态不得被另一请求越过领取。"""

    coordinator = NoticeDeliveryCoordinator(monotonic_now=lambda: 1.0)
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator, _terminal(activity_id="cognitive-b", source="cognitive"))

    first = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    blocked = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="request-b",
    )

    assert [notice["activity_id"] for notice in first["notice_batch"]] == [
        "cognitive-a"
    ]
    assert blocked["notice_batch"] == []
    assert coordinator.has_pending_notice("session-a") is False

    _deliver(coordinator, "request-a", "llm_output_completed")
    second = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="request-c",
    )

    assert [notice["activity_id"] for notice in second["notice_batch"]] == [
        "cognitive-b"
    ]


def test_rollback_restores_old_terminal_without_dropping_new_terminal() -> None:
    """旧请求回滚后仍应先领取旧终态，新终态继续留在来源车道。"""

    coordinator = NoticeDeliveryCoordinator(monotonic_now=lambda: 1.0)
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _apply(coordinator, _terminal(activity_id="task_result-b", source="task_result"))

    _deliver(coordinator, "request-a", "delivery_cancelled")
    rebuilt = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert [notice["activity_id"] for notice in rebuilt["notice_batch"]] == [
        "task_result-a",
    ]

    _deliver(coordinator, "request-b", "llm_output_completed")
    later = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-c",
    )

    assert [notice["activity_id"] for notice in later["notice_batch"]] == [
        "task_result-b",
    ]


def test_rollback_terminal_keeps_independent_same_source_progress() -> None:
    """旧终态回滚后先占车道，另一个活动的弱进度继续等待。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _apply(coordinator, _progress(activity_id="cognitive-b", source="cognitive", sequence=1))

    _deliver(coordinator, "request-a", "delivery_cancelled")
    rebuilt = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert [(notice["intent"], notice["activity_id"]) for notice in rebuilt["notice_batch"]] == [
        ("cognitive_result", "cognitive-a"),
    ]

    _deliver(coordinator, "request-b", "llm_output_completed")
    later = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-c",
    )

    assert [(notice["intent"], notice["activity_id"]) for notice in later["notice_batch"]] == [
        ("progress_report", "cognitive-b"),
    ]


def test_progress_invalidation_removes_pending_and_marks_reserved_request() -> None:
    """正文开始应删除 pending 进度，并标记已预留请求需要条件取消。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    result = _apply(coordinator,
        ActivityProgressNoticeInvalidation(
            activity_id="cognitive-a",
            interaction_session_id="session-a",
            source="cognitive",
        )
    )

    assert result.superseded_request_ids == ("request-a",)


def test_same_activity_keeps_only_latest_strong_fact() -> None:
    """用户介入和终态应逐级淘汰同一 Activity 的旧提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _started(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(
        coordinator,
        _user_intervention(activity_id="cognitive-a", source="cognitive"),
    )
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator, _started(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=2))
    _apply(
        coordinator,
        _user_intervention(
            activity_id="cognitive-a",
            source="cognitive",
            lifecycle="attention",
        ),
    )

    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    assert [(notice["intent"], notice["activity_id"]) for notice in context["notice_batch"]] == [
        ("cognitive_result", "cognitive-a"),
    ]


def test_user_intervention_supersedes_same_activity_progress() -> None:
    """尚未领取的弱进度在需要用户介入后不再具有表达价值。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(
        coordinator,
        _user_intervention(activity_id="cognitive-a", source="cognitive"),
    )

    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    assert [notice["intent"] for notice in context["notice_batch"]] == [
        "information_request"
    ]


def test_user_intervention_can_attach_while_superseded_progress_request_closes() -> None:
    """交互请求使用独立车道，可附着用户轮且同时取消旧进度播报。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    result = _apply(
        coordinator,
        _user_intervention(activity_id="cognitive-a", source="cognitive"),
    )
    blocked = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="request-b",
    )

    assert result.superseded_request_ids == ("request-a",)
    assert [notice["intent"] for notice in blocked["notice_batch"]] == [
        "information_request"
    ]

    _deliver(coordinator, "request-a", "superseded")
    later = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="request-c",
    )
    assert later["notice_batch"] == []


def test_superseded_strong_fact_cannot_revive_on_cancel_or_protocol_failure() -> None:
    """旧介入提醒被 terminal 替换后，任何异常补偿都不能让它复活。"""

    outcomes: tuple[NoticeDeliveryOutcome, ...] = (
        "delivery_cancelled",
        "provider_protocol_failed",
    )
    for outcome in outcomes:
        coordinator = NoticeDeliveryCoordinator(max_provider_protocol_retries=1)
        _apply(
            coordinator,
            _user_intervention(activity_id="cognitive-a", source="cognitive"),
        )
        coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="request-a",
        )

        result = _apply(
            coordinator,
            _terminal(activity_id="cognitive-a", source="cognitive"),
        )
        assert result.superseded_request_ids == ("request-a",)

        _deliver(coordinator, "request-a", outcome)
        terminal_context = coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="request-b",
        )
        assert [notice["intent"] for notice in terminal_context["notice_batch"]] == [
            "cognitive_result"
        ]

        _deliver(coordinator, "request-b", "llm_output_completed")
        assert coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="request-c",
        )["notice_batch"] == []


def test_duplicate_terminal_event_is_not_delivered_twice() -> None:
    """来源侧重复投递同一终态时，不得制造第二次强提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    terminal = _terminal(activity_id="task_result-a", source="task_result")
    _apply(coordinator, terminal)
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _deliver(coordinator, "request-a", "llm_output_completed")

    _apply(coordinator, terminal)
    second = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert second["notice_batch"] == []


def test_cancelled_terminal_emits_confirmation_notice_and_supersedes_progress() -> None:
    """用户取消的终态同样产生极简确认强提醒，并废止同活动迟到进度。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))

    _apply(coordinator,
        _terminal(
            activity_id="cognitive-a",
            source="cognitive",
            terminal_state="cancelled",
        )
    )
    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    assert [(notice["intent"], notice["terminal_state"]) for notice in context["notice_batch"]] == [
        ("terminal_status", "cancelled")
    ]


def test_silent_correction_terminal_cleans_activity_without_notice() -> None:
    """纠正取消必须封住旧活动并淘汰候选，但不能再排一条普通取消提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _started(activity_id="cognitive-a", source="cognitive"))
    _apply(
        coordinator,
        _progress(activity_id="cognitive-a", source="cognitive", sequence=1),
    )
    _apply(
        coordinator,
        _user_intervention(activity_id="cognitive-a", source="cognitive"),
    )
    _apply(
        coordinator,
        ActivityLifecycleEvent(
            lifecycle="waiting_user",
            activity_id="cognitive-a",
            interaction_session_id="session-a",
            source="cognitive",
            category="认知任务",
            goal="目标-cognitive-a",
            intent="permission_request",
            interaction_request_id="permission-cognitive-a",
            action_summary="运行完成这项工作所需的命令",
            grant_scope="activity",
        ),
    )
    result = _apply(
        coordinator,
        _terminal(
            activity_id="cognitive-a",
            source="cognitive",
            terminal_state="cancelled",
            suppress_notice=True,
        ),
    )

    key = (RUNTIME_EPOCH, "session-a", "cognitive", "cognitive-a")
    assert key in coordinator._state.terminal_activity_keys  # noqa: SLF001
    assert key not in coordinator._state.active_activities_by_key  # noqa: SLF001
    assert result.superseded_request_ids == ()
    assert coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-after-correction",
    )["notice_batch"] == []

    coordinator.interrupt_active(RUNTIME_EPOCH, "notify")
    assert coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-after-runtime-interrupt",
    )["notice_batch"] == []
    assert coordinator.has_pending_notice("session-a") is False


def test_duplicate_cancelled_terminal_is_not_delivered_twice() -> None:
    """同一取消终态重复投递时，不得制造第二次确认提醒。"""

    coordinator = NoticeDeliveryCoordinator()
    terminal = _terminal(
        activity_id="task_result-a",
        source="task_result",
        terminal_state="cancelled",
    )
    _apply(coordinator, terminal)
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _deliver(coordinator, "request-a", "llm_output_completed")

    _apply(coordinator, terminal)
    second = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert second["notice_batch"] == []


def test_notice_batch_includes_one_notice_from_every_source() -> None:
    """批次应从每个来源领取一条，不再设置独立的总数量上限。"""

    coordinator = NoticeDeliveryCoordinator()
    for number in range(4):
        _apply(coordinator,
            _terminal(activity_id=f"activity-{number}", source=f"source-{number}")
        )

    first = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    assert len(first["notice_batch"]) == 4
    _deliver(coordinator, "request-a", "llm_output_completed")
    second = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert second["notice_batch"] == []


def test_provider_protocol_failure_drops_weak_progress_immediately() -> None:
    """弱进度发生厂商协议泄漏后不得重新进入候选队列。"""

    coordinator = NoticeDeliveryCoordinator(max_provider_protocol_retries=1)
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    _deliver(coordinator, "request-a", "provider_protocol_failed")

    assert coordinator.has_pending_notice("session-a") is False


def test_provider_protocol_failure_retries_strong_notice_once_then_drops_it() -> None:
    """强提醒只允许一次自然重试，第二次协议泄漏后必须删除。"""

    coordinator = NoticeDeliveryCoordinator(max_provider_protocol_retries=1)
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    _deliver(coordinator, "request-a", "provider_protocol_failed")
    retry = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )
    assert [notice["activity_id"] for notice in retry["notice_batch"]] == ["cognitive-a"]

    _deliver(coordinator, "request-b", "provider_protocol_failed")

    assert coordinator.has_pending_notice("session-a") is False


def test_protocol_retry_keeps_old_terminal_ahead_of_later_same_source_terminal() -> None:
    """旧终态重试期间必须继续占据来源车道，直到重试预算耗尽。"""

    coordinator = NoticeDeliveryCoordinator(
        monotonic_now=lambda: 1.0,
        max_provider_protocol_retries=1,
    )
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator, _terminal(activity_id="cognitive-b", source="cognitive"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    _deliver(coordinator, "request-a", "provider_protocol_failed")
    retry = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )
    assert [notice["activity_id"] for notice in retry["notice_batch"]] == [
        "cognitive-a"
    ]

    _deliver(coordinator, "request-b", "provider_protocol_failed")
    later = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-c",
    )
    assert [notice["activity_id"] for notice in later["notice_batch"]] == [
        "cognitive-b"
    ]


def test_provider_protocol_failure_retries_strong_before_pending_weak_notice() -> None:
    """强请求协议失败时只重试强提醒，尚未领取的弱进度继续等待。"""

    coordinator = NoticeDeliveryCoordinator(max_provider_protocol_retries=1)
    _apply(coordinator, _progress(activity_id="cognitive-a", source="cognitive", sequence=1))
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    first = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    assert [notice["intent"] for notice in first["notice_batch"]] == [
        "work_result"
    ]

    _deliver(coordinator, "request-a", "provider_protocol_failed")
    retry = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    assert [(notice["intent"], notice["activity_id"]) for notice in retry["notice_batch"]] == [
        ("work_result", "task_result-a")
    ]

    _deliver(coordinator, "request-b", "llm_output_completed")
    weak = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-c",
    )
    assert [(notice["intent"], notice["activity_id"]) for notice in weak["notice_batch"]] == [
        ("progress_report", "cognitive-a")
    ]

    _deliver(coordinator, "request-c", "provider_protocol_failed")
    assert coordinator.has_pending_notice("session-a") is False


def test_normal_cancellation_does_not_consume_protocol_retry_budget() -> None:
    """普通取消后的首次协议泄漏仍应保留强提醒的一次重试机会。"""

    coordinator = NoticeDeliveryCoordinator(max_provider_protocol_retries=1)
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    _deliver(coordinator, "request-a", "delivery_cancelled")
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )

    _deliver(coordinator, "request-b", "provider_protocol_failed")
    retry = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-c",
    )

    assert [notice["activity_id"] for notice in retry["notice_batch"]] == ["cognitive-a"]


def test_concurrent_system_notice_reservation_has_one_session_winner() -> None:
    """同一会话并发准备时只能有一个 System Notice 请求取得批次。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    barrier = Barrier(2)

    def reserve(request_id: str) -> tuple[str, list[NoticeSnapshot]]:
        """在两个线程同时起跑后尝试预留提醒批次。"""

        barrier.wait()
        context = coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id=request_id,
        )
        return request_id, context["notice_batch"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, ("request-a", "request-b")))

    assert sorted(len(batch) for _, batch in results) == [0, 1]
    winner_request_id = next(request_id for request_id, batch in results if batch)
    _deliver(coordinator, winner_request_id, "request_enqueue_failed")

    retry = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-c",
    )
    assert [notice["activity_id"] for notice in retry["notice_batch"]] == [
        "task_result-a"
    ]


def test_concurrent_user_requests_cannot_bypass_same_source_fifo() -> None:
    """两个用户请求并发准备时，同一来源仍只能有一个请求取得旧终态。"""

    coordinator = NoticeDeliveryCoordinator(monotonic_now=lambda: 1.0)
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator, _terminal(activity_id="cognitive-b", source="cognitive"))
    barrier = Barrier(2)

    def reserve(request_id: str) -> tuple[str, list[NoticeSnapshot]]:
        """并发尝试把同一来源的强提醒附着到用户请求。

        Args:
            request_id: 本线程使用的助手请求编号。

        Returns:
            请求编号及其实际取得的 Notice 批次。
        """

        barrier.wait()
        context = coordinator.build_notice_context(
            "session-a",
            mode="user_request",
            request_id=request_id,
        )
        return request_id, context["notice_batch"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, ("request-a", "request-b")))

    assert sorted(len(batch) for _, batch in results) == [0, 1]
    winner_request_id, winner_batch = next(
        (request_id, batch) for request_id, batch in results if batch
    )
    assert [notice["activity_id"] for notice in winner_batch] == ["cognitive-a"]
    assert coordinator.has_pending_notice("session-a") is False

    _deliver(coordinator, winner_request_id, "llm_output_completed")
    assert coordinator.has_pending_notice("session-a") is True
    later = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="request-c",
    )
    assert [notice["activity_id"] for notice in later["notice_batch"]] == [
        "cognitive-b"
    ]


def test_late_enqueue_failure_cannot_rollback_enqueued_system_notice() -> None:
    """请求进入输出链路后，迟到的入队失败不得恢复或释放其批次。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )

    _deliver(coordinator, "request-a", "request_enqueued")
    _deliver(coordinator, "request-a", "request_enqueue_failed")

    assert coordinator.has_active_system_notice("session-a") is True
    assert coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-b",
    )["notice_batch"] == []

    _deliver(coordinator, "request-a", "llm_output_completed")
    assert coordinator.has_active_system_notice("session-a") is False
    assert coordinator.has_pending_notice("session-a") is False


def test_request_failure_does_not_release_another_session_output() -> None:
    """一个请求的永久失败不得释放另一个交互会话的输出占用。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="task_result-a", source="task_result"))
    _apply(
        coordinator,
        ActivityLifecycleEvent(
            lifecycle="terminal",
            activity_id="task_result-b",
            interaction_session_id="session-b",
            source="task_result",
            category="材料处理",
            goal="目标-task_result-b",
            intent="work_result",
            terminal_state="completed",
        ),
    )
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="request-a",
    )
    coordinator.build_notice_context(
        "session-b",
        mode="system_notice",
        request_id="request-b",
    )
    _deliver(coordinator, "request-a", "request_enqueued")
    _deliver(coordinator, "request-b", "request_enqueued")

    _deliver(coordinator, "request-a", "output_unavailable")

    assert coordinator.has_active_system_notice("session-a") is False
    assert coordinator.has_active_system_notice("session-b") is True
    assert coordinator.build_notice_context(
        "session-b",
        mode="system_notice",
        request_id="request-c",
    )["notice_batch"] == []

    _deliver(coordinator, "request-b", "llm_output_completed")
    assert coordinator.has_active_system_notice("session-b") is False


def test_results_and_parallel_interactions_share_one_strong_notice_batch() -> None:
    """结果、权限和资料问题都应进入同一强批次逐项表达。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _terminal(activity_id="cognitive-a", source="cognitive"))
    _apply(coordinator, _terminal(activity_id="task-result-a", source="task_result"))
    _apply(
        coordinator,
        ActivityLifecycleEvent(
            lifecycle="waiting_user",
            activity_id="background-a",
            interaction_session_id="session-a",
            source="background_agent",
            category="后台工作",
            goal="转换录音",
            intent="permission_request",
            interaction_request_id="permission-a",
            action_summary="运行完成这项工作所需的命令",
            grant_scope="activity",
        ),
    )
    _apply(
        coordinator,
        ActivityLifecycleEvent(
            lifecycle="waiting_user",
            activity_id="background-a",
            interaction_session_id="session-a",
            source="background_agent",
            category="后台工作",
            goal="转换录音",
            intent="information_request",
            interaction_request_id="question-a",
            questions=(
                {
                    "question": "输出什么格式？",
                    "header": "输出格式",
                    "options": [],
                    "multiSelect": False,
                },
            ),
        ),
    )

    context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="system-notice-a",
    )

    assert [notice["intent"] for notice in context["notice_batch"]] == [
        "cognitive_result",
        "work_result",
        "permission_request",
        "information_request",
    ]
    assert all(
        "interaction_request_id" not in notice
        for notice in context["notice_batch"]
    )
    assert coordinator.permission_interaction_request_ids("system-notice-a") == (
        "permission-a",
    )


def test_interaction_resolution_cancels_only_independent_system_notice() -> None:
    """解决交互应取消独立播报，但不能取消附着在真实用户轮的 generation。"""

    def interaction_event(request_id: str) -> ActivityLifecycleEvent:
        """构造一条测试权限请求。"""

        return ActivityLifecycleEvent(
            lifecycle="waiting_user",
            activity_id="background-a",
            interaction_session_id="session-a",
            source="background_agent",
            category="后台工作",
            goal="转换录音",
            intent="permission_request",
            interaction_request_id=request_id,
            action_summary="运行完成这项工作所需的命令",
            grant_scope="activity",
        )

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, interaction_event("permission-system"))
    coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="system-notice-a",
    )
    system_result = _apply(
        coordinator,
        ActivityInteractionNoticeInvalidation(
            activity_id="background-a",
            interaction_session_id="session-a",
            source="background_agent",
            interaction_request_id="permission-system",
        ),
    )

    assert system_result.superseded_request_ids == ("system-notice-a",)
    _deliver(coordinator, "system-notice-a", "superseded")

    _apply(coordinator, interaction_event("permission-user"))
    attached = coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="user-turn-a",
    )
    user_result = _apply(
        coordinator,
        ActivityInteractionNoticeInvalidation(
            activity_id="background-a",
            interaction_session_id="session-a",
            source="background_agent",
            interaction_request_id="permission-user",
        ),
    )

    assert [notice["intent"] for notice in attached["notice_batch"]] == [
        "permission_request"
    ]
    assert user_result.superseded_request_ids == ()


def test_terminal_fact_never_cancels_attached_user_generation() -> None:
    """后台终态替换附着提醒时，只废止提醒，不能返回真实用户请求编号。"""

    for terminal_state in ("completed", "failed", "cancelled"):
        coordinator = NoticeDeliveryCoordinator()
        _apply(coordinator, _started(activity_id="cognitive-a", source="cognitive"))
        _apply(
            coordinator,
            _user_intervention(activity_id="cognitive-a", source="cognitive"),
        )
        attached = coordinator.build_notice_context(
            "session-a",
            mode="user_request",
            request_id=f"user-turn-{terminal_state}",
        )

        result = _apply(
            coordinator,
            _terminal(
                activity_id="cognitive-a",
                source="cognitive",
                terminal_state=terminal_state,
                message="执行状态已经变化。",
            ),
        )

        assert attached["notice_batch"]
        assert result.superseded_request_ids == ()


def test_runtime_interruption_never_cancels_attached_user_generation() -> None:
    """运行时中断可废止附着提醒，但绝不能打断承载它的用户回复。"""

    coordinator = NoticeDeliveryCoordinator()
    _apply(coordinator, _started(activity_id="cognitive-a", source="cognitive"))
    _apply(
        coordinator,
        _user_intervention(activity_id="cognitive-a", source="cognitive"),
    )
    coordinator.build_notice_context(
        "session-a",
        mode="user_request",
        request_id="user-turn-a",
    )

    result = coordinator.interrupt_active(RUNTIME_EPOCH, "notify")

    assert result.superseded_request_ids == ()


def test_terminal_idempotency_ledger_is_permanent_for_runtime_lifetime() -> None:
    """终态去重身份在 Backend 运行期不能被淘汰，否则迟到事件会复活。"""

    coordinator = NoticeDeliveryCoordinator()
    for number in range(3):
        _apply(coordinator,
            _terminal(activity_id=f"task_result-{number}", source="task_result")
        )

    assert len(coordinator._state.terminal_activity_keys) == 3  # noqa: SLF001
