import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from threading import RLock
from typing import Any, cast

import pytest

from companion.background_agent.interaction_registry import (
    BackgroundAgentInteractionRegistry,
)
from companion.background_agent.interaction_service import (
    BackgroundAgentInteractionService,
)
from companion.core.application import container as container_module
from companion.core.application.material_lifecycle import MaterialLifecycleService
from companion.character_cards.models import CharacterCardDefinition
from companion.materials.models import MaterialAttachment
from companion.realtime import livekit_media_session as media_session_module
from companion.realtime.assistant_events import AssistantRequest, RuntimeEvent
from companion.realtime.livekit_media_session import (
    LiveKitMediaSession,
    LiveKitMediaSessionController,
)
from companion.realtime.notify_queue import NotifyQueue
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.runtime_public_event_publisher import (
    RuntimePublicEventPublishers,
)
from backend.tests.runtime_public_event_support import (
    runtime_public_event_publishers,
)


@pytest.mark.asyncio
async def test_media_session_start_resumes_timed_reminders_after_output_is_ready() -> None:
    """媒体会话完成全部启动步骤后，必须重新开放此前暂停的提醒。"""

    events: list[object] = []

    class RecordingRoom:
        """记录连接调用的最小 LiveKit Room。"""

        async def connect(
            self,
            url: str,
            token: str,
            *,
            options: object,
        ) -> None:
            """记录测试连接参数。"""

            events.append(("connect", url, token, options))

    session = cast(Any, object.__new__(LiveKitMediaSession))
    session.realtime_input_runtime = SimpleNamespace(
        bind_audio_session=lambda source_session_id: events.append(
            ("bind", source_session_id)
        )
    )
    session._setup_event_handlers = lambda: events.append("handlers")
    session.room = RecordingRoom()
    session.assistant_output_enabled = False
    session.runtime_event_service = None
    session._start_runtime_tasks = lambda: events.append("tasks")
    session.timed_reminder_delivery = SimpleNamespace(
        resume_requests=lambda: events.append("reminder-resume")
    )

    await session.start(
        url="ws://livekit.test",
        token="backend-token",
        identity="backend-media",
        source_session_id="companion-local",
    )

    assert events[-2:] == ["tasks", "reminder-resume"]


def test_interrupt_runtime_activities_releases_task_result_material_after_notice_failure() -> None:
    """Notice 收口异常时仍必须释放同一世代 TaskResult 的材料租约。"""

    release_calls: list[int] = []

    class RuntimeEventServiceStub:
        """记录 TaskResult 中断端口调用。"""

        def interrupt_task_result_deliveries(
            self,
            runtime_epoch: int,
        ) -> list[dict[str, object]]:
            """返回一条可验证的材料释放事实。"""

            release_calls.append(runtime_epoch)
            return [{"type": "materials.assets_released", "payload": {}}]

    media_session = cast(Any, object.__new__(LiveKitMediaSession))
    media_session.activity_notice_interruption_handler = (
        lambda _runtime_epoch, _strategy: (_ for _ in ()).throw(
            RuntimeError("notice interruption failed")
        )
    )
    media_session.runtime_event_service = RuntimeEventServiceStub()
    media_session.task_result_delivery_coordinator = cast(Any, None)

    with pytest.raises(RuntimeError, match="notice interruption failed"):
        media_session.interrupt_runtime_activities(7)

    assert release_calls == [7]


