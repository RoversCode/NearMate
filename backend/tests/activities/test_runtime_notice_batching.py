from __future__ import annotations

import asyncio
from queue import Queue
from types import SimpleNamespace

import pytest

from companion.activities.notice.delivery_coordinator import NoticeDeliveryCoordinator
from companion.activities.notice.event_mapper import map_activity_event
from companion.activities.notice.models import NoticeDeliveryCommand
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.interaction_state import InteractionState
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.runtime_memory.models import RuntimeMemorySnapshot
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.realtime.livekit_media_session import (
    SYSTEM_NOTICE_REQUEST_TEXT,
    LiveKitMediaSession,
)
from companion.realtime import runtime_event_queue_drainer as drainer_module
from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
from backend.tests.runtime_public_event_support import runtime_event_queue_drainer


@pytest.mark.asyncio
async def test_drainer_separates_cognitive_result_from_background_progress() -> None:
    """同批到达的认知结果和后台进度必须进入两次独立播报。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    event_queue.put(
        RuntimeEvent(
            type="cognitive_task.completed",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "activity_id": "cognitive-a",
                "interaction_session_id": "session-a",
                "kind": "lookup_current_fact",
                "brief": "查询天气",
                "result_text": "今天有雨。",
                "summary": "天气核对完成。",
            },
        )
    )
    event_queue.put(
        RuntimeEvent(
            type="background_agent.progress",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "activity_id": "background-a",
                "interaction_session_id": "session-a",
                "goal": "整理资料",
                "notice_seq": 1,
                "updated_at": 10.0,
            },
        )
    )
    coordinator = NoticeDeliveryCoordinator()
    reserved_batches: list[list[dict[str, object]]] = []

    def handle_activity(
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> None:
        """把生产事件映射后交给真实 Notice 协调器。"""

        event = map_activity_event(event_type, payload)
        assert event is not None
        coordinator.apply_event(event, runtime_epoch)

    def reserve_notice() -> None:
        """记录事件批次排空后实际预留的第一批提醒。"""

        context = coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="strong-request",
        )
        reserved_batches.append(context["notice_batch"])

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        """本测试只验证 Notice 预留，不需要保存控制面广播。"""

    drainer = runtime_event_queue_drainer(
        event_queue=event_queue,
        publish=ignore_broadcast,
        publish_ephemeral=ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        foreground_notice_handler=reserve_notice,
        activity_notice_handler=handle_activity,
    )

    await drainer.drain_once()

    assert len(reserved_batches) == 1
    assert [notice["intent"] for notice in reserved_batches[0]] == [
        "cognitive_result"
    ]

    coordinator.apply_delivery(
        NoticeDeliveryCommand(
            request_id="strong-request",
            outcome="llm_output_completed",
        )
    )
    next_context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="weak-request",
    )
    assert [notice["intent"] for notice in next_context["notice_batch"]] == [
        "progress_report"
    ]


@pytest.mark.asyncio
async def test_drainer_reserves_cross_source_notices_after_current_backlog() -> None:
    """同一轮积压里的认知和后台工作终态应在一次 notice 尝试中合并。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    event_queue.put(
        RuntimeEvent(
            type="cognitive_task.completed",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "activity_id": "cognitive-a",
                "interaction_session_id": "session-a",
                "kind": "lookup_current_fact",
                "brief": "查询天气",
                "result_text": "今天有雨。",
                "summary": "天气核对完成。",
            },
        )
    )
    event_queue.put(
        RuntimeEvent(
            type="background_agent.started",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "activity_id": "task_result-a",
                "interaction_session_id": "session-a",
                "goal": "总结资料",
                "deliverable": True,
                "created_at": 10.0,
            },
        )
    )
    event_queue.put(
        RuntimeEvent(
            type="background_agent.completed",
            source="assistant_runtime",
            runtime_epoch=1,
                payload={
                    "activity_id": "task_result-a",
                    "interaction_session_id": "session-a",
                    "goal": "总结资料",
                    "display_content": "完整正文",
                    "delivery_brief": "资料已整理。",
                    "source_materials": [],
                    "created_at": 10.0,
                },
        )
    )
    event_queue.put(
        RuntimeEvent(
            type="llm.foreground.tool_call_completed",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "name": "start_background_work",
                "state": "accepted",
                "activity_id": "task_result-a",
                "request_id": "request-a",
                "deliverable": True,
            },
        )
    )
    event_queue.put(
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "request_id": "request-a",
                "session_id": "session-a",
                "generation_id": "generation-a",
                "text": "我来整理。",
            },
        )
    )
    coordinator = NoticeDeliveryCoordinator()
    reserved_batches: list[list[dict[str, object]]] = []
    timeline: list[str] = []

    def handle_activity(
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> None:
        """把测试运行时事件同步到真实协调器。"""

        timeline.append(f"activity_applied:{event_type}")
        event = map_activity_event(event_type, payload)
        assert event is not None
        coordinator.apply_event(event, runtime_epoch)

    def reserve_notice() -> None:
        """记录队列排空时实际预留的提醒批次。"""

        timeline.append("notice_reserved")
        context = coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="notice-request-a",
        )
        reserved_batches.append(context["notice_batch"])

    async def record_broadcast(event: dict[str, object]) -> None:
        """记录结果事件与 notice 预留的先后顺序。"""

        timeline.append(str(event.get("type") or ""))

    drainer = runtime_event_queue_drainer(
        event_queue=event_queue,
        publish=record_broadcast,
        publish_ephemeral=record_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        foreground_notice_handler=reserve_notice,
        activity_notice_handler=handle_activity,
    )

    await drainer.drain_once()

    assert len(reserved_batches) == 1
    assert [notice["source"] for notice in reserved_batches[0]] == [
        "cognitive",
        "background_agent",
    ]
    assert reserved_batches[0][1]["intent"] == "work_result"
    assert "result_context" not in reserved_batches[0][1]
    assert reserved_batches[0][1]["goal"] == "总结资料"
    assert "完整正文" not in str(reserved_batches[0])
    assert timeline == [
        "activity_applied:cognitive_task.completed",
        "task_result.started",
        "activity_applied:background_agent.started",
        "assistant.text_final",
        "task_result.completed",
        "activity_applied:background_agent.completed",
        "notice_reserved",
    ]


