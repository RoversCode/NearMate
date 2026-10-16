from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from companion.activities.terminal_settlement import (
    ActivityTerminalSettlementRetryableError,
)
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.activities.runtime.registry import ActivityRuntimeRegistry
from companion.activities.runtime.service import ActivityService
from companion.realtime.assistant_events import RuntimeEvent
from backend.tests.runtime_public_event_support import runtime_event_router


RUNTIME_EPOCH = 1
RUNTIME_ID = "00000000-0000-4000-8000-0000000000bb"

from backend.tests.activities.runtime_event_router_test_support import (
    _append_async,
    _event,
)

@pytest.mark.asyncio
async def test_task_result_terminal_waits_for_foreground_turn_end_and_releases_material_lease() -> None:
    """结果卡必须晚于发起轮次结束，且 turn 结束不能提前释放 task_result 材料。"""

    timeline: list[str] = []
    retained: list[str] = []
    released: list[str] = []
    terminal_applied = False

    async def broadcast(event: dict[str, object]) -> None:
        timeline.append(str(event["type"]))

    def retain(context_id: str) -> bool:
        retained.append(context_id)
        return True

    def finish(context_id: str) -> list[dict[str, object]]:
        timeline.append(f"finish:{context_id}")
        return [{"type": "materials.contexts_changed", "payload": {}}]

    def release(context_id: str) -> list[dict[str, object]]:
        released.append(context_id)
        return [{"type": "materials.assets_released", "payload": {"managed_asset_ids": ["asset-a"]}}]

    def apply_terminal(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        nonlocal terminal_applied
        if event_type == "background_agent.started":
            return ActivityApplyResult("changed", ActivityProjection(revision=1))
        if terminal_applied:
            return ActivityApplyResult("duplicate")
        terminal_applied = True
        return ActivityApplyResult("changed", ActivityProjection(revision=2))

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=apply_terminal,
        activity_broadcast_handler=lambda projection: _append_async(
            timeline, f"project:{projection.revision}"
        ),
        activity_notice_handler=lambda event_type, _payload, _runtime_epoch: timeline.append(
            f"notice:{event_type}"
        ),
        turn_material_retain_handler=retain,
        turn_material_final_handler=finish,
        turn_material_release_handler=release,
    )
    context_id = "turn-context-a"
    request_id = "request-material-a"
    activity_id = "task_result-a"

    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": request_id,
                "turn_material_context_id": context_id,
            },
        )
    )
    await event_router.dispatch(
        _event(
            "background_agent.started",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "总结这些材料",
                "deliverable": True,
                "created_at": 10.0,
            },
        )
    )
    await event_router.dispatch(
        _event(
            "background_agent.completed",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "总结这些材料",
                "display_content": "正文",
                "delivery_brief": "结果已完成。",
                "source_materials": [],
                "created_at": 10.0,
            },
        )
    )
    # 重复终态必须被吸收，不能把第一条暂存卡片和材料租约提前释放。
    await event_router.dispatch(
        _event(
            "background_agent.completed",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "总结这些材料",
                "display_content": "不得覆盖",
                "delivery_brief": "结果已完成。",
                "source_materials": [],
                "created_at": 10.0,
            },
        )
    )
    assert "task_result.completed" not in timeline
    assert terminal_applied is True
    assert released == []

    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {
                "text": "这份材料我来处理。",
                "request_id": request_id,
                "turn_material_context_id": context_id,
            },
        )
    )

    assert retained == [context_id]
    assert terminal_applied is True
    assert released == [context_id]
    assert timeline.index("project:2") < timeline.index("assistant.text_final")
    assert timeline.index("assistant.text_final") < timeline.index(
        "task_result.completed"
    )
    assert timeline.index("task_result.completed") < timeline.index(
        "notice:background_agent.completed"
    )
    assert timeline.index("notice:background_agent.completed") < timeline.index(
        "materials.assets_released"
    )