def test_media_session_does_not_construct_an_event_drainer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """媒体会话只构造输入 Worker，不能重新取得 RuntimeEvent 消费权。

    Args:
        monkeypatch: pytest 提供的依赖替换工具。
    """

    worker_dependencies: dict[str, object] = {}

    class RecordingWorker:
        """记录输入 Worker 实际收到的装配参数。"""

        def __init__(self, **dependencies: object) -> None:
            worker_dependencies.update(dependencies)

    class FakeRealtimeInputRuntime:
        """提供媒体会话构造所需的最小输入 Runtime。"""

        def __init__(self) -> None:
            self.realtime_audio_config = SimpleNamespace(
                latency=SimpleNamespace(enabled=False),
                debug_audio=SimpleNamespace(enabled=False),
            )
            self.asr_provider = object()
            self.vad_provider = object()
            self.smart_turn_provider = object()
            self.current_audio_session_id = "session-a"
            self.bound_worker: object | None = None

        def bind_realtime_input_worker(self, worker: object) -> None:
            """保存媒体会话创建的输入 Worker。"""

            self.bound_worker = worker

    monkeypatch.setattr(media_session_module, "RealtimeInputWorker", RecordingWorker)
    monkeypatch.setattr(media_session_module, "AsrScheduler", lambda **_kwargs: object())
    monkeypatch.setattr(media_session_module, "AudioRuntimeThread", lambda **_kwargs: object())

    async def ignore_event(_event: dict[str, object]) -> None:
        """提供媒体会话构造所需的无副作用广播端口。"""

    runtime = FakeRealtimeInputRuntime()
    session = LiveKitMediaSession(
        realtime_input_runtime=cast(RealtimeInputRuntime, runtime),
        public_event_publishers=runtime_public_event_publishers(ignore_event),
        assistant_runtime=None,
        assistant_output_enabled=False,
        room=object(),
        assistant_request_context_preparer=SimpleNamespace(runtime_memory_store=None),
    )

    assert "current_input_runtime_identity" not in worker_dependencies
    assert not hasattr(session, "runtime_event_queue_drainer")


def test_committed_request_registers_turn_barrier_when_memory_commit_fails() -> None:
    """运行期记忆异常不能让已入队的 System Notice 绕过 Turn 顺序屏障。"""

    registered_requests: list[tuple[str, str]] = []
    interaction_requests: list[str] = []
    session = object.__new__(LiveKitMediaSession)
    session.turn_admission_sequencer = SimpleNamespace(
        register_assistant_request=lambda request_id, source: (
            registered_requests.append((request_id, source))
        )
    )
    session.assistant_request_context_preparer = SimpleNamespace(
        runtime_memory_store=SimpleNamespace(
            commit_request=lambda _request: (_ for _ in ()).throw(
                RuntimeError("memory unavailable")
            )
        )
    )
    session.realtime_input_runtime = SimpleNamespace(
        interaction_state=SimpleNamespace(
            register_assistant_request=lambda _now, *, request_id: (
                interaction_requests.append(request_id)
            )
        )
    )
    session.notice_delivery_handler = None
    request = AssistantRequest(
        request_id="notice-a",
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="请汇报进度。",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
    )

    session._handle_assistant_request_committed(request)

    assert registered_requests == [("notice-a", "system_notice")]
    assert interaction_requests == ["notice-a"]


