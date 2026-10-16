"""验证运行时替换时的资源回收顺序和失败重试。"""

import asyncio
from threading import RLock
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
import pytest
from companion.core.application.runtime_initialization import RuntimeInitialization

@pytest.fixture
def container():
    """创建独立资源记录器，测试不加载真实音频和助手进程。"""
    hub = SimpleNamespace(configuration_mutation_lock=asyncio.Lock(), runtime_events_enabled=True,
        close_control_connections=AsyncMock(), bootstrap=SimpleNamespace(runtime_blocked=False,
        status_payload=lambda: {"is_initialized": True, "llm": {"is_ready": True}}))
    return SimpleNamespace(hub=hub,
        timed_reminder_scheduler=SimpleNamespace(start=Mock(), shutdown=AsyncMock()),
        timed_reminder_delivery=SimpleNamespace(start=Mock(), shutdown=AsyncMock()),
        timed_reminder_service=None, livekit_session_service=SimpleNamespace(shutdown=AsyncMock()),
        assistant_process_supervisor=SimpleNamespace(shutdown=Mock()),
        realtime_input_runtime_owner=SimpleNamespace(shutdown=Mock()),
        turn_admission=SimpleNamespace(clear_pending_for_runtime_transition=AsyncMock(), resume_after_runtime_transition=Mock()),
        runtime_reconfiguration=SimpleNamespace(restore_runtime=AsyncMock(), runtime_cleanup_steps=lambda: {}))

@pytest.mark.asyncio
async def test_transition_finishes_real_reminders_before_scheduler_restarts(container):
    """旧等待项和响铃项在初始化后都不能被同一调度器重新投递。"""
    from companion.timed_reminders.repository import TimedReminderRepository
    from companion.timed_reminders.service import TimedReminderService
    from companion.timed_reminders.scheduler import TimedReminderScheduler
    current = datetime(2026, 9, 8, tzinfo=UTC)
    service = TimedReminderService(repository=TimedReminderRepository(runtime_id="test"),
        broadcast_snapshot=AsyncMock(), now=lambda: current)
    await service.create(content="先响铃", delay_seconds=1)
    current += timedelta(seconds=2)
    await service.activate_due()
    await service.create(content="稍后提醒", delay_seconds=60)
    ready = Mock()
    scheduler = TimedReminderScheduler(service=service, notify_ready=ready, now=lambda: current)
    service.bind_schedule_changed(scheduler.wake)
    container.timed_reminder_service = service
    container.timed_reminder_scheduler = scheduler
    runtime = RuntimeInitialization(container)
    await runtime._reconcile(retire_current=True)
    assert not service.snapshot().active
    assert len(service.snapshot().recent) == 2
    current += timedelta(seconds=120)
    await runtime.resume()
    try:
        await asyncio.sleep(0)
        ready.assert_not_called()
        assert not service.snapshot().active
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_transition_retries_failed_reminder_without_blocking_other_reminders(container):
    """某项取消失败仍继续关闭后续提醒，授权恢复必须等待失败项补完。"""
    from companion.timed_reminders.repository import TimedReminderRepository
    from companion.timed_reminders.service import TimedReminderService
    service = TimedReminderService(repository=TimedReminderRepository(runtime_id="test"), broadcast_snapshot=AsyncMock())
    first = await service.create(content="第一条", delay_seconds=60)
    await service.create(content="第二条", delay_seconds=120)
    cancel = service.cancel
    failures = 2

    async def fail_first(reminder_id):
        """首项连续两次失败，使一次恢复请求也能检查到未完成清理。"""
        nonlocal failures
        if reminder_id == first.reminder.reminder_id and failures:
            failures -= 1
            raise RuntimeError("暂时失败")
        return await cancel(reminder_id)

    service.cancel = AsyncMock(side_effect=fail_first)
    container.timed_reminder_service = service
    runtime = RuntimeInitialization(container)
    await runtime._reconcile(retire_current=True)
    assert len(service.snapshot().active) == 1
    with pytest.raises(RuntimeError):
        await runtime.resume()
    container.runtime_reconfiguration.restore_runtime.assert_not_called()
    await runtime.resume()
    assert not service.snapshot().active
    assert service.cancel.await_count == 4


