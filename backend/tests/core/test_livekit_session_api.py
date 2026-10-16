"""锁定媒体会话失败时可供安装版显示和重试的 HTTP 契约。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from livekit.rtc import ConnectError

from companion.core.api.dependencies import get_realtime_dependencies
from companion.core.api.routers.realtime import router
from companion.realtime.assistant_process_client import AssistantProcessNotReadyError


def _client(*, initialized=True, service=None):
    app = FastAPI()
    app.include_router(router)
    container = SimpleNamespace(
        hub=SimpleNamespace(bootstrap=SimpleNamespace(
            status_payload=lambda: {"is_initialized": initialized},
        )),
        livekit_session_service=service,
    )
    app.dependency_overrides[get_realtime_dependencies] = lambda: container
    return TestClient(app)


def test_missing_media_configuration_explains_dependency_without_retry_loop():
    response = _client().get("/api/livekit/session")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "语音通信服务未配置，请重新安装完整的 NearMate 安装包；使用外部服务时请检查通信配置。",
        "code": "livekit_not_configured",
        "retryable": False,
    }


@pytest.mark.parametrize("error, code", [
    (ConnectError("token=private-value"), "livekit_connection_failed"),
    (TimeoutError("private endpoint"), "livekit_connection_failed"),
    (OSError("private endpoint"), "livekit_connection_failed"),
    (AssistantProcessNotReadyError("private path"), "assistant_not_ready"),
    (RuntimeError("RealtimeInputRuntime 尚未初始化"), "assistant_not_ready"),
])
def test_known_startup_failure_is_retryable_without_exposing_raw_error(error, code):
    service = SimpleNamespace(create_desktop_session=AsyncMock(side_effect=error))
    response = _client(service=service).get("/api/livekit/session")
    assert response.status_code == 503
    assert response.json()["code"] == code
    assert response.json()["retryable"] is True
    assert "private" not in response.text
    assert isinstance(response.json()["detail"], str)


def test_bootstrap_gate_does_not_depend_on_room_connection():
    response = _client(initialized=False).get("/api/livekit/session")
    assert response.status_code == 409
    assert response.json()["detail"] == "bootstrap is not initialized"


def test_success_keeps_existing_session_payload():
    payload = {"url": "ws://127.0.0.1:12345", "room_name": "test-room"}
    service = SimpleNamespace(create_desktop_session=AsyncMock(
        return_value=SimpleNamespace(to_payload=lambda: payload),
    ))
    assert _client(service=service).get("/api/livekit/session").json() == payload
