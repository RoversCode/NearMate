from __future__ import annotations

import pytest

from companion.background_agent.contracts import AgentMaterial
from companion.foreground_tools.activity_correction.contracts import (
    CorrectionCancellationOutcome,
    CorrectionEvidence,
    CorrectionReservation,
)
from companion.foreground_tools.activity_correction.service import ActivityCorrectionService
from companion.foreground_tools.cognitive.contracts import CognitiveExecutionPlan, CognitiveGoal
from companion.materials.foreground_images import VisualEvidence
from companion.foreground_tools.orchestration import ToolFollowupFact
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)


class _FakeExecutor:
    """模拟可纠正源和目标执行器的最小事务接口。"""

    def __init__(
        self,
        *,
        correctable_ids: set[str] | None = None,
        reserve_succeeds: bool = True,
        cleanup_succeeds: bool = True,
        cancellation_outcome: CorrectionCancellationOutcome | None = None,
        evidence: CorrectionEvidence | None = None,
        cancel_error: Exception | None = None,
        start_succeeds: bool = True,
        prepared_event_has_activity_id: bool = True,
    ) -> None:
        self.correctable_ids = set(correctable_ids or set())
        self.reserve_succeeds = reserve_succeeds
        self.cleanup_succeeds = cleanup_succeeds
        self.cancellation_outcome = cancellation_outcome
        self.evidence = evidence or CorrectionEvidence()
        self.cancel_error = cancel_error
        self.start_succeeds = start_succeeds
        self.prepared_event_has_activity_id = prepared_event_has_activity_id
        self.cancelled: list[str] = []
        self.reserved: list[dict[str, object]] = []
        self.started: list[str] = []
        self.rolled_back: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.reserved_ids: set[str] = set()
        self.committed_ids: set[str] = set()

    def is_activity_correctable(self, activity_id: str) -> bool:
        return activity_id in self.correctable_ids

    def cancel_for_correction(
        self,
        activity_id: str,
        *,
        timeout: float = 5.0,
    ) -> CorrectionCancellationOutcome:
        del timeout
        if self.cancel_error is not None:
            raise self.cancel_error
        if activity_id not in self.correctable_ids:
            return "activity_no_longer_correctable"
        if self.cancellation_outcome == "cancel_not_accepted":
            return "cancel_not_accepted"
        self.cancelled.append(activity_id)
        outcome = self.cancellation_outcome or (
            "cleaned" if self.cleanup_succeeds else "cleanup_timeout"
        )
        if outcome == "cleaned":
            self.correctable_ids.discard(activity_id)
        return outcome

    def reserve_correction(self, **kwargs: object) -> CorrectionReservation:
        self.reserved.append(kwargs)
        if not self.reserve_succeeds:
            return CorrectionReservation(
                accepted=False,
                events=({"type": "rejected", "payload": {"reason": "target_busy"}},),
            )
        self.reserved_ids.add("new-id")
        prepared_payload = (
            {"activity_id": "new-id"}
            if self.prepared_event_has_activity_id
            else {}
        )
        return CorrectionReservation(
            accepted=True,
            activity_id="new-id",
            events=({"type": "prepared", "payload": prepared_payload},),
        )

    def commit_reserved_correction(self, activity_id: str) -> bool:
        if activity_id not in self.reserved_ids:
            return False
        self.committed_ids.add(activity_id)
        return True

    def start_reserved_correction(self, activity_id: str) -> bool:
        self.started.append(activity_id)
        self.reserved_ids.discard(activity_id)
        self.committed_ids.discard(activity_id)
        return self.start_succeeds

    def rollback_reserved_correction(self, activity_id: str) -> bool:
        self.rolled_back.append(activity_id)
        self.reserved_ids.discard(activity_id)
        self.committed_ids.discard(activity_id)
        return True

    def fail_reserved_correction(self, activity_id: str, *, reason: str) -> bool:
        self.failed.append((activity_id, reason))
        self.reserved_ids.discard(activity_id)
        self.committed_ids.discard(activity_id)
        return True

    def correction_evidence(self, activity_id: str) -> CorrectionEvidence:
        del activity_id
        return self.evidence


def _plan() -> CognitiveExecutionPlan:
    return CognitiveExecutionPlan(
        goals=(CognitiveGoal("call", "analyze_deeply", "新目标"),),
        reasoning="high",
        web_search=False,
        web_extract=False,
        images=(),
        source_urls=(),
        progress_policy="reasoning_driven",
        based_on_activity_id="old-id",
    )


def _active_context(
    source: str = "cognitive",
    *,
    interaction_session_id: str = "session",
) -> dict[str, object]:
    return {
        "activity_id": "old-id",
        "interaction_session_id": interaction_session_id,
        "source": source,
        "goal": "错误目标",
        "state": "running",
        "phase": "reasoning",
    }