@pytest.mark.asyncio
async def test_transition_closes_all_resources_and_retries_only_failed_cleanup(container):
    """提醒关闭异常不能留下助手和音频，重试也不能重复关闭其他资源。"""
    runtime = RuntimeInitialization(container)
    container.timed_reminder_delivery.shutdown.side_effect = [RuntimeError("private"), None]
    await runtime._reconcile(retire_current=True)
    container.assistant_process_supervisor.shutdown.assert_called_once()
    container.realtime_input_runtime_owner.shutdown.assert_called_once()
    container.livekit_session_service.shutdown.assert_awaited_once()
    container.turn_admission.clear_pending_for_runtime_transition.assert_awaited_once()
    assert set(runtime.pending_cleanup) == {"reminder_delivery"}
    await runtime._reconcile(retire_current=True)
    await runtime._reconcile(retire_current=True)
    assert not runtime.pending_cleanup
    assert container.timed_reminder_delivery.shutdown.await_count == 2
    container.assistant_process_supervisor.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_waits_for_configuration_transaction(container):
    """运行时变更尚未提交时，清理不得进入候选发布中间态。"""
    async with container.hub.configuration_mutation_lock:
        task = asyncio.create_task(RuntimeInitialization(container)._reconcile(retire_current=True))
        await asyncio.sleep(0)
        assert not task.done()
        container.assistant_process_supervisor.shutdown.assert_not_called()
    await task
    container.assistant_process_supervisor.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_reinitialization_finishes_cleanup_before_rebuilding(container):
    """新初始化不能跳过旧资源未完成的清理，失败时明确要求稍后恢复。"""
    runtime = RuntimeInitialization(container)
    container.livekit_session_service.shutdown.side_effect = RuntimeError("failed")
    await runtime._reconcile(retire_current=True)
    with pytest.raises(RuntimeError, match="正在关闭"):
        await runtime.resume()
    container.runtime_reconfiguration.restore_runtime.assert_not_called()
    container.livekit_session_service.shutdown.side_effect = None
    await runtime.resume()
    container.runtime_reconfiguration.restore_runtime.assert_awaited_once()
    assert not runtime.pending_cleanup


@pytest.mark.asyncio
async def test_slow_control_connection_cannot_delay_media_or_assistant_shutdown(container):
    """控制发送锁永远不释放时，其他资源仍先停止；挂起关闭不能重复启动。"""
    blocked = asyncio.Event()
    container.hub.close_control_connections.side_effect = blocked.wait
    runtime = RuntimeInitialization(container)
    await runtime._reconcile(retire_current=True)
    container.assistant_process_supervisor.shutdown.assert_called_once()
    container.livekit_session_service.shutdown.assert_awaited_once()
    container.realtime_input_runtime_owner.shutdown.assert_called_once()
    assert "control_connections" in runtime.pending_cleanup
    await runtime._reconcile(retire_current=True)
    container.hub.close_control_connections.assert_awaited_once()
    blocked.set()
    await runtime._reconcile(retire_current=True)
    assert not runtime.pending_cleanup


@pytest.mark.asyncio
async def test_input_waits_for_media_consumers(container):
    """媒体关闭尚未完成时不能释放正在推理的输入模型。"""
    blocked = asyncio.Event()
    container.livekit_session_service.shutdown.side_effect = blocked.wait
    runtime = RuntimeInitialization(container)
    await runtime._reconcile(retire_current=True)
    container.realtime_input_runtime_owner.shutdown.assert_not_called()
    blocked.set()
    await runtime._reconcile(retire_current=True)
    container.realtime_input_runtime_owner.shutdown.assert_called_once()


def test_input_owner_retains_failed_resource_for_retry():
    """资源第一次关闭失败时，拥有者必须保留可再次关闭的引用。"""
    from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
    owner = object.__new__(RealtimeInputRuntimeOwner)
    owner._shutdown_lock = RLock()
    owner._pending_shutdown_runtimes = {}
    resource = SimpleNamespace(shutdown=Mock(side_effect=[RuntimeError("busy"), None]))
    owner.runtime = resource
    owner.hub = SimpleNamespace()
    with pytest.raises(RuntimeError):
        owner.shutdown()
    assert owner.runtime is resource
    owner.shutdown()
    assert owner.runtime is None
    assert resource.shutdown.call_count == 2


def test_assistant_owner_detaches_and_retries_failed_process():
    """失败进程不会继续接收任务，也不会因稳定句柄清空而失去重试引用。"""
    from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
    from companion.realtime.assistant_process_handle import SwappableAssistantProcessClient
    process = SimpleNamespace(shutdown=Mock(side_effect=[RuntimeError("busy"), ()]))
    owner = object.__new__(AssistantProcessSupervisor)
    owner._shutdown_lock = RLock()
    owner._pending_shutdown_clients = {}
    owner.client = SwappableAssistantProcessClient(process)
    owner._target, owner._target_epoch = process, 1
    owner.shutdown_event_handler = owner.retired_event_handler = None
    with pytest.raises(RuntimeError):
        owner.shutdown()
    assert owner._target is process
    owner.shutdown()
    assert owner._target is None
    assert process.shutdown.call_count == 2


