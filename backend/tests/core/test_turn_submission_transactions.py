from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion.core.application.assistant_request_models import (
    AssistantRequestContextSnapshot,
)
from companion.core.application import turn_submission as turn_submission_module
from companion.core.application.turn_submission import (
    TextTurnDispatch,
    TurnSubmissionOutcome,
    TurnSubmissionService,
)
from companion.foreground_self_reflection.models import (
    InputDiversionResult,
    UnattendedMaterialReference,
)
from companion.materials.models import MaterialAttachment
from companion.realtime.assistant_events import CancelSupersededSystemNoticeCommand


class RecordingPreparedContext:
    """记录请求构造事务最终选择了提交还是回滚。"""

    def __init__(self) -> None:
        """初始化最小有效请求载荷和事务计数。"""

        self.snapshot = AssistantRequestContextSnapshot(
            character_card=None,
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            turn_material_context_id="context-a",
            environment_snapshot=None,
            foreground_context={},
        )
        self.committed = 0
        self.rolled_back = 0

    def commit_enqueued(self) -> bool:
        """记录请求已入队。"""

        self.committed += 1
        return True

    def rollback(self) -> bool:
        """记录入队前失败回滚。"""

        self.rolled_back += 1
        return True


class RecordingAttentionLease:
    """记录普通请求提交资格最终选择了入队还是回滚。"""

    def __init__(self) -> None:
        """初始化尚未结算的测试 lease。"""

        self.committed = 0
        self.rolled_back = 0

    def commit_enqueued(self) -> bool:
        """记录 Assistant Bridge 已经接管请求。"""

        self.committed += 1
        return True

    def rollback(self) -> bool:
        """记录请求在 Bridge 入队前失败。"""

        self.rolled_back += 1
        return True

class RecordingInteractionHub:
    """提供文本提交测试所需的 Interaction 领域 Publisher。"""

    def __init__(self, publish: object) -> None:
        self.publish = publish

    def publisher(self, domain: str) -> SimpleNamespace:
        """返回绑定到 Interaction 的测试 Publisher。

        Args:
            domain: 服务选择的共享事件领域。

        Returns:
            暴露当前可替换 publish 回调的对象。
        """

        assert domain == "interaction"
        return SimpleNamespace(publish=self.publish)


NOOP_MATERIAL_LIFECYCLE = SimpleNamespace(
    remember_recent_materials=lambda _attachments: False,
)


def _user_owned_attachment() -> MaterialAttachment:
    """构造一项应在正式输入成立后进入近期列表的用户自持文件。"""

    return MaterialAttachment(
        material_id="material-a",
        turn_id="turn-a",
        kind="dragged_file",
        source="test",
        display_name="app.py",
        path="D:\\project\\app.py",
        created_at="2026-08-23T10:00:00+08:00",
        node_kind="file",
        media_class="text",
    )


def test_text_notice_cancellation_targets_only_the_selected_request() -> None:
    """文本触发的 Notice 取消必须携带目标 request_id，不能取消当前任意 generation。"""

    commands: list[object] = []
    service = TurnSubmissionService(
        hub=RecordingInteractionHub(lambda _event: None),
        realtime_input_runtime_owner=SimpleNamespace(),
        livekit_media_session_controller=SimpleNamespace(
            enqueue_assistant_control=lambda command: commands.append(command) or True,
        ),
        assistant_request_context_preparer=SimpleNamespace(),
        material_lifecycle=NOOP_MATERIAL_LIFECYCLE,
        assistant_runtime_provider=lambda: SimpleNamespace(),
        now=lambda: 1.0,
    )

    service.cancel_superseded_system_notice("notice-request-a")

    assert commands == [
        CancelSupersededSystemNoticeCommand(
            request_id="notice-request-a",
            reason="text_input",
            issued_at=1.0,
        )
    ]

