"""验证初始化回执与后台资源装配分离，以及旧操作无法越过发布边界。"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from companion.core.application.status_broadcaster import ApplicationStatusBroadcaster
from companion.core.application.runtime_initialization import install_runtime_initialization
from companion.core.application.runtime_initialization import RuntimeInitialization


@pytest.fixture
def initialization_container():
    """用阶段闩锁验证初始化生命周期，不启动真实模型。"""
    service = SimpleNamespace(runtime_blocked=False)
    events = []

    async def publish(event):
        events.append(event)

    hub = SimpleNamespace(configuration_mutation_lock=asyncio.Lock(), runtime_events_enabled=True,
        close_runtime_control_connections=AsyncMock(),
        publisher=lambda _: SimpleNamespace(publish_ephemeral=publish))
    hub.bootstrap = service
    hub.bootstrap.status_payload = lambda: {
        "llm": {"is_ready": True, "missing": [], "errors": []},
        "voice": {"provider": "local_streamvox", "is_ready": True, "phase": "ready"},
        "has_qweather_api_key": False, "is_initialized": not service.runtime_blocked}
    container = SimpleNamespace(hub=hub, events=events,
        timed_reminder_scheduler=SimpleNamespace(start=Mock(), shutdown=AsyncMock()),
        timed_reminder_delivery=SimpleNamespace(start=Mock(), shutdown=AsyncMock()),
        timed_reminder_service=None, tts_setup=None,
        livekit_session_service=SimpleNamespace(shutdown=AsyncMock()),
        assistant_process_supervisor=SimpleNamespace(shutdown=Mock()),
        realtime_input_runtime_owner=SimpleNamespace(shutdown=Mock()),
        turn_admission=SimpleNamespace(clear_pending_for_runtime_transition=AsyncMock(),
                                      resume_after_runtime_transition=Mock()),
        runtime_reconfiguration=SimpleNamespace(restore_runtime=AsyncMock(return_value=True),
                                               runtime_cleanup_steps=lambda: {}))
    container.application_status = ApplicationStatusBroadcaster(hub=hub)
    return container


async def wait_for_state(runtime, state):
    """等待事件循环推进到确定终态，失败时给出当前快照。"""
    for _ in range(100):
        if runtime.snapshot.state == state:
            if state in {"ready", "idle"} and runtime._resume_task is not None:
                await runtime._resume_task
            return
        await asyncio.sleep(0.01)
    assert runtime.snapshot.state == state, runtime.status_payload()


@pytest.mark.asyncio
async def test_retry_returns_before_slow_runtime_and_keeps_business_blocked(initialization_container):
    """初始化请求立即返回；加载闩锁未打开时所有业务仍被阻断。"""
    container = initialization_container
    entered, release = asyncio.Event(), asyncio.Event()

    async def restore(*, report_phase=None):
        if report_phase is not None:
            await report_phase("input_models")
        entered.set()
        await release.wait()
        if report_phase is not None:
            await report_phase("assistant")
        return True

    container.runtime_reconfiguration.restore_runtime.side_effect = restore
    app = FastAPI()
    app.get("/api/business")(lambda: {"executed": True})
    install_runtime_initialization(app, container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as api:
        response = await asyncio.wait_for(api.post("/api/runtime/initialization/retry"), 1)
        assert response.status_code == 200
        assert response.json()["state"] == "initializing"
        await asyncio.wait_for(entered.wait(), 1)
        stage = (await api.get("/api/runtime/initialization")).json()
        assert stage["phase"] == "input_models"
        assert (await api.get("/api/business")).status_code == 503
        assert container.hub.bootstrap.status_payload()["is_initialized"] is False
        first = await api.post("/api/runtime/initialization/retry")
        second = await api.post("/api/runtime/initialization/retry")
        assert first.json()["operation_id"] == second.json()["operation_id"] == stage["operation_id"]
        assert container.runtime_reconfiguration.restore_runtime.await_count == 1
        release.set()
        for _ in range(100):
            terminal = (await api.get("/api/runtime/initialization")).json()
            if terminal["state"] == "ready":
                break
            await asyncio.sleep(0.01)
        assert terminal["state"] == "ready"
        assert (await api.get("/api/business")).status_code == 200
        event = next(event for event in reversed(container.events) if event["type"] == "runtime.initialization_changed")
        assert event["payload"] == terminal
        assert container.application_status.current_events()[0]["payload"] == terminal
        await container.hub.bootstrap.runtime_status_provider.__self__._resume_task


@pytest.mark.asyncio
async def test_failure_keeps_runtime_blocked_and_retry_uses_new_operation(initialization_container):
    """初始化失败保持阻断，重试生成新操作并恢复本机运行资源。"""
    container = initialization_container
    runtime = RuntimeInitialization(container)
    container.runtime_reconfiguration.restore_runtime.side_effect = [RuntimeError("secret"), True]
    original = await runtime.start_resume()
    await wait_for_state(runtime, "failed")
    assert runtime.snapshot.retryable is True
    assert "secret" not in runtime.snapshot.message
    assert container.hub.bootstrap.runtime_blocked is True
    assert container.hub.runtime_events_enabled is False
    retried = await runtime.start_resume()
    assert original["operation_id"] != retried["operation_id"]
    await wait_for_state(runtime, "ready")


@pytest.mark.asyncio
async def test_bootstrap_http_reports_blocked_status_while_models_are_loading(initialization_container):
    """模型加载占着配置锁时，Bootstrap HTTP 仍立即返回真实阻断状态。

    Args:
        initialization_container: 持有安全 Bootstrap 状态的受控容器。
    Returns:
        无；状态读取不能等初始化完成后才返回。
    """
    from companion.core.api.dependencies import get_bootstrap_dependencies
    from companion.core.api.routers.bootstrap import router
    from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator

    container = initialization_container
    container.hub.bootstrap.runtime_blocked = True
    coordinator = object.__new__(RuntimeReconfigurationCoordinator)
    coordinator.hub = container.hub
    coordinator._configuration_mutation_lock = container.hub.configuration_mutation_lock
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_bootstrap_dependencies] = lambda: SimpleNamespace(runtime_reconfiguration=coordinator)
    async with container.hub.configuration_mutation_lock:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as api:
            response = await asyncio.wait_for(api.get("/api/bootstrap/status"), timeout=1)
    assert response.status_code == 200
    assert response.json() == container.hub.bootstrap.status_payload()
    assert response.json()["is_initialized"] is False


@pytest.mark.asyncio
async def test_cancelled_model_load_waits_for_candidate_and_rolls_it_back():
    """取消请求不能取消正在加载的线程，必须收回并关闭其最终候选。"""
    from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator

    controller = object.__new__(RuntimeReconfigurationCoordinator)
    entered, release = threading.Event(), threading.Event()
    cleanup_entered, cleanup_release = threading.Event(), threading.Event()

    def rollback():
        cleanup_entered.set()
        assert cleanup_release.wait(2)

    candidate = SimpleNamespace(rollback=Mock(side_effect=rollback))

    def load(*, include_realtime_input):
        assert include_realtime_input is True
        entered.set()
        assert release.wait(2)
        return candidate

    controller._build_runtime_replacement = load
    loading = asyncio.create_task(controller._load_runtime_replacement(include_realtime_input=True))
    assert await asyncio.to_thread(entered.wait, 1)
    loading.cancel()
    await asyncio.sleep(0)
    assert not loading.done()
    loading.cancel()
    await asyncio.sleep(0)
    assert not loading.done()
    release.set()
    assert await asyncio.to_thread(cleanup_entered.wait, 1)
    loading.cancel()
    await asyncio.sleep(0)
    assert not loading.done()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await loading
    candidate.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_configuration_generation_clears_superseded_requested_resume(initialization_container):
    """配置接管世代时移除旧请求，runner 不得反复消费已失效的 tuple。"""
    runtime = RuntimeInitialization(initialization_container)
    async with runtime.transition_lock:
        await runtime.start_resume()
        await asyncio.sleep(0.01)
        try:
            await runtime.begin_configuration_initialization(include_realtime_input=True,
                expected_generation=runtime.initialization_generation)
            assert runtime._requested_resume is None
        finally:
            # 即使断言失败，也先移除旧请求，避免这个回归自身把测试事件循环困住。
            runtime.invalidate_initialization()
    await asyncio.wait_for(runtime._resume_task, 1)


@pytest.mark.asyncio
async def test_unconfigured_runtime_finishes_idle_without_starting_consumers(initialization_container):
    """首次配置缺失时放行向导，不能把向导本身困在恢复遮罩中。"""
    container = initialization_container
    container.runtime_reconfiguration.restore_runtime.return_value = False
    container.hub.bootstrap.status_payload = lambda: {"is_initialized": False}
    runtime = RuntimeInitialization(container)
    await runtime.start_resume()
    await wait_for_state(runtime, "idle")
    assert container.hub.bootstrap.runtime_blocked is False
    container.timed_reminder_scheduler.start.assert_not_called()




@pytest.mark.asyncio
async def test_shutdown_drains_slow_load_and_does_not_reopen_runtime(initialization_container):
    """退出等待加载线程交回资源，但迟到完成不能重新开放输入和事件。"""
    container = initialization_container
    entered, release = asyncio.Event(), asyncio.Event()

    async def restore(*, report_phase):
        entered.set()
        await release.wait()
        await report_phase("assistant")
        return True

    container.runtime_reconfiguration.restore_runtime.side_effect = restore
    runtime = RuntimeInitialization(container)
    await runtime.start_resume()
    await entered.wait()
    closing = asyncio.create_task(runtime.drain_cleanup())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert container.hub.runtime_events_enabled is False
    container.turn_admission.resume_after_runtime_transition.assert_not_called()




@pytest.mark.asyncio
async def test_slow_failed_status_broadcast_cannot_split_readiness_and_gate(initialization_container):
    """终态投递慢或失败时，公开快照与业务准入已经一起提交为 ready。"""
    container = initialization_container
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_failed_broadcast(**_kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("transport failed")

    container.application_status = SimpleNamespace(publish_changed=slow_failed_broadcast)
    runtime = RuntimeInitialization(container)
    await runtime.start_resume()
    await asyncio.wait_for(entered.wait(), 1)
    assert runtime.snapshot.state == "ready"
    assert container.hub.bootstrap.runtime_blocked is False
    assert container.hub.runtime_events_enabled is True
    release.set()
    await runtime._resume_task
    assert runtime.snapshot.state == "ready"
    assert container.hub.bootstrap.runtime_blocked is False


@pytest.mark.asyncio
async def test_voice_confirmation_reports_stages_without_blocking_event_loop(initialization_container, monkeypatch):
    """语音确认后的真实协调器在线程加载输入模型，主循环仍能处理状态读取。"""
    from companion.core.application import runtime_reconfiguration as coordinator_module
    from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator

    container = initialization_container
    monkeypatch.setattr(coordinator_module, "validate_llm_settings_ready", lambda _: SimpleNamespace(is_ready=True))
    controller = RuntimeReconfigurationCoordinator(settings=SimpleNamespace(llm=object()), hub=container.hub,
        realtime_input_runtime_owner=container.realtime_input_runtime_owner,
        assistant_process_supervisor=container.assistant_process_supervisor,
        activity_service=object(), background_agent_interactions=object(), interrupt_runtime_activities=lambda _: [],
        release_runtime_memory_compaction=lambda _: False, restart_livekit_media_session=AsyncMock(),
        configuration_mutation_lock=container.hub.configuration_mutation_lock,
        tts_setup=SimpleNamespace(confirm=Mock(), invalidate=Mock()))
    container.runtime_reconfiguration = controller
    runtime = RuntimeInitialization(container)
    loaded, release_loading = threading.Event(), threading.Event()
    assistant_started, release_assistant = asyncio.Event(), asyncio.Event()
    main_thread = threading.get_ident()

    class CandidateTransaction:
        """用闩锁模拟 readiness，阶段报告仍经过正式协调器。"""
        state = "prepared"

        async def commit(self, *, report_phase):
            await report_phase("assistant")
            assistant_started.set()
            await release_assistant.wait()
            await report_phase("media")
            self.state = "committed"

        def rollback(self):
            self.state = "rolled_back"

    def load_input_models(*, include_realtime_input):
        assert include_realtime_input is True
        assert threading.get_ident() != main_thread
        loaded.set()
        assert release_loading.wait(2)
        return CandidateTransaction()

    controller._build_runtime_replacement = load_input_models
    confirmation = asyncio.create_task(controller.confirm_voice_setup(task_id="test-task"))
    assert await asyncio.to_thread(loaded.wait, 1)
    # 在线程闩锁仍关闭时等待一次事件循环 tick；同步加载留在主线程会在此前卡住。
    await asyncio.wait_for(asyncio.sleep(0), 0.1)
    assert runtime.status_payload()["phase"] == "input_models"
    assert container.hub.bootstrap.status_payload()["is_initialized"] is False
    generation = runtime.initialization_generation
    current_operation = runtime.snapshot.operation_id
    for _ in range(2):
        assert (await runtime.start_resume())["operation_id"] == current_operation
    assert runtime.initialization_generation == generation
    assert runtime._requested_resume is None
    assert runtime._resume_task is None
    release_loading.set()
    await asyncio.wait_for(assistant_started.wait(), 1)
    assert runtime.status_payload()["phase"] == "assistant"
    release_assistant.set()
    response = await confirmation
    assert response["is_initialized"] is True
    assert runtime.status_payload()["state"] == "ready"
    phases = [event["payload"]["phase"] for event in container.events if event["type"] == "runtime.initialization_changed"]
    assert phases == ["input_models", "assistant", "media", "media", "ready"]


@pytest.mark.asyncio
async def test_configuration_persistence_failure_settles_prepared_initialization(initialization_container, monkeypatch):
    """模型候选已构造但密钥保存失败时，回滚后必须进入可重试终态。"""
    from companion.core.application import runtime_reconfiguration as coordinator_module
    from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator

    container = initialization_container
    monkeypatch.setattr(coordinator_module, "validate_llm_settings_ready", lambda _: SimpleNamespace(is_ready=True))
    container.hub.bootstrap.normalize_secrets = Mock(return_value=SimpleNamespace(qweather_api_key=None,
                                                                                qweather_api_host=None))
    container.hub.bootstrap.initialize = Mock(side_effect=OSError("private database path"))
    container.hub.bootstrap.secret_store = SimpleNamespace(snapshot_service_secrets=Mock(return_value={}),
                                                          restore_service_secrets=Mock())
    controller = RuntimeReconfigurationCoordinator(settings=SimpleNamespace(llm=object(), qweather_api_key="",
        qweather_api_host=""), hub=container.hub,
        realtime_input_runtime_owner=container.realtime_input_runtime_owner,
        assistant_process_supervisor=container.assistant_process_supervisor,
        activity_service=object(), background_agent_interactions=object(), interrupt_runtime_activities=lambda _: [],
        release_runtime_memory_compaction=lambda _: False, restart_livekit_media_session=AsyncMock(),
        configuration_mutation_lock=container.hub.configuration_mutation_lock)
    container.runtime_reconfiguration = controller
    runtime = RuntimeInitialization(container)
    transaction = SimpleNamespace(state="prepared", rollback=Mock(), commit=AsyncMock())
    controller._build_runtime_replacement = Mock(return_value=transaction)
    with pytest.raises(OSError):
        await controller.initialize_bootstrap(qweather_api_key=None, qweather_api_host=None)
    transaction.rollback.assert_called_once()
    transaction.commit.assert_not_called()
    assert controller._configuration_initialization_token is None
    assert runtime.snapshot.state == "failed"
    assert runtime.snapshot.retryable is True
    assert "private" not in runtime.snapshot.message
    assert container.hub.bootstrap.runtime_blocked is True


@pytest.mark.asyncio
async def test_existing_input_does_not_skip_missing_assistant_readiness(initialization_container, monkeypatch):
    """输入模型已加载也不能跳过缺失的助手，恢复须构造并等待助手候选。"""
    from companion.core.application import runtime_reconfiguration as coordinator_module
    from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator

    container = initialization_container
    monkeypatch.setattr(coordinator_module, "validate_llm_settings_ready", lambda _: SimpleNamespace(is_ready=True))
    controller = object.__new__(RuntimeReconfigurationCoordinator)
    controller.hub = container.hub
    controller.settings = SimpleNamespace(llm=object())
    controller._configuration_mutation_lock = container.hub.configuration_mutation_lock
    controller.realtime_input_runtime_owner = SimpleNamespace(runtime=object())
    controller.assistant_process_supervisor = SimpleNamespace(is_ready=False, output_enabled=True)
    transaction = SimpleNamespace(state="prepared", commit=AsyncMock(), rollback=Mock())
    controller._load_runtime_replacement = AsyncMock(return_value=transaction)
    controller._publish_application_status = AsyncMock()
    phases = AsyncMock()
    assert await controller.restore_runtime(report_phase=phases) is True
    controller._load_runtime_replacement.assert_awaited_once_with(include_realtime_input=False)
    transaction.commit.assert_awaited_once_with(report_phase=phases)
    phases.assert_awaited_once_with("assistant")


@pytest.mark.asyncio
async def test_current_input_identity_is_republished_after_real_hub_unmutes(initialization_container):
    """真实 Hub 在静音期间丢弃运行帧，恢复后补齐当前身份和交互状态。

    Args:
        initialization_container: 已装配初始化控制面的测试容器。

    Returns:
        无返回值；验证静音门禁和恢复后的权威快照广播。
    """
    from uuid import uuid4
    from companion.contracts.events import load_event_catalog
    from companion.core.broadcast import BroadcastHub
    from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner

    container = initialization_container
    hub = BroadcastHub.__new__(BroadcastHub)
    hub._connections = []
    hub._domain_publishers = {}
    hub._event_catalog = load_event_catalog()
    hub.runtime_logs = SimpleNamespace(append=Mock())
    hub.configuration_mutation_lock = asyncio.Lock()
    hub.runtime_events_enabled = True
    hub.bootstrap = container.hub.bootstrap
    container.hub = hub
    container.application_status = ApplicationStatusBroadcaster(hub=hub)
    identity = {"input_runtime_id": f"input-runtime-{uuid4()}", "input_runtime_epoch": 2}
    interaction_event = {"type": "interaction.state_changed", "payload": {
        **identity, "revision": 0, "followup_state": "followup_cool",
        "turn_state": "idle", "session_id": "interaction-a",
    }}
    owner = RealtimeInputRuntimeOwner.__new__(RealtimeInputRuntimeOwner)
    owner.hub = hub
    owner.runtime = SimpleNamespace(
        runtime_identity_payload=lambda: dict(identity),
        interaction_state_event=lambda: interaction_event,
    )
    container.realtime_input_runtime_owner = owner
    runtime = RuntimeInitialization(container)
    events = []
    socket = SimpleNamespace(send_json=AsyncMock(side_effect=events.append), close=AsyncMock())
    await hub.subscribe_control(socket, lambda: [], scope="runtime")
    token = await runtime.begin_configuration_initialization(include_realtime_input=True)
    await owner.broadcast_current_runtime_identity()
    assert not any(event["type"] == "input.runtime_changed" for event in events)
    assert not any(event["type"] == "interaction.state_changed" for event in events)
    await runtime.finish_configuration_initialization(token, success=True)
    identities = [event for event in events if event["type"] == "input.runtime_changed"]
    assert identities == [{"type": "input.runtime_changed", "payload": identity}]
    assert [event for event in events if event["type"] == "interaction.state_changed"] == [interaction_event]
    assert runtime.snapshot.state == "ready"
    assert events[-1]["type"] == "runtime.initialization_changed"
    assert events[-1]["payload"]["state"] == "ready"


@pytest.mark.asyncio
async def test_removed_product_endpoints_stay_unregistered_while_runtime_is_blocked(initialization_container):
    """删除的产品接口始终返回 404，不能被初始化中间件改写成业务故障。"""
    app = FastAPI()
    initialization_container.hub.bootstrap.runtime_blocked = True
    install_runtime_initialization(app, initialization_container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as api:
        for method, path in (("GET", "/api/license/status"), ("GET", "/api/license/runtime"),
                             ("POST", "/api/license/activate"), ("POST", "/api/license/claim-trial")):
            assert (await api.request(method, path)).status_code == 404
        snapshot = (await api.get("/api/runtime/initialization")).json()
    assert set(snapshot) == {"runtime_id", "operation_id", "revision", "state", "phase", "message", "retryable"}


@pytest.mark.asyncio
async def test_configuration_cannot_publish_during_pending_cleanup(initialization_container):
    """旧资源关闭失败时，新配置不能抢先加载或解除输入阻断。"""
    from companion.core.application.errors import StateConflict
    runtime = RuntimeInitialization(initialization_container)
    runtime.pending_cleanup["media"] = AsyncMock()
    with pytest.raises(StateConflict, match="清理尚未完成"):
        await runtime.begin_configuration_initialization(include_realtime_input=True)
    assert runtime.snapshot.state == "idle"


@pytest.mark.asyncio
async def test_lifespan_waits_for_inflight_load_before_original_shutdown(initialization_container):
    """原资源所有者退出前必须等加载交回候选，不能遗留无主线程。"""
    from contextlib import asynccontextmanager
    entered, release, original_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def restore(*, report_phase):
        entered.set()
        await release.wait()
        await report_phase("assistant")
        return True

    @asynccontextmanager
    async def original(_app):
        yield
        original_closed.set()

    initialization_container.runtime_reconfiguration.restore_runtime.side_effect = restore
    app = FastAPI(lifespan=original)
    install_runtime_initialization(app, initialization_container)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    await entered.wait()
    closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
    await asyncio.sleep(0)
    assert not original_closed.is_set()
    release.set()
    await closing
    assert original_closed.is_set()
    assert initialization_container.hub.runtime_events_enabled is False
