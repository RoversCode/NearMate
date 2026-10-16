from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from companion.core.api.dependencies import get_realtime_dependencies
from companion.core.api.routers.realtime import control_socket
from companion.core.application.status_broadcaster import ApplicationStatusBroadcaster
from companion.core.control_socket import build_control_socket_initial_events


def test_control_socket_initial_events_include_pending_snapshot_and_delivered_results() -> None:
    """控制 WebSocket 首帧应补发完整待答快照，而不是重放业务过程。"""

    events = build_control_socket_initial_events(
        SimpleNamespace(current_settings_payload=lambda: {"settings_revision": 2}),
        interaction_state=SimpleNamespace(followup_state="idle"),
        activity_snapshot=SimpleNamespace(to_payload=lambda: {"revision": 4}),
        timed_reminder_snapshot=SimpleNamespace(
            to_payload=lambda: {
                "runtime_id": "00000000-0000-4000-8000-0000000000aa",
                "revision": 2,
                "active": [],
                "recent": [],
            }
        ),
        assistant_output_status_snapshot=lambda: {
            "type": "assistant.output_status_changed",
            "payload": {
                "available": True,
                "code": None,
                "message": None,
            },
        },
        input_runtime_snapshot=lambda: {
            "type": "input.runtime_changed",
            "payload": {
                "input_runtime_id": (
                    "input-runtime-00000000-0000-4000-8000-000000000001"
                ),
                "input_runtime_epoch": 1,
            },
        },
        interaction_state_snapshot=lambda: {
            "type": "interaction.state_changed",
            "payload": {
                "input_runtime_id": "input-runtime-00000000-0000-4000-8000-000000000001",
                "input_runtime_epoch": 1,
                "revision": 4,
                "followup_state": "followup_hot",
                "turn_state": "processing",
                "session_id": "interaction-a",
            },
        },
        turn_submissions_snapshot=lambda: {
            "type": "turn_submissions.snapshot_changed",
            "payload": {"revision": 3, "items": []},
        },
        pending_interactions_snapshot=lambda: {
            "type": "background_agent.interactions_snapshot",
            "payload": {"items": [{"request_id": "question-a"}]},
        },
        result_card_events=lambda: [
            {
                "type": "task_result.completed",
                "payload": {"activity_id": "activity-a"},
            }
        ],
    )

    assert [event["type"] for event in events] == [
        "followup.state_changed",
        "input.runtime_changed",
        "interaction.state_changed",
        "settings.changed",
        "assistant.output_status_changed",
        "activities.snapshot_changed",
        "reminders.snapshot_changed",
        "turn_submissions.snapshot_changed",
        "background_agent.interactions_snapshot",
        "task_result.completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "runtime_ready"),
    [("runtime", True), ("status", True), ("runtime", False)],
)
async def test_control_socket_dependency_slice_preserves_application_status(
    scope: str,
    runtime_ready: bool,
) -> None:
    """真实依赖切面不能丢掉首帧 Bootstrap 状态，否则聊天余额不会触发首查。"""

    sent_events: list[dict[str, object]] = []
    websocket = SimpleNamespace(
        query_params={"scope": scope},
        accept=AsyncMock(),
        send_json=AsyncMock(side_effect=sent_events.append),
        receive_text=AsyncMock(side_effect=WebSocketDisconnect),
        close=AsyncMock(),
    )

    async def subscribe_control(current_websocket, initial_events_factory, **_kwargs):
        for event in initial_events_factory():
            await current_websocket.send_json(event)

    hub = SimpleNamespace(
        bootstrap=SimpleNamespace(
            runtime_blocked=not runtime_ready,
            status_payload=lambda: {
                "llm": {"is_ready": True, "missing": [], "errors": []},
                "voice": {"is_ready": True, "phase": "ready"},
                "has_qweather_api_key": False,
                "is_initialized": runtime_ready,
            },
        ),
        current_settings_payload=lambda: {"settings_revision": 0},
        subscribe_control=subscribe_control,
        unsubscribe_control=lambda _websocket: None,
    )
    container = SimpleNamespace(
        hub=hub,
        application_status=ApplicationStatusBroadcaster(hub=hub),
        realtime_input_runtime_owner=SimpleNamespace(
            current_interaction_state=lambda: SimpleNamespace(followup_state="idle"),
            current_runtime_identity_event=lambda: None,
            current_interaction_state_event=lambda: None,
        ),
        assistant_process_supervisor=object(),
        material_lifecycle=object(),
        turn_admission=SimpleNamespace(
            snapshot_event=lambda: {
                "type": "turn_submissions.snapshot_changed",
                "payload": {"revision": 0, "items": []},
            },
        ),
        livekit_session_service=None,
        activity_service=SimpleNamespace(
            desktop_snapshot=lambda: SimpleNamespace(to_payload=lambda: {}),
        ),
        timed_reminder_service=SimpleNamespace(
            snapshot=lambda: SimpleNamespace(to_payload=lambda: {}),
        ),
        background_agent_interactions=SimpleNamespace(
            pending_control_snapshot_event=lambda: {
                "type": "background_agent.interactions_snapshot",
                "payload": {"items": []},
            },
        ),
        task_result_delivery_coordinator=SimpleNamespace(
            committed_result_card_events=lambda: [],
        ),
        livekit_media_session_controller=object(),
        assistant_output_health_ledger=SimpleNamespace(
            snapshot_event=lambda: {
                "type": "assistant.output_status_changed",
                "payload": {"available": True},
            },
        ),
    )

    # 必须经过生产依赖工厂；直接把完整容器传给路由会掩盖漏接服务的问题。
    dependencies = get_realtime_dependencies(container)  # type: ignore[arg-type]
    await control_socket(websocket, dependencies)  # type: ignore[arg-type]

    assert [event["type"] for event in sent_events[:1]] == [
        "bootstrap.status_changed",
    ]
    assert sent_events[0]["payload"]["llm"]["is_ready"] is True
    if scope == "status" or not runtime_ready:
        assert len(sent_events) == 1
    if not runtime_ready:
        websocket.close.assert_awaited_once_with(code=4403, reason="NearMate 运行时尚未就绪")