@pytest.mark.asyncio
async def test_text_submission_forwards_explicit_material_context_id() -> None:
    """文本请求必须把路由创建的材料上下文编号传入请求构造。"""

    captured: dict[str, object] = {}
    broadcasts: list[dict[str, object]] = []
    prepared = RecordingPreparedContext()

    def prepare_context(**kwargs: object) -> RecordingPreparedContext:
        """记录请求构造参数并返回可提交事务。"""

        captured.update(kwargs)
        return prepared

    service = TurnSubmissionService(
        hub=RecordingInteractionHub(lambda _event: None),
                realtime_input_runtime_owner=SimpleNamespace(
                    require_for_turn_state=lambda: SimpleNamespace(
                        interaction_state=SimpleNamespace(interaction_session_id="interaction-a"),
                        apply_text_input_started=lambda **_kwargs: [],
                    )
        ),
        livekit_media_session_controller=SimpleNamespace(
            enqueue_assistant_request=lambda _request: True,
        ),
        assistant_request_context_preparer=SimpleNamespace(
            prepare=prepare_context,
        ),
        material_lifecycle=NOOP_MATERIAL_LIFECYCLE,
        assistant_runtime_provider=lambda: SimpleNamespace(),
        now=lambda: 1.0,
    )

    async def broadcast(event: dict[str, object]) -> None:
        """记录控制面广播。"""

        broadcasts.append(event)

    service.hub.publish = broadcast
    result = await service.dispatch(
        TextTurnDispatch(
            submission_id="submission-a",
            request_id="text-input-submission-a",
            message_text="你好",
            turn_material_context_id="context-a",
            environment_snapshot_id=None,
            attachments=(),
        )
    )

    assert result == TurnSubmissionOutcome(
        request_id="text-input-submission-a",
        assistant_request_started=True,
    )
    assert captured["turn_material_context_id"] == "context-a"
    assert broadcasts[0] == {
        "type": "user.final_transcript",
        "payload": {
            "session_id": "desktop-text",
            "turn_id": None,
            "request_id": "text-input-submission-a",
            "submission_id": "submission-a",
            "text": "你好",
            "history_disposition": "ordinary_turn",
        },
    }
    assert prepared.committed == 1
    assert prepared.rolled_back == 0


@pytest.mark.asyncio
async def test_text_submission_remembers_material_after_transcript_is_public() -> None:
    """普通文本只能在 transcript 发布成功后登记附件，并立即推进材料 revision。"""

    order: list[str] = []
    prepared = RecordingPreparedContext()
    attachment = _user_owned_attachment()

    async def publish(event: dict[str, object]) -> None:
        order.append(f"publish:{event['type']}")

    async def broadcast_changed() -> dict[str, object]:
        order.append("materials:broadcast")
        return {}

    lifecycle = SimpleNamespace(
        remember_recent_materials=lambda attachments: order.append(
            f"materials:remember:{tuple(attachments)[0].material_id}"
        )
        or True,
        broadcast_changed=broadcast_changed,
    )
    service = TurnSubmissionService(
        hub=RecordingInteractionHub(publish),
        realtime_input_runtime_owner=SimpleNamespace(
            require_for_turn_state=lambda: SimpleNamespace(
                interaction_state=SimpleNamespace(
                    interaction_session_id="interaction-a"
                ),
                apply_text_input_started=lambda **_kwargs: [],
            )
        ),
        livekit_media_session_controller=SimpleNamespace(
            enqueue_assistant_request=lambda _request: True,
        ),
        assistant_request_context_preparer=SimpleNamespace(
            prepare=lambda **_kwargs: prepared,
        ),
        material_lifecycle=lifecycle,
        assistant_runtime_provider=lambda: SimpleNamespace(),
        now=lambda: 1.0,
    )

    outcome = await service.dispatch(
        TextTurnDispatch(
            submission_id="submission-material",
            request_id="text-input-material",
            message_text="继续修改这个文件",
            turn_material_context_id="context-a",
            environment_snapshot_id=None,
            attachments=(attachment,),
        )
    )

    assert outcome.assistant_request_started is True
    assert order[:3] == [
        "publish:user.final_transcript",
        "materials:remember:material-a",
        "materials:broadcast",
    ]


