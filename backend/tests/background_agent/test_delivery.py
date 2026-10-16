from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplication,
)
from companion.foreground_tools.orchestration import (
    BackgroundActivityPrepared,
    DeferredActionPrepared,
    ExecutedToolCall,
    ToolReplyRequirement,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from companion.realtime.assistant_events import (
    RECENT_MATERIAL_OBSERVATION_FIELD,
    RuntimeEvent,
)
from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
from backend.tests.foreground_processor_factory import (
    build_foreground_processor_for_test,
)
from backend.tests.runtime_public_event_support import runtime_event_router


RUNTIME_EPOCH = 1


def _event(event_type: str, payload: dict[str, object]) -> RuntimeEvent:
    """构造属于当前助手运行时的测试事件。

    Args:
        event_type: 运行时事件名称。
        payload: 事件携带的业务字段。

    Returns:
        可交给主进程事件 Router 的运行时事件。
    """

    return RuntimeEvent(
        type=event_type,
        source="assistant_runtime",
        payload=payload,
        produced_at=10.0,
        runtime_epoch=RUNTIME_EPOCH,
    )


def test_dispatch_acceptance_carries_deliverable_and_material_context() -> None:
    """派单接受事实必须冻结交付规则和材料租约身份。"""

    foreground = build_foreground_processor_for_test()
    tool_call = ForegroundToolCall(
        tool_call_id="tool-call-a",
        name="start_background_work",
        arguments={"goal": "整理录音", "deliverable": True},
    )
    result = {
        "state": "accepted",
        "activity_id": "background-agent-a",
        "goal": "整理录音",
        "deliverable": True,
        RECENT_MATERIAL_OBSERVATION_FIELD: {
            "outcome": "accepted",
            "paths": [r"C:\materials\录音.m4a"],
        },
    }
    completed_event = foreground._tool_call_completed_event(
        executed_call=ExecutedToolCall(
            call=tool_call,
            outcome=BackgroundActivityPrepared(
                activity_id="background-agent-a",
                disposition="accepted",
                payload=result,
            ),
            reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
            channel="background",
        ),
        interaction_session_id="session-a",
        request_id="foreground-request-a",
        protocol_transaction_id="transaction-a",
        current_user_message=None,
        foreground_context={
            "active_turn_material_context": {"context_id": "context-a"}
        },
    )

    assert completed_event.payload["request_id"] == "foreground-request-a"
    assert completed_event.payload["deliverable"] is True
    assert completed_event.payload["turn_material_context_id"] == "context-a"
    assert completed_event.payload[RECENT_MATERIAL_OBSERVATION_FIELD] == {
        "outcome": "accepted",
        "paths": [r"C:\materials\录音.m4a"],
    }
    assert RECENT_MATERIAL_OBSERVATION_FIELD not in completed_event.payload["result"]


def test_correction_acceptance_carries_parent_identity_and_material_context() -> None:
    """后台纠正 acceptance 必须携带父 Activity，供主进程续接材料租约。"""

    foreground = build_foreground_processor_for_test()
    completed_event = foreground._tool_call_completed_event(
        executed_call=ExecutedToolCall(
            call=ForegroundToolCall(
                tool_call_id="tool-call-correction",
                name="correct_background_work",
                arguments={"activity_id": "activity-a", "goal": "正确目标"},
            ),
            outcome=BackgroundActivityPrepared(
                activity_id="activity-b",
                disposition="accepted",
                payload={
                    "state": "accepted",
                    "activity_id": "activity-b",
                    "based_on_activity_id": "activity-a",
                    "goal": "正确目标",
                    "deliverable": True,
                },
            ),
            reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
            channel="background",
        ),
        interaction_session_id="session-a",
        request_id="foreground-request-correction",
        protocol_transaction_id="transaction-correction",
        current_user_message=None,
        foreground_context={
            "active_turn_material_context": {"context_id": "context-correction"}
        },
    )

    assert completed_event.payload["request_id"] == "foreground-request-correction"
    assert completed_event.payload["deliverable"] is True
    assert completed_event.payload["based_on_activity_id"] == "activity-a"
    assert completed_event.payload["turn_material_context_id"] == "context-correction"


def test_question_answer_acceptance_carries_activity_and_supplemental_context() -> None:
    """资料回答事件必须让主进程知道目标 Activity 和本轮材料上下文。"""

    foreground = build_foreground_processor_for_test()
    result = {
        "state": "accepted",
        "request_id": "question-a",
        "activity_id": "background-agent-a",
        "safe_message": "补充信息已交回后台任务。",
    }
    completed_event = foreground._tool_call_completed_event(
        executed_call=ExecutedToolCall(
            call=ForegroundToolCall(
                tool_call_id="tool-call-answer",
                name="respond_to_work_interaction",
                arguments={
                    "request_id": "question-a",
                    "answers": {"使用哪个版本？": "第三版"},
                },
            ),
            outcome=DeferredActionPrepared(
                resource_id="question-a",
                payload=result,
            ),
            reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
            channel="deferred",
        ),
        interaction_session_id="session-a",
        request_id="foreground-request-answer",
        protocol_transaction_id="transaction-answer",
        current_user_message=None,
        foreground_context={
            "active_turn_material_context": {
                "context_id": "context-supplemental",
                "attachments": [
                    {
                        "material_id": "material-a",
                        "display_name": "第三版.md",
                        "kind": "dragged_file",
                        "path": r"C:\materials\第三版.md",
                        "node_kind": "file",
                        "media_class": "text",
                    }
                ],
            }
        },
    )

    assert completed_event.payload["request_id"] == "foreground-request-answer"
    assert completed_event.payload["activity_id"] == "background-agent-a"
    assert completed_event.payload["state"] == "accepted"
    assert completed_event.payload["agent_interaction_request_id"] == "question-a"
    assert completed_event.payload["turn_material_context_id"] == "context-supplemental"
    assert "answers" not in completed_event.payload


def test_text_only_question_answer_does_not_retain_empty_material_context() -> None:
    """纯文字回答没有新增材料时，不得附带空材料上下文租约。"""

    foreground = build_foreground_processor_for_test()
    result = {
        "state": "accepted",
        "request_id": "question-a",
        "activity_id": "background-agent-a",
    }
    completed_event = foreground._tool_call_completed_event(
        executed_call=ExecutedToolCall(
            call=ForegroundToolCall(
                tool_call_id="tool-call-answer",
                name="respond_to_work_interaction",
                arguments={},
            ),
            outcome=DeferredActionPrepared(
                resource_id="question-a",
                payload=result,
            ),
            reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
            channel="deferred",
        ),
        interaction_session_id="session-a",
        request_id="foreground-request-answer-empty",
        protocol_transaction_id="transaction-answer-empty",
        current_user_message=None,
        foreground_context={
            "active_turn_material_context": {
                "context_id": "context-empty",
                "attachments": [],
            }
        },
    )

    assert completed_event.payload["request_id"] == "foreground-request-answer-empty"
    assert "turn_material_context_id" not in completed_event.payload


def test_runtime_interruption_releases_initial_and_supplemental_materials() -> None:
    """运行时失效时必须释放后台 Activity 持有的全部材料上下文。"""

    retained: list[str] = []
    released: list[str] = []
    coordinator = TaskResultDeliveryCoordinator(
        retain_material=lambda context_id: retained.append(context_id) or True,
        release_material=lambda context_id: released.append(context_id) or [],
    )
    coordinator.track_tool_acceptance(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "start_background_work",
            "state": "accepted",
            "activity_id": "activity-a",
            "request_id": "request-a",
            "deliverable": True,
            "turn_material_context_id": "context-initial",
        },
    ))
    assert coordinator.track_supplemental_material(_event(
        "llm.foreground.tool_call_completed",
        {
            "name": "respond_to_work_interaction",
            "state": "accepted",
            "activity_id": "activity-a",
            "request_id": "question-a",
            "turn_material_context_id": "context-supplemental",
        },
    )) is True

    coordinator.interrupt_runtime_epoch(RUNTIME_EPOCH)

    assert retained == ["context-initial", "context-supplemental"]
    assert released == ["context-initial", "context-supplemental"]


