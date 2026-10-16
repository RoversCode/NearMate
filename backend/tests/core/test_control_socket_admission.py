"""覆盖等待配置锁期间初始化失败的真实运行连接准入。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocketDisconnect

from companion.core.api.routers.realtime import control_socket
from companion.core.application.status_broadcaster import ApplicationStatusBroadcaster
from companion.core.broadcast import BroadcastHub


@pytest.mark.asyncio
async def test_runtime_socket_rechecks_admission_after_waiting_for_configuration_lock():
    """首检通过后再被阻断，只能发送安全状态，不能泄露业务首帧或登记运行连接。"""
    lock = asyncio.Lock()
    await lock.acquire()
    hub = object.__new__(BroadcastHub)
    hub.configuration_mutation_lock = lock
    hub._connections = []
    hub.bootstrap = SimpleNamespace(runtime_blocked=False, status_payload=lambda: {
        "llm": {"is_ready": True, "missing": [], "errors": []},
        "voice": {"is_ready": True, "phase": "ready"},
        "has_qweather_api_key": False, "is_initialized": False,
    })
    business_snapshot = Mock(side_effect=AssertionError("业务首帧不应在初始化阻断后构造"))
    container = SimpleNamespace(hub=hub, application_status=ApplicationStatusBroadcaster(hub=hub),
        realtime_input_runtime_owner=SimpleNamespace(current_interaction_state=business_snapshot))
    websocket = SimpleNamespace(query_params={"scope": "runtime"}, accept=AsyncMock(), send_json=AsyncMock(),
        receive_text=AsyncMock(side_effect=WebSocketDisconnect), close=AsyncMock())
    connection = asyncio.create_task(control_socket(websocket, container))
    try:
        await asyncio.sleep(0.01)
        # 首检已完成，连接仍在等配置锁；模拟初始化失败。
        hub.bootstrap.runtime_blocked = True
        lock.release()
        await asyncio.wait_for(connection, 1)
    finally:
        if lock.locked():
            lock.release()
        if not connection.done():
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
    business_snapshot.assert_not_called()
    assert hub._connections == []
    assert [call.args[0]["type"] for call in websocket.send_json.await_args_list] == [
        "bootstrap.status_changed",
    ]
    websocket.close.assert_awaited_once_with(code=4403, reason="NearMate 运行时尚未就绪")


@pytest.mark.asyncio
async def test_status_socket_does_not_wait_for_loading_or_read_business_snapshots():
    """加载期间打开状态连接，只读取应用安全投影，不能触碰尚未装配的业务服务。

    Returns:
        无；状态首帧不依赖模型加载持有的配置锁。
    """

    hub = object.__new__(BroadcastHub)
    hub.configuration_mutation_lock = asyncio.Lock()
    hub._connections = []
    hub.bootstrap = SimpleNamespace(runtime_blocked=True, status_payload=lambda: {
        "llm": {"is_ready": True, "missing": [], "errors": []},
        "voice": {"is_ready": True, "phase": "ready"},
        "has_qweather_api_key": False, "is_initialized": False,
    })
    # 不提供业务依赖：status 范围只应向应用状态所有者取首帧。
    container = SimpleNamespace(hub=hub, application_status=ApplicationStatusBroadcaster(hub=hub))
    websocket = SimpleNamespace(query_params={"scope": "status"}, accept=AsyncMock(), send_json=AsyncMock(),
        receive_text=AsyncMock(side_effect=WebSocketDisconnect), close=AsyncMock())
    async with hub.configuration_mutation_lock:
        await asyncio.wait_for(control_socket(websocket, container), timeout=1)
    assert [call.args[0]["type"] for call in websocket.send_json.await_args_list] == [
        "bootstrap.status_changed",
    ]
    websocket.close.assert_not_awaited()
    assert hub._connections == []