@pytest.mark.asyncio
async def test_text_submission_settles_attention_lease_at_bridge_boundary() -> None:
    """普通文本必须在 Bridge 入队成功后提交 Attention lease。"""

    attention_lease = RecordingAttentionLease()
    prepared = RecordingPreparedContext()

    class Admission:
        """返回允许普通文本提交的 Attention reservation。"""

        def divert_final_input(self, _command: object) -> InputDiversionResult:
            """把普通请求 lease 随未改道结果交给提交层。"""

            return InputDiversionResult(
                status="not_diverted",
                reason="assistant_request_reserved",
                assistant_request_lease=attention_lease,
            )

    async def publish(_event: dict[str, object]) -> None:
        """吸收本测试不关心的公开事件。"""

    service = TurnSubmissionService(
        hub=RecordingInteractionHub(publish),
        realtime_input_runtime_owner=SimpleNamespace(
            require_for_turn_state=lambda: SimpleNamespace(
                interaction_state=SimpleNamespace(
                    interaction_session_id="interaction-a",
                ),
                apply_text_input_started=lambda **_kwargs: [],
            ),
        ),
        livekit_media_session_controller=SimpleNamespace(
            enqueue_assistant_request=lambda _request: True,
        ),
        assistant_request_context_preparer=SimpleNamespace(
            prepare=lambda **_kwargs: prepared,
        ),
        material_lifecycle=NOOP_MATERIAL_LIFECYCLE,
        assistant_runtime_provider=lambda: SimpleNamespace(),
        input_diversion_port=Admission(),
        current_runtime_epoch=lambda: 7,
        now=lambda: 1.0,
    )

    result = await service.dispatch(
        TextTurnDispatch(
            submission_id="submission-reserved",
            request_id="text-input-reserved",
            message_text="普通文本",
            turn_material_context_id="context-a",
            environment_snapshot_id=None,
            attachments=(),
        )
    )

    assert result.assistant_request_started is True
    assert attention_lease.committed == 1
    assert attention_lease.rolled_back == 0


@pytest.mark.asyncio
async def test_text_submission_diverts_after_public_bubble_before_prepare() -> None:
    """Attention active 时文本只公开气泡并进入 episode，不冻结普通请求上下文。"""

    broadcasts: list[dict[str, object]] = []
    captured: list[object] = []
    prepare_calls: list[dict[str, object]] = []
    runtime = SimpleNamespace(
        interaction_state=SimpleNamespace(interaction_session_id="interaction-a"),
    )
    material = UnattendedMaterialReference(file_name="input.wav", media_type="audio/wav")

    class Diversion:
        def divert_final_input(self, command: object) -> InputDiversionResult:
            captured.append(command)
            return InputDiversionResult(
                status="accepted",
                reason="input_diverted_to_episode",
            )

    async def publish(event: dict[str, object]) -> None:
        broadcasts.append(event)

    service = TurnSubmissionService(
        hub=RecordingInteractionHub(publish),
        realtime_input_runtime_owner=SimpleNamespace(
            require_for_turn_state=lambda: runtime,
        ),
        livekit_media_session_controller=SimpleNamespace(
            enqueue_assistant_request=lambda _request: (_ for _ in ()).throw(
                AssertionError("diverted text must not enqueue a request")
            ),
        ),
        assistant_request_context_preparer=SimpleNamespace(
            prepare=lambda **kwargs: prepare_calls.append(kwargs)
            or (_ for _ in ()).throw(AssertionError("diverted text must not prepare context")),
        ),
        material_lifecycle=NOOP_MATERIAL_LIFECYCLE,
        assistant_runtime_provider=lambda: (_ for _ in ()).throw(
            AssertionError("diverted text must not read assistant runtime")
        ),
        input_diversion_port=Diversion(),
        current_runtime_epoch=lambda: 7,
        now=lambda: 1.0,
    )

    result = await service.dispatch(
        TextTurnDispatch(
            submission_id="submission-diverted",
            request_id="text-input-submission-diverted",
            message_text="改道文本",
            turn_material_context_id="context-a",
            environment_snapshot_id=None,
            attachments=(),
            materials=(material,),
        )
    )

    assert result == TurnSubmissionOutcome(
        request_id="text-input-submission-diverted",
        assistant_request_started=False,
        diverted_to_episode=True,
    )
    assert [event["type"] for event in broadcasts] == ["user.final_transcript"]
    assert len(captured) == 1
    command = captured[0]
    assert command.runtime_epoch == 7
    assert command.input_identity == "submission-diverted"
    assert command.source == "text"
    assert command.materials == (material,)
    assert prepare_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "epoch"),
    [
        ("port_exception", 7),
        ("episode_memory_append_failed", 7),
        ("runtime_epoch_unavailable", 0),
    ],
)
async def test_text_diversion_failure_blocks_normal_request_before_prepare(
    failure_kind: str,
    epoch: int,
) -> None:
    """改道无法完成时，文本必须释放到失败收口，不能偷偷创建普通请求。"""

    broadcasts: list[dict[str, object]] = []
    prepare_calls: list[dict[str, object]] = []
    enqueue_calls: list[object] = []

    class Diversion:
        """构造测试所需的 Attention 改道失败事实。"""

        def divert_final_input(self, _command: object) -> InputDiversionResult:
            """返回 episode 账本拒绝或模拟端口异常。"""

            if failure_kind == "port_exception":
                raise RuntimeError("attention unavailable")
            return InputDiversionResult(
                status="not_diverted",
                reason=failure_kind,
            )

    async def publish(event: dict[str, object]) -> None:
        """记录公开气泡与后续控制事件。"""

        broadcasts.append(event)

    service = TurnSubmissionService(
        hub=RecordingInteractionHub(publish),
        realtime_input_runtime_owner=SimpleNamespace(
            require_for_turn_state=lambda: SimpleNamespace(
                interaction_state=SimpleNamespace(interaction_session_id="interaction-a"),
            ),
        ),
        livekit_media_session_controller=SimpleNamespace(
            enqueue_assistant_request=lambda request: enqueue_calls.append(request) or True,
        ),
        assistant_request_context_preparer=SimpleNamespace(
            prepare=lambda **kwargs: prepare_calls.append(kwargs)
            or (_ for _ in ()).throw(
                AssertionError("改道失败的文本不能准备普通请求")
            ),
        ),
        material_lifecycle=NOOP_MATERIAL_LIFECYCLE,
        assistant_runtime_provider=lambda: (_ for _ in ()).throw(
            AssertionError("改道失败的文本不能读取 Assistant Runtime")
        ),
        input_diversion_port=Diversion(),
        current_runtime_epoch=lambda: epoch,
        now=lambda: 1.0,
    )

    result = await service.dispatch(
        TextTurnDispatch(
            submission_id=f"submission-{failure_kind}",
            request_id=f"text-input-{failure_kind}",
            message_text="改道失败文本",
            turn_material_context_id="context-a",
            environment_snapshot_id=None,
            attachments=(),
        )
    )

    assert result == TurnSubmissionOutcome(
        request_id=f"text-input-{failure_kind}",
        assistant_request_started=False,
    )
    assert broadcasts == []
    assert prepare_calls == []
    assert enqueue_calls == []


