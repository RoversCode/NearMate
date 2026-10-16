"""验证 Reminder Notice 在媒体会话中的不可逆 takeover 顺序。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.core.application.assistant_request_models import (
    AssistantRequestContextSnapshot,
)
from companion.realtime.livekit_media_session import LiveKitMediaSession
from companion.core.application.turn_admission import TurnAdmissionSequencer
from companion.realtime.assistant_events import AssistantRequest, RuntimeEvent
from companion.realtime.assistant_request_ownership import AssistantRequestOwnershipLedger
from companion.realtime.interaction_state import InteractionState
from companion.realtime.runtime_identity import AssistantRequestIdentityRegistry
from companion.realtime.visible_output_journal import VisibleOutputSettlement
from companion.runtime_memory.models import RuntimeMemorySnapshot


class RecordingTurnAdmission:
    """记录 Reminder reservation、提交和补偿调用顺序。"""

    def __init__(self, *, reserve_allowed: bool = True) -> None:
        """初始化可配置的准入替身。

        Args:
            reserve_allowed: Reminder 是否能取得唯一 reservation。
        """

        self.reserve_allowed = reserve_allowed
        self.calls: list[str] = []

    def reserve_timed_reminder(self, request_id: str) -> bool:
        """记录不可逆输入清理前的 reservation。

        Args:
            request_id: 本次 Reminder Notice 请求编号。

        Returns:
            构造时指定的准入结果。
        """

        self.calls.append(f"reserve:{request_id}")
        return self.reserve_allowed

    def submit_reserved_timed_reminder(
        self,
        request_id: str,
        submit_request: Any,
    ) -> bool:
        """执行测试 Bridge 提交回调。

        Args:
            request_id: 已经取得 reservation 的请求编号。
            submit_request: 真正提交 AssistantRequest 的回调。

        Returns:
            Bridge 是否接纳请求。
        """

        self.calls.append(f"submit:{request_id}")
        return bool(submit_request())

    def release_notice_reservation(self, request_id: str) -> None:
        """记录构造失败时的 reservation 释放。

        Args:
            request_id: 需要释放的 Reminder Notice 请求编号。
        """

        self.calls.append(f"release:{request_id}")


def build_media_session_for_takeover(
    turn_admission: RecordingTurnAdmission,
) -> tuple[Any, list[str]]:
    """构造只覆盖 Reminder 编排的最小媒体会话。

    Args:
        turn_admission: 记录准入顺序的测试端口。

    Returns:
        媒体会话替身和跨端口调用顺序。
    """

    calls = turn_admission.calls
    reminder = SimpleNamespace(reminder_id="reminder-a")
    batch = SimpleNamespace(reminders=(reminder,))
    snapshot = AssistantRequestContextSnapshot(
        character_card=None,
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        turn_material_context_id=None,
        environment_snapshot=None,
        foreground_context={},
        runtime_memory_snapshot=RuntimeMemorySnapshot(),
        history_policy="stable_dialogue",
    )
    coordinator = SimpleNamespace(
        has_pending=lambda: True,
        freeze_ready_batch=lambda **_kwargs: calls.append("freeze") or batch,
        rollback_prepared_request=lambda _request_id: calls.append("rollback"),
    )
    session = cast(Any, object.__new__(LiveKitMediaSession))
    session.timed_reminder_delivery = coordinator
    session.assistant_request_context_preparer = SimpleNamespace(
        prepare_timed_reminder_notice_snapshot=lambda _batch: snapshot
    )
    session.turn_admission_sequencer = turn_admission
    session.assistant_output_enabled = True
    session._assistant_output_available = lambda: True
    session.realtime_input_runtime = SimpleNamespace(
        current_audio_session_id="audio-a",
        interaction_state=InteractionState(interaction_session_id="interaction-a"),
    )
    session.assistant_request_ownership_ledger = AssistantRequestOwnershipLedger()
    session.assistant_request_identity_registry = AssistantRequestIdentityRegistry()
    session.runtime_identity_factory = SimpleNamespace(
        new=lambda _prefix: "timed-reminder-request-a"
    )
    session.foreground_attention_admission_port = None
    session.current_runtime_epoch = lambda: 1
    session._take_over_unfinished_voice_turn = (
        lambda: calls.append("takeover_input") or True
    )
    session.enqueue_assistant_request = (
        lambda _request: calls.append("enqueue") or True
    )
    return session, calls


def register_settled_output(session: Any, *, source: str, playing: bool = True) -> None:
    """登记真实请求并交付合成及文字终态，保留待播或播放中的输出身份。

    Args:
        session: 测试媒体会话。
        source: 请求来源。
        playing: 是否已经发布首帧音频。
    """

    request = AssistantRequest(
        request_id="old-request",
        source_session_id="audio-a",
        interaction_session_id="interaction-a",
        character_card=cast(Any, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="提醒正文",
        source=cast(Any, source),
        source_turn_id=None,
        requested_at=1.0,
    )
    registry = session.assistant_request_identity_registry
    registry.claim(request)
    registry.bind_generation(request.request_id, "old-generation")
    ledger = session.assistant_request_ownership_ledger
    ledger.claim(request, bridge_id="bridge-a")
    admission = TurnAdmissionSequencer(
        hub=cast(Any, SimpleNamespace()),
        material_lifecycle=cast(Any, SimpleNamespace()),
        dispatch_text_turn=cast(Any, lambda _: None),
        cancel_system_notice=lambda _: None,
    )
    session.turn_admission_sequencer = admission
    admission.register_assistant_request(request.request_id, request.source)
    state = session.realtime_input_runtime.interaction_state
    state.register_assistant_request(
        1.0, request_id=request.request_id, generation_id="old-generation"
    )
    if playing:
        state.on_assistant_audio_publishing_started(
            2.0, request_id=request.request_id, generation_id="old-generation"
        )
    for event_type in ("assistant.audio_finished", "assistant.output_settled"):
        ledger.observe_event(RuntimeEvent(
            type=event_type,
            source="assistant_runtime",
            produced_at=3.0,
            payload={"request_id": request.request_id, "generation_id": "old-generation"},
        ))
    admission.handle_output_settled(VisibleOutputSettlement(
        settlement_id="settled-old",
        request_id=request.request_id,
        generation_id="old-generation",
        history_policy="stable_dialogue",
        outcome="completed",
        visible_text="提醒正文",
        progress_references=(),
        derived_from_activity_ids=(),
        settled_at=3.0,
    ))
    assert ledger.get_request(request.request_id) is None
    assert admission.is_request_unsettled(request.request_id) is False


@pytest.mark.parametrize("source", [
    "system_notice", "timed_reminder_notice", "self_reflection_entry",
    "self_reflection_recovery", "self_reflection_interruption_reply",
])
@pytest.mark.parametrize("playing", [False, True])
def test_settled_protected_output_blocks_takeover_until_audio_ends(source: str, playing: bool) -> None:
    """文字和合成都结算后，受保护请求仍要等实际音频结束才能让出输出。"""

    session, calls = build_media_session_for_takeover(RecordingTurnAdmission())
    register_settled_output(session, source=source, playing=playing)
    assert session.enqueue_timed_reminder_notice() is False
    assert calls == []
    state = session.realtime_input_runtime.interaction_state
    assert state.on_assistant_output_finished(4.0, request_id="old-request", generation_id="old-generation")
    assert session.enqueue_timed_reminder_notice() is True
    assert calls == ["takeover_input", "freeze", "enqueue"]


@pytest.mark.parametrize("source", ["text_input", "final_asr"])
def test_settled_ordinary_audio_is_still_reported_as_interrupted(source: str) -> None:
    """普通回复只剩音频时仍允许接管，且冻结的打断事实必须为真。"""

    session, _ = build_media_session_for_takeover(RecordingTurnAdmission())
    register_settled_output(session, source=source)
    commands = []
    frozen = {}
    session.enqueue_assistant_control = lambda command: commands.append(command) or True
    session.timed_reminder_delivery.freeze_ready_batch = (
        lambda **kwargs: frozen.update(kwargs) or SimpleNamespace(reminders=())
    )
    assert session.enqueue_timed_reminder_notice() is True
    assert frozen["interrupted_assistant"] is True
    assert len(commands) == 1
    assert commands[0].generation_id == "old-generation"


def test_unknown_active_output_does_not_allow_takeover() -> None:
    """有输出而来源尚未登记时先等待，不能把未知来源当作普通回复。"""

    session, calls = build_media_session_for_takeover(RecordingTurnAdmission())
    session.realtime_input_runtime.interaction_state.register_assistant_request(1.0, request_id="unknown")
    assert session.enqueue_timed_reminder_notice() is False
    assert calls == []


def test_mismatched_generation_does_not_allow_takeover() -> None:
    """实时输出与永久身份的生成编号不一致时不得清理用户输入。"""

    session, calls = build_media_session_for_takeover(RecordingTurnAdmission())
    register_settled_output(session, source="text_input")
    state = session.realtime_input_runtime.interaction_state
    state.on_assistant_output_finished(4.0, request_id="old-request")
    state.register_assistant_request(5.0, request_id="old-request", generation_id="mismatched")
    assert session.enqueue_timed_reminder_notice() is False
    assert calls == []


def test_new_protected_output_after_reservation_is_checked_before_input_cleanup() -> None:
    """预留期间新输出接管时必须重新校验，旧取消回执不得释放新输出。"""

    turn_admission = RecordingTurnAdmission()
    session, calls = build_media_session_for_takeover(turn_admission)
    register_settled_output(session, source="system_notice")
    state = session.realtime_input_runtime.interaction_state
    state.on_assistant_output_finished(4.0, request_id="old-request")
    session.turn_admission_sequencer = turn_admission
    original_reserve = turn_admission.reserve_timed_reminder

    def reserve_and_start_protected_output(request_id: str) -> bool:
        """在预留成功与输入清理之间插入受保护输出。"""

        accepted = original_reserve(request_id)
        state.register_assistant_request(5.0, request_id="old-request", generation_id="old-generation")
        assert not state.on_assistant_output_finished(6.0, request_id="unrelated-old-request")
        return accepted

    turn_admission.reserve_timed_reminder = reserve_and_start_protected_output
    assert session.enqueue_timed_reminder_notice() is False
    assert calls == ["reserve:timed-reminder-request-a", "release:timed-reminder-request-a"]


def test_media_session_reserves_before_input_takeover() -> None:
    """输入清理必须发生在 Reminder reservation 之后。"""

    turn_admission = RecordingTurnAdmission()
    session, calls = build_media_session_for_takeover(turn_admission)

    assert session.enqueue_timed_reminder_notice() is True
    assert calls == [
        "reserve:timed-reminder-request-a",
        "takeover_input",
        "freeze",
        "submit:timed-reminder-request-a",
        "enqueue",
    ]


def test_media_session_does_not_touch_input_when_reservation_is_rejected() -> None:
    """准入失败时不得投递任何不可逆输入 takeover。"""

    turn_admission = RecordingTurnAdmission(reserve_allowed=False)
    session, calls = build_media_session_for_takeover(turn_admission)

    assert session.enqueue_timed_reminder_notice() is False
    assert calls == ["reserve:timed-reminder-request-a"]


def test_media_session_releases_reservation_when_input_takeover_fails() -> None:
    """输入 owner 抛出异常时必须释放已取得的 Reminder reservation。"""

    turn_admission = RecordingTurnAdmission()
    session, calls = build_media_session_for_takeover(turn_admission)
    session._take_over_unfinished_voice_turn = lambda: (_ for _ in ()).throw(
        RuntimeError("input worker unavailable")
    )

    assert session.enqueue_timed_reminder_notice() is False
    assert calls == [
        "reserve:timed-reminder-request-a",
        "rollback",
        "release:timed-reminder-request-a",
    ]


def test_cancel_pending_reminder_clears_audio_before_settling_request() -> None:
    """Bridge 待请求被移除前也要先登记音频墓碑，拦住并发迟到音频。"""

    calls: list[str] = []
    session = cast(Any, object.__new__(LiveKitMediaSession))
    session.runtime_event_service = SimpleNamespace(
        cancel_request=lambda request_id: calls.append(f"cancel_text:{request_id}")
    )
    session.assistant_audio_output_scheduler = SimpleNamespace(
        clear_pending_audio_for_request=lambda request_id: calls.append(
            f"clear_audio:{request_id}"
        )
    )
    session.assistant_process_bridge = SimpleNamespace(
        remove_pending_requests=lambda _predicate: calls.append("remove_pending")
        or True
    )
    session.notice_delivery_handler = lambda command: calls.append(
        f"settle:{command.request_id}:{command.outcome}"
    )
    session.turn_admission_sequencer = SimpleNamespace(
        handle_request_submission_failed=lambda request_id: calls.append(
            f"release_admission:{request_id}"
        )
    )
    session.enqueue_assistant_control = lambda _command: calls.append(
        "cancel_generation"
    )

    session.cancel_timed_reminder_request("request-a")

    assert calls == [
        "cancel_text:request-a",
        "clear_audio:request-a",
        "remove_pending",
        "settle:request-a:superseded",
        "release_admission:request-a",
    ]


def test_cancel_started_reminder_clears_audio_before_generation_command() -> None:
    """已启动 Reminder 必须先停 TTS，再向子进程发送精确取消命令。"""

    calls: list[str] = []
    commands: list[object] = []
    session = cast(Any, object.__new__(LiveKitMediaSession))
    session.runtime_event_service = SimpleNamespace(
        cancel_request=lambda request_id: calls.append(f"cancel_text:{request_id}")
    )
    session.assistant_audio_output_scheduler = SimpleNamespace(
        clear_pending_audio_for_request=lambda request_id: calls.append(
            f"clear_audio:{request_id}"
        )
    )
    session.assistant_process_bridge = SimpleNamespace(
        remove_pending_requests=lambda _predicate: calls.append("remove_pending")
        or False
    )
    session.enqueue_assistant_control = lambda command: (
        calls.append("cancel_generation"),
        commands.append(command),
    )

    session.cancel_timed_reminder_request("request-a")

    assert calls == [
        "cancel_text:request-a",
        "clear_audio:request-a",
        "remove_pending",
        "cancel_generation",
    ]
    assert len(commands) == 1
    assert commands[0].request_id == "request-a"
    assert commands[0].reason == "timed_reminder_cancelled"
