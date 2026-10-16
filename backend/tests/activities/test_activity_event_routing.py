from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from uuid import uuid4

import pytest

from companion.activities.notice.delivery_coordinator import NoticeDeliveryCoordinator
from companion.activities.notice.event_mapper import map_activity_event
from companion.activities.runtime.failure_reporting import failure_from_reason
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.activities.runtime.registry import ActivityRuntimeRegistry
from companion.activities.runtime.service import ActivityService
from companion.background_agent.interaction_registry import (
    BackgroundAgentInteractionRegistry,
)
from companion.realtime.assistant_events import RuntimeEvent
from companion.runtime_memory.store import RuntimeMemoryStore
from backend.tests.runtime_public_event_support import runtime_event_router


RUNTIME_EPOCH = 1
RUNTIME_ID = "00000000-0000-4000-8000-0000000000bb"

from backend.tests.activities.runtime_event_router_test_support import (
    _event,
)

@pytest.mark.asyncio
async def test_internal_progress_events_reach_coordinator_without_desktop_broadcast() -> None:
    """两个来源的弱提醒与失效事件都只能进入内部协调器。"""

    broadcasts: list[dict[str, object]] = []
    activities: list[tuple[str, dict[str, object]]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_notice_handler=lambda event_type, payload, _runtime_epoch: activities.append(
            (event_type, payload)
        ),
    )

    internal_events = [
        _event(
            "background_agent.progress",
            {
                "activity_id": "task_result-a",
                "interaction_session_id": "session-a",
                "notice_seq": 1,
            },
        ),
        _event(
            "cognitive_task.progress",
            {
                "activity_id": "cognitive-a",
                "interaction_session_id": "session-a",
                "notice_seq": 2,
            },
        ),
        _event(
            "cognitive_task.progress_invalidated",
            {
                "activity_id": "cognitive-a",
                "interaction_session_id": "session-a",
            },
        ),
    ]
    for internal_event in internal_events:
        await event_router.dispatch(internal_event)

    assert broadcasts == []
    assert [event_type for event_type, _payload in activities] == [
        "background_agent.progress",
        "cognitive_task.progress",
        "cognitive_task.progress_invalidated",
    ]
    assert all("reasoning" not in str(payload) for _event_type, payload in activities)