@pytest.mark.asyncio
async def test_text_request_construction_failure_rolls_back_notice_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AssistantRequest 构造失败时必须回收已经预留的提醒批次。"""

    prepared = RecordingPreparedContext()

    async def publish(_event: object) -> None:
        """模拟用户气泡已进入控制面。"""

        return None

    service = TurnSubmissionService(
        hub=RecordingInteractionHub(publish),
        realtime_input_runtime_owner=SimpleNamespace(
                require_for_turn_state=lambda: SimpleNamespace(
                    interaction_state=SimpleNamespace(interaction_session_id="interaction-a"),
                )
        ),
        livekit_media_session_controller=SimpleNamespace(),
        assistant_request_context_preparer=SimpleNamespace(
            prepare=lambda **_kwargs: prepared
        ),
        material_lifecycle=NOOP_MATERIAL_LIFECYCLE,
        assistant_runtime_provider=lambda: SimpleNamespace(),
        now=lambda: 1.0,
    )

    def fail_request_construction(**_kwargs: object) -> object:
        """模拟请求模型校验失败。"""

        raise RuntimeError("request construction failed")

    monkeypatch.setattr(
        turn_submission_module,
        "AssistantRequest",
        fail_request_construction,
    )

    with pytest.raises(RuntimeError, match="request construction failed"):
        await service.dispatch(
            TextTurnDispatch(
                submission_id="submission-a",
                request_id="text-input-submission-a",
                message_text="你好",
                turn_material_context_id="context-a",
                environment_snapshot_id=None,
                attachments=(),
            )
        )

    assert prepared.committed == 0
    assert prepared.rolled_back == 1


@pytest.mark.asyncio
async def test_user_broadcast_failure_does_not_enter_processing_state() -> None:
    """用户气泡广播失败时不得修改 RealtimeInputRuntime，也不存在待回滚 Notice。"""

    prepared = RecordingPreparedContext()
    processing_calls: list[float] = []
    registrations: list[tuple[MaterialAttachment, ...]] = []

    async def fail_user_broadcast(_event: object) -> None:
        raise RuntimeError("control broadcast failed")

    service = TurnSubmissionService(
        hub=RecordingInteractionHub(fail_user_broadcast),
        realtime_input_runtime_owner=SimpleNamespace(
            require_for_turn_state=lambda: SimpleNamespace(
                    interaction_state=SimpleNamespace(
                        interaction_session_id="interaction-a"
                    ),
                    apply_text_input_started=lambda **kwargs: processing_calls.append(
                    kwargs["now"]
                ) or [],
            )
        ),
        livekit_media_session_controller=SimpleNamespace(
            enqueue_assistant_request=lambda _request: True,
        ),
        assistant_request_context_preparer=SimpleNamespace(
            prepare=lambda **_kwargs: prepared
        ),
        material_lifecycle=SimpleNamespace(
            remember_recent_materials=lambda attachments: registrations.append(
                tuple(attachments)
            )
            or True,
        ),
        assistant_runtime_provider=lambda: SimpleNamespace(),
        now=lambda: 1.0,
    )

    with pytest.raises(RuntimeError, match="control broadcast failed"):
        await service.dispatch(
            TextTurnDispatch(
                submission_id="submission-a",
                request_id="text-input-submission-a",
                message_text="你好",
                turn_material_context_id="context-a",
                environment_snapshot_id=None,
                attachments=(_user_owned_attachment(),),
            )
        )

    assert processing_calls == []
    assert registrations == []
    assert prepared.committed == 0
    assert prepared.rolled_back == 0


@pytest.mark.asyncio
async def test_submission_without_media_session_fails_without_bypassing_bridge() -> None:
    """无媒体会话时文本提交必须收口为启动失败，绝不能绕过 Bridge 直连运行时。"""

    prepared = RecordingPreparedContext()
    broadcasts: list[dict[str, object]] = []
    state_transitions: list[str] = []
    runtime_submit_calls: list[object] = []
    memory_commit_calls: list[object] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    def apply_processing(**kwargs: object) -> list[dict[str, object]]:
        assert kwargs["request_id"] == "text-input-submission-a"
        state_transitions.append("processing")
        return [{"type": "turn.state_changed", "payload": {"turn_state": "processing"}}]

    def apply_idle(**kwargs: object) -> list[dict[str, object]]:
        assert kwargs["request_id"] == "text-input-submission-a"
        state_transitions.append("idle")
        return [{"type": "turn.state_changed", "payload": {"turn_state": "idle"}}]

    service = TurnSubmissionService(
        hub=RecordingInteractionHub(broadcast),
        realtime_input_runtime_owner=SimpleNamespace(
            require_for_turn_state=lambda: SimpleNamespace(
                    interaction_state=SimpleNamespace(
                        interaction_session_id="interaction-a"
                    ),
                    apply_text_input_started=apply_processing,
                apply_assistant_output_finished=apply_idle,
            )
        ),
        livekit_media_session_controller=SimpleNamespace(
            enqueue_assistant_request=lambda _request: False,
        ),
        assistant_request_context_preparer=SimpleNamespace(
            prepare=lambda **_kwargs: prepared,
            runtime_memory_store=SimpleNamespace(
                commit_request=lambda request: memory_commit_calls.append(request)
            ),
        ),
        material_lifecycle=NOOP_MATERIAL_LIFECYCLE,
        assistant_runtime_provider=lambda: SimpleNamespace(
            submit_existing_request=lambda request: runtime_submit_calls.append(request)
        ),
        now=lambda: 1.0,
    )

    outcome = await service.dispatch(
        TextTurnDispatch(
            submission_id="submission-a",
            request_id="text-input-submission-a",
            message_text="你好",
            turn_material_context_id="context-a",
            environment_snapshot_id=None,
            attachments=(),
        )
    )

    # 绕过 Bridge 的直连提交会让遗留事件毒化后续新建 Bridge 的身份 Registry；
    # 这里必须只返回结构化失败，由准入层把待发文本置为可重试的失败结果。
    assert outcome == TurnSubmissionOutcome(
        request_id="text-input-submission-a",
        assistant_request_started=False,
    )
    assert runtime_submit_calls == []
    assert memory_commit_calls == []
    assert [event["type"] for event in broadcasts] == [
        "user.final_transcript",
        "turn.state_changed",
        "turn.state_changed",
    ]
    assert state_transitions == ["processing", "idle"]
    assert prepared.committed == 0
    assert prepared.rolled_back == 1
    assert prepared.rolled_back == 1
