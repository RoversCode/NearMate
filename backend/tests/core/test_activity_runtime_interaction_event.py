from __future__ import annotations

from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplication,
)
from companion.realtime.activity_runtime_event_handler import (
    ActivityRuntimeEventHandler,
)
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.task_result_delivery import (
    TaskResultAcceptanceTrackingResult,
)


class StubTaskResultDeliveryCoordinator:
    """记录资料回答是否被登记到当前后台工作的材料范围。"""

    def __init__(self) -> None:
        self.supplemental_events: list[RuntimeEvent] = []
        self.acceptance_events: list[RuntimeEvent] = []

    def track_supplemental_material(self, event: RuntimeEvent) -> None:
        """保存一条已确认成功的补充材料事件。"""

        self.supplemental_events.append(event)

    def track_tool_acceptance(
        self,
        event: RuntimeEvent,
    ) -> TaskResultAcceptanceTrackingResult:
        """保存工具 acceptance，并表示当前没有待交付终态。"""

        self.acceptance_events.append(event)
        return TaskResultAcceptanceTrackingResult(
            accepted=True,
            first_acceptance=True,
            material_context_id="",
            material_retained=False,
        )


async def test_successful_foreground_interaction_broadcasts_processing_event() -> None:
    """语音或文本回答真正进入运行时后，Desktop 必须收到 processing 事件。"""

    broadcasts: list[dict[str, object]] = []
    delivery = StubTaskResultDeliveryCoordinator()

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        """记录 Handler 释放的瞬时控制事件。"""

        broadcasts.append(event)

    expected_event = {
        "type": "background_agent.interaction_submitted",
        "payload": {"request_id": "request-a", "activity_id": "activity-a"},
    }
    handler = ActivityRuntimeEventHandler(
        publish_activity=broadcast_ephemeral,
        publish_activity_ephemeral=broadcast_ephemeral,
        publish_materials_ephemeral=broadcast_ephemeral,
        background_agent_interaction_event_handler=lambda *_args: (
            AgentInteractionEventApplication(control_event=expected_event)
        ),
        task_result_delivery_coordinator=delivery,  # type: ignore[arg-type]
    )
    runtime_event = _foreground_interaction_event()

    assert await handler.try_handle(runtime_event) is True

    assert broadcasts == [expected_event]
    assert delivery.supplemental_events == [runtime_event]
    assert delivery.acceptance_events == []


async def test_failed_runtime_submission_does_not_register_supplemental_material() -> None:
    """Registry 或运行时拒绝回答时，材料访问范围和结果资格都不能推进。"""

    broadcasts: list[dict[str, object]] = []
    delivery = StubTaskResultDeliveryCoordinator()

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        """记录意外广播。"""

        broadcasts.append(event)

    handler = ActivityRuntimeEventHandler(
        publish_activity=broadcast_ephemeral,
        publish_activity_ephemeral=broadcast_ephemeral,
        publish_materials_ephemeral=broadcast_ephemeral,
        background_agent_interaction_event_handler=lambda *_args: None,
        task_result_delivery_coordinator=delivery,  # type: ignore[arg-type]
    )

    assert await handler.try_handle(_foreground_interaction_event()) is True

    assert broadcasts == []
    assert delivery.supplemental_events == []
    assert delivery.acceptance_events == []


def _foreground_interaction_event() -> RuntimeEvent:
    """构造后台执行器已经真实接收资料回答的运行时事件。"""

    return RuntimeEvent(
        type="llm.foreground.work_interaction_submission_completed",
        source="assistant_process",
        payload={
            "name": "respond_to_work_interaction",
            "state": "accepted",
            "receipt": "accepted",
            "submission_id": "submission-a",
            "request_id": "request-a",
            "activity_id": "activity-a",
        },
        runtime_epoch=3,
    )
