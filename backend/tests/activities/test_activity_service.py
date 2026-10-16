from __future__ import annotations

import logging
from uuid import uuid4

import pytest

from companion.activities.runtime.registry import (
    ActivityNotFound,
    ActivityRuntimeRegistry,
    ActivityStateConflict,
)
from companion.activities.runtime.service import ActivityRuntimeUnavailable, ActivityService
from companion.realtime.assistant_events import CancelActivityCommand


RUNTIME_EPOCH = 1
RUNTIME_ID = "00000000-0000-4000-8000-0000000000aa"
ACTIVITY_SERVICE_LOGGER = "companion.activities.runtime.service"


def test_service_logs_transition_with_readable_activity_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Activity 状态变化日志必须包含人能直接理解的动作与公开上下文。"""

    activity_id = str(uuid4())
    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=lambda _event: None,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    caplog.set_level(logging.INFO, logger=ACTIVITY_SERVICE_LOGGER)

    result = service.apply_source_event(
        "background_agent.started",
        {"activity_id": activity_id, "goal": "整理会议纪要"},
        RUNTIME_EPOCH,
    )

    assert result.disposition == "changed"
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "activity_transition"
    )
    assert record.structured_fields == {
        "action": "后台工作状态已更新为“运行中”，当前执行阶段为“准备任务”",
        "activity_id": activity_id,
        "activity_kind": "background_agent",
        "activity_type": "后台工作",
        "goal_summary": "整理会议纪要",
        "status": "running",
        "stage_code": "preparing",
        "stage_label": "准备任务",
        "failure_code": "",
        "failure_summary": "",
        "runtime_epoch": RUNTIME_EPOCH,
        "runtime_id": RUNTIME_ID,
        "revision": 1,
    }


def test_service_logs_ignored_stale_event_with_actionable_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """过期世代日志必须说明为何忽略事件及其关联 Activity。"""

    activity_id = str(uuid4())
    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=lambda _event: None,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH + 1,
    )
    caplog.set_level(logging.INFO, logger=ACTIVITY_SERVICE_LOGGER)

    result = service.apply_source_event(
        "background_agent.started",
        {"activity_id": activity_id},
        RUNTIME_EPOCH,
    )

    assert result.disposition == "absorbed"
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "activity_event_stale_epoch"
    )
    assert record.structured_fields == {
        "action": "忽略来源事件",
        "reason": "事件来自已失效的助手运行时，不能改写当前 Activity 状态",
        "activity_id": activity_id,
        "source_event_type": "background_agent.started",
        "received_runtime_epoch": RUNTIME_EPOCH,
        "active_runtime_epoch": RUNTIME_EPOCH + 1,
    }


def test_service_logs_rejected_contract_event_with_actionable_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """来源契约不合法时，日志必须写明拒绝原因和异常信息。"""

    activity_id = str(uuid4())
    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=lambda _event: None,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    caplog.set_level(logging.WARNING, logger=ACTIVITY_SERVICE_LOGGER)

    result = service.apply_source_event(
        "background_agent.stage_changed",
        {"activity_id": activity_id, "stage": "unknown"},
        RUNTIME_EPOCH,
    )

    assert result.disposition == "absorbed"
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "activity_event_rejected"
    )
    assert record.structured_fields["action"] == "拒绝并隔离来源事件"
    assert record.structured_fields["reason"] == (
        "事件包含非法阶段代码或故障报告，无法转换为 Activity 状态变化"
    )
    assert record.structured_fields["activity_id"] == activity_id
    assert record.structured_fields["source_event_type"] == "background_agent.stage_changed"
    assert record.structured_fields["exception_type"] == "ValueError"
    assert record.structured_fields["exception_message"] == "非法 Activity 阶段代码：'unknown'"


def test_service_logs_lifecycle_invariant_rejection_with_actionable_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """生命周期倒退时，日志必须说明权威状态因此受到保护。"""

    activity_id = str(uuid4())
    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=lambda _event: None,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    assert service.apply_source_event(
        "background_agent.started",
        {"activity_id": activity_id},
        RUNTIME_EPOCH,
    ).disposition == "changed"
    assert service.apply_source_event(
        "background_agent.stage_changed",
        {"activity_id": activity_id, "stage": "processing"},
        RUNTIME_EPOCH,
    ).disposition == "changed"
    caplog.set_level(logging.WARNING, logger=ACTIVITY_SERVICE_LOGGER)

    result = service.apply_source_event(
        "background_agent.stage_changed",
        {"activity_id": activity_id, "stage": "preparing"},
        RUNTIME_EPOCH,
    )

    assert result.disposition == "absorbed"
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "activity_event_rejected"
    )
    assert record.structured_fields["action"] == "拒绝并隔离来源事件"
    assert record.structured_fields["reason"] == (
        "事件会破坏 Activity 生命周期不变量，已保留当前权威状态"
    )
    assert record.structured_fields["activity_id"] == activity_id
    assert record.structured_fields["exception_type"] == "ActivityInvariantViolation"


def test_service_logs_duplicate_event_with_actionable_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重复来源事件日志必须说明不会再次广播的原因。"""

    activity_id = str(uuid4())
    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=lambda _event: None,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    payload = {"activity_id": activity_id}
    assert service.apply_source_event(
        "background_agent.started",
        payload,
        RUNTIME_EPOCH,
    ).disposition == "changed"
    caplog.set_level(logging.DEBUG, logger=ACTIVITY_SERVICE_LOGGER)

    result = service.apply_source_event("background_agent.started", payload, RUNTIME_EPOCH)

    assert result.disposition == "duplicate"
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "activity_event_duplicate"
    )
    assert record.structured_fields == {
        "action": "忽略重复来源事件",
        "reason": "相同 Activity 状态已经写入，重复事件不再次广播",
        "activity_id": activity_id,
        "activity_kind": "background_agent",
        "activity_change": "started",
        "source_event_type": "background_agent.started",
        "runtime_epoch": RUNTIME_EPOCH,
    }