@pytest.mark.asyncio
async def test_task_result_broadcast_failure_preserves_snapshot_notice_and_release() -> None:
    """结果卡实时发送失败时，快照、终态提醒和材料释放仍必须完成。"""

    activity_id = "task-result-broadcast-failure"
    event_queue: Queue[RuntimeEvent] = Queue()
    for event in (
        RuntimeEvent(
            type="background_agent.started",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "整理资料",
                "deliverable": True,
            },
        ),
        RuntimeEvent(
            type="llm.foreground.tool_call_completed",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "name": "start_background_work",
                "state": "accepted",
                "activity_id": activity_id,
                "request_id": "request-a",
                "deliverable": True,
                "turn_material_context_id": "context-a",
            },
        ),
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "request_id": "request-a",
                "session_id": "session-a",
                "generation_id": "generation-a",
                "text": "我来整理。",
            },
        ),
        RuntimeEvent(
            type="background_agent.completed",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "整理资料",
                "display_content": "完整正文",
                "delivery_brief": "资料已整理。",
                "source_materials": [],
            },
        ),
    ):
        event_queue.put(event)

    notices: list[str] = []
    released: list[str] = []
    coordinator = TaskResultDeliveryCoordinator(
        retain_material=lambda _context_id: True,
        release_material=lambda context_id: released.append(context_id) or [],
    )

    async def broadcast(event: dict[str, object]) -> None:
        """只让结果卡终态的实时发送失败。"""

        if event.get("type") == "task_result.completed":
            raise ValueError("synthetic result-card broadcast failure")

    async def ignore_activity_broadcast(_projection: ActivityProjection) -> None:
        """测试只关心结果卡通道，不记录任务中心快照广播。"""

    drainer = runtime_event_queue_drainer(
        event_queue=event_queue,
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=lambda _type, _payload, _epoch: ActivityApplyResult(
            "changed", ActivityProjection(revision=1)
        ),
        activity_broadcast_handler=ignore_activity_broadcast,
        activity_notice_handler=lambda event_type, _payload, _epoch: notices.append(
            event_type
        ),
        turn_material_retain_handler=lambda _context_id: True,
        turn_material_release_handler=lambda context_id: released.append(context_id) or [],
        task_result_delivery_coordinator=coordinator,
    )

    await drainer.drain_once()

    assert coordinator.committed_result_card_events()[0]["type"] == "task_result.completed"
    assert notices[-1] == "background_agent.completed"
    assert released == ["context-a"]
    assert drainer.background_task_errors == [
        "ValueError: event_type=background_agent.completed"
    ]

    event_queue.put(
        RuntimeEvent(
            type="background_agent.completed",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "activity_id": activity_id,
                "interaction_session_id": "session-a",
                "goal": "整理资料",
                "display_content": "重复正文",
                "delivery_brief": "重复终态。",
                "source_materials": [],
            },
        )
    )
    await drainer.drain_once()

    assert notices.count("background_agent.completed") == 1
    assert len(coordinator.committed_result_card_events()) == 1


