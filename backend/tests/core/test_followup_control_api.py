"""跟进快捷键必须在回执前取消语音，并返回可抵抗迟到交付的请求身份。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.core.api.dependencies import get_realtime_dependencies
from companion.core.api.routers.realtime import router


def _client(*, accepted: bool = True, failure: Exception | None = None):
    event = {"type": "followup.state_changed", "payload": {
        "followup_state": "followup_cool", "source": "manual_toggle",
    }}
    state_event = {"type": "interaction.state_changed", "payload": {
        "input_runtime_id": "input-runtime-00000000-0000-4000-8000-000000000001",
        "input_runtime_epoch": 1, "revision": 2, "followup_state": "followup_cool",
        "turn_state": "idle", "session_id": "interaction-a", "source": "manual_toggle",
    }}
    interrupt = Mock(return_value=("playing", "queued"), side_effect=failure)
    publish = AsyncMock()
    container = SimpleNamespace(
        hub=SimpleNamespace(publisher=lambda _domain: SimpleNamespace(publish=publish)),
        realtime_input_runtime_owner=SimpleNamespace(require_for_turn_state=lambda: SimpleNamespace(
            apply_followup_toggle=lambda **_kwargs: (
                accepted, "followup_cool", [event, state_event] if accepted else [],
                None if accepted else "user_input_active",
            ),
        )),
        livekit_media_session_controller=SimpleNamespace(interrupt_current_output=interrupt),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_realtime_dependencies] = lambda: container
    return TestClient(app), interrupt, publish, event


def test_toggle_cancels_before_publishing_and_returns_exact_request_identities():
    client, interrupt, publish, event = _client()
    order: list[str] = []
    interrupt.side_effect = lambda **_kwargs: (order.append("interrupt") or ("playing", "queued"))
    publish.side_effect = lambda _event: order.append("publish")
    response = client.post("/api/followup/toggle")
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["followup_state"] == "followup_cool"
    assert body["interrupted_request_ids"] == ["playing", "queued"]
    assert body["speech_interrupted_at"] == interrupt.call_args.kwargs["issued_at"]
    assert order == ["interrupt", "publish", "publish"]
    assert publish.await_args_list[0].args[0] == event
    assert body["interaction_state"] == publish.await_args_list[1].args[0]["payload"]


def test_rejected_toggle_does_not_cancel_an_active_user_input():
    client, interrupt, publish, _event = _client(accepted=False)
    response = client.post("/api/followup/toggle")
    assert response.status_code == 409
    interrupt.assert_not_called()
    publish.assert_not_awaited()


@pytest.mark.parametrize("failure", [RuntimeError("private path"), OSError("private key")])
def test_failed_interruption_is_not_acknowledged_as_stopped(failure):
    client, _interrupt, publish, event = _client(failure=failure)
    response = client.post("/api/followup/toggle")
    assert response.status_code == 503
    assert "private" not in response.text
    assert "interrupted_request_ids" not in response.json()
    # 状态切换已成立，取消失败也必须广播真实状态，不能让两个前端继续显示旧值。
    assert publish.await_count == 2
    assert publish.await_args_list[0].args[0] == event
    assert publish.await_args_list[1].args[0]["type"] == "interaction.state_changed"
