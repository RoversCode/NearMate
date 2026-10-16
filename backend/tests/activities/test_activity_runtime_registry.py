from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from companion.activities.runtime.failure_reporting import failure_from_reason
from companion.activities.runtime.models import (
    ActivityCompletionResult,
    ActivitySourceChange,
)
from companion.activities.runtime.registry import (
    ActivityInvariantViolation,
    ActivityNotFound,
    ActivityRuntimeRegistry,
    ActivityStateConflict,
)
from companion.activities.work_plan import AgentWorkPlan, AgentWorkPlanItem


_RUNTIME_ID = "00000000-0000-4000-8000-000000000099"


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def _change(
    activity_id: str,
    *,
    change: str = "started",
    kind: str = "task_result",
    runtime_epoch: int = 1,
    stage: str | None = None,
    goal: str = "整理资料",
) -> ActivitySourceChange:
    return ActivitySourceChange(
        change=change,  # type: ignore[arg-type]
        activity_id=activity_id,
        kind=kind,
        type_label="答案产物" if kind == "task_result" else "认知任务",
        control_source="task_result" if kind == "task_result" else "cognitive",
        runtime_epoch=runtime_epoch,
        goal=goal,
        stage=stage,  # type: ignore[arg-type]
        completion_result=(
            ActivityCompletionResult(
                display_content="完成结果",
                delivery_brief="任务已完成。",
                output_paths=("result.md",),
            )
            if change == "completed"
            else None
        ),
        failure=(
            failure_from_reason("empty_model_output", stage_code="generating_result")
            if change == "failed"
            else None
        ),
    )


def _uuid(number: int) -> str:
    return str(UUID(int=number))


def test_registry_applies_idempotent_lifecycle_and_terminal_absorbs_late_started() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)

    assert registry.apply(_change(activity_id)) is True
    assert registry.apply(_change(activity_id)) is False
    assert registry.desktop_snapshot().revision == 1

    assert registry.apply(_change(activity_id, change="completed")) is True
    assert registry.apply(_change(activity_id, change="completed")) is False
    assert registry.apply(_change(activity_id)) is False

    snapshot = registry.desktop_snapshot()
    assert snapshot.revision == 2
    assert snapshot.runtime_id == _RUNTIME_ID
    assert snapshot.active == ()
    assert snapshot.recent[0].status == "completed"
    assert registry.detail(activity_id).result_text == "完成结果"
    assert registry.completion_result(activity_id).delivery_brief == "任务已完成。"
    assert registry.completion_result(activity_id).output_paths == ("result.md",)


def test_started_automatically_opens_preparing_stage() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)

    registry.apply(_change(activity_id))

    detail = registry.detail(activity_id)
    assert detail.compact.stage_code == "preparing"
    assert detail.compact.stage_label == "准备任务"
    assert [period.stage_code for period in detail.stage_timeline] == ["preparing"]
    assert detail.stage_timeline[0].finished_at is None
    assert [entry.stage_code for entry in detail.stage_plan] == [
        "preparing",
        "loading_materials",
        "processing",
        "generating_result",
        "delivering_result",
    ]


def test_stage_advances_forward_and_closes_previous_period() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))
    revision_before = registry.desktop_snapshot().revision

    assert (
        registry.apply(_change(activity_id, change="stage_changed", stage="processing"))
        is True
    )

    detail = registry.detail(activity_id)
    assert detail.compact.stage_code == "processing"
    assert [period.stage_code for period in detail.stage_timeline] == [
        "preparing",
        "processing",
    ]
    assert detail.stage_timeline[0].finished_at is not None
    assert detail.stage_timeline[1].finished_at is None
    assert registry.desktop_snapshot().revision == revision_before + 1


def test_stage_may_skip_forward_for_material_free_tasks() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))

    assert (
        registry.apply(
            _change(activity_id, change="stage_changed", stage="generating_result")
        )
        is True
    )

    detail = registry.detail(activity_id)
    assert [period.stage_code for period in detail.stage_timeline] == [
        "preparing",
        "generating_result",
    ]


def test_repeated_same_stage_is_idempotent_without_revision() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))
    registry.apply(_change(activity_id, change="stage_changed", stage="processing"))
    revision_before = registry.desktop_snapshot().revision

    assert (
        registry.apply(_change(activity_id, change="stage_changed", stage="processing"))
        is False
    )

    assert registry.desktop_snapshot().revision == revision_before
    detail = registry.detail(activity_id)
    assert [period.stage_code for period in detail.stage_timeline] == [
        "preparing",
        "processing",
    ]


