from __future__ import annotations

import asyncio
from queue import Queue
from types import SimpleNamespace
from uuid import uuid4

import pytest

from companion.activities.runtime.registry import ActivityRuntimeRegistry
from companion.activities.runtime.service import ActivityService
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.activities.runtime.failure_reporting import failure_from_reason
from companion.llm.foreground_models import ForegroundEvent
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_output_lanes import AssistantOutputLanes
from companion.realtime.playout_delivery import PlayoutDeliveryRegistry
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from backend.tests.runtime_public_event_support import runtime_event_queue_drainer


class BudgetAwareAgent:
    """按调用预算返回并保留剩余事件的测试 agent。"""

    def __init__(self) -> None:
        self.events = [
            ForegroundEvent(type=f"task_result.event_{number}", payload={"number": number})
            for number in range(4)
        ]
        self.limits: list[int] = []

    def drain_events(self, limit: int = 100) -> list[ForegroundEvent]:
        """只移除本次预算允许返回的事件。

        Args:
            limit: 本次最多返回的事件数量。

        Returns:
            当前预算内的事件。
        """

        self.limits.append(limit)
        events = self.events[:limit]
        del self.events[:limit]
        return events


def test_lane_drain_passes_remaining_budget_without_losing_agent_events() -> None:
    """运行时队列接近上限时，agent 尾部事件必须留给下一轮。"""

    agent = BudgetAwareAgent()
    runner = AssistantOutputLanes(
        agent=agent,  # type: ignore[arg-type]
        tts_provider=None,  # type: ignore[arg-type]
        now=lambda: 10.0,
    )
    for number in range(99):
        runner.event_queue.put(
            RuntimeEvent(
                type=f"runtime.event_{number}",
                source="test",
                payload={},
                produced_at=10.0,
            )
        )

    first = runner.drain_events(limit=100)
    second = runner.drain_events(limit=100)

    assert len(first) == 100
    assert [event.payload["number"] for event in second] == [1, 2, 3]
    assert agent.limits == [1, 100]


@pytest.mark.asyncio
async def test_drainer_discards_one_bad_event_and_continues_with_later_events() -> None:
    """单条事件处理异常只能丢弃当前事件，不能杀死共享 Drainer。"""

    queue: Queue[RuntimeEvent] = Queue()
    queue.put(RuntimeEvent(type="bad", source="test"))
    queue.put(RuntimeEvent(type="good", source="test"))
    drainer = runtime_event_queue_drainer(
        event_queue=queue,
        publish=_broadcast,
        publish_ephemeral=_broadcast,
        realtime_input_runtime=object.__new__(RealtimeInputRuntime),
    )
    handled: list[str] = []

    async def handle(event: RuntimeEvent) -> None:
        if event.type == "bad":
            raise RuntimeError("bad event")
        handled.append(event.type)

    drainer._handle_event = handle  # type: ignore[method-assign]

    await drainer.drain_once()

    assert handled == ["good"]