def test_acceptance_result_proves_background_material_lease() -> None:
    """调用方只能在 Background 自己 retain 成功后看到材料交接证明。"""

    retained: list[str] = []
    coordinator = TaskResultDeliveryCoordinator(
        retain_material=lambda context_id: retained.append(context_id) or True,
    )

    first = coordinator.track_tool_acceptance(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "state": "accepted",
                "activity_id": "activity-a",
                "request_id": "request-a",
                "deliverable": True,
                "turn_material_context_id": "context-a",
            },
        )
    )
    duplicate = coordinator.track_tool_acceptance(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "state": "accepted",
                "activity_id": "activity-a",
                "request_id": "request-a",
                "deliverable": True,
                "turn_material_context_id": "context-a",
            },
        )
    )

    assert first.accepted is True
    assert first.first_acceptance is True
    assert first.material_context_id == "context-a"
    assert first.material_retained is True
    assert duplicate.accepted is True
    assert duplicate.first_acceptance is False
    assert duplicate.material_retained is True
    assert retained == ["context-a"]


def test_acceptance_result_does_not_claim_failed_material_retain() -> None:
    """材料 retain 失败时任务可继续，但监督租约绝不能被提前释放。"""

    coordinator = TaskResultDeliveryCoordinator(
        retain_material=lambda _context_id: False,
    )

    result = coordinator.track_tool_acceptance(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "state": "accepted",
                "activity_id": "activity-a",
                "request_id": "request-a",
                "deliverable": True,
                "turn_material_context_id": "context-a",
            },
        )
    )

    assert result.accepted is True
    assert result.first_acceptance is True
    assert result.material_context_id == "context-a"
    assert result.material_retained is False