@pytest.mark.asyncio
async def test_task_result_notice_failure_retries_without_releasing_or_rebroadcasting_card() -> None:
    """Notice 写入失败时必须保留交付状态，重试成功后才释放材料。"""

    broadcasts: list[dict[str, object]] = []
    released: list[str] = []
    notice_attempts: list[str] = []
    terminal_applied = False

    async def broadcast(event: dict[str, object]) -> None:
        """记录结果卡与其它公开控制事件。

        Args:
            event: 当前已经越过公开交付边界的控制事件。
        """

        broadcasts.append(event)

    def apply_activity(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        """让首条终态改变 Registry，重试事件返回重复。

        Args:
            event_type: 正在应用的 Activity 来源事件名。
            _payload: 本测试不需要解析的事件载荷。
            _runtime_epoch: 本测试不需要分支的运行时世代。

        Returns:
            首条 started/终态返回 changed，重复终态返回 duplicate。
        """

        nonlocal terminal_applied
        if event_type == "background_agent.started":
            return ActivityApplyResult("changed", ActivityProjection(revision=1))
        if event_type == "background_agent.completed":
            if terminal_applied:
                return ActivityApplyResult("duplicate")
            terminal_applied = True
            return ActivityApplyResult("changed", ActivityProjection(revision=2))
        return ActivityApplyResult("changed", ActivityProjection(revision=0))

    def apply_notice(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> None:
        """让第一次终态 Notice 写入失败，第二次正常完成。

        Args:
            event_type: 当前需要登记的 Activity 终态事件名。
            _payload: 本测试不需要解析的事件载荷。
            _runtime_epoch: 本测试不需要分支的运行时世代。

        Raises:
            RuntimeError: 首次写入时模拟 Notice 账本暂时不可写。
        """

        if event_type != "background_agent.completed":
            return
        notice_attempts.append(event_type)
        if len(notice_attempts) == 1:
            raise RuntimeError("notice-ledger-temporarily-unavailable")

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=apply_activity,
        activity_broadcast_handler=lambda _projection: _append_async(
            [],
            "project",
        ),
        activity_notice_handler=apply_notice,
        turn_material_retain_handler=lambda _context_id: True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
    )
    activity_id = "task-result-notice-retry"
    request_id = "request-notice-retry"
    context_id = "context-notice-retry"
    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": request_id,
                "turn_material_context_id": context_id,
            },
        )
    )
    await event_router.dispatch(
        _event(
            "background_agent.started",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "整理结果",
                "deliverable": True,
                "created_at": 10.0,
            },
        )
    )
    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {"request_id": request_id, "text": "我会整理好。"},
        )
    )
    terminal = _event(
        "background_agent.completed",
        {
            "activity_id": activity_id,
            "interaction_session_id": "session-a",
            "goal": "整理结果",
            "display_content": "正文",
            "delivery_brief": "结果已完成。",
            "source_materials": [],
            "created_at": 11.0,
        },
    )

    with pytest.raises(ActivityTerminalSettlementRetryableError):
        await event_router.dispatch(terminal)

    assert released == []
    assert notice_attempts == ["background_agent.completed"]
    assert [
        event["type"] for event in broadcasts if event["type"] == "task_result.completed"
    ] == ["task_result.completed"]

    await event_router.dispatch(terminal)

    assert notice_attempts == [
        "background_agent.completed",
        "background_agent.completed",
    ]
    assert released == [context_id]
    assert [
        event["type"] for event in broadcasts if event["type"] == "task_result.completed"
    ] == ["task_result.completed"]