def test_livekit_media_session_factory_binds_material_ownership_handlers(
    monkeypatch,
) -> None:
    """验证 LiveKit 媒体会话构造时绑定完整的材料租约与 turn 终态入口。"""

    captured_dependencies: dict[str, object] = {}
    captured_dependency_sets: list[dict[str, object]] = []
    runtime_event_queue = NotifyQueue()

    class FakeLiveKitMediaSession:
        """记录组合根传给 LiveKit 媒体会话的依赖。"""

        def __init__(self, **dependencies: object) -> None:
            captured_dependencies.update(dependencies)
            captured_dependency_sets.append(dependencies)

    class RecordingDomainPublisher:
        """记录组合根申请的领域，并提供真实异步方法形状。"""

        def __init__(self, domain: str) -> None:
            self.domain = domain

        async def publish(self, _event: dict[str, object]) -> None:
            """提供无副作用的持久发布端口。"""

        async def publish_ephemeral(self, _event: dict[str, object]) -> None:
            """提供无副作用的瞬时发布端口。"""

    hub = SimpleNamespace(
        material_revision=0,
        turn_materials=SimpleNamespace(staged_preflight_snapshot=lambda: (0, [], "closed")),
        publisher=lambda domain: RecordingDomainPublisher(domain),
    )
    lifecycle = MaterialLifecycleService(hub=hub)
    realtime_input_runtime_owner = SimpleNamespace(
        require_for_livekit=lambda: object(),
        current_runtime_identity=lambda: None,
    )
    assistant_process_supervisor = SimpleNamespace(
        output_enabled=False,
        client=SimpleNamespace(current_epoch=7),
    )
    assistant_request_context_preparer = SimpleNamespace(
        prepare=lambda **_kwargs: None,
        handle_activity_event=lambda _event_type, _payload, _runtime_epoch: None,
        apply_notice_delivery=lambda _command: None,
        interrupt_activity_notices=lambda _runtime_epoch, _strategy: None,
    )
    notice_delivery_coordinator = SimpleNamespace(
        has_pending_notice=lambda _interaction_session_id: False,
        interrupt_active=lambda _runtime_epoch, _strategy: None,
        apply_delivery=lambda _command: None,
    )
    activity_service = SimpleNamespace(
        apply_source_event=lambda _event_type, _payload, _runtime_epoch: None,
        broadcast_projection=lambda _projection: None,
        interrupt_active=lambda _runtime_epoch, _error_text: None,
    )
    background_agent_interactions = BackgroundAgentInteractionRegistry()
    background_agent_interaction_service = BackgroundAgentInteractionService(
        registry=background_agent_interactions,
        runtime_provider=lambda: object(),  # type: ignore[return-value]
        current_runtime_epoch=lambda: 7,
    )
    background_agent_usage_service = SimpleNamespace(
        record_terminal_event=lambda _event_type, _payload: None,
    )
    runtime_event_service = object()
    timed_reminder_delivery = object()
    monkeypatch.setattr(
        container_module,
        "LiveKitMediaSession",
        FakeLiveKitMediaSession,
    )

    factory = container_module.LiveKitMediaSessionFactory(
        settings=SimpleNamespace(
            livekit_aec_identity_prefix="desktop-aec",
            assistant_request_handoff_timeout_s=0.25,
        ),
        runtime_event_queue=runtime_event_queue,
        hub=hub,
        realtime_input_runtime_owner=realtime_input_runtime_owner,
        assistant_process_supervisor=assistant_process_supervisor,
        assistant_request_context_preparer=assistant_request_context_preparer,
        notice_delivery_coordinator=notice_delivery_coordinator,
        timed_reminder_delivery=timed_reminder_delivery,  # type: ignore[arg-type]
        material_lifecycle=lifecycle,
        activity_service=activity_service,
        background_agent_interactions=background_agent_interactions,
        background_agent_interaction_service=background_agent_interaction_service,
        background_agent_usage_service=background_agent_usage_service,
        runtime_event_service=runtime_event_service,
    )

    factory()
    factory._diversion_materials_by_input["turn-1"] = None
    factory()

    assert captured_dependencies["turn_material_retain_handler"] == lifecycle.retain_turn
    assert captured_dependencies["turn_material_final_handler"] == lifecycle.finish_turn
    assert captured_dependencies["turn_material_release_handler"] == lifecycle.release_turn
    assert (
        captured_dependencies["activity_apply_handler"]
        == activity_service.apply_source_event
    )
    assert (
        captured_dependencies["activity_broadcast_handler"]
        == activity_service.broadcast_projection
    )
    assert (
        captured_dependencies["activity_interruption_handler"]
        == activity_service.interrupt_active
    )
    assert (
        captured_dependencies["activity_notice_interruption_handler"]
        == assistant_request_context_preparer.interrupt_activity_notices
    )
    assert (
        captured_dependencies["notice_delivery_handler"]
        == assistant_request_context_preparer.apply_notice_delivery
    )
    assert (
        captured_dependencies["background_agent_interaction_event_handler"]
        == background_agent_interaction_service.apply_runtime_event
    )
    assert (
        captured_dependencies["background_agent_interaction_interruption_handler"]
        == background_agent_interaction_service.invalidate_runtime_epoch
    )
    assert (
        captured_dependencies["background_agent_usage_handler"]
        == background_agent_usage_service.record_terminal_event
    )
    current_runtime_epoch = captured_dependencies["current_runtime_epoch"]
    assert callable(current_runtime_epoch)
    assert current_runtime_epoch() == 7
    assert captured_dependencies["assistant_request_handoff_timeout_sec"] == 0.25
    assert captured_dependencies["event_queue"] is runtime_event_queue
    assert captured_dependencies["runtime_event_service"] is runtime_event_service
    assert captured_dependencies["timed_reminder_delivery"] is timed_reminder_delivery
    public_event_publishers = cast(
        RuntimePublicEventPublishers,
        captured_dependencies["public_event_publishers"],
    )
    assert public_event_publishers.publish_interaction.__self__.domain == "interaction"
    assert (
        public_event_publishers.publish_assistant_output.__self__.domain
        == "assistant_output"
    )
    assert public_event_publishers.publish_activity.__self__.domain == "activity"
    assert (
        public_event_publishers.publish_activity_ephemeral.__self__.domain
        == "activity"
    )
    assert (
        public_event_publishers.publish_materials_ephemeral.__self__.domain
        == "materials"
    )
    assert len(captured_dependency_sets) == 2
    assert factory._diversion_materials_by_input == {}
    assert captured_dependency_sets[0]["event_queue"] is runtime_event_queue
    assert captured_dependency_sets[1]["event_queue"] is runtime_event_queue
    assert captured_dependency_sets[0]["runtime_event_service"] is runtime_event_service
    assert captured_dependency_sets[1]["runtime_event_service"] is runtime_event_service

    late_event = RuntimeEvent(
        type="assistant.output_settled",
        source="assistant_process",
        payload={"request_id": "request-from-old-bridge"},
    )
    runtime_event_queue.put(late_event)
    new_session_queue = cast(NotifyQueue[RuntimeEvent], captured_dependency_sets[1]["event_queue"])
    assert new_session_queue.get_nowait() is late_event


