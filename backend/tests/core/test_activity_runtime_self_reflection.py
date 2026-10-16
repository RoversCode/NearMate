"""验证 Activity 事件边界会及时释放 Self-Reflection 来源快照。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from companion.activities.notice.delivery_coordinator import NoticeDeliveryCoordinator
from companion.activities.runtime.models import (
    ActivityApplyResult,
    ActivityProjection,
)
from companion.core.application.assistant_request_context_preparer import (
    AssistantRequestContextPreparer,
)
from companion.realtime.activity_runtime_event_handler import (
    ActivityRuntimeEventHandler,
)
from companion.realtime.assistant_events import RuntimeEvent
from companion.foreground_self_reflection.contracts import TakeoverReceipt


class _RecordingSelfReflectionService:
    """记录 Activity Handler 交给自我反思服务的 Cognitive 终态。"""

    def __init__(self, calls: list[str], *, recovery_taken: bool = True) -> None:
        """保存调用记录和本次恢复准入结果。

        Args:
            calls: 测试需要检查的调用顺序。
            recovery_taken: 失败是否已经被 Self-Reflection 接管。
        """

        self.calls = calls
        self.recovery_taken = recovery_taken

    def observe_cognitive_terminal(self, event: RuntimeEvent) -> bool:
        """记录非恢复终态并返回成功。

        Args:
            event: 已经应用到 Activity Registry 的 Cognitive 终态。

        Returns:
            固定返回真，表示测试替身已经消费该事实。
        """

        self.calls.append(f"terminal:{event.type}")
        return True

    def observe_cognitive_started(self, event: RuntimeEvent) -> None:
        """忽略测试中的 Cognitive 启动事实。

        Args:
            event: 已经应用到 Activity Registry 的 Cognitive 启动事件。
        """

        del event

    def observe_cognitive_failed(self, event: RuntimeEvent) -> TakeoverReceipt:
        """按测试指定结果声明失败是否被 Self-Reflection 接管。

        Args:
            event: 已经应用到 Activity Registry 的 Cognitive 失败事件。

        Returns:
            返回测试指定的结构化接管回执。
        """

        self.calls.append(f"admitted:{event.type}")
        return TakeoverReceipt(
            status="accepted" if self.recovery_taken else "rejected",
            entry_output_accepted=self.recovery_taken,
            reason="test",
        )


async def test_completed_cognitive_releases_source_before_notice_mapping() -> None:
    """Cognitive 完成后先释放来源快照，再继续普通 Notice 映射。"""

    calls: list[str] = []

    async def ignore_public_event(_event: dict[str, object]) -> None:
        """提供测试不关心的公共事件端口。"""

    async def record_broadcast(_projection: ActivityProjection) -> None:
        """记录 Activity 快照先于终态清理完成广播。"""

        calls.append("activity_broadcast")

    handler = ActivityRuntimeEventHandler(
        publish_activity=ignore_public_event,
        publish_activity_ephemeral=ignore_public_event,
        publish_materials_ephemeral=ignore_public_event,
        activity_apply_handler=lambda _type, _payload, _epoch: ActivityApplyResult(
            disposition="changed",
            projection=ActivityProjection(revision=1),
        ),
        activity_broadcast_handler=record_broadcast,
        activity_notice_handler=lambda event_type, _payload, _epoch: calls.append(
            f"notice:{event_type}"
        ),
        self_reflection_service=_RecordingSelfReflectionService(calls),  # type: ignore[arg-type]
    )
    event = RuntimeEvent(
        type="cognitive_task.completed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={
            "activity_id": "cognitive-a",
            "interaction_session_id": "interaction-a",
        },
    )

    assert await handler.try_handle(event) is True
    assert calls == [
        "terminal:cognitive_task.completed",
        "activity_broadcast",
        "notice:cognitive_task.completed",
    ]


async def test_cognitive_terminal_cleanup_precedes_broadcast_failure() -> None:
    """公开快照广播失败时，来源终态仍先完成材料收口。"""

    calls: list[str] = []

    async def ignore_public_event(_event: dict[str, object]) -> None:
        """提供测试不关心的公共事件端口。"""

    async def fail_broadcast(_projection: ActivityProjection) -> None:
        """模拟控制连接广播失败。"""

        calls.append("activity_broadcast")
        raise RuntimeError("activity broadcast failed")

    handler = ActivityRuntimeEventHandler(
        publish_activity=ignore_public_event,
        publish_activity_ephemeral=ignore_public_event,
        publish_materials_ephemeral=ignore_public_event,
        activity_apply_handler=lambda _type, _payload, _epoch: ActivityApplyResult(
            disposition="changed",
            projection=ActivityProjection(revision=1),
        ),
        activity_broadcast_handler=fail_broadcast,
        self_reflection_service=_RecordingSelfReflectionService(calls),  # type: ignore[arg-type]
    )
    event = RuntimeEvent(
        type="cognitive_task.completed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={"activity_id": "cognitive-a"},
    )

    with pytest.raises(RuntimeError, match="activity broadcast failed"):
        await handler.try_handle(event)
    assert calls == ["terminal:cognitive_task.completed", "activity_broadcast"]


async def test_recovery_notice_is_suppressed_before_broadcast_failure() -> None:
    """恢复接管后即使公开广播失败，也不能遗留普通失败提醒。"""

    calls: list[str] = []

    async def ignore_public_event(_event: dict[str, object]) -> None:
        """提供测试不关心的公共事件端口。"""

    async def fail_broadcast(_projection: ActivityProjection) -> None:
        """模拟控制连接广播失败。"""

        calls.append("activity_broadcast")
        raise RuntimeError("activity broadcast failed")

    def record_notice(
        event_type: str,
        payload: dict[str, object],
        _runtime_epoch: int,
    ) -> None:
        """记录 Notice 抑制事实是否先于广播提交。"""

        calls.append(
            f"notice:{event_type}:suppress={payload.get('suppress_notice')}"
        )

    handler = ActivityRuntimeEventHandler(
        publish_activity=ignore_public_event,
        publish_activity_ephemeral=ignore_public_event,
        publish_materials_ephemeral=ignore_public_event,
        activity_apply_handler=lambda _type, _payload, _epoch: ActivityApplyResult(
            disposition="changed",
            projection=ActivityProjection(revision=1),
        ),
        activity_broadcast_handler=fail_broadcast,
        activity_notice_handler=record_notice,
        self_reflection_service=_RecordingSelfReflectionService(calls),  # type: ignore[arg-type]
    )
    event = RuntimeEvent(
        type="cognitive_task.failed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={"activity_id": "cognitive-a"},
    )

    with pytest.raises(RuntimeError, match="activity broadcast failed"):
        await handler.try_handle(event)
    assert calls == [
        "admitted:cognitive_task.failed",
        "notice:cognitive_task.failed:suppress=True",
        "activity_broadcast",
    ]


async def test_infrastructure_failure_keeps_normal_failure_notice() -> None:
    """基础设施故障未被恢复接管时必须继续走普通 Notice 路径。"""

    calls: list[str] = []

    async def ignore_public_event(_event: dict[str, object]) -> None:
        """提供测试不关心的公共事件端口。"""

    async def record_broadcast(_projection: ActivityProjection) -> None:
        """记录公开 Activity 已经先于 Notice 广播。"""

        calls.append("activity_broadcast")

    handler = ActivityRuntimeEventHandler(
        publish_activity=ignore_public_event,
        publish_activity_ephemeral=ignore_public_event,
        publish_materials_ephemeral=ignore_public_event,
        activity_apply_handler=lambda _type, _payload, _epoch: ActivityApplyResult(
            disposition="changed",
            projection=ActivityProjection(revision=1),
        ),
        activity_broadcast_handler=record_broadcast,
        activity_notice_handler=lambda event_type, _payload, _epoch: calls.append(
            f"notice:{event_type}"
        ),
        self_reflection_service=_RecordingSelfReflectionService(
            calls,
            recovery_taken=False,
        ),  # type: ignore[arg-type]
    )

    assert await handler.try_handle(
        RuntimeEvent(
            type="cognitive_task.failed",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "activity_id": "cognitive-a",
                "failure": {"code": "provider_unavailable"},
            },
        )
    ) is True
    assert calls == [
        "admitted:cognitive_task.failed",
        "activity_broadcast",
        "notice:cognitive_task.failed",
    ]


async def test_cognitive_notice_write_failure_is_replayed_after_registry_duplicate() -> None:
    """Notice 首次写入失败时，重复来源事件仍会重试而不是永久丢失。"""

    attempts = 0
    applied = False

    async def ignore_public_event(_event: dict[str, object]) -> None:
        """提供测试不关心的公共事件端口。"""

    async def ignore_activity_broadcast(_projection: ActivityProjection) -> None:
        """提供测试不关心的快照广播端口。"""

    def apply(
        _event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        nonlocal applied
        if applied:
            return ActivityApplyResult("absorbed")
        applied = True
        return ActivityApplyResult(
            "changed",
            ActivityProjection(revision=1),
        )

    def notice(
        _event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("notice store temporarily unavailable")

    handler = ActivityRuntimeEventHandler(
        publish_activity=ignore_public_event,
        publish_activity_ephemeral=ignore_public_event,
        publish_materials_ephemeral=ignore_public_event,
        activity_apply_handler=apply,
        activity_broadcast_handler=ignore_activity_broadcast,
        activity_notice_handler=notice,
    )
    event = RuntimeEvent(
        type="cognitive_task.failed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={"activity_id": "cognitive-a"},
    )

    with pytest.raises(RuntimeError, match="notice store temporarily unavailable"):
        await handler.try_handle(event)
    assert attempts == 1

    assert await handler.try_handle(event) is True
    assert attempts == 2


async def test_admitted_failure_closes_notice_without_creating_false_interruption() -> None:
    """恢复接管业务失败后静默关闭原 Notice，热替换不能再伪造 interrupted。"""

    calls: list[str] = []
    coordinator = NoticeDeliveryCoordinator()
    preparer = AssistantRequestContextPreparer(
        hub=cast(Any, None),
        realtime_input_runtime_owner=cast(Any, None),
        notice_coordinator=coordinator,
    )

    async def ignore_public_event(_event: dict[str, object]) -> None:
        """提供测试不关心的公共事件端口。"""

    async def ignore_activity_broadcast(_projection: ActivityProjection) -> None:
        """提供与 Activity 应用端口成对出现的广播端口。"""

    handler = ActivityRuntimeEventHandler(
        publish_activity=ignore_public_event,
        publish_activity_ephemeral=ignore_public_event,
        publish_materials_ephemeral=ignore_public_event,
        activity_apply_handler=lambda _type, _payload, _epoch: ActivityApplyResult(
            disposition="changed",
            projection=ActivityProjection(revision=1),
        ),
        activity_broadcast_handler=ignore_activity_broadcast,
        activity_notice_handler=preparer.handle_activity_event,
        self_reflection_service=_RecordingSelfReflectionService(calls),  # type: ignore[arg-type]
    )
    base_payload = {
        "activity_id": "cognitive-a",
        "interaction_session_id": "interaction-a",
        "category": "事实核对",
        "brief": "核对一项事实",
    }

    assert await handler.try_handle(
        RuntimeEvent(
            type="cognitive_task.started",
            source="assistant_runtime",
            runtime_epoch=7,
            payload=dict(base_payload),
        )
    ) is True
    notice_key = (7, "interaction-a", "cognitive", "cognitive-a")
    assert notice_key in coordinator._state.active_activities_by_key  # noqa: SLF001
    failed_payload = {**base_payload, "failure_code": "cognitive_task_failed"}
    failed_event = RuntimeEvent(
        type="cognitive_task.failed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload=failed_payload,
    )

    assert await handler.try_handle(failed_event) is True
    assert notice_key not in coordinator._state.active_activities_by_key  # noqa: SLF001
    interruption = coordinator.interrupt_active(7, "notify")

    assert failed_event.payload == failed_payload
    assert interruption.superseded_request_ids == ()
    assert coordinator.build_notice_context(
        "interaction-a",
        mode="preview",
    )["notice_batch"] == []
    assert coordinator._state.active_activities_by_key == {}  # noqa: SLF001
    session_notices = (
        notice
        for session in coordinator._state.sessions_by_id.values()  # noqa: SLF001
        for notice in session.notices_by_id.values()
    )
    assert all(notice.intent != "terminal_status" for notice in session_notices)