@pytest.mark.asyncio
async def test_transition_retires_real_activity_and_preserves_current_epoch(container):
    from uuid import uuid4
    from companion.activities.runtime.registry import ActivityRuntimeRegistry
    from companion.activities.runtime.service import ActivityService
    from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
    from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator
    from companion.realtime.assistant_process_handle import SwappableAssistantProcessClient
    owner = object.__new__(AssistantProcessSupervisor)
    owner._shutdown_lock = RLock()
    owner._pending_shutdown_clients = {}
    process = SimpleNamespace(shutdown=Mock(return_value=()))
    owner.client = SwappableAssistantProcessClient(process)
    owner._target, owner._target_epoch = process, 1
    owner.shutdown_event_handler = owner.retired_event_handler = None
    activities = ActivityService(registry=ActivityRuntimeRegistry(runtime_id=str(uuid4())),
        broadcast_snapshot=AsyncMock(), submit_cancel_command=Mock(), current_runtime_epoch=lambda: owner.client.current_epoch)
    activity_id = str(uuid4())
    activities.apply_source_event("background_agent.started", {"activity_id": activity_id, "goal": "测试任务"}, 1)
    coordinator = object.__new__(RuntimeReconfigurationCoordinator)
    coordinator.assistant_process_supervisor = owner
    coordinator.activity_service = activities
    coordinator.interrupt_runtime_activities = Mock(return_value=[])
    from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
    from backend.tests.activities.runtime_event_router_test_support import _event
    release_material = Mock(side_effect=[RuntimeError("first release failed"), []])
    materials = TaskResultDeliveryCoordinator(retain_material=lambda _: True, release_material=release_material)
    materials.track_tool_acceptance(_event("llm.foreground.tool_call_completed", {
        "name": "start_background_work", "deliverable": True, "state": "accepted",
        "activity_id": activity_id, "request_id": "old-turn", "turn_material_context_id": "context-a",
    }))
    coordinator.interrupt_transition_materials = materials.interrupt_runtime_epoch
    coordinator.interrupt_transition_notices = Mock()
    coordinator.release_runtime_memory_compaction = Mock()
    coordinator.background_agent_interactions = SimpleNamespace(invalidate_runtime_epoch=Mock(return_value=[]))
    coordinator.terminate_self_reflection_runtime = Mock()
    coordinator.hub = container.hub
    coordinator.restore_runtime = AsyncMock()
    container.assistant_process_supervisor = owner
    container.runtime_reconfiguration = coordinator
    runtime = RuntimeInitialization(container)
    await runtime._reconcile(retire_current=True)
    assert "materials" in runtime.pending_cleanup
    assert container.livekit_session_service.shutdown.await_count == 1
    await runtime._reconcile(retire_current=True)
    assert not runtime.pending_cleanup
    assert release_material.call_count == 2
    assert materials.interrupt_runtime_epoch(1) == []
    assert release_material.call_count == 2
    detail = activities.detail(activity_id)
    assert detail.compact.status == "interrupted"
    assert detail.failure.code == "runtime_interrupted"
    assert owner.client.current_epoch == 2
    coordinator.terminate_self_reflection_runtime.assert_called_once_with(1, 2)
    coordinator.release_runtime_memory_compaction.assert_called_once_with(1)
    coordinator.background_agent_interactions.invalidate_runtime_epoch.assert_called_once_with(1)
    await runtime.resume()
    assert not runtime.stopped


@pytest.mark.asyncio
async def test_transition_cleanup_survives_control_connection_reset(container):
    """冻结前的强制广播遇到坏连接时，stopped 与 pending_cleanup 仍必须置位。"""
    from companion.core.application.status_broadcaster import ApplicationStatusBroadcaster
    from companion.core.broadcast import BroadcastHub, _ControlConnection
    from companion.contracts.events import load_event_catalog

    class ResettingWebSocket:
        """模拟初始化阻断广播时直接重置连接的对端。"""

        async def send_json(self, _payload: dict) -> None:
            """始终抛出连接重置错误。"""

            raise ConnectionResetError("client gone")

    class RecordingWebSocket:
        """记录仍在线连接收到的应用状态事件。"""

        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_json(self, payload: dict) -> None:
            """记录一条已送达事件。"""

            self.sent.append(payload)

    healthy_websocket = RecordingWebSocket()
    hub = BroadcastHub.__new__(BroadcastHub)
    hub._connections = [
        _ControlConnection(websocket=ResettingWebSocket(), scope="status"),
        _ControlConnection(websocket=healthy_websocket, scope="status"),
    ]
    hub.runtime_logs = SimpleNamespace(append=lambda **_kwargs: None)
    hub._event_catalog = load_event_catalog()
    hub._domain_publishers = {}
    hub.configuration_mutation_lock = asyncio.Lock()
    hub.runtime_events_enabled = True
    hub.bootstrap = container.hub.bootstrap
    hub.close_control_connections = AsyncMock()
    container.hub = hub
    container.application_status = ApplicationStatusBroadcaster(hub=hub)

    runtime = RuntimeInitialization(container)
    await runtime._reconcile(retire_current=True)

    # 坏连接只被淘汰，不能中止整个替换清理：冻结与收口标记必须置位，
    # 各资源关闭必须真实执行（fixture 的关闭立即完成时 pending_cleanup 会当即排空）。
    assert runtime.stopped is True
    assert container.hub.bootstrap.runtime_blocked is True
    assert hub.runtime_events_enabled is False
    container.assistant_process_supervisor.shutdown.assert_called_once()
    container.realtime_input_runtime_owner.shutdown.assert_called_once()
    container.livekit_session_service.shutdown.assert_awaited_once()
    # 在线状态连接必须收到失效事实，而不是被坏连接一起拖死。
    assert [event["type"] for event in healthy_websocket.sent] == [
        "bootstrap.status_changed",
    ]