def test_diverted_final_asr_remembers_frozen_materials_before_precise_release() -> None:
    """改道语音应登记投影时附件，并保留 ASR 期间新增的下一轮 staged 材料。"""

    def attachment(material_id: str, path: str) -> MaterialAttachment:
        """构造测试使用的用户自持普通文件。"""

        return MaterialAttachment(
            material_id=material_id,
            turn_id="staged",
            kind="dragged_file",
            source="test",
            display_name=path.rsplit("/", 1)[-1],
            path=path,
            created_at="2026-08-23T10:00:00+08:00",
            node_kind="file",
            media_class="text",
        )

    old_material = attachment("material-old", "D:/project/old.py")
    new_material = attachment("material-new", "D:/project/new.py")
    staged = [old_material]
    removed_material_ids: list[str] = []
    remembered_batches: list[tuple[MaterialAttachment, ...]] = []
    broadcasts: list[list[str]] = []

    def remove_staged(material_id: str) -> SimpleNamespace | None:
        """只删除指定 material_id，并保留其它 staged 材料。"""

        for index, material in enumerate(staged):
            if material.material_id != material_id:
                continue
            staged.pop(index)
            removed_material_ids.append(material_id)
            return SimpleNamespace(released_managed_asset_ids=[])
        return None

    factory = cast(Any, object.__new__(container_module.LiveKitMediaSessionFactory))
    factory.hub = SimpleNamespace(
        turn_materials=SimpleNamespace(
            staged_materials=lambda: list(staged),
            clear_staged=lambda: staged.clear() or [],
            remove_staged=remove_staged,
        )
    )
    factory.material_lifecycle = SimpleNamespace(
        remember_recent_materials=lambda attachments: remembered_batches.append(
            tuple(attachments)
        )
        or True,
    )
    factory._diversion_materials_by_input = {}
    factory._diversion_material_lock = RLock()
    factory._schedule_material_broadcast = (
        lambda released_asset_ids: broadcasts.append(list(released_asset_ids))
    )

    projected = factory._project_staged_materials("voice-a")
    staged.append(new_material)
    factory._release_diverted_staged_materials("voice-a", True)

    assert [item.file_name for item in projected] == ["old.py"]
    assert remembered_batches == [(old_material,)]
    assert removed_material_ids == ["material-old"]
    assert staged == [new_material]
    assert broadcasts == [[]]

    factory._project_staged_materials("voice-b")
    factory._release_diverted_staged_materials("voice-b", False)

    assert remembered_batches == [(old_material,)]
    assert staged == []