@pytest.mark.asyncio
async def test_task_result_without_materials_waits_for_foreground_request_terminal() -> None:
    """无材料答案产物也必须晚于发起它的 Foreground 请求终态交付。"""

    timeline: list[str] = []
    retained: list[str] = []
    released: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        timeline.append(str(event["type"]))

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=lambda event_type, _payload, _runtime_epoch: (
            ActivityApplyResult(
                "changed",
                ActivityProjection(
                    revision=1 if event_type == "background_agent.started" else 2
                ),
            )
        ),
        activity_broadcast_handler=lambda projection: _append_async(
            timeline, f"project:{projection.revision}"
        ),
        activity_notice_handler=lambda event_type, _payload, _runtime_epoch: timeline.append(
            f"notice:{event_type}"
        ),
        turn_material_retain_handler=lambda context_id: retained.append(context_id) or True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
    )
    request_id = "request-independent-a"
    activity_id = "task_result-independent-a"

    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": request_id,
            },
        )
    )
    await event_router.dispatch(
        _event(
            "background_agent.started",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "写一首关于月夜的古诗",
                "deliverable": True,
                "created_at": 10.0,
            },
        )
    )
    await event_router.dispatch(
        _event(
            "background_agent.completed",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "写一首关于月夜的古诗",
                "display_content": "明月照长川。",
                "delivery_brief": "结果已完成。",
                "source_materials": [],
                "created_at": 10.0,
            },
        )
    )

    assert "task_result.completed" not in timeline
    assert retained == []
    assert released == []

    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {
                "request_id": request_id,
                "text": "我来写，完成后告诉你。",
            },
        )
    )

    assert timeline.index("assistant.text_final") < timeline.index(
        "task_result.completed"
    )
    assert retained == []
    assert released == []


@pytest.mark.asyncio
async def test_task_result_terminal_without_acceptance_does_not_infer_request_identity() -> None:
    """已有 started 但缺少 acceptance 时不得从公开载荷伪造交付身份。"""

    timeline: list[str] = []
    applied: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        timeline.append(str(event["type"]))

    def apply(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        applied.append(event_type)
        return ActivityApplyResult(
            "changed",
            ActivityProjection(revision=len(applied)),
        )

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=apply,
        activity_broadcast_handler=lambda _projection: _append_async(
            timeline,
            "project",
        ),
    )
    activity_id = "task_result-terminal-first"

    await event_router.dispatch(
        _event(
            "background_agent.started",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "生成极快结果",
                "created_at": 10.0,
            },
        )
    )
    await event_router.dispatch(
        _event(
            "background_agent.completed",
                {
                    "activity_id": activity_id,
                    "interaction_session_id": "session-a",
                    "goal": "生成极快结果",
                    "display_content": "正文",
                    "delivery_brief": "结果已完成。",
                    "source_materials": [],
                    "created_at": 10.0,
                },
            )
        )

    assert applied == ["background_agent.started", "background_agent.completed"]
    assert timeline == ["project", "project"]


@pytest.mark.asyncio
async def test_task_result_terminal_without_delivery_proof_only_projects_activity() -> None:
    """终态自身立即进入任务中心，但缺少 started 与 acceptance 时不得交付结果卡。"""

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
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=service.apply_source_event,
        activity_broadcast_handler=service.broadcast_projection,
    )

    await event_router.dispatch(
        _event(
            "background_agent.completed",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "极快完成的任务",
                "display_content": "完整结果",
                "delivery_brief": "结果已完成。",
                "source_materials": [],
                "created_at": 10.0,
            },
        )
    )

    assert service.detail(activity_id).compact.status == "completed"
    assert any(event["type"] == "activities.snapshot_changed" for event in broadcasts)
    assert not any(event["type"] == "task_result.completed" for event in broadcasts)