def test_backward_stage_is_rejected_as_invariant_violation() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))
    registry.apply(_change(activity_id, change="stage_changed", stage="processing"))

    with pytest.raises(ActivityInvariantViolation):
        registry.apply(_change(activity_id, change="stage_changed", stage="preparing"))
    with pytest.raises(ActivityInvariantViolation):
        registry.apply(_change(activity_id, change="stage_changed", stage=None))
    # 未知活动的阶段事件按迟到事实静默吸收，不产生公共变化。
    assert (
        registry.apply(_change(_uuid(2), change="stage_changed", stage="processing"))
        is False
    )


def test_terminal_closes_last_stage_and_first_terminal_wins() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))
    registry.apply(_change(activity_id, change="stage_changed", stage="generating_result"))

    assert registry.apply(_change(activity_id, change="failed")) is True

    detail = registry.detail(activity_id)
    assert detail.compact.status == "failed"
    assert detail.compact.stage_code == "generating_result"
    assert all(period.finished_at is not None for period in detail.stage_timeline)
    assert detail.failure is not None
    assert detail.failure.code == "invalid_provider_response"
    assert detail.result_text == ""

    # 迟到终态不可覆盖第一个合法终态。
    assert registry.apply(_change(activity_id, change="completed")) is False
    assert registry.detail(activity_id).compact.status == "failed"


def test_cognitive_business_failure_reason_reaches_public_detail() -> None:
    """Cognitive 自主失败的安全原因只进入详情，不进入列表摘要。"""

    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(2)
    registry.apply(_change(activity_id, kind="cognitive"))
    registry.apply(
        _change(
            activity_id,
            kind="cognitive",
            change="stage_changed",
            stage="generating_result",
        )
    )
    registry.apply(
        ActivitySourceChange(
            change="failed",
            activity_id=activity_id,
            kind="cognitive",
            type_label="事实查询",
            control_source="cognitive",
            runtime_epoch=1,
            goal="核对最新事实",
            failure=failure_from_reason(
                "cognitive_task_failed",
                stage_code="generating_result",
            ),
            failure_reason="必要网页来源无法读取。",
        )
    )

    detail = registry.detail(activity_id)
    assert detail.failure_reason == "必要网页来源无法读取。"
    assert detail.to_payload()["failure_reason"] == "必要网页来源无法读取。"
    assert "failure_reason" not in detail.compact.to_payload()


def test_cognitive_business_failure_requires_specific_reason() -> None:
    """缺少具体原因的 Cognitive 自主失败不能污染权威 Activity 详情。"""

    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(3)
    registry.apply(_change(activity_id, kind="cognitive"))
    registry.apply(
        _change(
            activity_id,
            kind="cognitive",
            change="stage_changed",
            stage="generating_result",
        )
    )

    with pytest.raises(ActivityInvariantViolation):
        registry.apply(
            ActivitySourceChange(
                change="failed",
                activity_id=activity_id,
                kind="cognitive",
                type_label="事实查询",
                control_source="cognitive",
                runtime_epoch=1,
                goal="核对最新事实",
                failure=failure_from_reason(
                    "cognitive_task_failed",
                    stage_code="generating_result",
                ),
            )
        )


def test_cancel_and_complete_race_keeps_a_single_terminal() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))
    registry.cancellation_target(activity_id)
    assert registry.mark_cancelling(activity_id) == "marked"

    assert registry.apply(_change(activity_id, change="cancelled")) is True
    assert registry.apply(_change(activity_id, change="completed")) is False

    detail = registry.detail(activity_id)
    assert detail.compact.status == "cancelled"
    assert detail.failure is None
    assert detail.result_text == ""


def test_cancelled_terminal_keeps_last_stage_without_failure_report() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))
    registry.apply(_change(activity_id, change="stage_changed", stage="processing"))

    assert registry.apply(_change(activity_id, change="cancelled")) is True

    detail = registry.detail(activity_id)
    assert detail.compact.stage_code == "processing"
    assert detail.failure is None
    assert all(period.finished_at is not None for period in detail.stage_timeline)


def test_failed_terminal_without_failure_report_is_rejected() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))

    change = _change(activity_id, change="failed")
    object.__setattr__(change, "failure", None)
    with pytest.raises(ActivityInvariantViolation):
        registry.apply(change)