def test_diverted_final_asr_replay_cannot_capture_next_turn_materials() -> None:
    """旧语音结算后的重放不能登记或释放下一轮刚上传的材料。"""

    old_material = MaterialAttachment(
        material_id="material-old",
        turn_id="staged",
        kind="dragged_file",
        source="test",
        display_name="old.py",
        path="D:/project/old.py",
        created_at="2026-08-23T10:00:00+08:00",
        node_kind="file",
        media_class="text",
    )
    new_material = MaterialAttachment(
        material_id="material-new",
        turn_id="staged",
        kind="dragged_file",
        source="test",
        display_name="new.py",
        path="D:/project/new.py",
        created_at="2026-08-23T10:01:00+08:00",
        node_kind="file",
        media_class="text",
    )
    staged = [old_material]
    remembered_batches: list[tuple[MaterialAttachment, ...]] = []
    removed_material_ids: list[str] = []
    staged_reads = 0

    def staged_materials() -> list[MaterialAttachment]:
        """记录 provider 是否越过墓碑重新读取 staged。"""

        nonlocal staged_reads
        staged_reads += 1
        return list(staged)

    def remove_staged(material_id: str) -> SimpleNamespace | None:
        """精确删除指定材料并记录删除目标。"""

        for index, material in enumerate(staged):
            if material.material_id != material_id:
                continue
            staged.pop(index)
            removed_material_ids.append(material_id)
            return SimpleNamespace(released_managed_asset_ids=[])
        return None

    factory = cast(Any, object.__new__(container_module.LiveKitMediaSessionFactory))
    factory.hub = SimpleNamespace(
        turn_materials=SimpleNamespace(
            staged_materials=staged_materials,
            clear_staged=lambda: staged.clear() or [],
            remove_staged=remove_staged,
        )
    )
    factory.material_lifecycle = SimpleNamespace(
        remember_recent_materials=lambda attachments: remembered_batches.append(
            tuple(attachments)
        )
        or True,
    )
    factory._diversion_materials_by_input = {}
    factory._diversion_material_lock = RLock()
    factory._schedule_material_broadcast = lambda _released_asset_ids: None

    first_projection = factory._project_staged_materials("voice-a")
    pending_replay = factory._project_staged_materials("voice-a")
    factory._release_diverted_staged_materials("voice-a", True)
    staged_reads_after_first_settlement = staged_reads
    assert staged == []
    staged.append(new_material)
    settled_replay = factory._project_staged_materials("voice-a")
    factory._release_diverted_staged_materials("voice-a", True)

    assert first_projection == pending_replay
    assert [item.file_name for item in first_projection] == ["old.py"]
    assert settled_replay == ()
    assert staged_reads_after_first_settlement == 2
    assert staged_reads == staged_reads_after_first_settlement
    assert remembered_batches == [(old_material,)]
    assert removed_material_ids == []
    assert staged == [new_material]


def test_normal_final_asr_discard_leaves_tombstone_for_replay() -> None:
    """普通语音接管材料后，同身份重放不能捕获随后上传的材料。"""

    staged_reads = 0
    factory = cast(Any, object.__new__(container_module.LiveKitMediaSessionFactory))

    def staged_materials() -> list[MaterialAttachment]:
        """记录是否发生了不该出现的第二次 staged 读取。"""

        nonlocal staged_reads
        staged_reads += 1
        return []

    factory.hub = SimpleNamespace(
        turn_materials=SimpleNamespace(staged_materials=staged_materials)
    )
    factory._diversion_materials_by_input = {}
    factory._diversion_material_lock = RLock()

    assert factory._project_staged_materials("voice-a") == ()
    assert factory._diversion_materials_by_input["voice-a"] == ()

    factory._discard_projected_staged_materials("voice-a")

    assert factory._diversion_materials_by_input["voice-a"] is None
    assert factory._project_staged_materials("voice-a") == ()
    assert staged_reads == 1