@pytest.mark.asyncio
async def test_task_result_request_ack_is_scoped_to_runtime_epoch() -> None:
    """热替换后复用的 request id 不能继承旧运行时的已确认状态。"""

    current_epoch = 1
    timeline: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        timeline.append(str(event["type"]))

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        current_runtime_epoch=lambda: current_epoch,
        activity_apply_handler=lambda _event_type, _payload, _runtime_epoch: (
            ActivityApplyResult("changed", ActivityProjection(revision=1))
        ),
        activity_broadcast_handler=lambda _projection: _append_async(timeline, "project"),
    )
    request_id = "recycled-request-a"

    await event_router.dispatch(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "start_background_work",
            "deliverable": True,
            "state": "accepted",
            "activity_id": "task_result-old",
            "request_id": request_id,
        },
        runtime_epoch=1,
    ))
    await event_router.dispatch(_event(
        "assistant.text_final",
        {"request_id": request_id, "text": "旧运行时已接单。"},
        runtime_epoch=1,
    ))
    await event_router.dispatch(_event(
        "background_agent.started",
        {
            "activity_id": "task_result-old",
            "interaction_session_id": "session-a",
            "goal": "生成旧结果",
            "created_at": 10.0,
        },
        runtime_epoch=1,
    ))
    await event_router.dispatch(_event(
        "background_agent.completed",
        {
            "activity_id": "task_result-old",
            "interaction_session_id": "session-a",
            "goal": "生成旧结果",
            "display_content": "旧正文",
            "delivery_brief": "结果已完成。",
            "source_materials": [],
            "created_at": 10.0,
        },
        runtime_epoch=1,
    ))
    timeline.clear()

    current_epoch = 2
    await event_router.dispatch(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "start_background_work",
            "deliverable": True,
            "state": "accepted",
            "activity_id": "task_result-new",
            "request_id": request_id,
        },
        runtime_epoch=2,
    ))
    await event_router.dispatch(_event(
        "background_agent.started",
        {
            "activity_id": "task_result-new",
            "interaction_session_id": "session-a",
            "goal": "生成新结果",
            "created_at": 10.0,
        },
        runtime_epoch=2,
    ))
    await event_router.dispatch(_event(
        "background_agent.completed",
        {
            "activity_id": "task_result-new",
            "interaction_session_id": "session-a",
            "goal": "生成新结果",
            "display_content": "新正文",
            "delivery_brief": "结果已完成。",
            "source_materials": [],
            "created_at": 10.0,
        },
        runtime_epoch=2,
    ))

    assert "task_result.completed" not in timeline


@pytest.mark.asyncio
async def test_task_result_started_terminal_acceptance_and_foreground_final_may_interleave() -> None:
    """started、terminal、acceptance 与 Foreground final 乱序时仍只交付首条终态。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
    )
    activity_id = "task_result-interleaved"
    request_id = "request-interleaved"
    started_payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "生成结果",
        "deliverable": True,
        "created_at": 10.0,
    }

    await event_router.dispatch(_event("background_agent.started", started_payload))
    await event_router.dispatch(_event(
        "background_agent.completed",
        {**started_payload, "display_content": "首个正文", "source_materials": []},
    ))
    await event_router.dispatch(_event(
        "background_agent.failed",
        {
            **started_payload,
            "message": "不得覆盖",
            "reason": "duplicate",
            "source_materials": [],
        },
    ))
    await event_router.dispatch(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "start_background_work",
            "deliverable": True,
            "state": "accepted",
            "activity_id": activity_id,
            "request_id": request_id,
        },
    ))
    assert [event["type"] for event in broadcasts] == ["task_result.started"]

    await event_router.dispatch(_event(
        "assistant.text_final",
        {"request_id": request_id, "text": "我来完成。"},
    ))

    terminal_events = [
        event for event in broadcasts if str(event["type"]).startswith("task_result.")
    ]
    assert [event["type"] for event in terminal_events] == [
        "task_result.started",
        "task_result.completed",
    ]
    assert terminal_events[-1]["payload"]["display_content"] == "首个正文"


@pytest.mark.asyncio
async def test_task_result_acceptance_and_foreground_final_before_started_are_replayed() -> None:
    """acceptance 与 Foreground final 先到时，started 后的 terminal 仍可立即交付。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
    )
    activity_id = "task_result-acceptance-first"
    request_id = "request-acceptance-first"
    await event_router.dispatch(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "start_background_work",
            "deliverable": True,
            "state": "accepted",
            "activity_id": activity_id,
            "request_id": request_id,
        },
    ))
    await event_router.dispatch(_event(
        "assistant.text_final",
        {"request_id": request_id, "text": "已经接单。"},
    ))
    started_payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "生成结果",
        "deliverable": True,
        "created_at": 10.0,
    }
    await event_router.dispatch(_event("background_agent.started", started_payload))
    await event_router.dispatch(_event(
        "background_agent.completed",
        {**started_payload, "display_content": "正文", "source_materials": []},
    ))

    assert [event["type"] for event in broadcasts] == [
        "assistant.text_final",
        "task_result.started",
        "task_result.completed",
    ]