@pytest.mark.asyncio
async def test_service_projects_once_and_broadcasts_desktop_snapshot() -> None:
    """来源事件只有首次改变公共事实时才产生可广播投影。"""

    activity_id = str(uuid4())
    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    payload = {
        "activity_id": activity_id,
        "goal": "总结资料",
    }

    result = service.apply_source_event("background_agent.started", payload, RUNTIME_EPOCH)
    assert result.disposition == "changed"
    duplicate = service.apply_source_event("background_agent.started", payload, RUNTIME_EPOCH)
    assert duplicate.disposition == "duplicate"
    assert broadcasts == []

    assert result.projection is not None
    await service.broadcast_projection(result.projection)

    assert len(broadcasts) == 1
    assert broadcasts[0]["type"] == "activities.snapshot_changed"
    snapshot_payload = broadcasts[0]["payload"]
    assert isinstance(snapshot_payload, dict)
    assert snapshot_payload["revision"] == 1
    assert snapshot_payload["runtime_id"] == RUNTIME_ID
    assert snapshot_payload["active"][0]["activity_id"] == activity_id
    assert snapshot_payload["active"][0]["stage_code"] == "preparing"


@pytest.mark.asyncio
async def test_delayed_projection_broadcast_uses_latest_revision() -> None:
    """延迟广播不得把已经提交的更高 revision 覆盖回旧快照。"""

    activity_id = str(uuid4())
    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    started_result = service.apply_source_event(
        "background_agent.started",
        {"activity_id": activity_id, "goal": "总结资料"},
        RUNTIME_EPOCH,
    )
    completed_result = service.apply_source_event(
        "background_agent.completed",
        {
            "activity_id": activity_id,
            "goal": "总结资料",
            "display_content": "完成正文",
            "delivery_brief": "资料已整理。",
        },
        RUNTIME_EPOCH,
    )
    assert started_result.projection is not None
    assert completed_result.projection is not None

    await service.broadcast_projection(started_result.projection)

    payload = broadcasts[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["revision"] == completed_result.projection.revision
    assert payload["active"] == []
    assert payload["recent"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_service_marks_cancelling_only_after_command_is_enqueued() -> None:
    """取消命令入队成功后才公开 cancelling，并禁止重复取消。"""

    activity_id = str(uuid4())
    submitted: list[CancelActivityCommand] = []
    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda command: submitted.append(command),
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
        monotonic_now=lambda: 42.0,
    )
    result = service.apply_source_event(
        "cognitive_task.started",
        {
            "activity_id": activity_id,
            "kind": "lookup_current_fact",
            "brief": "查询天气",
        },
        RUNTIME_EPOCH,
    )
    assert result.projection is not None
    await service.broadcast_projection(result.projection)

    await service.cancel(activity_id)

    assert submitted == [
        CancelActivityCommand(
            activity_id=activity_id,
            source="cognitive",
            reason="user_cancelled",
            issued_at=42.0,
        )
    ]
    assert service.detail(activity_id).compact.status == "cancelling"
    assert len(broadcasts) == 2
    with pytest.raises(ActivityStateConflict):
        await service.cancel(activity_id)


@pytest.mark.asyncio
async def test_submitted_cancel_is_accepted_when_completion_wins_race() -> None:
    """取消命令提交后并发完成时应幂等返回成功且不广播 cancelling。"""

    activity_id = str(uuid4())
    broadcasts: list[dict[str, object]] = []
    submitted: list[CancelActivityCommand] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    service: ActivityService

    def submit(command: CancelActivityCommand) -> None:
        submitted.append(command)
        result = service.apply_source_event(
            "background_agent.completed",
            {
                "activity_id": activity_id,
                "goal": "总结资料",
                "display_content": "完成正文",
                "delivery_brief": "资料已整理。",
            },
            RUNTIME_EPOCH,
        )
        assert result.disposition == "changed"

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=submit,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    started = service.apply_source_event(
        "background_agent.started",
        {"activity_id": activity_id, "goal": "总结资料"},
        RUNTIME_EPOCH,
    )
    assert started.projection is not None
    await service.broadcast_projection(started.projection)

    await service.cancel(activity_id)

    assert len(submitted) == 1
    assert service.detail(activity_id).compact.status == "completed"
    assert len(broadcasts) == 1


@pytest.mark.asyncio
async def test_submitted_cancel_is_accepted_after_terminal_record_is_evicted() -> None:
    """取消提交后的终态即使被 recent 上限淘汰，也不得把已接受请求改判为 404。"""

    activity_id = str(uuid4())
    later_activity_id = str(uuid4())
    submitted: list[CancelActivityCommand] = []

    async def broadcast(_event: dict[str, object]) -> None:
        return None

    service: ActivityService

    def submit(command: CancelActivityCommand) -> None:
        submitted.append(command)
        completed = service.apply_source_event(
            "background_agent.completed",
            {
                "activity_id": activity_id,
                "goal": "总结资料",
                "display_content": "完成正文",
                "delivery_brief": "资料已整理。",
            },
            RUNTIME_EPOCH,
        )
        assert completed.disposition == "changed"
        started = service.apply_source_event(
            "background_agent.started",
            {"activity_id": later_activity_id, "goal": "生成后续内容"},
            RUNTIME_EPOCH,
        )
        assert started.disposition == "changed"
        later_completed = service.apply_source_event(
            "background_agent.completed",
            {
                "activity_id": later_activity_id,
                "goal": "生成后续内容",
                "display_content": "后续正文",
                "delivery_brief": "后续内容已完成。",
            },
            RUNTIME_EPOCH,
        )
        assert later_completed.disposition == "changed"

    service = ActivityService(
        registry=ActivityRuntimeRegistry(
            runtime_id=RUNTIME_ID,
            max_terminal_records=1,
        ),
        broadcast_snapshot=broadcast,
        submit_cancel_command=submit,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    started = service.apply_source_event(
        "background_agent.started",
        {"activity_id": activity_id, "goal": "总结资料"},
        RUNTIME_EPOCH,
    )
    assert started.disposition == "changed"

    await service.cancel(activity_id)

    assert len(submitted) == 1
    with pytest.raises(ActivityNotFound):
        service.detail(activity_id)
    assert service.detail(later_activity_id).compact.status == "completed"


@pytest.mark.asyncio
async def test_service_does_not_publish_cancelling_when_runtime_is_unavailable() -> None:
    """子进程入队失败时保留 running，并映射为运行时不可用。"""

    activity_id = str(uuid4())

    async def broadcast(_event: dict[str, object]) -> None:
        return None

    def fail_submit(_command: CancelActivityCommand) -> None:
        raise RuntimeError("process stopped")

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=fail_submit,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    result = service.apply_source_event(
        "background_agent.started",
        {"activity_id": activity_id, "goal": "总结资料"},
        RUNTIME_EPOCH,
    )
    assert result.projection is not None
    await service.broadcast_projection(result.projection)

    with pytest.raises(ActivityRuntimeUnavailable):
        await service.cancel(activity_id)

    assert service.detail(activity_id).compact.status == "running"
    with pytest.raises(ActivityNotFound):
        await service.cancel(str(uuid4()))


@pytest.mark.asyncio
async def test_service_interrupts_active_activities_without_notice_side_effects() -> None:
    """运行时生命周期中断只广播一次 Activity 快照。"""

    activity_id = str(uuid4())
    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    result = service.apply_source_event(
        "background_agent.started",
        {"activity_id": activity_id, "goal": "总结资料"},
        RUNTIME_EPOCH,
    )
    assert result.projection is not None
    await service.broadcast_projection(result.projection)
    broadcasts.clear()

    assert await service.interrupt_active(RUNTIME_EPOCH, "runtime_interrupted") is True
    assert await service.interrupt_active(RUNTIME_EPOCH, "runtime_interrupted") is False

    detail = service.detail(activity_id)
    assert detail.compact.status == "interrupted"
    assert detail.failure is not None
    assert detail.failure.code == "runtime_interrupted"
    assert not hasattr(detail, "error_text")
    assert len(broadcasts) == 1
    assert broadcasts[0]["type"] == "activities.snapshot_changed"