@pytest.mark.asyncio
async def test_media_session_controller_restarts_current_session_from_saved_connection() -> None:
    """输入运行时替换后必须关闭旧会话，并用原连接事实创建新会话。"""

    events: list[object] = []
    sessions: list[object] = []

    class RecordingMediaSession:
        """记录媒体会话启动、关闭和提交行为。"""

        def __init__(self, number: int) -> None:
            self.number = number

        async def start(self, **connection: object) -> None:
            events.append((f"session-{self.number}:start", connection))

        async def close(self) -> None:
            events.append(f"session-{self.number}:close")

        def enqueue_assistant_request(self, _request: object) -> bool:
            return True

        def enqueue_assistant_control(self, _command: object) -> bool:
            return True

        def interrupt_runtime_activities(
            self,
            _runtime_epoch: int,
        ) -> list[dict[str, object]]:
            return []

    def build_session() -> RecordingMediaSession:
        session = RecordingMediaSession(len(sessions) + 1)
        sessions.append(session)
        return session

    controller = LiveKitMediaSessionController(media_session_factory=build_session)
    connection = {
        "url": "ws://livekit.test",
        "room_name": "companion-local",
        "token": "backend-token",
        "identity": "backend-media",
    }
    await controller.ensure_session_started(**connection)

    restarted = await controller.restart_current_session()

    expected_start = {
        "url": connection["url"],
        "token": connection["token"],
        "identity": connection["identity"],
        "source_session_id": connection["room_name"],
    }
    assert restarted is True
    assert len(sessions) == 2
    assert events == [
        ("session-1:start", expected_start),
        "session-1:close",
        ("session-2:start", expected_start),
    ]


@pytest.mark.asyncio
async def test_media_session_controller_cancels_old_reminder_during_restart() -> None:
    """媒体重建尚未完成时，关闭命令必须仍能到达旧会话。"""

    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    cancelled_request_ids: list[str] = []
    sessions: list[object] = []

    class BlockingMediaSession:
        """在首个会话关闭边界暂停，稳定复现重建窗口。"""

        def __init__(self, number: int) -> None:
            self.number = number

        async def start(self, **_connection: object) -> None:
            """候选会话启动不执行外部连接。"""

        async def close(self) -> None:
            """首个会话等待测试放行，后续会话直接关闭。"""

            if self.number != 1:
                return
            close_started.set()
            await allow_close.wait()

        def cancel_timed_reminder_request(self, request_id: str) -> None:
            """记录旧会话收到的定向取消。"""

            cancelled_request_ids.append(request_id)

    def build_session() -> BlockingMediaSession:
        """按创建顺序返回可识别的媒体会话。"""

        session = BlockingMediaSession(len(sessions) + 1)
        sessions.append(session)
        return session

    controller = LiveKitMediaSessionController(media_session_factory=build_session)
    await controller.ensure_session_started(
        url="ws://livekit.test",
        room_name="companion-local",
        token="backend-token",
        identity="backend-media",
    )

    restart_task = asyncio.create_task(controller.restart_current_session())
    await close_started.wait()
    controller.cancel_timed_reminder_requests(("reminder-request-a",))
    allow_close.set()

    assert await restart_task is True
    assert cancelled_request_ids == ["reminder-request-a"]


class DisconnectableMediaSession:
    """支持最终断连通知回调的媒体会话测试替身。"""

    def __init__(self, number: int) -> None:
        """初始化可识别的会话编号与空回调。

        Args:
            number: 按创建顺序分配的会话序号。
        """

        self.number = number
        self.on_final_disconnect: Callable[[], None] | None = None
        self.close_calls = 0

    def set_on_final_disconnect(self, callback: Callable[[], None] | None) -> None:
        """记录 Controller 注入的最终断连回调。

        Args:
            callback: Controller 绑定的占位归还回调。
        """

        self.on_final_disconnect = callback

    async def start(self, **_connection: object) -> None:
        """候选会话启动不执行外部连接。"""

    async def close(self) -> None:
        """关闭替身不持有外部资源。"""

        self.close_calls += 1