@pytest.mark.asyncio
async def test_permission_granted_runtime_fact_reaches_media_session_port() -> None:
    """Gate 授权事实必须继续到达媒体会话，但不产生公共广播。"""

    broadcasts: list[dict[str, object]] = []
    media_session_events: list[tuple[str, dict[str, object], int]] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录不应出现的公共事件。

        Args:
            event: Router 尝试发布的公共事件。
        """

        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_notice_handler=lambda event_type, payload, runtime_epoch: (
            media_session_events.append((event_type, payload, runtime_epoch))
        ),
    )
    event = _event(
        "background_agent.permission_granted",
        {
            "activity_id": "background-a",
            "interaction_session_id": "session-a",
            "request_id": "permission-a",
            "grant_scope": "activity",
            "suppress_notice": False,
        },
    )

    await event_router.dispatch(event)

    assert broadcasts == []
    assert media_session_events == [
        (
            "background_agent.permission_granted",
            event.payload,
            RUNTIME_EPOCH,
        )
    ]


@pytest.mark.asyncio
async def test_task_result_tool_acceptance_is_consumed_without_public_broadcast() -> None:
    """工具接受事件只建立内部交付身份，不能泄漏请求和材料事务字段。"""

    broadcasts: list[dict[str, object]] = []
    retained: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    def retain(context_id: str) -> bool:
        retained.append(context_id)
        return True

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        turn_material_retain_handler=retain,
    )

    await event_router.dispatch(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "start_background_work",
            "deliverable": True,
            "state": "accepted",
            "activity_id": "task_result-internal",
            "tool_call_id": "tool-call-internal",
            "request_id": "request-internal",
            "source_user_message_id": 42,
            "turn_material_context_id": "context-internal",
        },
    ))

    assert retained == ["context-internal"]
    assert broadcasts == []


@pytest.mark.asyncio
async def test_self_reflection_commit_request_registers_canonical_before_approval() -> None:
    """Router 必须复用普通 Tool Fact 应用顺序，再允许 Service 回复批准。"""

    order: list[str] = []

    async def broadcast(_event: dict[str, object]) -> None:
        """本用例不应产生任何公共广播。"""

    def retain(_context_id: str) -> bool:
        """记录 TaskResult acceptance 已经越过材料登记边界。"""

        order.append("canonical_registered")
        return True

    async def handle_tool_commit_request(
        _event: RuntimeEvent,
        *,
        register_canonical_event: Callable[[RuntimeEvent], Awaitable[None]],
    ) -> bool:
        """模拟 Service 在 canonical 应用返回后才发送批准。"""

        order.append("request_received")
        await register_canonical_event(
            _event.__class__(
                type="llm.foreground.tool_call_completed",
                source="assistant_runtime",
                runtime_epoch=RUNTIME_EPOCH,
                produced_at=9.5,
                payload={
                    "name": "start_background_work",
                    "deliverable": True,
                    "state": "accepted",
                    "activity_id": "background-from-recovery",
                    "tool_call_id": "tool-call-recovery",
                    "request_id": "request-recovery",
                    "turn_material_context_id": "context-recovery",
                },
            )
        )
        order.append("approval_sent")
        return True

    service = SimpleNamespace(
        handle_tool_commit_request=handle_tool_commit_request,
        observe_tool_call_completed=lambda _event: False,
    )
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        turn_material_retain_handler=retain,
        self_reflection_service=service,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )

    await event_router.dispatch(
        _event(
            "foreground.self_reflection_tool_commit_requested",
            {
                "commit_request_id": "commit-request-1",
                "self_reflection_id": "self-reflection-1",
            },
        )
    )

    assert order == [
        "request_received",
        "canonical_registered",
        "approval_sent",
    ]


@pytest.mark.asyncio
async def test_interaction_resolved_restores_running_and_invalidates_notice() -> None:
    """资料回答必须恢复 Activity，并让 Notice 协调器废止同一请求。"""

    activity_id = str(uuid4())
    broadcasts: list[dict[str, object]] = []
    notice_results: list[tuple[str, tuple[str, ...]]] = []
    notice_coordinator = NoticeDeliveryCoordinator()

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    def apply_notice(
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> None:
        """把 Router 转来的来源事件真实写进 Notice 协调器并记录失效结果。"""

        mapped_event = map_activity_event(event_type, payload)
        assert mapped_event is not None
        result = notice_coordinator.apply_event(mapped_event, runtime_epoch)
        notice_results.append((event_type, result.superseded_request_ids))

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    interaction_registry = BackgroundAgentInteractionRegistry()
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=service.apply_source_event,
        activity_broadcast_handler=service.broadcast_projection,
        activity_notice_handler=apply_notice,
        background_agent_interaction_event_handler=(
            interaction_registry.apply_runtime_event
        ),
    )
    base_payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "整理采访",
        "deliverable": True,
        "created_at": 10.0,
    }
    await event_router.dispatch(_event("background_agent.started", base_payload))
    await event_router.dispatch(_event(
        "background_agent.interaction_requested",
        {
            **base_payload,
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "kind": "question",
            "tool_name": "AskUserQuestion",
            "input_data": {"questions": [{"question": "使用哪个版本？"}]},
            "questions": [{"question": "使用哪个版本？"}],
        },
    ))
    assert service.detail(activity_id).compact.status == "waiting_user"

    notice_context = notice_coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="system-notice-a",
    )
    assert notice_context["notice_batch"][0]["intent"] == "information_request"

    notice_results.clear()
    await event_router.dispatch(_event(
        "background_agent.interaction_resolved",
        {
            **base_payload,
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "outcome": "answered",
        },
    ))

    assert service.detail(activity_id).compact.status == "running"
    assert notice_results == [
        ("background_agent.interaction_resolved", ("system-notice-a",))
    ]
    interaction_events = [
        event
        for event in broadcasts
        if str(event["type"]).startswith("background_agent.interaction_")
    ]
    assert interaction_events == [
        {
            "type": "background_agent.interaction_requested",
            "payload": {
                "request_id": "123e4567-e89b-42d3-a456-426614174000",
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "kind": "question",
                "tool_name": "AskUserQuestion",
                "input_data": {"questions": [{"question": "使用哪个版本？"}]},
                "questions": [
                    {
                        "question": "使用哪个版本？",
                        "header": "",
                        "options": [],
                        "multiSelect": False,
                    }
                ],
            },
        },
        {
            "type": "background_agent.interaction_resolved",
            "payload": {
                "request_id": "123e4567-e89b-42d3-a456-426614174000",
                "activity_id": activity_id,
                "outcome": "answered",
            },
        },
    ]


@pytest.mark.asyncio
async def test_cognitive_raw_lifecycle_is_internal_but_activity_projection_is_public() -> None:
    """Cognitive 原始事件不广播，但必须先投影统一快照再更新提醒账本。"""

    broadcasts: list[dict[str, object]] = []
    timeline: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    def apply(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        timeline.append(f"apply:{event_type}")
        return ActivityApplyResult(
            "changed",
            ActivityProjection(revision=len(timeline)),
        )

    async def broadcast_activity(_projection: ActivityProjection) -> None:
        timeline.append("project")

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=apply,
        activity_broadcast_handler=broadcast_activity,
        activity_notice_handler=lambda event_type, _payload, _runtime_epoch: timeline.append(
            f"notice:{event_type}"
        ),
    )

    activity_id = str(uuid4())
    for event_type, terminal_fields in (
        ("cognitive_task.started", {}),
        (
            "cognitive_task.completed",
            {"result_text": "核对完成。", "summary": "结果正常。"},
        ),
        (
            "cognitive_task.failed",
            {
                "failure_reason": "现有方法无法完成。",
                    "failure": failure_from_reason(
                        "cognitive_task_failed",
                        stage_code="generating_result",
                    ).to_payload(),
            },
        ),
    ):
        await event_router.dispatch(_event(
            event_type,
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                **terminal_fields,
            },
        ))

    assert broadcasts == []
    assert timeline == [
        "apply:cognitive_task.started",
        "project",
        "notice:cognitive_task.started",
        "apply:cognitive_task.completed",
        "project",
        "notice:cognitive_task.completed",
        "apply:cognitive_task.failed",
        "project",
        "notice:cognitive_task.failed",
    ]


@pytest.mark.asyncio
async def test_cognitive_cancelled_terminal_converges_activity_and_task_memory() -> None:
    """Cognitive 执行器退出后必须从取消中收敛为已取消终态。

    Returns:
        无。
    """

    activity_id = str(uuid4())
    broadcasts: list[dict[str, object]] = []
    memory_store = RuntimeMemoryStore()

    async def broadcast(event: dict[str, object]) -> None:
        """记录 Desktop 实际接收的 Activity 权威快照。

        Args:
            event: Backend 准备广播给 Desktop 的公共事件。

        Returns:
            无。
        """

        broadcasts.append(event)

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=service.apply_source_event,
        activity_broadcast_handler=service.broadcast_projection,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
        strict_activity_identity=True,
        runtime_memory_store=memory_store,
    )
    activity_payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "kind": "lookup_current_fact",
        "brief": "核对最近发布的游戏",
    }
    await event_router.dispatch(_event("cognitive_task.started", activity_payload))

    await service.cancel(activity_id)

    assert service.desktop_snapshot().active[0].status == "cancelling"

    await event_router.dispatch(_event(
        "cognitive_task.cancelled",
        {
            **activity_payload,
            "failure": None,
            "cancellation_reason": "user_cancelled",
        },
    ))

    snapshot = service.desktop_snapshot()
    assert snapshot.active == ()
    assert snapshot.revision == 3
    assert snapshot.recent[0].activity_id == activity_id
    assert snapshot.recent[0].status == "cancelled"
    latest_broadcast = broadcasts[-1]["payload"]
    assert isinstance(latest_broadcast, dict)
    assert latest_broadcast["revision"] == 3
    assert latest_broadcast["active"] == []
    assert latest_broadcast["recent"][0]["status"] == "cancelled"
    recalled = memory_store.recall_task(activity_id, purpose="foreground_recall")
    assert recalled["terminal_state"] == "cancelled"
    assert recalled["cancellation_reason"] == "user_cancelled"


@pytest.mark.asyncio
async def test_task_result_stage_changed_only_projects_activity_snapshot() -> None:
    """TaskResult 阶段事件只做投影与快照广播，不公开原始事件、不生成提醒。"""

    broadcasts: list[dict[str, object]] = []
    timeline: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    def apply(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        timeline.append(f"apply:{event_type}")
        return ActivityApplyResult(
            "changed",
            ActivityProjection(revision=len(timeline)),
        )

    async def broadcast_activity(_projection: ActivityProjection) -> None:
        timeline.append("project")

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=apply,
        activity_broadcast_handler=broadcast_activity,
        activity_notice_handler=lambda event_type, _payload, _runtime_epoch: timeline.append(
            f"notice:{event_type}"
        ),
    )

    await event_router.dispatch(_event(
        "background_agent.stage_changed",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "stage": "processing",
            "created_at": 10.0,
        },
    ))

    assert broadcasts == []
    assert timeline == ["apply:background_agent.stage_changed", "project"]


@pytest.mark.asyncio
async def test_cognitive_stage_changed_flows_through_cognitive_branch() -> None:
    """Cognitive 阶段事件沿认知内部分支完成投影、快照与提醒账本同步。"""

    timeline: list[str] = []

    async def broadcast(_event: dict[str, object]) -> None:
        return None

    def apply(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        timeline.append(f"apply:{event_type}")
        return ActivityApplyResult(
            "changed",
            ActivityProjection(revision=len(timeline)),
        )

    async def broadcast_activity(_projection: ActivityProjection) -> None:
        timeline.append("project")

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=apply,
        activity_broadcast_handler=broadcast_activity,
        activity_notice_handler=lambda event_type, _payload, _runtime_epoch: timeline.append(
            f"notice:{event_type}"
        ),
    )

    await event_router.dispatch(_event(
        "cognitive_task.stage_changed",
        {
            "activity_id": "cognitive-a",
            "interaction_session_id": "session-a",
            "stage": "generating_result",
            "updated_at": 10.0,
        },
    ))

    assert timeline == [
        "apply:cognitive_task.stage_changed",
        "project",
        "notice:cognitive_task.stage_changed",
    ]

@pytest.mark.asyncio
async def test_terminal_projection_allows_next_same_kind_before_foreground_ack() -> None:
    """TaskResult 终态投影后应立即释放同类 Activity 占用。"""

    first_id = str(uuid4())
    second_id = str(uuid4())
    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=service.apply_source_event,
        activity_broadcast_handler=service.broadcast_projection,
    )
    await event_router.dispatch(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "start_background_work",
            "deliverable": True,
            "state": "accepted",
            "activity_id": first_id,
            "request_id": "request-first",
        },
    ))
    await event_router.dispatch(_event(
        "background_agent.started",
        {
            "activity_id": first_id,
            "interaction_session_id": "session-a",
            "goal": "第一项任务",
            "deliverable": True,
            "created_at": 10.0,
        },
    ))
    await event_router.dispatch(_event(
        "background_agent.completed",
        {
            "activity_id": first_id,
            "interaction_session_id": "session-a",
            "goal": "第一项任务",
            "display_content": "第一项结果",
            "delivery_brief": "结果已完成。",
            "source_materials": [],
            "created_at": 11.0,
        },
    ))

    assert service.detail(first_id).compact.status == "completed"
    assert not any(event["type"] == "task_result.completed" for event in broadcasts)

    await event_router.dispatch(_event(
        "background_agent.started",
        {
            "activity_id": second_id,
            "interaction_session_id": "session-a",
            "goal": "第二项任务",
            "deliverable": True,
            "created_at": 12.0,
        },
    ))

    assert service.detail(second_id).compact.status == "running"
    snapshot = service.desktop_snapshot()
    assert [item.activity_id for item in snapshot.active] == [second_id]
    assert [item.activity_id for item in snapshot.recent] == [first_id]