def test_background_correction_retain_parent_and_current_material_contexts() -> None:
    """B 必须重新持有 A 的全部租约，A 取消后材料才能继续可用。"""

    retained: list[str] = []
    released: list[str] = []
    coordinator = TaskResultDeliveryCoordinator(
        retain_material=lambda context_id: retained.append(context_id) or True,
        release_material=lambda context_id: released.append(context_id) or [],
    )
    coordinator.track_tool_acceptance(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "state": "accepted",
                "activity_id": "activity-a",
                "request_id": "request-a",
                "deliverable": True,
                "turn_material_context_id": "context-initial",
            },
        )
    )
    coordinator.track_supplemental_material(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "respond_to_work_interaction",
                "state": "accepted",
                "activity_id": "activity-a",
                "request_id": "question-a",
                "turn_material_context_id": "context-supplemental",
            },
        )
    )

    coordinator.track_tool_acceptance(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "correct_background_work",
                "state": "accepted",
                "activity_id": "activity-b",
                "based_on_activity_id": "activity-a",
                "request_id": "request-b",
                "deliverable": True,
                "turn_material_context_id": "context-correction",
            },
        )
    )

    assert retained == [
        "context-initial",
        "context-supplemental",
        "context-correction",
        "context-initial",
        "context-supplemental",
    ]
    coordinator.release_terminal(
        _event(
            "background_agent.cancelled",
            {"activity_id": "activity-a"},
        )
    )
    assert released == ["context-initial", "context-supplemental"]
    coordinator.release_terminal(
        _event(
            "background_agent.failed",
            {"activity_id": "activity-b"},
        )
    )
    assert released == [
        "context-initial",
        "context-supplemental",
        "context-correction",
        "context-initial",
        "context-supplemental",
    ]


@pytest.mark.asyncio
async def test_deliverable_background_agent_waits_for_all_proofs_and_projects_files() -> None:
    """可交付后台任务必须凑齐四项证明后才公开正文和文件路径。"""

    activity_id = "background-agent-a"
    request_id = "request-a"
    context_id = "context-a"
    public_events: list[dict[str, object]] = []
    notices: list[str] = []
    retained: list[str] = []
    released: list[str] = []
    revision = 0
    coordinator = TaskResultDeliveryCoordinator(
        retain_material=lambda value: retained.append(value) or True,
        release_material=lambda value: released.append(value) or [],
    )

    async def broadcast_public(event: dict[str, object]) -> None:
        """记录公开控制事件。"""

        public_events.append(event)

    def apply_activity(
        _event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        """用递增版本模拟任务中心接受来源事实。"""

        nonlocal revision
        revision += 1
        return ActivityApplyResult("changed", ActivityProjection(revision=revision))

    router = runtime_event_router(
        publish=broadcast_public,
        publish_ephemeral=broadcast_public,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=apply_activity,
        activity_broadcast_handler=lambda _projection: _noop_async(),
        activity_notice_handler=lambda event_type, _payload, _epoch: notices.append(
            event_type
        ),
        background_agent_interaction_event_handler=lambda *_args: (
            AgentInteractionEventApplication()
        ),
        turn_material_retain_handler=lambda value: retained.append(value) or True,
        turn_material_release_handler=lambda value: released.append(value) or [],
        task_result_delivery_coordinator=coordinator,
    )
    terminal_payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "整理采访并输出文档",
        "deliverable": True,
        "display_content": "采访稿已经整理完成。",
        "delivery_brief": "采访稿已经整理完成并生成文档。",
        "output_paths": [r"C:\NearMate Workspace\tasks\a\outputs\采访稿.md"],
        "source_materials": ["采访录音.wav"],
        "created_at": 12.0,
    }

    # 故意让 terminal 最先到，验证协调器不会依赖理想事件顺序。
    await router.dispatch(_event("background_agent.completed", terminal_payload))
    await router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": request_id,
                "deliverable": True,
                "turn_material_context_id": context_id,
            },
        )
    )
    await router.dispatch(
        _event(
            "llm.foreground.work_interaction_submission_completed",
            {
                "name": "respond_to_work_interaction",
                "state": "accepted",
                "receipt": "accepted",
                "submission_id": "submission-a",
                "activity_id": activity_id,
                "request_id": "question-a",
                "turn_material_context_id": "context-supplemental",
            },
        )
    )
    await router.dispatch(
        _event(
            "assistant.text_final",
            {
                "request_id": request_id,
                "session_id": "session-a",
                "generation_id": "generation-a",
                "text": "任务已经开始。",
            },
        )
    )

    assert retained == [context_id, "context-supplemental"]
    assert released == []
    assert not any(event["type"] == "task_result.completed" for event in public_events)

    await router.dispatch(
        _event(
            "background_agent.started",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "整理采访并输出文档",
                "deliverable": True,
                "created_at": 11.0,
            },
        )
    )

    completed = next(
        event for event in public_events if event["type"] == "task_result.completed"
    )
    payload = completed["payload"]
    assert isinstance(payload, dict)
    assert payload["activity_id"] == activity_id
    assert payload["display_content"] == "采访稿已经整理完成。"
    assert payload["output_paths"] == terminal_payload["output_paths"]
    assert payload["source_materials"] == ["采访录音.wav"]
    assert notices[-1] == "background_agent.completed"
    assert released == [context_id, "context-supplemental"]
    assert [
        event["type"] for event in coordinator.committed_result_card_events()
    ] == ["task_result.completed"]