@pytest.mark.asyncio
async def test_cognitive_notice_failure_retries_without_source_event_replay() -> None:
    """Cognitive Notice 写入失败后由独立账本重试，不依赖 Drainer 重放来源事件。"""

    queue: Queue[RuntimeEvent] = Queue()
    attempts = 0
    changed = True

    def apply_activity(
        _event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        nonlocal changed
        if changed:
            changed = False
            return ActivityApplyResult(
                disposition="changed",
                projection=ActivityProjection(revision=1),
            )
        return ActivityApplyResult(disposition="absorbed")

    async def broadcast_activity(_projection: ActivityProjection) -> None:
        """提供不产生副作用的 Activity 快照端口。"""

    def write_notice(
        _event_type: str,
        _payload: dict[str, object],
        _runtime_epoch: int,
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary notice store failure")

    event = RuntimeEvent(
        type="cognitive_task.failed",
        source="assistant_runtime",
        runtime_epoch=7,
        payload={
            "activity_id": "cognitive-a",
            "failure_reason": "现有方法无法完成。",
            "failure": failure_from_reason(
                "cognitive_task_failed",
                stage_code="generating_result",
            ).to_payload(),
        },
    )
    queue.put(event)
    drainer = runtime_event_queue_drainer(
        event_queue=queue,
        publish=_broadcast,
        publish_ephemeral=_broadcast,
        realtime_input_runtime=object.__new__(RealtimeInputRuntime),
        activity_apply_handler=apply_activity,
        activity_broadcast_handler=broadcast_activity,
        activity_notice_handler=write_notice,
    )

    await drainer.drain_once()
    await asyncio.sleep(0.08)

    assert attempts == 2
    assert queue.empty()


@pytest.mark.asyncio
async def test_unregistered_runtime_event_records_only_sanitized_routing_diagnostic() -> None:
    """未知事件不得泄露 payload，也不能让 Drainer 停止排水。"""

    queue: Queue[RuntimeEvent] = Queue()
    queue.put(
        RuntimeEvent(
            type="runtime.secret_event",
            source="assistant_runtime",
            payload={"text": "SECRET_PROVIDER_TOKEN"},
        )
    )
    drainer = runtime_event_queue_drainer(
        event_queue=queue,
        publish=_broadcast,
        publish_ephemeral=_broadcast,
        realtime_input_runtime=object.__new__(RealtimeInputRuntime),
    )

    await drainer.drain_once()

    assert drainer.background_task_errors == [
        "reason_code=unregistered_runtime_event "
        "event_type=runtime.secret_event source=assistant_runtime"
    ]
    assert "SECRET_PROVIDER_TOKEN" not in str(drainer.background_task_errors)


@pytest.mark.asyncio
async def test_playout_conflict_does_not_block_background_turn_output_projection() -> None:
    """迟到的重复播放事件不能阻止后续后台 Turn 写入任务详情。"""

    activity_id = str(uuid4())
    delivery_id = str(uuid4())
    request_id = "request-a"
    generation_id = "generation-a"
    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录控制面广播，方便确认 Activity 快照确实被提交。"""

        broadcasts.append(event)

    activity_service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=str(uuid4())),
        broadcast_snapshot=broadcast,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: 1,
    )
    playout_registry = PlayoutDeliveryRegistry()
    playout_registry.start_publishing(
        delivery_id=delivery_id,
        request_id=request_id,
        generation_id=generation_id,
        occurred_at=1.0,
    )
    playout_registry.acknowledge(
        delivery_id=delivery_id,
        request_id=request_id,
        generation_id=generation_id,
        client_instance_id="desktop-a",
        outcome="playing",
        occurred_at=2.0,
    )
    playout_registry.finish(
        delivery_id=delivery_id,
        request_id=request_id,
        generation_id=generation_id,
        completion="drained",
        occurred_at=3.0,
    )

    queue: Queue[RuntimeEvent] = Queue()
    queue.put(
        RuntimeEvent(
            type="background_agent.started",
            source="assistant_runtime",
            payload={"activity_id": activity_id, "goal": "整理资料"},
            runtime_epoch=1,
        )
    )
    # 这是日志中导致旧版 Drainer 退出的同身份迟到 start；当前账本应幂等吸收它。
    queue.put(
        RuntimeEvent(
            type="assistant.audio_publishing_started",
            source="assistant_runtime",
            payload={
                "delivery_id": delivery_id,
                "request_id": request_id,
                "generation_id": generation_id,
            },
            runtime_epoch=1,
        )
    )
    queue.put(
        RuntimeEvent(
            type="background_agent.turn_output",
            source="assistant_runtime",
            payload={
                "activity_id": activity_id,
                "turn_index": 1,
                "content_text": "已经读取到第一批资料。",
                "created_at": 1_000.0,
            },
            runtime_epoch=1,
        )
    )
    drainer = runtime_event_queue_drainer(
        event_queue=queue,
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(
            apply_assistant_audio_publishing_started=lambda **_kwargs: [],
        ),
        activity_apply_handler=activity_service.apply_source_event,
        activity_broadcast_handler=activity_service.broadcast_projection,
        current_runtime_epoch=lambda: 1,
        playout_delivery_registry=playout_registry,
    )

    await drainer.drain_once()

    detail = activity_service.detail(activity_id)
    assert [output.content_text for output in detail.turn_outputs] == [
        "已经读取到第一批资料。"
    ]
    assert any(event["type"] == "activities.snapshot_changed" for event in broadcasts)
    assert drainer.background_task_errors == []


async def _broadcast(_event: dict[str, object]) -> None:
    """测试用的异步广播桩。"""
