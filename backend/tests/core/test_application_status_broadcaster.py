from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion.contracts.events import load_event_catalog
from companion.core.application.status_broadcaster import ApplicationStatusBroadcaster
from companion.core.broadcast import BroadcastHub


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        return None


def _hub(*, initialized: bool = True) -> BroadcastHub:
    """构造状态广播所需的最小 Hub，避免启动完整音频运行时。"""

    hub = BroadcastHub.__new__(BroadcastHub)
    hub._connections = []
    hub._domain_publishers = {}
    hub._event_catalog = load_event_catalog()
    hub.runtime_events_enabled = True
    hub.runtime_logs = SimpleNamespace(append=lambda **_kwargs: None)
    hub.configuration_mutation_lock = __import__("asyncio").Lock()
    hub.bootstrap = SimpleNamespace(
        runtime_blocked=False,
        status_payload=lambda: {
            "is_initialized": initialized,
            "llm": {"is_ready": initialized, "missing": [], "errors": []},
            "voice": {
                "provider": "local_streamvox",
                "is_ready": initialized,
                "phase": "ready" if initialized else "unconfigured",
                "model_id": None,
                "role_name": None,
                "execution": {
                    "model_directory": "C:/Users/demo/NearMate/models",
                    "capabilities": {"backends": [{"path": "C:/CUDA/bin"}]},
                },
                "error": "底层错误：C:/Users/demo/NearMate/runtime-temp/trace.log",
            },
            "has_qweather_api_key": True,
            "release_token": "secret-token",
        },
    )
    return hub






@pytest.mark.asyncio
async def test_status_broadcaster_deduplicates_and_redacts_sensitive_fields() -> None:
    """同一状态只发一次，且事件载荷不会把内部秘密带到前端。"""

    hub = _hub()
    socket = RecordingWebSocket()
    await hub.subscribe_control(socket, lambda: [], scope="status")
    broadcaster = ApplicationStatusBroadcaster(hub=hub)

    await broadcaster.publish_changed(force=True)
    await broadcaster.publish_changed()

    assert [event["type"] for event in socket.sent] == [
        "bootstrap.status_changed",
    ]
    serialized = repr(socket.sent)
    assert "secret-token" not in serialized
    assert "secret-key" not in serialized
    assert "C:/Users/demo" not in serialized
    assert "execution" not in serialized
    assert "capabilities" not in serialized
    assert "底层错误" not in serialized
    bootstrap_payload = socket.sent[0]["payload"]
    assert bootstrap_payload["has_qweather_api_key"] is True  # type: ignore[index]
    assert set(bootstrap_payload) == {  # type: ignore[arg-type]
        "llm",
        "voice",
        "has_qweather_api_key",
        "is_initialized",
    }


@pytest.mark.asyncio
async def test_status_scope_never_receives_business_events() -> None:
    """状态连接只允许 application 领域，业务事件必须被隔离。"""

    hub = _hub()
    socket = RecordingWebSocket()
    await hub.subscribe_control(socket, lambda: [], scope="status")

    await hub.publisher("activity").publish(
        {"type": "activities.snapshot_changed", "payload": {"revision": 1}}
    )
    await hub.publisher("application").publish_ephemeral(
        {"type": "runtime.initialization_changed", "payload": {"state": "failed"}}
    )

    assert [event["type"] for event in socket.sent] == ["runtime.initialization_changed"]


@pytest.mark.asyncio
async def test_runtime_close_keeps_status_connection_alive() -> None:
    """资源清理关闭业务连接时，状态连接必须继续在线。"""

    hub = _hub()
    runtime_socket = RecordingWebSocket()
    status_socket = RecordingWebSocket()
    await hub.subscribe_control(runtime_socket, lambda: [], scope="runtime")
    await hub.subscribe_control(status_socket, lambda: [], scope="status")

    await hub.close_runtime_control_connections()
    await hub.publisher("application").publish_ephemeral(
        {"type": "runtime.initialization_changed", "payload": {"state": "failed"}}
    )

    assert len(hub._connections) == 1
    assert hub._connections[0].websocket is status_socket
    assert status_socket.sent == [{"type": "runtime.initialization_changed", "payload": {"state": "failed"}}]


@pytest.mark.asyncio
async def test_runtime_freeze_projects_initialization_snapshot() -> None:
    """前端通过独立初始化快照保持阻断。"""

    hub = _hub()
    hub.runtime_events_enabled = False
    from companion.core.application.runtime_initialization_state import RuntimeInitializationSnapshot
    snapshot = RuntimeInitializationSnapshot.initial().changed(state="initializing", phase="cleanup",
                                                        operation_id="test-operation")
    hub.bootstrap.runtime_status_provider = snapshot.to_payload
    socket = RecordingWebSocket()
    await hub.subscribe_control(socket, lambda: [], scope="status")
    broadcaster = ApplicationStatusBroadcaster(hub=hub)

    await broadcaster.publish_changed(force=True)

    assert broadcaster.current_events()[0] == {
        "type": "runtime.initialization_changed", "payload": snapshot.to_payload()
    }



@pytest.mark.asyncio
async def test_status_projection_replaces_malformed_nested_bootstrap_values() -> None:
    """状态投影遇到异常嵌套值时也只能广播固定安全形状。"""

    hub = _hub()
    hub.bootstrap.status_payload = lambda: {
        "llm": "C:/secret/llm.json",
        "voice": "C:/secret/voice-state.json",
        "has_qweather_api_key": False,
        "is_initialized": True,
    }
    socket = RecordingWebSocket()
    await hub.subscribe_control(socket, lambda: [], scope="status")
    broadcaster = ApplicationStatusBroadcaster(hub=hub)

    await broadcaster.publish_changed(force=True)

    payload = socket.sent[0]["payload"]
    assert payload["llm"] == {"is_ready": False, "missing": [], "errors": []}
    assert payload["voice"] == {
        "provider": "local_streamvox",
        "is_ready": False,
        "phase": "unconfigured",
        "model_id": None,
        "role_name": None,
        "has_confirmed_voice": False,
    }