@pytest.mark.asyncio
async def test_task_result_terminal_after_acceptance_waits_for_late_started() -> None:
    """acceptance 已建立 provisional 状态时，terminal 可先于 started 暂存。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
    )
    activity_id = "task_result-terminal-before-started"
    request_id = "request-terminal-before-started"
    started_payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "生成结果",
        "deliverable": True,
        "created_at": 10.0,
    }
    await event_router.dispatch(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "start_background_work",
            "deliverable": True,
            "state": "accepted",
            "activity_id": activity_id,
            "request_id": request_id,
        },
    ))
    await event_router.dispatch(_event(
        "background_agent.completed",
        {**started_payload, "display_content": "正文", "source_materials": []},
    ))
    await event_router.dispatch(_event(
        "assistant.text_final",
        {"request_id": request_id, "text": "已接单。"},
    ))
    assert [event["type"] for event in broadcasts] == ["assistant.text_final"]

    await event_router.dispatch(_event("background_agent.started", started_payload))

    assert [event["type"] for event in broadcasts] == [
        "assistant.text_final",
        "task_result.started",
        "task_result.completed",
    ]


@pytest.mark.asyncio
async def test_before_start_failure_delivers_without_fake_started_and_releases_material() -> None:
    """已接受任务的启动前失败应交付一次，并走正常材料释放。"""

    broadcasts: list[dict[str, object]] = []
    retained: list[str] = []
    released: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        turn_material_retain_handler=lambda context_id: retained.append(context_id) or True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
    )
    acceptance = {
        "name": "start_background_work",
        "deliverable": True,
        "state": "accepted",
        "activity_id": "background-before-start",
        "request_id": "request-before-start",
        "turn_material_context_id": "context-a",
    }
    await event_router.dispatch(
        _event("llm.foreground.tool_call_completed", acceptance)
    )
    await event_router.dispatch(
        _event(
            "background_agent.failed",
            {
                "activity_id": "background-before-start",
                "interaction_session_id": "session-a",
                "goal": "转换音频",
                "deliverable": True,
                "execution_phase": "before_start",
                "message": "后台工作没有启动。",
                "source_materials": [],
                "created_at": 10.0,
            },
        )
    )
    assert "task_result.failed" not in [event["type"] for event in broadcasts]

    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {"request_id": "request-before-start", "text": "我会处理。"},
        )
    )

    assert "task_result.started" not in [event["type"] for event in broadcasts]
    assert "task_result.failed" in [event["type"] for event in broadcasts]
    assert retained == ["context-a"]
    assert released == ["context-a"]


@pytest.mark.asyncio
async def test_retired_background_backlog_only_closes_existing_task_result_state() -> None:
    """退役 terminal 不能重建 Activity 或结果卡，材料只能释放一次。"""

    broadcasts: list[dict[str, object]] = []
    retained: list[str] = []
    released: list[str] = []
    usage_records: list[str] = []
    current_epoch = 1

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        current_runtime_epoch=lambda: current_epoch,
        turn_material_retain_handler=lambda context_id: retained.append(context_id) or True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
        background_agent_usage_handler=lambda event_type, _payload: usage_records.append(
            event_type
        ),
    )
    activity_id = "retired-task-result"
    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": "retired-request",
                "turn_material_context_id": "retired-context",
            },
        )
    )
    current_epoch = 99

    retired_terminal = RuntimeEvent(
        type="background_agent.completed",
        source="assistant_runtime",
        runtime_epoch=1,
        retired_runtime_event=True,
        payload={
            "activity_id": activity_id,
            "interaction_session_id": "session-retired",
            "goal": "转换音频",
            "display_content": "不应展示",
            "source_materials": [],
            "internal_usage": {"is_complete": True, "by_model": []},
        },
    )
    await event_router.dispatch(retired_terminal)
    await event_router.dispatch(retired_terminal)

    assert retained == ["retired-context"]
    assert released == ["retired-context"]
    assert usage_records == ["background_agent.completed"]
    assert broadcasts == []


@pytest.mark.asyncio
async def test_absorbed_before_start_terminal_is_not_deliverable() -> None:
    """Registry 未接纳的启动前终态不得交付结果卡，材料只能释放一次。"""

    broadcasts: list[dict[str, object]] = []
    retained: list[str] = []
    released: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录 TaskResult 公共广播。

        Args:
            event: 事件路由准备向 Desktop 发布的公共事件。
        """

        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=lambda _event_type, _payload, _runtime_epoch: (
            ActivityApplyResult("absorbed")
        ),
        activity_broadcast_handler=lambda _projection: _append_async([], "unused"),
        turn_material_retain_handler=lambda context_id: retained.append(context_id) or True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
    )
    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": "background-absorbed-before-start",
                "request_id": "request-absorbed-before-start",
                "turn_material_context_id": "context-absorbed-before-start",
            },
        )
    )
    terminal = _event(
        "background_agent.failed",
        {
            "activity_id": "background-absorbed-before-start",
            "interaction_session_id": "session-a",
            "goal": "转换音频",
            "deliverable": True,
            "execution_phase": "before_start",
            "message": "后台工作没有启动。",
            "source_materials": [],
            "created_at": 10.0,
        },
    )
    await event_router.dispatch(terminal)
    await event_router.dispatch(terminal)
    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {"request_id": "request-absorbed-before-start", "text": "我会处理。"},
        )
    )

    assert "task_result.failed" not in [event["type"] for event in broadcasts]
    assert retained == ["context-absorbed-before-start"]
    assert released == ["context-absorbed-before-start"]


