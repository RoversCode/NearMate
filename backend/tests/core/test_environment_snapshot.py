from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile, WebSocketDisconnect
from PIL import Image
from starlette.datastructures import Headers

from companion.core.api.dependencies import get_realtime_dependencies
from companion.core.api.routers import realtime
from companion.core.api.routers.realtime import control_socket, update_current_environment
from companion.core.application.assistant_request_context_preparer import AssistantRequestContextPreparer
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.environment.service import EnvironmentContextService
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger


def make_snapshot(
    snapshot_id: str,
    *,
    turn_id: str | None = None,
    image: bytes = b"png",
) -> FrozenEnvironmentSnapshot:
    """构造环境仓储测试使用的不可变快照。"""

    return FrozenEnvironmentSnapshot(
        snapshot_id=snapshot_id,
        captured_at="2026-07-12T00:00:00+08:00",
        active_window_title="Code",
        screenshot_mime_type="image/png" if image else None,
        screenshot_bytes=image or None,
        capture_error=None,
        turn_id=turn_id,
        reason="voice_turn_started" if turn_id else "chat_submit",
    )


class DisconnectingWebSocket:
    """发送初始事件后模拟客户端断开的控制 WebSocket。"""

    def __init__(self) -> None:
        """初始化连接接受状态。"""

        self.accepted = False

    async def accept(self) -> None:
        """记录 Backend 已接受连接。"""

        self.accepted = True

    async def send_json(self, _event: dict[str, object]) -> None:
        """模拟初始事件发送阶段客户端断开。"""

        raise WebSocketDisconnect()


def test_environment_store_never_falls_back_to_latest_snapshot() -> None:
    """文本提交指定不存在编号时不能误用其它 turn 的最新图片。"""

    service = EnvironmentContextService()
    service.update(make_snapshot("snapshot-b", turn_id="turn-b", image=b"B"))

    assert service.claim_snapshot_by_id("missing") is None
    assert service.wait_and_claim_for_turn("turn-a", timeout_seconds=0) is None


def test_frozen_turn_snapshot_survives_next_turn_update() -> None:
    """turn A 已取得的不可变 bytes 不会被 turn B 的上传覆盖。"""

    service = EnvironmentContextService()
    service.update(make_snapshot("snapshot-a", turn_id="turn-a", image=b"A"))
    frozen_a = service.wait_and_claim_for_turn("turn-a", timeout_seconds=0)
    service.update(make_snapshot("snapshot-b", turn_id="turn-b", image=b"B"))

    frozen_b = service.wait_and_claim_for_turn("turn-b", timeout_seconds=0)
    assert frozen_a is not None and frozen_a.screenshot_bytes == b"A"
    assert frozen_b is not None and frozen_b.screenshot_bytes == b"B"
    assert service.current_snapshot() is None
    assert service.current_payload() == frozen_b.to_payload()


def test_environment_store_evicts_oldest_snapshot_over_byte_limit() -> None:
    """待绑定图片超过内存预算时优先淘汰最旧项。"""

    service = EnvironmentContextService(max_pending_bytes=3)
    service.update(make_snapshot("snapshot-a", image=b"AA"))
    service.update(make_snapshot("snapshot-b", image=b"BB"))

    assert service.claim_snapshot_by_id("snapshot-a") is None
    assert service.claim_snapshot_by_id("snapshot-b") is not None


def test_environment_store_limits_metadata_only_items() -> None:
    """无截图失败快照也必须受条目上限约束。"""

    service = EnvironmentContextService(max_pending_items=2)
    service.update(make_snapshot("snapshot-a", image=b""))
    service.update(make_snapshot("snapshot-b", image=b""))
    service.update(make_snapshot("snapshot-c", image=b""))

    assert service.claim_snapshot_by_id("snapshot-a") is None
    assert service.claim_snapshot_by_id("snapshot-b") is not None
    assert service.claim_snapshot_by_id("snapshot-c") is not None


