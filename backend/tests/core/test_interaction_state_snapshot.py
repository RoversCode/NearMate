"""验证桌面交互状态在自动热区变化、重连和迟到事件下仍可校准。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.tests.runtime_public_event_support import runtime_event_router
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.interaction_state import InteractionState
from companion.realtime.livekit_media_session import LiveKitMediaSession
from companion.realtime.notify_queue import NotifyQueue
from companion.realtime.realtime_input_public_event_handler import RealtimeInputPublicEventHandler
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.runtime_session import InputTurnSession


INPUT_RUNTIME_ID = "input-runtime-00000000-0000-4000-8000-000000000001"


def _runtime() -> RealtimeInputRuntime:
    """构造不加载语音模型、但使用真实状态机的测试 Runtime。

    Args:
        无。

    Returns:
        具有输入身份和初始快照基线的 Runtime。
    """

    runtime = object.__new__(RealtimeInputRuntime)
    runtime.interaction_state = InteractionState(followup_timeout_s=5.0)
    runtime.current_audio_session_id = "media-a"
    runtime.input_turn_session = InputTurnSession.empty()
    runtime.input_runtime_id = ""
    runtime.input_runtime_epoch = 0
    runtime.bind_runtime_identity(input_runtime_id=INPUT_RUNTIME_ID, input_runtime_epoch=1)
    return runtime


def test_text_submission_publishes_automatic_hot_state_and_output_lifecycle() -> None:
    """文字开始、播音和完成均发布完整快照，自动 hot 不能留在旧 cool 展示。"""

    runtime = _runtime()
    initial = runtime.interaction_state_event()["payload"]
    text_events = runtime.apply_text_input_started(now=1.0, request_id="request-a")
    text = text_events[-1]["payload"]
    assert text_events[-1]["type"] == "interaction.state_changed"
    assert initial["revision"] == 0
    assert text["revision"] == 1
    assert text["followup_state"] == "followup_hot"
    assert text["turn_state"] == "processing"
    assert text["session_id"] == runtime.interaction_state.interaction_session_id

    speaking = runtime.apply_assistant_audio_publishing_started(
        now=2.0, request_id="request-a", generation_id="generation-a",
    )[-1]["payload"]
    idle = runtime.apply_assistant_output_finished(
        reason="assistant_audio_finished", now=3.0,
        request_id="request-a", generation_id="generation-a",
    )[-1]["payload"]
    cooled = runtime.apply_timeouts(now=8.0)[-1]["payload"]
    assert [speaking["turn_state"], idle["turn_state"], cooled["followup_state"]] == [
        "speaking", "idle", "followup_cool",
    ]
    assert [speaking["revision"], idle["revision"], cooled["revision"]] == [2, 3, 4]


def test_manual_toggle_source_is_ephemeral_and_reconnect_keeps_current_revision() -> None:
    """手动切换只在本次事件带反馈来源，首帧不会重播按键强调。"""

    runtime = _runtime()
    accepted, _, events, rejected = runtime.apply_followup_toggle(now=1.0)
    manual = events[-1]["payload"]
    reconnect = runtime.interaction_state_event()["payload"]
    assert accepted and rejected is None
    assert manual["source"] == "manual_toggle"
    assert reconnect == {key: value for key, value in manual.items() if key != "source"}
    assert reconnect["revision"] == 1

    runtime.apply_user_turn_started(now=2.0)
    before_rejection = runtime.interaction_state_event()
    assert runtime.apply_followup_toggle(now=3.0)[0] is False
    assert runtime.interaction_state_event() == before_rejection


def test_input_reset_preserves_speaking_in_authoritative_snapshot() -> None:
    """收口输入只能露出仍在继续的助手输出，不能把整个交互误报成空闲。"""

    runtime = _runtime()
    runtime.apply_text_input_started(now=1.0, request_id="request-a")
    runtime.apply_assistant_audio_publishing_started(
        now=2.0, request_id="request-a", generation_id="generation-a",
    )
    runtime.apply_user_turn_started(now=3.0)
    snapshot = runtime.apply_input_reset("track_changed", now=4.0)[-1]["payload"]
    assert snapshot["turn_state"] == "speaking"
    assert snapshot["followup_state"] == "followup_hot"


def test_system_notice_admission_publishes_processing_and_automatic_hot_snapshot() -> None:
    """系统主动表达也应即时更新桌面，不能等下一次语音事件才出现处理状态。"""

    runtime = _runtime()
    session = object.__new__(LiveKitMediaSession)
    session.realtime_input_runtime = runtime
    session.turn_admission_sequencer = None
    session.assistant_request_context_preparer = SimpleNamespace(
        runtime_memory_store=SimpleNamespace(commit_request=lambda _request: None),
    )
    session.notice_delivery_handler = None
    session.event_queue = NotifyQueue()
    session._handle_assistant_request_committed(SimpleNamespace(
        request_id="notice-a", source="system_notice", source_turn_id=None, requested_at=1.0,
    ))
    event = session.event_queue.get_nowait()
    assert event.type == "interaction.state_changed"
    assert event.payload["turn_state"] == "processing"
    assert event.payload["followup_state"] == "followup_hot"
    assert event.payload["revision"] == 1
    assert session.event_queue.empty()


@pytest.mark.asyncio
async def test_interaction_snapshot_routes_once_and_old_runtime_is_absorbed() -> None:
    """完整状态沿输入事件出口发布，旧 Runtime 的迟到快照不能接管当前现场。"""

    runtime = _runtime()
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    router = runtime_event_router(publish=publish, realtime_input_runtime=runtime)
    event = runtime.interaction_state_event()
    await router.dispatch(RuntimeEvent(
        type=event["type"], payload=event["payload"], source="realtime_input", produced_at=1.0,
    ))
    assert published == [event]

    old_payload = {**event["payload"], "input_runtime_epoch": 0}
    await router.dispatch(RuntimeEvent(
        type=event["type"], payload=old_payload, source="realtime_input", produced_at=2.0,
    ))
    assert published == [event]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_patch", [
    {"revision": -1}, {"revision": 2**53}, {"revision": 1.5}, {"revision": True},
    {"input_runtime_epoch": 2**53},
    {"session_id": " "}, {"followup_state": "hot"}, {"turn_state": "thinking"},
    {"source": None}, {"source": "wake"}, {"extra": "not-allowed"},
])
async def test_public_snapshot_rejects_invalid_or_open_payload(invalid_patch) -> None:
    """公共快照严格拒绝未知状态、不安全版本和多余字段。"""

    runtime = _runtime()
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    payload = {**runtime.interaction_state_event()["payload"], **invalid_patch}
    handler = RealtimeInputPublicEventHandler(publish_interaction=publish)
    with pytest.raises(ValidationError):
        await handler.handle(RuntimeEvent(
            type="interaction.state_changed", payload=payload, source="realtime_input",
        ))
    assert published == []


def test_public_snapshot_revision_exhaustion_never_wraps() -> None:
    """版本用尽必须明确失败，不能让新状态绕回零被前端当成旧状态。"""

    state = InteractionState()
    state.public_snapshot()
    state._public_state_revision = 2**53 - 1
    state.on_wake_word(now=1.0)
    with pytest.raises(RuntimeError, match="安全整数上限"):
        state.public_snapshot()