@pytest.mark.asyncio
async def test_direct_before_start_terminal_changed_activity_releases_acceptance_material() -> None:
    """Registry 接纳启动前终态后，必须等待原 Foreground 收口再交付。"""

    broadcasts: list[dict[str, object]] = []
    retained: list[str] = []
    released: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录 Activity、结果卡和材料释放事件。"""

        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=lambda _event_type, _payload, _runtime_epoch: (
            ActivityApplyResult("changed", ActivityProjection(revision=1))
        ),
        activity_broadcast_handler=lambda _projection: _append_async([], "unused"),
        turn_material_retain_handler=lambda context_id: retained.append(context_id) or True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
    )
    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": "background-direct-start-failed",
                "request_id": "request-direct-start-failed",
                "turn_material_context_id": "context-direct-start-failed",
            },
        )
    )
    await event_router.dispatch(
        _event(
            "background_agent.failed",
            {
                "activity_id": "background-direct-start-failed",
                "interaction_session_id": "session-a",
                "goal": "转换音频",
                "deliverable": True,
                "execution_phase": "before_start",
                "message": "后台工作没有启动。",
                "source_materials": [],
                "created_at": 10.0,
            },
        )
    )

    assert retained == ["context-direct-start-failed"]
    assert released == []
    assert not any(
        event["type"] == "task_result.failed" for event in broadcasts
    )

    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {"request_id": "request-direct-start-failed", "text": "我会处理。"},
        )
    )

    assert released == ["context-direct-start-failed"]
    assert any(event["type"] == "task_result.failed" for event in broadcasts)


@pytest.mark.asyncio
async def test_runtime_interrupt_invalidates_inflight_task_result_notice_delivery() -> None:
    """热替换与结果卡广播交错时，旧世代不能在 await 后继续写终态 Notice。"""

    broadcasts: list[dict[str, object]] = []
    notices: list[str] = []
    retained: list[str] = []
    released: list[str] = []
    publish_started = asyncio.Event()
    allow_publish = asyncio.Event()

    async def broadcast(event: dict[str, object]) -> None:
        """阻塞结果卡广播，制造运行时中断的交错窗口。"""

        if event["type"] == "task_result.completed":
            publish_started.set()
            await allow_publish.wait()
        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=lambda _event_type, _payload, _runtime_epoch: (
            ActivityApplyResult("changed", ActivityProjection(revision=1))
        ),
        activity_broadcast_handler=lambda _projection: _append_async([], "unused"),
        activity_notice_handler=lambda event_type, _payload, _runtime_epoch: notices.append(
            event_type
        ),
        turn_material_retain_handler=lambda context_id: retained.append(context_id) or True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
    )
    activity_id = "background-inflight-delivery"
    request_id = "request-inflight-delivery"
    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": request_id,
                "turn_material_context_id": "context-inflight-delivery",
            },
        )
    )
    await event_router.dispatch(
        _event(
            "background_agent.started",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "等待结果",
                "deliverable": True,
                "created_at": 10.0,
            },
        )
    )
    await event_router.dispatch(
        _event("assistant.text_final", {"request_id": request_id, "text": "已接单。"})
    )
    notices.clear()

    terminal_task = asyncio.create_task(
        event_router.dispatch(
            _event(
                "background_agent.completed",
                {
                    "activity_id": activity_id,
                    "interaction_session_id": "session-a",
                    "goal": "等待结果",
                    "deliverable": True,
                    "display_content": "结果",
                    "source_materials": [],
                    "created_at": 11.0,
                },
            )
        )
    )
    await asyncio.wait_for(publish_started.wait(), timeout=1.0)

    release_events = event_router.activity_event_handler.interrupt_task_result_deliveries(
        RUNTIME_EPOCH
    )
    allow_publish.set()
    await terminal_task

    assert retained == ["context-inflight-delivery"]
    assert released == ["context-inflight-delivery"]
    assert release_events == []
    assert notices == []


@pytest.mark.asyncio
async def test_task_result_acceptance_identity_is_immutable_and_releases_retained_context() -> None:
    """重复 acceptance 不得把首次保留的材料 A 串换成材料 B。"""

    broadcasts: list[dict[str, object]] = []
    retained: list[str] = []
    released: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        turn_material_retain_handler=lambda context_id: retained.append(context_id) or True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
    )
    activity_id = "task_result-immutable"
    request_id = "request-a"
    acceptance = {
        "name": "start_background_work",
        "deliverable": True,
        "state": "accepted",
        "activity_id": activity_id,
        "request_id": request_id,
        "turn_material_context_id": "context-a",
    }
    await event_router.dispatch(_event("llm.foreground.tool_call_completed", acceptance))
    await event_router.dispatch(_event("llm.foreground.tool_call_completed", acceptance))
    await event_router.dispatch(_event(
        "llm.foreground.tool_call_completed",
        {**acceptance, "request_id": "request-b", "turn_material_context_id": "context-b"},
    ))
    started_payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "总结材料",
        "deliverable": True,
        "created_at": 10.0,
    }
    await event_router.dispatch(_event("background_agent.started", started_payload))
    await event_router.dispatch(_event(
        "background_agent.completed",
        {**started_payload, "display_content": "正文", "source_materials": ["a.pdf"]},
    ))
    await event_router.dispatch(_event(
        "assistant.text_final",
        {"request_id": "request-b", "text": "错误请求结束。"},
    ))
    assert "task_result.completed" not in [event["type"] for event in broadcasts]

    await event_router.dispatch(_event(
        "assistant.text_final",
        {"request_id": request_id, "text": "正确请求结束。"},
    ))

    assert retained == ["context-a"]
    assert released == ["context-a"]


@pytest.mark.asyncio
async def test_late_task_result_terminal_after_interruption_is_fully_absorbed() -> None:
    """已中断任务的迟到完成不得生成结果卡、快照或口头提醒。"""

    activity_id = str(uuid4())
    broadcasts: list[dict[str, object]] = []
    notices: list[str] = []

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
        activity_notice_handler=lambda event_type, _payload, _runtime_epoch: notices.append(
            event_type
        ),
    )
    payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "总结资料",
        "created_at": 10.0,
    }
    await event_router.dispatch(_event("background_agent.started", payload))
    await service.interrupt_active(RUNTIME_EPOCH, "runtime_reconfigured")
    broadcasts.clear()
    notices.clear()

    await event_router.dispatch(
        _event(
            "background_agent.completed",
            {**payload, "display_content": "不得公开", "source_materials": []},
        )
    )

    assert broadcasts == []
    assert notices == []
    assert service.detail(activity_id).compact.status == "interrupted"

@pytest.mark.asyncio
async def test_terminal_before_started_projects_once_and_delivers_after_proof() -> None:
    """终态先投影，started 补齐交付证明后只交付首条结果卡。"""

    activity_id = str(uuid4())
    request_id = "request-terminal-first"
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
            "activity_id": activity_id,
            "request_id": request_id,
        },
    ))
    await event_router.dispatch(_event(
        "assistant.text_final",
        {"request_id": request_id, "text": "已接受任务。"},
    ))
    terminal_payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "极快任务",
        "display_content": "唯一结果",
        "delivery_brief": "结果已完成。",
        "source_materials": [],
        "created_at": 10.0,
    }
    await event_router.dispatch(_event("background_agent.completed", terminal_payload))

    assert service.detail(activity_id).compact.status == "completed"
    assert not any(event["type"] == "task_result.completed" for event in broadcasts)

    await event_router.dispatch(_event(
        "background_agent.started",
        {
            "activity_id": activity_id,
            "interaction_session_id": "session-a",
            "goal": "极快任务",
            "deliverable": True,
            "created_at": 9.0,
        },
    ))
    await event_router.dispatch(_event("background_agent.completed", terminal_payload))

    delivered = [
        event for event in broadcasts if event["type"] == "task_result.completed"
    ]
    assert len(delivered) == 1
    assert delivered[0]["payload"]["display_content"] == "唯一结果"


@pytest.mark.asyncio
async def test_interrupted_task_result_with_context_releases_lease_without_holding_terminal() -> None:
    """带材料上下文的迟到终态也必须先被吸收，并立即释放原租约。"""

    activity_id = str(uuid4())
    context_id = "turn-context-a"
    public_events: list[dict[str, object]] = []
    ephemeral_events: list[dict[str, object]] = []
    released: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        public_events.append(event)

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        ephemeral_events.append(event)

    def release(current_context_id: str) -> list[dict[str, object]]:
        released.append(current_context_id)
        return [{"type": "materials.assets_released", "payload": {}}]

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=RUNTIME_ID),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
    )
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast_ephemeral,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=service.apply_source_event,
        activity_broadcast_handler=service.broadcast_projection,
        turn_material_retain_handler=lambda _context_id: True,
        turn_material_release_handler=release,
    )
    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": "request-interrupted-a",
                "turn_material_context_id": context_id,
            },
        )
    )
    payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "总结资料",
        "created_at": 10.0,
    }
    await event_router.dispatch(_event("background_agent.started", payload))
    await service.interrupt_active(RUNTIME_EPOCH, "runtime_reconfigured")
    for release_event in event_router.activity_event_handler.interrupt_task_result_deliveries(
        RUNTIME_EPOCH
    ):
        await broadcast_ephemeral(release_event)
    public_events.clear()

    await event_router.dispatch(
        _event(
            "background_agent.completed",
            {**payload, "display_content": "不得公开", "source_materials": []},
        )
    )

    assert public_events == []
    assert released == [context_id]
    assert [event["type"] for event in ephemeral_events] == [
        "materials.assets_released"
    ]