@pytest.mark.asyncio
async def test_environment_endpoint_accepts_png_multipart_without_writing_path() -> None:
    """multipart 接口只保存图片字节和精简元数据。"""

    output = io.BytesIO()
    Image.new("RGB", (1600, 900), color="white").save(output, format="PNG")
    upload = UploadFile(
        file=io.BytesIO(output.getvalue()),
        filename="environment.png",
        headers=Headers({"content-type": "image/png"}),
    )
    environment = EnvironmentContextService()
    container = SimpleNamespace(hub=SimpleNamespace(environment=environment))

    response = await update_current_environment(
        container,
        json.dumps({
            "captured_at": "2026-07-12T00:00:00+08:00",
            "active_window_title": "Code",
            "reason": "chat_submit",
        }),
        upload,
    )

    snapshot = environment.current_snapshot()
    assert snapshot is not None
    assert snapshot.screenshot_bytes == output.getvalue()
    assert snapshot.captured_at == "2026年7月12日 00时00分00秒（UTC+08:00）"
    assert snapshot.to_payload()["screen_available"] is True
    assert "screenshot_path" not in snapshot.to_payload()
    assert response == {"snapshot_id": snapshot.snapshot_id}


@pytest.mark.asyncio
async def test_control_socket_initialization_does_not_depend_on_environment_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """控制 WebSocket 初始化不得引用环境上传端点的 metadata。"""

    websocket = DisconnectingWebSocket()
    connections: list[object] = []

    async def subscribe_control(current_websocket: object, initial_factory: object) -> None:
        connections.append(current_websocket)
        try:
            for event in initial_factory():  # type: ignore[operator]
                await current_websocket.send_json(event)  # type: ignore[attr-defined]
        except WebSocketDisconnect:
            connections.remove(current_websocket)
            raise

    def unsubscribe_control(current_websocket: object) -> None:
        if current_websocket in connections:
            connections.remove(current_websocket)

    container = SimpleNamespace(
        application_status=SimpleNamespace(current_events=lambda: []),
        hub=SimpleNamespace(
            bootstrap=SimpleNamespace(runtime_blocked=False),
            subscribe_control=subscribe_control,
            unsubscribe_control=unsubscribe_control,
        ),
            realtime_input_runtime_owner=SimpleNamespace(
                current_interaction_state=lambda: object(),
                current_runtime_identity_event=lambda: None,
                current_interaction_state_event=lambda: None,
            ),
            activity_service=SimpleNamespace(desktop_snapshot=lambda: object()),
            timed_reminder_service=SimpleNamespace(snapshot=lambda: object()),
            background_agent_interactions=SimpleNamespace(
                pending_control_snapshot_event=lambda: {
                    "type": "background_agent.interactions_snapshot",
                    "payload": {"items": []},
                },
            ),
            task_result_delivery_coordinator=SimpleNamespace(
                committed_result_card_events=lambda: [],
            ),
            turn_admission=SimpleNamespace(
                snapshot_event=lambda: {
                    "type": "turn_submissions.snapshot_changed",
                    "payload": {"revision": 0, "items": []},
                }
            ),
            assistant_output_health_ledger=AssistantOutputHealthLedger(),
        )
    monkeypatch.setattr(realtime, "build_control_socket_initial_events", lambda *_args, **_kwargs: [{}])

    await control_socket(websocket, container)  # type: ignore[arg-type]

    assert websocket.accepted is True
    assert connections == []


def test_realtime_dependencies_include_control_snapshot_ledgers() -> None:
    """实时路由依赖必须携带待答卡和结果卡的首帧账本。"""

    background_agent_interactions = object()
    task_result_delivery_coordinator = object()
    livekit_media_session_controller = object()
    assistant_output_health_ledger = object()
    timed_reminder_service = object()
    container = SimpleNamespace(
        hub=object(),
        application_status=object(),
        realtime_input_runtime_owner=object(),
        assistant_process_supervisor=object(),
        material_lifecycle=object(),
        turn_admission=object(),
        livekit_session_service=object(),
        activity_service=object(),
        timed_reminder_service=timed_reminder_service,
        background_agent_interactions=background_agent_interactions,
        task_result_delivery_coordinator=task_result_delivery_coordinator,
        livekit_media_session_controller=livekit_media_session_controller,
        assistant_output_health_ledger=assistant_output_health_ledger,
    )

    dependencies = get_realtime_dependencies(container)  # type: ignore[arg-type]

    assert dependencies.background_agent_interactions is background_agent_interactions
    assert dependencies.task_result_delivery_coordinator is task_result_delivery_coordinator
    assert dependencies.turn_admission is container.turn_admission
    assert dependencies.timed_reminder_service is timed_reminder_service
    assert dependencies.livekit_media_session_controller is livekit_media_session_controller
    assert dependencies.assistant_output_health_ledger is assistant_output_health_ledger