def test_revision_only_increases_on_real_public_changes() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    assert registry.desktop_snapshot().revision == 0

    registry.apply(_change(activity_id))
    assert registry.desktop_snapshot().revision == 1

    # 重复 started、同阶段重复与迟到阶段事件都不产生公共变化。
    registry.apply(_change(activity_id))
    registry.apply(_change(activity_id, change="stage_changed", stage="processing"))
    assert registry.desktop_snapshot().revision == 2
    registry.apply(_change(activity_id, change="stage_changed", stage="processing"))
    assert registry.desktop_snapshot().revision == 2

    registry.apply(_change(activity_id, change="completed"))
    assert registry.desktop_snapshot().revision == 3
    registry.apply(_change(activity_id, change="stage_changed", stage="delivering_result"))
    assert registry.desktop_snapshot().revision == 3


def test_registry_creates_missing_terminal_and_bounds_recent_history() -> None:
    registry = ActivityRuntimeRegistry(
        runtime_id=_RUNTIME_ID,
        max_terminal_records=3,
        now=_Clock(),
    )

    for number in range(1, 6):
        assert registry.apply(_change(_uuid(number), change="failed")) is True

    snapshot = registry.console_snapshot()
    assert len(snapshot.recent) == 3
    assert [item.activity_id for item in snapshot.recent] == [
        _uuid(5),
        _uuid(4),
        _uuid(3),
    ]
    assert snapshot.recent[0].started_at is None
    assert snapshot.recent[0].finished_at is not None
    # 直接重建的终态记录没有阶段时间线，以故障报告所在阶段兜底展示。
    detail = registry.detail(_uuid(5))
    assert detail.stage_timeline == ()
    assert detail.compact.stage_code == "generating_result"


def test_registry_allows_multiple_active_activities_with_the_same_kind() -> None:
    """Registry 只投影事实，不替具体执行器实施并发容量政策。"""

    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    assert registry.apply(_change(_uuid(1), kind="background_agent")) is True
    assert registry.apply(_change(_uuid(2), kind="background_agent")) is True
    assert registry.apply(_change(_uuid(3), kind="cognitive")) is True

    snapshot = registry.desktop_snapshot()
    assert [item.activity_id for item in snapshot.active] == [
        _uuid(1),
        _uuid(2),
        _uuid(3),
    ]

    assert registry.apply(
        _change(_uuid(1), kind="background_agent", change="completed")
    ) is True
    assert [item.activity_id for item in registry.desktop_snapshot().active] == [
        _uuid(2),
        _uuid(3),
    ]


def test_registry_marks_cancelling_only_after_a_valid_cancel_target() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))

    target = registry.cancellation_target(activity_id)
    assert target.control_source == "task_result"
    assert registry.mark_cancelling(activity_id) == "marked"
    assert registry.desktop_snapshot().active[0].status == "cancelling"
    assert registry.desktop_snapshot().active[0].can_cancel is False

    with pytest.raises(ActivityStateConflict):
        registry.cancellation_target(activity_id)
    with pytest.raises(ActivityNotFound):
        registry.cancellation_target(_uuid(2))


def test_registry_reports_terminal_and_missing_cancellation_marks() -> None:
    """取消标记必须区分终态竞态与从未存在的活动。"""

    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))
    registry.apply(_change(activity_id, change="completed"))

    assert registry.mark_cancelling(activity_id) == "already_terminal"
    assert registry.mark_cancelling(_uuid(2)) == "missing"


def test_registry_interrupts_all_active_activities_in_one_revision() -> None:
    """运行时失效应原子关闭全部活动，并只推进一次公开版本。"""

    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    task_result_id = _uuid(1)
    cognitive_id = _uuid(2)
    registry.apply(_change(task_result_id, kind="task_result"))
    registry.apply(_change(cognitive_id, kind="cognitive"))
    registry.apply(
        _change(cognitive_id, kind="cognitive", change="stage_changed", stage="processing")
    )
    revision_before = registry.desktop_snapshot().revision

    interrupted = registry.interrupt_active(1, "runtime_reconfigured")
    assert len(interrupted) == 2
    assert {detail.compact.activity_id for detail in interrupted} == {
        task_result_id,
        cognitive_id,
    }

    snapshot = registry.desktop_snapshot()
    assert snapshot.revision == revision_before + 1
    assert snapshot.active == ()
    assert [item.activity_id for item in snapshot.recent] == [
        cognitive_id,
        task_result_id,
    ]
    assert all(item.status == "interrupted" for item in snapshot.recent)
    task_result_detail = registry.detail(task_result_id)
    assert task_result_detail.goal == "整理资料"
    assert task_result_detail.failure is not None
    assert task_result_detail.failure.code == "runtime_reconfigured"
    assert task_result_detail.failure.stage_code == "preparing"
    cognitive_detail = registry.detail(cognitive_id)
    assert cognitive_detail.failure is not None
    assert cognitive_detail.failure.code == "runtime_reconfigured"
    # 中断保留中断时最后所在阶段，并关闭最后阶段区间。
    assert cognitive_detail.compact.stage_code == "processing"
    assert all(
        period.finished_at is not None for period in cognitive_detail.stage_timeline
    )
    assert registry.interrupt_active(1, "runtime_reconfigured") == ()

    # 中断已释放 kind 约束，新运行时可以立即投影同类任务。
    assert registry.apply(_change(_uuid(3), kind="task_result", runtime_epoch=2)) is True
    # 旧运行时队列中的不同任务 started 也不得重新占用 kind。
    assert registry.apply(_change(_uuid(4), kind="cognitive", runtime_epoch=1)) is False