@pytest.mark.asyncio
async def test_non_deliverable_background_agent_releases_without_result_card() -> None:
    """无需交付的后台任务在终态释放材料，但不向聊天窗口登记结果卡。"""

    activity_id = "background-agent-b"
    public_events: list[dict[str, object]] = []
    notices: list[str] = []
    released: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录公开和瞬时事件。"""

        public_events.append(event)

    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=lambda _type, _payload, _epoch: ActivityApplyResult(
            "changed", ActivityProjection(revision=1)
        ),
        activity_broadcast_handler=lambda _projection: _noop_async(),
        activity_notice_handler=lambda event_type, _payload, _epoch: notices.append(
            event_type
        ),
        turn_material_retain_handler=lambda _context_id: True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
    )
    await router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": "request-b",
                "deliverable": False,
                "turn_material_context_id": "context-b",
            },
        )
    )
    await router.dispatch(
        _event(
            "background_agent.started",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-b",
                "goal": "检查目录状态",
                "deliverable": False,
                "created_at": 10.0,
            },
        )
    )
    await router.dispatch(
        _event(
            "background_agent.completed",
            {
                "activity_id": activity_id,
                "interaction_session_id": "session-b",
                "goal": "检查目录状态",
                "deliverable": False,
                "display_content": "检查完成。",
                "delivery_brief": "目录状态已经检查完成。",
                "output_paths": [],
                "source_materials": ["目录清单.txt"],
                "created_at": 11.0,
            },
        )
    )

    assert not any(
        str(event["type"]).startswith("task_result.") for event in public_events
    )
    assert notices == ["background_agent.started", "background_agent.completed"]
    assert released == ["context-b"]


@pytest.mark.asyncio
async def test_work_plan_change_only_refreshes_activity_snapshot() -> None:
    """真实计划变化只刷新任务中心，不直接创建 Notice。"""

    applied_events: list[str] = []
    broadcast_revisions: list[int] = []
    notices: list[str] = []

    def apply_activity(
        event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        """记录来源事件并模拟 Registry 提交成功。"""

        applied_events.append(event_type)
        return ActivityApplyResult("changed", ActivityProjection(revision=7))

    async def broadcast_activity(projection: ActivityProjection) -> None:
        """记录任务中心广播使用的权威版本。"""

        broadcast_revisions.append(projection.revision)

    router = runtime_event_router(
        publish=lambda _event: _noop_async(),
        publish_ephemeral=lambda _event: _noop_async(),
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=apply_activity,
        activity_broadcast_handler=broadcast_activity,
        activity_notice_handler=lambda event_type, _payload, _epoch: notices.append(
            event_type
        ),
    )

    await router.dispatch(
        _event(
            "background_agent.work_plan_changed",
            {
                "activity_id": "background-agent-c",
                "goal": "整理任务计划",
                "work_plan": {
                    "revision": 1,
                    "items": [
                        {
                            "task_id": "1",
                            "subject": "理解材料",
                            "active_form": "正在理解材料",
                            "status": "in_progress",
                            "order": 1,
                        }
                    ],
                },
            },
        )
    )

    assert applied_events == ["background_agent.work_plan_changed"]
    assert broadcast_revisions == [7]
    assert notices == []


async def _noop_async() -> None:
    """提供不产生副作用的异步测试端口。"""

    return None