@pytest.mark.asyncio
async def test_empty_drainer_cycle_does_not_attempt_notice_reservation() -> None:
    """空队列轮询不得触发 foreground notice 预留。"""

    reservation_count = 0

    def reserve_notice() -> None:
        """记录意外发生的空批次预留。"""

        nonlocal reservation_count
        reservation_count += 1

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        """忽略测试中不会发生的广播。"""

    drainer = runtime_event_queue_drainer(
        event_queue=Queue(),
        publish=ignore_broadcast,
        publish_ephemeral=ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        foreground_notice_handler=reserve_notice,
    )

    await drainer.drain_once()

    assert reservation_count == 0


@pytest.mark.asyncio
async def test_output_settlement_route_failure_is_retried_without_losing_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """必达 settlement 路由失败后应退避重试，不能像普通事件一样丢弃。"""

    monkeypatch.setattr(
        drainer_module,
        "OUTPUT_SETTLEMENT_RETRY_BASE_DELAY_SECONDS",
        0.0,
    )
    event_queue: Queue[RuntimeEvent] = Queue()
    settlement_event = RuntimeEvent(
        type="assistant.output_settled",
        source="assistant_runtime",
        payload={"request_id": "request-a"},
    )
    event_queue.put(settlement_event)
    route_attempt_count = 0

    async def route_with_transient_failure(event: RuntimeEvent) -> None:
        """第一次模拟归档异常，第二次接受同一结算事件。

        Args:
            event: 事件消费器交付的运行时事件。
        """

        nonlocal route_attempt_count
        assert event.type == "assistant.output_settled"
        route_attempt_count += 1
        if route_attempt_count == 1:
            raise RuntimeError("temporary settlement failure")

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        """本测试只观察内部重试，不产生控制面广播。

        Args:
            _event: 不会实际出现的公开事件。
        """

    drainer = runtime_event_queue_drainer(
        event_queue=event_queue,
        publish=ignore_broadcast,
        publish_ephemeral=ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
    )
    drainer.event_router = SimpleNamespace(dispatch=route_with_transient_failure)

    await drainer.drain_once()
    await asyncio.sleep(0.01)

    assert event_queue.qsize() == 1
    assert route_attempt_count == 1

    await drainer.drain_once()

    assert route_attempt_count == 2
    assert event_queue.qsize() == 0