def _commit(service: ActivityCorrectionService) -> ToolFollowupFact:
    """提交纠正事务并返回 after_commit 最终事实。

    Args:
        service: 已经登记新 Activity 预留的纠正服务。

    Returns:
        工具二轮必须向用户表达的最终纠正事实。
    """

    reservation = service.take_reservation("new-id")
    assert reservation is not None
    assert reservation.action.accept() is True
    assert reservation.action.commit_enqueued() is True
    assert reservation.action.run_after_commit() is True
    fact = reservation.action.after_commit_fact
    assert fact is not None
    return fact


def _prepare_cognitive(
    service: ActivityCorrectionService,
    **kwargs: object,
) -> tuple[bool, tuple[dict[str, object], ...]]:
    """调用结构化 Cognitive 纠正准备接口并返回断言字段。"""

    reservation = service.prepare_cognitive_correction(**kwargs)  # type: ignore[arg-type]
    return reservation.accepted, reservation.events


def _prepare_background(
    service: ActivityCorrectionService,
    **kwargs: object,
) -> tuple[bool, tuple[dict[str, object], ...]]:
    """调用结构化 Background 纠正准备接口并返回断言字段。"""

    reservation = service.prepare_background_correction(**kwargs)  # type: ignore[arg-type]
    return reservation.accepted, reservation.events


def test_prepare_does_not_cancel_old_activity_and_rollback_keeps_it_running() -> None:
    cognitive = _FakeExecutor(correctable_ids={"old-id"})
    service = ActivityCorrectionService(cognitive_executor=cognitive, background_executor=None)

    accepted, events = _prepare_cognitive(service,
        activity_id="old-id",
        interaction_session_id="session",
        plan=_plan(),
        source_user_text="不是旧目标，是新目标",
        parent_task_context=_active_context(),
    )

    assert accepted is True
    assert events[0]["payload"]["activity_id"] == "new-id"
    assert cognitive.cancelled == []
    assert service.rollback_prepared("new-id") is True
    assert cognitive.rolled_back == ["new-id"]
    assert cognitive.is_activity_correctable("old-id") is True


def test_service_uses_explicit_activity_id_when_prepared_event_has_no_identity() -> None:
    """准备事件可裁剪编号，服务仍应按结构化字段登记并回滚同一预留。"""

    cognitive = _FakeExecutor(
        correctable_ids={"old-id"},
        prepared_event_has_activity_id=False,
    )
    service = ActivityCorrectionService(
        cognitive_executor=cognitive,
        background_executor=None,
    )

    reservation = service.prepare_cognitive_correction(
        activity_id="old-id",
        interaction_session_id="session",
        plan=_plan(),
        source_user_text="纠正目标",
        parent_task_context=_active_context(),
    )

    assert reservation.accepted is True
    assert reservation.activity_id == "new-id"
    assert reservation.events[0]["payload"] == {}
    assert service.rollback_prepared("new-id") is True
    assert cognitive.rolled_back == ["new-id"]


def test_accepted_correction_reservation_requires_explicit_activity_id() -> None:
    """伪成功结果没有编号时，应在进入服务登记前直接违反契约。"""

    with pytest.raises(ValueError, match="必须包含 Activity 编号"):
        CorrectionReservation(accepted=True)


def test_background_correction_reservation_holds_commit_barrier() -> None:
    """Background correction 从 accept 到 rollback 必须保护 Executor 资源。"""

    barrier = BackgroundCommitDrainBarrier()
    background = _FakeExecutor(correctable_ids={"old-id"})
    service = ActivityCorrectionService(
        cognitive_executor=None,
        background_executor=background,
        background_commit_barrier=barrier,
    )
    reservation = service.prepare_background_correction(
        activity_id="old-id",
        interaction_session_id="session",
        goal="修正后的目标",
        deliverable=False,
        requirements="",
        materials=(),
        parent_task_context=_active_context(source="background_agent"),
    )
    assert reservation.accepted is True
    action_reservation = service.take_reservation("new-id")
    assert action_reservation is not None
    assert action_reservation.action.accept() is True
    assert barrier.begin_shutdown(timeout=0.0).drained is False

    assert action_reservation.action.rollback() is True
    assert barrier.wait_for_drain(timeout=0.1).drained is True