def test_registry_distinguishes_unexpected_runtime_interruption() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    registry.apply(_change(activity_id))

    assert len(registry.interrupt_active(1, "runtime_interrupted")) == 1

    detail = registry.detail(activity_id)
    assert detail.compact.status == "interrupted"
    assert detail.failure is not None
    assert detail.failure.code == "runtime_interrupted"
    assert "意外中断" in detail.failure.summary


def test_desktop_and_console_use_different_recent_limits() -> None:
    registry = ActivityRuntimeRegistry(
        runtime_id=_RUNTIME_ID,
        max_terminal_records=10,
        now=_Clock(),
    )
    for number in range(1, 7):
        registry.apply(_change(_uuid(number), change="cancelled"))

    assert len(registry.desktop_snapshot().recent) == 3
    assert len(registry.console_snapshot().recent) == 6


def test_goal_summary_is_truncated_but_full_goal_stays_in_detail() -> None:
    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(1)
    long_goal = "整理" * 200
    registry.apply(_change(activity_id, goal=long_goal))

    detail = registry.detail(activity_id)
    assert detail.goal == long_goal
    assert len(detail.compact.goal_summary) <= 80
    assert detail.compact.goal_summary.endswith("…")


def test_registry_saves_monotonic_work_plan_and_keeps_it_after_terminal() -> None:
    """Activity 保存最新计划，重复或倒退版本不改 revision，终态后继续可查。"""

    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(30)
    registry.apply(_change(activity_id, kind="background_agent"))
    plan = AgentWorkPlan(
        revision=1,
        items=(
            AgentWorkPlanItem(
                task_id="1",
                subject="理解参考项目",
                active_form="正在理解参考项目",
                status="in_progress",
                order=1,
            ),
        ),
    )
    plan_change = ActivitySourceChange(
        change="work_plan_changed",
        activity_id=activity_id,
        kind="background_agent",
        type_label="后台智能体",
        control_source="background_agent",
        runtime_epoch=1,
        goal="",
        work_plan=plan,
    )

    assert registry.apply(plan_change) is True
    revision = registry.console_snapshot().revision
    assert registry.apply(plan_change) is False
    assert registry.console_snapshot().revision == revision
    assert registry.detail(activity_id).work_plan == plan

    assert registry.apply(_change(activity_id, kind="background_agent", change="completed")) is True
    detail = registry.detail(activity_id)
    assert detail.work_plan == plan
    assert detail.to_payload()["work_plan"] == plan.to_payload()


def test_registry_keeps_background_turn_outputs_after_activity_terminal() -> None:
    """后台纯文本 Turn 在任务终态后仍可查询，并按序吸收重复事件。"""

    registry = ActivityRuntimeRegistry(runtime_id=_RUNTIME_ID, now=_Clock())
    activity_id = _uuid(31)
    registry.apply(_change(activity_id, kind="background_agent"))

    first_output = ActivitySourceChange(
        change="turn_output",
        activity_id=activity_id,
        kind="background_agent",
        type_label="后台工作",
        control_source="background_agent",
        runtime_epoch=1,
        goal="",
        turn_index=1,
        content_text="第一轮\n继续",
        created_at="2026-07-18T08:00:01+00:00",
    )
    second_output = ActivitySourceChange(
        change="turn_output",
        activity_id=activity_id,
        kind="background_agent",
        type_label="后台工作",
        control_source="background_agent",
        runtime_epoch=1,
        goal="",
        turn_index=2,
        content_text="第二轮",
        created_at="2026-07-18T08:00:02+00:00",
    )

    assert registry.apply(first_output) is True
    assert registry.apply(first_output) is False
    assert registry.apply(second_output) is True
    assert registry.apply(_change(activity_id, kind="background_agent", change="completed")) is True

    outputs = registry.detail(activity_id).turn_outputs
    assert [output.turn_index for output in outputs] == [1, 2]
    assert outputs[0].content_text == "第一轮\n继续"