@pytest.mark.asyncio
async def test_activity_terminal_memory_failure_is_retried_before_notice_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态任务记忆暂时失败时，来源事件必须保留到完整结算成功。"""

    monkeypatch.setattr(
        drainer_module,
        "ACTIVITY_TERMINAL_RETRY_BASE_DELAY_SECONDS",
        0.0,
    )
    memory_store = RuntimeMemoryStore()
    original_record_task_terminal = memory_store.record_task_terminal
    record_attempt_count = 0

    def fail_first_record(
        event_type: str,
        payload: dict[str, object],
    ) -> object:
        """第一次模拟任务记忆写入故障，随后恢复真实实现。"""

        nonlocal record_attempt_count
        record_attempt_count += 1
        if record_attempt_count == 1:
            raise RuntimeError("temporary task memory failure")
        return original_record_task_terminal(event_type, payload)

    monkeypatch.setattr(memory_store, "record_task_terminal", fail_first_record)
    progress_ledger = ActivityProgressLedger()
    event_queue: Queue[RuntimeEvent] = Queue()
    event_queue.put(
        RuntimeEvent(
            type="cognitive_task.completed",
            source="assistant_runtime",
                payload={
                    "activity_id": "cognitive-retry",
                    "brief": "核对事实",
                    "result_text": "已经核对完成。",
                    "summary": "事实核对完成。",
                },
        )
    )

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        """本测试只检查主进程必达结算，不需要控制面连接。"""

    drainer = runtime_event_queue_drainer(
        event_queue=event_queue,
        publish=ignore_broadcast,
        publish_ephemeral=ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=memory_store,
        activity_progress_ledger=progress_ledger,
    )

    await drainer.drain_once()
    await asyncio.sleep(0.01)

    assert event_queue.qsize() == 1
    assert memory_store.recall_task(
        "cognitive-retry",
        purpose="foreground_recall",
    )["state"] == "not_available"

    await drainer.drain_once()

    recalled = memory_store.recall_task(
        "cognitive-retry",
        purpose="foreground_recall",
    )
    assert recalled["terminal_state"] == "completed"
    assert record_attempt_count == 2
    assert event_queue.qsize() == 0


@pytest.mark.asyncio
async def test_superseded_generation_releases_turn_before_permission_notice_retry() -> None:
    """权限提醒替代在途进度后，旧 generation 必须先释放 Turn 再重试播报。"""

    coordinator = NoticeDeliveryCoordinator()
    progress_event = map_activity_event(
        "background_agent.progress",
        {
            "activity_id": "background-a",
            "interaction_session_id": "session-a",
            "goal": "压缩文件",
            "notice_seq": 1,
            "updated_at": 1.0,
        },
    )
    assert progress_event is not None
    coordinator.apply_event(progress_event, 1)
    old_context = coordinator.build_notice_context(
        "session-a",
        mode="system_notice",
        request_id="system-notice-old",
    )
    assert [notice["intent"] for notice in old_context["notice_batch"]] == [
        "progress_report"
    ]
    coordinator.apply_delivery(
        NoticeDeliveryCommand(
            request_id="system-notice-old",
            outcome="request_enqueued",
        )
    )

    permission_event = map_activity_event(
        "background_agent.interaction_requested",
        {
            "activity_id": "background-a",
            "interaction_session_id": "session-a",
            "goal": "压缩文件",
            "request_id": "permission-a",
            "kind": "permission",
            "action_summary": "运行完成这项工作所需的命令",
            "grant_scope": "activity",
            "created_at": 2.0,
        },
    )
    assert permission_event is not None
    supersession = coordinator.apply_event(permission_event, 1)
    assert supersession.superseded_request_ids == ("system-notice-old",)

    realtime_input_runtime = object.__new__(RealtimeInputRuntime)
    realtime_input_runtime.interaction_state = InteractionState(
        interaction_session_id="session-a"
    )
    realtime_input_runtime.current_audio_session_id = "session-a"
    realtime_input_runtime.input_runtime_id = (
        "input-runtime-00000000-0000-4000-8000-000000000001"
    )
    realtime_input_runtime.input_runtime_epoch = 1
    realtime_input_runtime.register_assistant_request(
        "system-notice-old",
        now=2.0,
    )
    event_queue: Queue[RuntimeEvent] = Queue()
    event_queue.put(
        RuntimeEvent(
            type="assistant.generation_interrupted",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "request_id": "system-notice-old",
                "source": "system_notice",
                "reason": "activity_notice_superseded",
            },
        )
    )
    reserved_batches: list[list[dict[str, object]]] = []
    broadcasts: list[dict[str, object]] = []

    def reserve_permission_notice() -> None:
        """在旧 generation 收口后领取仍然有效的权限提醒。"""

        assert realtime_input_runtime.interaction_state.turn_state == "idle"
        context = coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="system-notice-permission",
        )
        reserved_batches.append(context["notice_batch"])

    async def record_broadcast(event: dict[str, object]) -> None:
        """记录旧 generation 释放产品 Turn 产生的公开状态。"""

        broadcasts.append(event)

    drainer = runtime_event_queue_drainer(
        event_queue=event_queue,
        publish=record_broadcast,
        publish_ephemeral=record_broadcast,
        realtime_input_runtime=realtime_input_runtime,
        foreground_notice_handler=reserve_permission_notice,
        notice_delivery_handler=coordinator.apply_delivery,
    )

    await drainer.drain_once()

    assert [
        event["payload"]["turn_state"]
        for event in broadcasts
        if event["type"] == "turn.state_changed"
    ] == ["idle"]
    assert len(reserved_batches) == 1
    assert [notice["intent"] for notice in reserved_batches[0]] == [
        "permission_request"
    ]


@pytest.mark.asyncio
async def test_audio_finished_releases_turn_before_permission_notice_retry() -> None:
    """播音期间到达的权限提醒必须在 audio_finished 同批收口后立即领取。"""

    coordinator = NoticeDeliveryCoordinator()
    realtime_input_runtime = object.__new__(RealtimeInputRuntime)
    realtime_input_runtime.interaction_state = InteractionState(
        interaction_session_id="session-a"
    )
    realtime_input_runtime.current_audio_session_id = "session-a"
    realtime_input_runtime.input_runtime_id = (
        "input-runtime-00000000-0000-4000-8000-000000000001"
    )
    realtime_input_runtime.input_runtime_epoch = 1
    realtime_input_runtime.register_assistant_request("request-a", now=0.0)
    realtime_input_runtime.apply_assistant_audio_publishing_started(
        now=1.0,
        generation_id="generation-a",
        request_id="request-a",
    )

    event_queue: Queue[RuntimeEvent] = Queue()
    event_queue.put(
        RuntimeEvent(
            type="background_agent.interaction_requested",
            source="assistant_runtime",
            runtime_epoch=1,
            produced_at=2.0,
            payload={
                "activity_id": "background-a",
                "interaction_session_id": "session-a",
                "goal": "压缩文件",
                "request_id": "permission-a",
                "kind": "permission",
                "action_summary": "运行完成这项工作所需的命令",
                "grant_scope": "activity",
                "created_at": 2.0,
            },
        )
    )
    reservation_states: list[str] = []
    reserved_batches: list[list[dict[str, object]]] = []
    broadcasts: list[dict[str, object]] = []

    def apply_permission_notice(
        event_type: str,
        payload: dict[str, object],
        _runtime_epoch: int,
    ) -> None:
        """把真实权限来源事件登记为待表达 Notice。"""

        notice_event = map_activity_event(event_type, payload)
        assert notice_event is not None
        coordinator.apply_event(notice_event, 1)

    def reserve_permission_notice() -> None:
        """模拟媒体会话的空闲门禁，并记录权限批次何时被领取。"""

        turn_state = realtime_input_runtime.interaction_state.turn_state
        reservation_states.append(turn_state)
        if turn_state != "idle":
            return
        context = coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="system-notice-permission",
        )
        reserved_batches.append(context["notice_batch"])

    async def record_broadcast(event: dict[str, object]) -> None:
        """记录来源事件和 Turn 收口事件。"""

        broadcasts.append(event)

    drainer = runtime_event_queue_drainer(
        event_queue=event_queue,
        publish=record_broadcast,
        publish_ephemeral=record_broadcast,
        realtime_input_runtime=realtime_input_runtime,
        foreground_notice_handler=reserve_permission_notice,
        activity_notice_handler=apply_permission_notice,
    )

    await drainer.drain_once()

    assert reservation_states == ["speaking"]
    assert reserved_batches == []

    event_queue.put(
        RuntimeEvent(
            type="assistant.audio_finished",
            source="assistant_runtime",
            runtime_epoch=1,
            produced_at=3.0,
            payload={
                "request_id": "request-a",
                "generation_id": "generation-a",
                "playout_completion": "drained",
            },
        )
    )

    await drainer.drain_once()

    assert reservation_states == ["speaking", "idle"]
    assert len(reserved_batches) == 1
    assert [notice["intent"] for notice in reserved_batches[0]] == [
        "permission_request"
    ]
    assert [
        event["payload"]["turn_state"]
        for event in broadcasts
        if event["type"] == "turn.state_changed"
    ] == ["idle"]


@pytest.mark.asyncio
async def test_drainer_does_not_reserve_notice_when_output_is_unavailable() -> None:
    """输出链路失败后仍应消费事件，但不得再创建 system notice 请求。"""

    reservation_count = 0
    event_queue: Queue[RuntimeEvent] = Queue()
    event_queue.put(
        RuntimeEvent(
            type="runtime.internal_error",
            source="test",
            payload={"message": "diagnostic"},
        )
    )

    def reserve_notice() -> None:
        """记录不应发生的提醒预留。"""

        nonlocal reservation_count
        reservation_count += 1

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        """忽略测试中不会发生的广播。"""

    drainer = runtime_event_queue_drainer(
        event_queue=event_queue,
        publish=ignore_broadcast,
        publish_ephemeral=ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        foreground_notice_handler=reserve_notice,
        output_available=lambda: False,
    )

    await drainer.drain_once()

    assert reservation_count == 0
    assert drainer.background_task_errors == ["diagnostic"]


def test_livekit_skips_profile_build_when_session_has_no_pending_notice() -> None:
    """轻量 pending 检查为空时不得构造完整 system notice 请求。"""

    media_session = object.__new__(LiveKitMediaSession)
    media_session.assistant_output_enabled = True
    media_session.assistant_process_bridge = SimpleNamespace(
        is_output_available=lambda: True,
    )
    media_session.pending_notice_provider = lambda _interaction_session_id: False
    media_session.timed_reminder_delivery = None
    media_session.assistant_request_context_preparer = SimpleNamespace(
        prepare=lambda **_kwargs: pytest.fail("不应冻结完整请求资料")
    )
    media_session.turn_admission_sequencer = None
    media_session.realtime_input_runtime = SimpleNamespace(
        interaction_state=SimpleNamespace(
            interaction_session_id="session-a",
            input_state=SimpleNamespace(active=False),
            assistant_output_state=SimpleNamespace(playout_active=False),
            turn_state="idle",
        )
    )

    assert media_session._enqueue_system_notice_request() is None


def test_system_notice_commit_failure_does_not_undo_submitted_request() -> None:
    """System Notice 已入队后，lease 异常只能记录诊断。"""

    submitted_requests: list[object] = []
    attention_commits: list[str] = []

    class AttentionLease:
        """记录 System Notice 是否越过 Attention 提交边界。"""

        def commit_enqueued(self) -> bool:
            """记录 Bridge 已接管请求。"""

            attention_commits.append("committed")
            return True

        def rollback(self) -> bool:
            """已入队请求不得回滚 Attention 资格。"""

            pytest.fail("已入队请求不得回滚 Attention lease")

    def fail_commit() -> bool:
        """模拟请求已入队后 Notice 账本异常。"""

        raise RuntimeError("notice commit failed")

    def submit_request(request: object) -> bool:
        """记录已进入 Bridge 的 System Notice 请求。"""

        submitted_requests.append(request)
        return True

    prepared_context = SimpleNamespace(
        snapshot=SimpleNamespace(
            character_card=None,
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            foreground_context={"notice_batch": [{"notice_id": "notice-a"}]},
            runtime_memory_snapshot=RuntimeMemorySnapshot(),
            history_policy="stable_dialogue",
            activity_progress_references=(),
            derived_from_activity_ids=(),
        ),
        commit_enqueued=fail_commit,
        rollback=lambda: pytest.fail("已入队请求不得回滚"),
    )
    media_session = object.__new__(LiveKitMediaSession)
    media_session.assistant_output_enabled = True
    media_session.assistant_process_bridge = SimpleNamespace(
        is_output_available=lambda: True,
    )
    media_session.pending_notice_provider = lambda _interaction_session_id: True
    media_session.timed_reminder_delivery = None
    media_session.foreground_attention_admission_port = SimpleNamespace(
        reserve_assistant_request=lambda **_kwargs: AttentionLease(),
    )
    media_session.current_runtime_epoch = lambda: 7
    media_session.assistant_request_context_preparer = SimpleNamespace(
        prepare=lambda **_kwargs: prepared_context,
    )
    media_session.turn_admission_sequencer = None
    media_session.realtime_input_runtime = SimpleNamespace(
        current_audio_session_id="session-a",
        interaction_state=SimpleNamespace(
            interaction_session_id="interaction-a",
            input_state=SimpleNamespace(active=False),
            assistant_output_state=SimpleNamespace(playout_active=False),
            turn_state="idle",
        ),
    )
    media_session.enqueue_assistant_request = submit_request

    assert media_session._enqueue_system_notice_request() is None
    assert len(submitted_requests) == 1
    assert attention_commits == ["committed"]


def test_system_notice_does_not_claim_when_attention_reservation_is_rejected() -> None:
    """Attention 先取得所有权时，System Notice 不得领取提醒或进入 Bridge。"""

    media_session = object.__new__(LiveKitMediaSession)
    media_session.assistant_output_enabled = True
    media_session.assistant_process_bridge = SimpleNamespace(
        is_output_available=lambda: True,
    )
    media_session.pending_notice_provider = lambda _interaction_session_id: True
    media_session.timed_reminder_delivery = None
    media_session.foreground_attention_admission_port = SimpleNamespace(
        reserve_assistant_request=lambda **_kwargs: None,
    )
    media_session.current_runtime_epoch = lambda: 7
    media_session.assistant_request_context_preparer = SimpleNamespace(
        prepare=lambda **_kwargs: pytest.fail("Attention active 时不得 claim Notice"),
    )
    media_session.turn_admission_sequencer = None
    media_session.realtime_input_runtime = SimpleNamespace(
        interaction_state=SimpleNamespace(
            interaction_session_id="interaction-a",
            input_state=SimpleNamespace(active=False),
            assistant_output_state=SimpleNamespace(playout_active=False),
            turn_state="idle",
        ),
    )

    assert media_session._enqueue_system_notice_request() is None


def test_system_notice_request_text_references_notice_only_environment() -> None:
    """主动提醒指令必须引用 Prompt 实际渲染的 notice-only 根节点。"""

    assert "<system_notice_environment>" in SYSTEM_NOTICE_REQUEST_TEXT
    assert "<visible_conversation_history>" in SYSTEM_NOTICE_REQUEST_TEXT
    assert "<dynamic_environment>" not in SYSTEM_NOTICE_REQUEST_TEXT
    assert "现在轮到你主动对他说话" in SYSTEM_NOTICE_REQUEST_TEXT
    assert "他没有发新消息" in SYSTEM_NOTICE_REQUEST_TEXT
    assert "也没有人催你" in SYSTEM_NOTICE_REQUEST_TEXT