def test_commit_cancels_old_activity_before_starting_reserved_activity() -> None:
    cognitive = _FakeExecutor(correctable_ids={"old-id"})
    service = ActivityCorrectionService(cognitive_executor=cognitive, background_executor=None)
    accepted, _ = _prepare_cognitive(service,
        activity_id="old-id",
        interaction_session_id="session",
        plan=_plan(),
        source_user_text="改做新目标",
        parent_task_context=_active_context(),
    )
    assert accepted is True

    fact = _commit(service)

    assert cognitive.cancelled == ["old-id"]
    assert cognitive.started == ["new-id"]
    assert cognitive.failed == []
    assert fact == ToolFollowupFact(
        kind="activity_replaced",
        activity_id="new-id",
        old_goal="错误目标",
        new_goal="新目标",
    )


def test_accept_keeps_correction_rollbackable_until_event_is_enqueued() -> None:
    """generation accept 只接纳事件，不能提前冻结 B 或取消 A。"""

    cognitive = _FakeExecutor()
    background = _FakeExecutor(correctable_ids={"old-id"})
    service = ActivityCorrectionService(
        cognitive_executor=cognitive,
        background_executor=background,
    )
    accepted, _ = _prepare_background(service,
        activity_id="old-id",
        interaction_session_id="session",
        goal="正确目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context=_active_context("background_agent"),
    )
    assert accepted is True
    reservation = service.take_reservation("new-id")
    assert reservation is not None

    assert reservation.action.accept() is True
    assert background.committed_ids == set()
    assert reservation.action.rollback() is True

    assert cognitive.cancelled == []
    assert background.rolled_back == ["new-id"]
    assert background.started == []


def test_cross_type_prepare_is_rejected_before_reserving_target() -> None:
    cognitive = _FakeExecutor(correctable_ids={"old-id"})
    background = _FakeExecutor()
    service = ActivityCorrectionService(
        cognitive_executor=cognitive,
        background_executor=background,
    )

    accepted, events = _prepare_background(service,
        activity_id="old-id",
        interaction_session_id="session",
        goal="改做后台成果",
        deliverable=True,
        requirements="",
        materials=(
            AgentMaterial(
                material_id="m",
                display_name="资料",
                material_kind="dragged_file",
                media_class="text",
                local_path=None,
                node_kind="file",
            ),
        ),
        parent_task_context=_active_context(),
    )

    assert accepted is False
    assert cognitive.cancelled == []
    assert background.reserved == []
    assert events[0]["payload"]["reason"] == "correction_activity_not_found"


def test_target_reservation_failure_leaves_old_activity_running() -> None:
    cognitive = _FakeExecutor()
    background = _FakeExecutor(
        correctable_ids={"old-id"},
        reserve_succeeds=False,
    )
    service = ActivityCorrectionService(
        cognitive_executor=cognitive,
        background_executor=background,
    )

    accepted, _ = _prepare_background(service,
        activity_id="old-id",
        interaction_session_id="session",
        goal="新目标",
        deliverable=False,
        requirements="",
        materials=(),
        parent_task_context=_active_context("background_agent"),
    )

    assert accepted is False
    assert cognitive.cancelled == []
    assert background.is_activity_correctable("old-id") is True


def test_activity_that_finishes_before_commit_is_not_rewritten_as_cancelled() -> None:
    cognitive = _FakeExecutor(correctable_ids={"old-id"})
    service = ActivityCorrectionService(cognitive_executor=cognitive, background_executor=None)
    accepted, _ = _prepare_cognitive(service,
        activity_id="old-id",
        interaction_session_id="session",
        plan=_plan(),
        source_user_text="纠正",
        parent_task_context=_active_context(),
    )
    assert accepted is True
    cognitive.correctable_ids.clear()

    fact = _commit(service)

    assert cognitive.cancelled == []
    assert cognitive.started == []
    assert cognitive.failed == [
        ("new-id", "correction_activity_no_longer_correctable")
    ]
    assert fact.kind == "activity_replacement_failed"
    assert fact.safe_message == "旧任务已经结束，新任务没有开始。"


def test_cleanup_timeout_fails_new_activity_without_starting_it() -> None:
    cognitive = _FakeExecutor()
    background = _FakeExecutor(
        correctable_ids={"old-id"},
        cleanup_succeeds=False,
    )
    service = ActivityCorrectionService(
        cognitive_executor=cognitive,
        background_executor=background,
    )
    accepted, _ = _prepare_background(service,
        activity_id="old-id",
        interaction_session_id="session",
        goal="新目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context=_active_context("background_agent"),
    )
    assert accepted is True

    fact = _commit(service)

    assert background.cancelled == ["old-id"]
    assert background.started == []
    assert background.failed == [("new-id", "correction_cleanup_timeout")]
    assert fact.kind == "activity_replacement_failed"
    assert fact.safe_message == "旧任务仍在取消收尾，新任务没有开始。"


