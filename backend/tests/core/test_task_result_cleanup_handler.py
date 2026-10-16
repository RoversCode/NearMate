"""通过真实事件 Handler 验证材料释放失败后的交付与重试边界。"""

import asyncio
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from companion.activities.runtime.registry import ActivityRuntimeRegistry
from companion.activities.runtime.service import ActivityService
from companion.activities.terminal_settlement import ActivityTerminalSettlementRetryableError
from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
from backend.tests.activities.runtime_event_router_test_support import _event
from backend.tests.runtime_public_event_support import runtime_event_queue_drainer, runtime_event_router


async def prepare_activity(router, suffix, *, request_id=None, acknowledge=True):
    """让真实 Registry 与 Handler 接纳任务，并返回尚未交付的完成事件。

    Args:
        router: 接收任务事实的真实路由器。
        suffix: 隔离各任务的请求和材料编号。
        request_id: 显式指定同一轮发起的多个任务；为空时独立生成。
        acknowledge: 是否在返回前结束发起轮次。

    Returns:
        可用来触发完成交付的事件。
    """
    activity_id = str(uuid4())
    payload = {"activity_id": activity_id, "request_id": request_id or "turn-" + suffix,
               "interaction_session_id": "session-a", "goal": "整理材料", "deliverable": True}
    await router.dispatch(_event("llm.foreground.tool_call_completed", payload | {
        "name": "start_background_work", "state": "accepted",
        "turn_material_context_id": "context-" + suffix,
    }))
    await router.dispatch(_event("background_agent.started", payload))
    if acknowledge:
        await router.activity_event_handler.acknowledge_task_result_request(1, payload["request_id"])
    return _event("background_agent.completed", payload | {
        "display_content": "整理后的正文", "delivery_brief": "材料已整理。", "source_materials": [],
    })


def create_router(release, publish, notice):
    """用真实活动账本构造路由器，只替换外部发布和材料端口。

    Args:
        release: 可注入故障的材料释放端口。
        publish: 记录或阻塞公共事件的异步端口。
        notice: 记录完成通知的同步端口。

    Returns:
        路由器及其共享材料交付协调器。
    """
    activities = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=str(uuid4())),
        broadcast_snapshot=AsyncMock(), submit_cancel_command=Mock(), current_runtime_epoch=lambda: 1,
    )
    coordinator = TaskResultDeliveryCoordinator(retain_material=lambda _: True, release_material=release)
    router = runtime_event_router(
        publish=publish, realtime_input_runtime=SimpleNamespace(),
        activity_apply_handler=activities.apply_source_event,
        activity_broadcast_handler=AsyncMock(), activity_notice_handler=notice,
        task_result_delivery_coordinator=coordinator,
    )
    return router, coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_context", ["context-a", "context-b"])
async def test_epoch_failure_stops_suspended_handler_and_returns_all_events_on_retry(failed_context):
    """整批释放失败也必须使等待广播的任务失效，并在重试时交出全部释放事实。"""
    failures = [failed_context]
    released = []
    entered = asyncio.Event()
    resume = asyncio.Event()
    notices = Mock()

    def release(context_id):
        """记录真实成功项，只让指定上下文的首次释放失败。"""
        if context_id in failures:
            failures.remove(context_id)
            raise RuntimeError("temporary")
        released.append(context_id)
        return [{"type": "materials.assets_released", "payload": {"managed_asset_ids": [context_id]}}]

    async def publish(event):
        """让结果卡停在网络广播处，以便在恢复前关闭整个世代。"""
        if event["type"] == "task_result.completed":
            entered.set()
            await resume.wait()

    router, coordinator = create_router(release, publish, notices)
    await prepare_activity(router, "a")
    terminal_b = await prepare_activity(router, "b")
    notices.reset_mock()
    pending = asyncio.create_task(router.dispatch(terminal_b))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(RuntimeError, match="temporary"):
            router.activity_event_handler.interrupt_task_result_deliveries(1)
    finally:
        resume.set()
        await pending
    notices.assert_not_called()
    events = coordinator.interrupt_runtime_epoch(1)
    assert sorted(event["payload"]["managed_asset_ids"][0] for event in events) == ["context-a", "context-b"]
    assert sorted(released) == ["context-a", "context-b"]
    assert coordinator.interrupt_runtime_epoch(1) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["notice", "release"])
