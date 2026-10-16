"""验证失败候选仍有资源所有者，并且关闭不会冻结控制面。"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from companion.core.application.runtime_replacement import RuntimeReplacementTransaction
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.core.application.runtime_initialization import SupersededInitialization
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime


@pytest.fixture(params=["assistant", "input"])
def resource_owner(request, monkeypatch):
    """只构造真实 Owner，用替身隔离模型、子进程和用户配置。

    Args:
        request: 本次测试选择的资源类型。
        monkeypatch: 替换真正模型构造的 pytest 工具。
    Returns:
        Owner、单项关闭端口和新的候选构造记录器。
    """
    hub = SimpleNamespace(bootstrap=SimpleNamespace(status_payload=lambda: {"is_initialized": False}))
    settings = SimpleNamespace()
    build = Mock(return_value=SimpleNamespace(input_runtime_id="", input_runtime_epoch=0,
        bind_runtime_identity=Mock()))
    if request.param == "assistant":
        owner = AssistantProcessSupervisor(settings=settings, hub=hub,
            output_enabled=True, process_client=None)
        owner.build_owned_process_client = build
        return owner, owner.shutdown_process, build
    owner = RealtimeInputRuntimeOwner(settings=settings, hub=hub, runtime=None)
    monkeypatch.setattr(RealtimeInputRuntime, "from_settings", build)
    owner.attach_wake_response_runtime = lambda runtime: runtime
    owner.active_wakeword_keywords = lambda: ()
    return owner, owner.shutdown_runtime, build


def test_failed_candidate_blocks_new_build_until_cleanup_succeeds(resource_owner):
    """关闭失败的旧候选不能被新候选绕过，释放成功后才允许继续构造。"""
    owner, close, build = resource_owner
    candidate = SimpleNamespace(shutdown=Mock(side_effect=[OSError("busy"), OSError("busy"), ()]))
    close(candidate)
    with pytest.raises(RuntimeError, match="尚未"):
        owner.build_candidate()
    build.assert_not_called()
    assert owner.build_candidate() is build.return_value
    assert candidate.shutdown.call_count == 3


def test_shutdown_retries_unpublished_candidate_without_active_runtime(resource_owner):
    """即使候选从未发布，退出也要保留失败引用并继续关闭。"""
    owner, close, _build = resource_owner
    candidate = SimpleNamespace(shutdown=Mock(side_effect=[OSError("busy"), OSError("busy"), ()]))
    close(candidate)
    with pytest.raises(RuntimeError, match="尚未"):
        owner.shutdown()
    owner.shutdown()
    assert candidate.shutdown.call_count == 3


@pytest.mark.asyncio
async def test_same_candidate_close_calls_are_serial(resource_owner):
    """不同线程重试同一资源时不能同时关闭同一组模型或 IPC。"""
    _owner, close, _build = resource_owner
    entered, release, attempted_second = threading.Event(), threading.Event(), threading.Event()
    count_lock = threading.Lock()
    active_calls = 0
    maximum_active_calls = 0

    def shutdown():
        nonlocal active_calls, maximum_active_calls
        with count_lock:
            active_calls += 1
            maximum_active_calls = max(maximum_active_calls, active_calls)
        entered.set()
        try:
            assert release.wait(2)
        finally:
            with count_lock:
                active_calls -= 1
        return ()

    candidate = SimpleNamespace(shutdown=shutdown)
    first = asyncio.create_task(asyncio.to_thread(close, candidate))
    assert await asyncio.to_thread(entered.wait, 1)

    def second_close():
        attempted_second.set()
        close(candidate)

    second = asyncio.create_task(asyncio.to_thread(second_close))
    try:
        assert await asyncio.to_thread(attempted_second.wait, 1)
        await asyncio.sleep(0.02)
        assert maximum_active_calls == 1
    finally:
        release.set()
        await asyncio.gather(first, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_operation", ["start", "close"])
async def test_candidate_process_io_keeps_event_loop_responsive(slow_operation):
    """启动和回滚关闭候选都可以很慢，但阶段查询所在的事件循环必须继续运行。"""
    entered, release = threading.Event(), threading.Event()
    called_threads = []
    main_thread = threading.get_ident()

    def wait_for_release():
        called_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return ()

    candidate = SimpleNamespace(start=wait_for_release if slow_operation == "start" else Mock(),
        shutdown=wait_for_release if slow_operation == "close" else Mock(return_value=()))
    hub = SimpleNamespace(bootstrap=SimpleNamespace(status_payload=lambda: {"is_initialized": False}))
    supervisor = AssistantProcessSupervisor(settings=SimpleNamespace(), hub=hub,
        output_enabled=True, process_client=None)
    owner = RealtimeInputRuntimeOwner(settings=SimpleNamespace(), hub=hub, runtime=None)
    transaction = RuntimeReplacementTransaction(realtime_input_runtime_owner=owner,
        assistant_process_supervisor=supervisor, activity_service=object(),
        interrupt_runtime_activities=lambda _: [], interrupt_background_agent_interactions=lambda _: [],
        release_runtime_memory_compaction=lambda _: False, broadcast_ephemeral=AsyncMock(),
        restart_livekit_media_session=AsyncMock(), terminate_self_reflection_runtime=None,
        publish_assistant_candidate=None, realtime_input_candidate=None, assistant_process_candidate=candidate)

    async def report_phase(phase):
        if slow_operation == "close" and phase == "media":
            raise SupersededInitialization("测试使候选在发布前失效")

    commit = asyncio.create_task(transaction.commit(report_phase=report_phase))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not commit.done()
        await asyncio.wait_for(asyncio.sleep(0), 0.1)
        assert len(called_threads) == 1 and called_threads[0] != main_thread
    finally:
        release.set()
        if slow_operation == "close":
            with pytest.raises(SupersededInitialization):
                await commit
        else:
            await commit


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_operation", ["start", "readiness"])
async def test_repeated_cancel_waits_for_candidate_worker_before_rollback(slow_operation):
    """取消启动或 readiness 请求时，先等待线程交回候选，再执行一次回滚关闭。"""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def wait_for_release():
        entered.set()
        try:
            assert release.wait(2)
        finally:
            finished.set()

    close_before_finished = []
    candidate = SimpleNamespace(
        start=wait_for_release if slow_operation == "start" else Mock(),
        wait_until_ready=lambda **_: wait_for_release() if slow_operation == "readiness" else None,
        shutdown=Mock(side_effect=lambda: close_before_finished.append(not finished.is_set()) or ()),
    )
    hub = SimpleNamespace(bootstrap=SimpleNamespace(status_payload=lambda: {"is_initialized": False}))
    supervisor = AssistantProcessSupervisor(settings=SimpleNamespace(), hub=hub,
        output_enabled=True, process_client=None)
    owner = RealtimeInputRuntimeOwner(settings=SimpleNamespace(), hub=hub, runtime=None)
    transaction = RuntimeReplacementTransaction(realtime_input_runtime_owner=owner,
        assistant_process_supervisor=supervisor, activity_service=object(),
        interrupt_runtime_activities=lambda _: [], interrupt_background_agent_interactions=lambda _: [],
        release_runtime_memory_compaction=lambda _: False, broadcast_ephemeral=AsyncMock(),
        restart_livekit_media_session=AsyncMock(), terminate_self_reflection_runtime=None,
        publish_assistant_candidate=None, realtime_input_candidate=None, assistant_process_candidate=candidate)
    commit = asyncio.create_task(transaction.commit())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        commit.cancel()
        await asyncio.sleep(0.01)
        commit.cancel()
        await asyncio.sleep(0.01)
        assert not commit.done()
        candidate.shutdown.assert_not_called()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await commit
    assert finished.is_set()
    assert transaction.state == "rolled_back"
    assert close_before_finished == [False]


@pytest.mark.asyncio
async def test_cancel_after_commit_still_closes_both_previous_resources():
    """句柄已交换后的取消必须等旧输入和旧助手都收口，不能恢复旧世代。"""
    entered, release = threading.Event(), threading.Event()

    def close_old_input():
        entered.set()
        assert release.wait(2)

    old_input = SimpleNamespace(input_runtime_id="old", input_runtime_epoch=1,
        interaction_state=object(), shutdown=Mock(side_effect=close_old_input))
    new_input = SimpleNamespace(input_runtime_id="new", input_runtime_epoch=2,
        interaction_state=object(), shutdown=Mock(), runtime_identity_payload=lambda: {
            "input_runtime_id": "new", "input_runtime_epoch": 2})
    old_assistant = SimpleNamespace(shutdown=Mock(return_value=()))
    new_assistant = SimpleNamespace(start=Mock(), shutdown=Mock())
    hub = SimpleNamespace(bootstrap=SimpleNamespace(status_payload=lambda: {"is_initialized": False}),
        publisher=lambda _: SimpleNamespace(publish=AsyncMock()))
    supervisor = AssistantProcessSupervisor(settings=SimpleNamespace(), hub=hub,
        output_enabled=True, process_client=old_assistant)
    owner = RealtimeInputRuntimeOwner(settings=SimpleNamespace(), hub=hub, runtime=old_input)
    transaction = RuntimeReplacementTransaction(realtime_input_runtime_owner=owner,
        assistant_process_supervisor=supervisor,
        activity_service=SimpleNamespace(interrupt_active=AsyncMock()),
        interrupt_runtime_activities=lambda _: [], interrupt_background_agent_interactions=lambda _: [],
        release_runtime_memory_compaction=lambda _: False, broadcast_ephemeral=AsyncMock(),
        restart_livekit_media_session=AsyncMock(), terminate_self_reflection_runtime=None,
        publish_assistant_candidate=None, realtime_input_candidate=new_input,
        assistant_process_candidate=new_assistant)
    commit = asyncio.create_task(transaction.commit())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert transaction.state == "committed"
        commit.cancel()
        await asyncio.sleep(0.01)
        commit.cancel()
        await asyncio.sleep(0.01)
        assert not commit.done()
        assert owner.runtime is new_input
        assert supervisor.is_current_target(new_assistant)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await commit
    old_input.shutdown.assert_called_once()
    old_assistant.shutdown.assert_called_once()
    new_input.shutdown.assert_not_called()
    new_assistant.shutdown.assert_not_called()