def test_new_task_start_rejection_reports_old_cancelled_and_new_not_started() -> None:
    """A 已清理但 B 的本地执行器拒绝时，不能误报纠正成功。"""

    cognitive = _FakeExecutor(
        correctable_ids={"old-id"},
        start_succeeds=False,
    )
    service = ActivityCorrectionService(
        cognitive_executor=cognitive,
        background_executor=None,
    )
    accepted, _ = _prepare_cognitive(
        service,
        activity_id="old-id",
        interaction_session_id="session",
        plan=_plan(),
        source_user_text="纠正",
        parent_task_context=_active_context(),
    )
    assert accepted is True

    fact = _commit(service)

    assert cognitive.cancelled == ["old-id"]
    assert fact.kind == "activity_replacement_failed"
    assert fact.safe_message == "旧任务已经取消，但新任务没有开始。"


def test_cancel_not_accepted_is_not_misreported_as_cleanup_timeout() -> None:
    """Runner 没接纳 interrupt 时，应报告取消失败而不是等待清理超时。"""

    background = _FakeExecutor(
        correctable_ids={"old-id"},
        cancellation_outcome="cancel_not_accepted",
    )
    service = ActivityCorrectionService(
        cognitive_executor=_FakeExecutor(),
        background_executor=background,
    )
    accepted, _ = _prepare_background(service,
        activity_id="old-id",
        interaction_session_id="session",
        goal="新目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context=_active_context("background_agent"),
    )
    assert accepted is True

    _commit(service)

    assert background.cancelled == []
    assert background.failed == [("new-id", "correction_cancel_failed")]


def test_cancel_exception_fails_committed_activity_without_escaping_after_commit() -> None:
    """A 的取消入口崩溃时，已提交 B 必须失败收口且不能悬挂事务。"""

    cognitive = _FakeExecutor()
    background = _FakeExecutor(
        correctable_ids={"old-id"},
        cancel_error=RuntimeError("旧执行器的事件循环已经关闭"),
    )
    service = ActivityCorrectionService(
        cognitive_executor=cognitive,
        background_executor=background,
    )
    accepted, _ = _prepare_background(service,
        activity_id="old-id",
        interaction_session_id="session",
        goal="新目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context=_active_context("background_agent"),
    )
    assert accepted is True

    _commit(service)

    assert background.started == []
    assert background.failed == [("new-id", "correction_cancel_failed")]


def test_terminal_activity_context_is_rejected_instead_of_derived() -> None:
    cognitive = _FakeExecutor()
    service = ActivityCorrectionService(cognitive_executor=cognitive, background_executor=None)

    accepted, events = _prepare_cognitive(service,
        activity_id="old-id",
        interaction_session_id="session",
        plan=_plan(),
        source_user_text="重做历史任务",
        parent_task_context={
            **_active_context(),
            "terminal_state": "completed",
        },
    )

    assert accepted is False
    assert events[0]["payload"]["reason"] == "correction_activity_not_found"
    assert cognitive.reserved == []


def test_cross_interaction_session_activity_is_rejected() -> None:
    """当前用户轮不能拿旧交互会话暴露的 Activity ID 执行纠正。"""

    cognitive = _FakeExecutor(correctable_ids={"old-id"})
    service = ActivityCorrectionService(cognitive_executor=cognitive, background_executor=None)

    accepted, events = _prepare_cognitive(service,
        activity_id="old-id",
        interaction_session_id="session-new",
        plan=_plan(),
        source_user_text="把旧会话任务改掉",
        parent_task_context=_active_context(interaction_session_id="session-old"),
    )

    assert accepted is False
    assert events[0]["payload"]["reason"] == "correction_activity_not_found"
    assert cognitive.reserved == []
    assert cognitive.cancelled == []


def test_cognitive_correction_inherits_parent_images_and_urls() -> None:
    """Cognitive→Cognitive 应保留父任务已冻结的图片和 URL 证据。"""

    cognitive = _FakeExecutor(
        correctable_ids={"old-id"},
        evidence=CorrectionEvidence(
            images=(VisualEvidence("material", "父图.png", "data:image/png;base64,parent"),),
            source_urls=("https://example.com/parent",),
        ),
    )
    service = ActivityCorrectionService(cognitive_executor=cognitive, background_executor=None)

    accepted, _ = _prepare_cognitive(service,
        activity_id="old-id",
        interaction_session_id="session",
        plan=_plan(),
        source_user_text="纠正",
        parent_task_context=_active_context(),
    )

    assert accepted is True
    reserved_plan = cognitive.reserved[0]["plan"]
    assert isinstance(reserved_plan, CognitiveExecutionPlan)
    assert reserved_plan.encoded_images == ("data:image/png;base64,parent",)
    assert reserved_plan.images[0].label == "父图.png"
    assert reserved_plan.source_urls == ("https://example.com/parent",)
    assert reserved_plan.web_extract is True