def _build_disconnectable_controller() -> (
    tuple[LiveKitMediaSessionController, list[DisconnectableMediaSession]]
):
    """构造注入断连替身的 Controller 和会话登记表。"""

    sessions: list[DisconnectableMediaSession] = []

    def build_session() -> DisconnectableMediaSession:
        """按创建顺序返回可识别的媒体会话。"""

        session = DisconnectableMediaSession(len(sessions) + 1)
        sessions.append(session)
        return session

    return LiveKitMediaSessionController(media_session_factory=build_session), sessions


@pytest.mark.asyncio
async def test_final_disconnect_releases_placeholder_so_next_ensure_rejoins() -> None:
    """SDK 彻底断连后，Controller 必须释放当前占位并让下一次 ensure 重新入房。"""

    controller, sessions = _build_disconnectable_controller()
    connection = {
        "url": "ws://livekit.test",
        "room_name": "companion-local",
        "token": "backend-token",
        "identity": "backend-media",
    }
    await controller.ensure_session_started(**connection)
    assert controller.has_started_session

    # SDK 只在房间彻底关闭时派发 disconnected；通知后 Controller 不能再把
    # 已经掏空的会话当成活跃会话，否则 Backend 永远不再入房。
    sessions[0].on_final_disconnect()
    assert not controller.has_started_session
    # 收口任务尚未完成时也不能把新的助手或提醒请求送进旧会话。
    assert controller.enqueue_timed_reminder_notice() is False
    assert controller.enqueue_assistant_control(cast(Any, object())) is False
    assert controller.assistant_output_available() is False

    await controller.ensure_session_started(**connection)
    await asyncio.sleep(0)

    assert controller.has_started_session
    assert len(sessions) == 2
    assert sessions[0].close_calls == 1
    assert sessions[0].on_final_disconnect is not None
    assert sessions[1].on_final_disconnect is not None


@pytest.mark.asyncio
async def test_stale_final_disconnect_does_not_evict_replacement_session() -> None:
    """热替换后旧会话的迟到断连通知不能误删新会话占位。"""

    controller, sessions = _build_disconnectable_controller()
    connection = {
        "url": "ws://livekit.test",
        "room_name": "companion-local",
        "token": "backend-token",
        "identity": "backend-media",
    }
    await controller.ensure_session_started(**connection)
    stale_disconnect = sessions[0].on_final_disconnect

    restarted = await controller.restart_current_session()
    assert restarted is True
    assert len(sessions) == 2

    stale_disconnect()

    assert controller.has_started_session


def test_on_disconnected_invokes_final_disconnect_callback_without_aborting() -> None:
    """会话收到 SDK 断连时必须通知 Controller，即使回调自身抛错也不阻断收口。"""

    notifications: list[str] = []
    session = object.__new__(LiveKitMediaSession)
    session._audio_stop_event = None
    session._audio_task = None
    session.audio_runtime_thread = SimpleNamespace(stop=lambda **_kwargs: None)
    session.asr_scheduler = SimpleNamespace(stop=lambda **_kwargs: None)
    session.assistant_process_bridge = None
    session.runtime_event_service = None
    session.assistant_audio_output_scheduler = None
    session._room_disconnect_reset_sent = False
    session._input_track_generation = 0
    session.control_queue = NotifyQueue()
    session.realtime_input_worker = SimpleNamespace(notify_work_available=lambda: None)
    session._on_final_disconnect_callback = lambda: notifications.append("released")

    session._on_disconnected()
    # SDK 可能重复派发 disconnected；每次都要通知，由 Controller 侧按身份幂等。
    session._on_disconnected()

    assert notifications == ["released", "released"]

    def raising_callback() -> None:
        """模拟 Controller 占位归还逻辑自身异常。"""

        raise RuntimeError("release failed")

    session._on_final_disconnect_callback = raising_callback
    # 回调异常必须被吞掉并记录，不能中断断连收口或向 SDK 传播。
    session._on_disconnected()