async def test_request_acknowledgement_retries_later_claims_after_first_failure(failure_phase):
    """同一轮确认多个任务时，首项失败不能让后续已领取的交付永久卡住。"""
    released = []
    failures = [failure_phase]

    def release(context_id):
        """在首项释放时模拟一次临时故障。"""
        if failures == ["release"]:
            failures.clear()
            raise RuntimeError("temporary")
        released.append(context_id)
        return []

    def notice(event_type, payload, runtime_epoch):
        """在首项完成通知中模拟一次临时故障。"""
        if event_type == "background_agent.completed" and failures == ["notice"]:
            failures.clear()
            raise RuntimeError("temporary")

    router, coordinator = create_router(release, AsyncMock(), notice)
    for suffix in ("a", "b"):
        terminal = await prepare_activity(router, suffix, request_id="shared-turn", acknowledge=False)
        await router.dispatch(terminal)
    with pytest.raises(ActivityTerminalSettlementRetryableError):
        await router.activity_event_handler.acknowledge_task_result_request(1, "shared-turn")
    await router.activity_event_handler.acknowledge_task_result_request(1, "shared-turn")
    assert released == ["context-a", "context-b"]
    assert coordinator.interrupt_runtime_epoch(1) == []


@pytest.mark.asyncio
async def test_normal_terminal_release_failure_retries_without_duplicate_notice():
    """正常终态释放失败应进入队列重试，且不能重复已完成的通知和结果卡。"""
    released_event = {"type": "materials.assets_released", "payload": {"managed_asset_ids": ["asset-a"]}}
    release = Mock(side_effect=[RuntimeError("temporary"), [released_event]])
    publish = AsyncMock()
    notices = Mock()
    router, coordinator = create_router(release, publish, notices)
    terminal = await prepare_activity(router, "a")
    notices.reset_mock()
    publish.reset_mock()
    with pytest.raises(ActivityTerminalSettlementRetryableError):
        await router.dispatch(terminal)
    await router.dispatch(terminal)
    await router.dispatch(terminal)
    assert release.call_count == 2
    notices.assert_called_once()
    assert [call.args[0]["type"] for call in publish.call_args_list] == ["task_result.completed", "materials.assets_released"]
    assert coordinator.interrupt_runtime_epoch(1) == []


@pytest.mark.asyncio
async def test_failed_material_cleanup_does_not_block_another_request():
    """一项任务的材料暂时不可释放，不能阻止无关任务完成和交付。"""
    def release(context_id):
        if context_id == "context-a":
            raise RuntimeError("temporary")
        return []

    notices = Mock()
    router, _ = create_router(release, AsyncMock(), notices)
    terminal_a = await prepare_activity(router, "a")
    with pytest.raises(ActivityTerminalSettlementRetryableError):
        await router.dispatch(terminal_a)
    terminal_b = await prepare_activity(router, "b")
    notices.reset_mock()
    await router.dispatch(terminal_b)
    notices.assert_called_once_with("background_agent.completed", terminal_b.payload, 1)


@pytest.mark.asyncio
async def test_drainer_automatically_retries_failed_terminal_material_release(monkeypatch):
    """真实队列必须自行补放失败事件，不能依赖调用方再次发送同一终态。"""
    import companion.realtime.runtime_event_queue_drainer as drainer_module

    release = Mock(side_effect=[RuntimeError("temporary"), []])
    router, _ = create_router(release, AsyncMock(), Mock())
    terminal = await prepare_activity(router, "a")
    queue = Queue()
    drainer = runtime_event_queue_drainer(
        publish=AsyncMock(), event_queue=queue, realtime_input_runtime=SimpleNamespace(),
    )
    drainer.event_router = router
    monkeypatch.setattr(drainer_module, "ACTIVITY_TERMINAL_RETRY_BASE_DELAY_SECONDS", 0)
    queue.put(terminal)
    await drainer.drain_once()
    assert release.call_count == 1
    # 让已安排的零延迟回调把原始身份重新放回队列，不依赖真实时钟等待。
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert queue.qsize() == 1
    await drainer.drain_once()
    assert release.call_count == 2
    assert queue.empty()