@pytest.mark.asyncio
async def test_environment_endpoint_rejects_non_png() -> None:
    """错误 MIME 不得进入内存快照仓储。"""

    upload = UploadFile(
        file=io.BytesIO(b"jpeg"),
        filename="environment.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )
    container = SimpleNamespace(hub=SimpleNamespace(environment=EnvironmentContextService()))

    with pytest.raises(HTTPException) as raised:
        await update_current_environment(
            container,
            json.dumps({"captured_at": "2026-07-12T00:00:00+08:00", "reason": "chat_submit"}),
            upload,
        )

    assert raised.value.status_code == 415


@pytest.mark.asyncio
async def test_environment_endpoint_rejects_legacy_metadata_fields() -> None:
    """旧截图路径等字段不得被静默兼容。"""

    container = SimpleNamespace(hub=SimpleNamespace(environment=EnvironmentContextService()))

    with pytest.raises(HTTPException) as raised:
        await update_current_environment(
            container,
            json.dumps({
                "captured_at": "2026-07-12T00:00:00+08:00",
                "reason": "chat_submit",
                "screenshot_path": "G:\\legacy.png",
            }),
            None,
        )

    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_environment_endpoint_rejects_incomplete_voice_binding() -> None:
    """语音快照缺少 turn 标识时不得进入可认领索引。"""

    container = SimpleNamespace(hub=SimpleNamespace(environment=EnvironmentContextService()))

    with pytest.raises(HTTPException) as raised:
        await update_current_environment(
            container,
            json.dumps({
                "captured_at": "2026-07-12T00:00:00+08:00",
                "reason": "voice_turn_started",
                "request_id": "request-a",
            }),
            None,
        )

    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_environment_endpoint_rejects_oversized_raw_metadata() -> None:
    """超大 metadata 必须在 JSON 解析前被拒绝。"""

    container = SimpleNamespace(hub=SimpleNamespace(environment=EnvironmentContextService()))

    with pytest.raises(HTTPException) as raised:
        await update_current_environment(container, "x" * (16 * 1024 + 1), None)

    assert raised.value.status_code == 413


def test_final_asr_timeout_freezes_same_turn_error_without_other_image() -> None:
    """final ASR 超时后只生成本 turn 的无图片错误快照。"""

    environment = SimpleNamespace(
        wait_and_claim_for_turn=lambda turn_id, timeout_seconds: (
            None
            if turn_id == "turn-a" and timeout_seconds == 0.2
            else pytest.fail("final ASR 使用了错误的 turn 或等待时间")
        )
    )
    context = object.__new__(AssistantRequestContextPreparer)
    context.hub = SimpleNamespace(environment=environment)

    frozen = context._freeze_environment_snapshot(
        request_source="final_asr",
        environment_snapshot_id=None,
        source_turn_id="turn-a",
    )

    assert frozen is not None
    assert frozen.turn_id == "turn-a"
    assert frozen.screenshot_bytes is None
    assert frozen.capture_error == "environment_capture_timeout"


def test_system_notice_never_freezes_pending_environment_image() -> None:
    """系统通知只能读取公开元数据，不得持有待绑定图片 bytes。"""

    context = object.__new__(AssistantRequestContextPreparer)
    context.hub = SimpleNamespace(environment=EnvironmentContextService())
    context.hub.environment.update(make_snapshot("snapshot-user", image=b"private"))

    frozen = context._freeze_environment_snapshot(
        request_source="system_notice",
        environment_snapshot_id=None,
        source_turn_id=None,
    )

    assert frozen is None
    assert context.hub.environment.current_payload() is not None
