from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.core.broadcast import BroadcastHub, _ControlConnection
from companion.contracts.events import load_event_catalog


class RecordingWebSocket:
    """记录收到的全部控制事件的在线连接。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, object]) -> None:
        """记录一条已送达事件。"""

        self.sent.append(payload)

    async def close(self) -> None:
        """记录连接已被广播中心主动关闭。"""

        self.closed = True


class BlockingWebSocket:
    """在首条初始快照发送期间制造并发广播窗口。"""

    def __init__(self) -> None:
        self.initial_send_started = asyncio.Event()
        self.allow_initial_send = asyncio.Event()
        self.sent: list[dict[str, object]] = []
        self.active_sends = 0
        self.max_active_sends = 0
        self.send_count = 0

    async def send_json(self, payload: dict[str, object]) -> None:
        """记录并发度，并按测试闸门暂停首条发送。"""

        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        self.send_count += 1
        try:
            if self.send_count == 1:
                self.initial_send_started.set()
                await self.allow_initial_send.wait()
            self.sent.append(payload)
        finally:
            self.active_sends -= 1


def _hub() -> BroadcastHub:
    """构造只包含连接与运行日志端口的轻量广播中心。"""

    hub = BroadcastHub.__new__(BroadcastHub)
    hub._connections = []
    hub.runtime_logs = SimpleNamespace(append=lambda **_kwargs: None)
    hub._event_catalog = load_event_catalog()
    hub._domain_publishers = {}
    hub.configuration_mutation_lock = asyncio.Lock()
    return hub


@pytest.mark.asyncio
async def test_initial_snapshot_and_realtime_broadcast_are_serialized_per_connection() -> None:
    """新连接必须先收到初始 revision，再收到注册后的更高 revision。"""

    hub = _hub()
    websocket = BlockingWebSocket()
    initial_event = {
        "type": "activities.snapshot_changed",
        "payload": {"revision": 10},
    }
    realtime_event = {
        "type": "activities.snapshot_changed",
        "payload": {"revision": 11},
    }

    subscribe_task = asyncio.create_task(
        hub.subscribe_control(
            cast(Any, websocket),
            lambda: [initial_event],
        )
    )
    await websocket.initial_send_started.wait()
    broadcast_task = asyncio.create_task(
        hub.publisher("activity").publish(realtime_event)
    )
    await asyncio.sleep(0)

    assert broadcast_task.done() is False
    assert websocket.max_active_sends == 1

    websocket.allow_initial_send.set()
    await asyncio.gather(subscribe_task, broadcast_task)

    assert [event["payload"]["revision"] for event in websocket.sent] == [10, 11]
    assert websocket.max_active_sends == 1


@pytest.mark.asyncio
async def test_status_subscription_receives_progress_while_configuration_is_loading() -> None:
    """模型加载占着配置锁时，状态连接仍能先收首帧，再接收后续进度。

    Returns:
        无；业务连接的配置锁边界仍由相邻测试验证。
    """

    hub = _hub()
    websocket = BlockingWebSocket()
    initial_event = {
        "type": "runtime.initialization_changed",
        "payload": {"revision": 1, "phase": "input_models"},
    }
    next_event = {
        "type": "runtime.initialization_changed",
        "payload": {"revision": 2, "phase": "assistant"},
    }
    async with hub.configuration_mutation_lock:
        subscription = asyncio.create_task(hub.subscribe_control(
            cast(Any, websocket), lambda: [initial_event], scope="status",
        ))
        broadcast = None
        try:
            # 这把锁代表尚未结束的模型加载；状态订阅不能等它释放才登记。
            await asyncio.wait_for(websocket.initial_send_started.wait(), timeout=1)
            broadcast = asyncio.create_task(hub.publisher("application").publish_ephemeral(next_event))
            await asyncio.sleep(0)
            assert websocket.sent == []
            assert not broadcast.done()
            websocket.allow_initial_send.set()
            await asyncio.wait_for(asyncio.gather(subscription, broadcast), timeout=1)
        finally:
            websocket.allow_initial_send.set()
            for task in (subscription, broadcast):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (subscription, broadcast) if task is not None),
                                 return_exceptions=True)
    assert websocket.sent == [initial_event, next_event]
    assert websocket.max_active_sends == 1


@pytest.mark.asyncio
async def test_initial_settings_snapshot_waits_for_configuration_commit() -> None:
    """控制连接不能在设置事务提交点前读取临时内存值。"""

    hub = _hub()
    websocket = BlockingWebSocket()
    state = {"settings_revision": 10}
    hub.current_settings_payload = lambda: dict(state)

    await hub.configuration_mutation_lock.acquire()
    subscribe_task = asyncio.create_task(
        hub.subscribe_control(
            cast(Any, websocket),
            lambda: [
                {
                    "type": "settings.changed",
                    "payload": hub.current_settings_payload(),
                }
            ],
        )
    )
    await asyncio.sleep(0)

    state["settings_revision"] = 11
    assert subscribe_task.done() is False
    hub.configuration_mutation_lock.release()

    await websocket.initial_send_started.wait()
    websocket.allow_initial_send.set()
    await subscribe_task

    assert websocket.sent[0]["payload"]["settings_revision"] == 11


@pytest.mark.asyncio
async def test_slow_initial_send_does_not_hold_configuration_mutation_lock() -> None:
    """初始帧网络发送变慢时，新的设置事务仍能先完成提交。"""

    hub = _hub()
    websocket = BlockingWebSocket()
    commit_finished = asyncio.Event()

    subscribe_task = asyncio.create_task(
        hub.subscribe_control(
            cast(Any, websocket),
            lambda: [{"type": "activities.snapshot_changed", "payload": {}}],
        )
    )
    await websocket.initial_send_started.wait()

    async def record_configuration_commit() -> None:
        """模拟只需要取得配置门禁的设置提交。"""

        async with hub.configuration_mutation_lock:
            commit_finished.set()

    commit_task = asyncio.create_task(record_configuration_commit())
    await asyncio.wait_for(commit_finished.wait(), timeout=1)
    assert commit_task.done() is True
    assert subscribe_task.done() is False

    websocket.allow_initial_send.set()
    await subscribe_task


@pytest.mark.asyncio
async def test_broadcast_skips_reset_connection_and_delivers_to_others() -> None:
    """单条连接被对端重置时，其余连接仍必须收到同一事件。"""

    hub = _hub()

    class ResettingWebSocket:
        """模拟发送时直接重置连接的对端。"""

        def __init__(self) -> None:
            self.closed = False

        async def send_json(self, _payload: dict[str, object]) -> None:
            """始终抛出连接重置错误。"""

            raise ConnectionResetError("client gone")

        async def close(self) -> None:
            """记录广播中心仍然尝试关闭坏连接。"""

            self.closed = True

    resetting_websocket = ResettingWebSocket()
    hub._connections = [
        _ControlConnection(websocket=cast(Any, resetting_websocket), scope="status"),
        _ControlConnection(websocket=cast(Any, RecordingWebSocket()), scope="status"),
    ]
    healthy_connection = hub._connections[1]
    event = {"type": "runtime.initialization_changed", "payload": {"state": "failed"}}

    await hub._send_to_control_connections(event)

    assert cast(Any, healthy_connection.websocket).sent == [event]
    # 坏连接被逐出订阅列表，好连接保留继续接收后续广播。
    assert hub._connections == [healthy_connection]
    assert resetting_websocket.closed is True


@pytest.mark.asyncio
async def test_broadcast_times_out_stuck_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发送永远不返回的连接必须在限时后淘汰，不能卡住整轮广播。"""

    hub = _hub()

    class StuckWebSocket:
        """模拟发送缓冲爆满、永不完成的连接。"""

        def __init__(self) -> None:
            self.closed = False

        async def send_json(self, _payload: dict[str, object]) -> None:
            """永久挂起等待永远不会到来的发送完成。"""

            await asyncio.Event().wait()

        async def close(self) -> None:
            """记录超时连接被主动关闭。"""

            self.closed = True

    stuck_websocket = StuckWebSocket()
    hub._connections = [
        _ControlConnection(websocket=cast(Any, stuck_websocket), scope="status"),
        _ControlConnection(websocket=cast(Any, RecordingWebSocket()), scope="status"),
    ]
    healthy_connection = hub._connections[1]
    monkeypatch.setattr(
        "companion.core.broadcast.CONTROL_CONNECTION_SEND_TIMEOUT_SECONDS",
        0.05,
    )
    event = {"type": "runtime.initialization_changed", "payload": {"state": "failed"}}

    await hub._send_to_control_connections(event)

    assert cast(Any, healthy_connection.websocket).sent == [event]
    assert hub._connections == [healthy_connection]
    assert stuck_websocket.closed is True
