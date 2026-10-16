from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event, enumerate as enumerate_threads
from time import monotonic
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from companion.realtime import assistant_child_runtime as child_runtime_module
from companion.realtime.assistant_child_runtime import AssistantChildRuntime
from companion.foreground_self_reflection.contracts import (
    SelfReflectionToolCommitDecisionCommand,
)
from companion.foreground_self_reflection.tool_commit_broker import (
    SelfReflectionToolCommitBroker,
)
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_process import _drain_child_runtime_events
from companion.background_agent.activity_executor import BackgroundAgentActivityExecutor
from companion.background_agent.model_config import AgentExecutionConfig, AgentModelConfig
from companion.background_agent.workspace import WorkspaceManager
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)


class RecordingRunner:
    """记录运行时传给主 worker 的关闭预算。"""

    def __init__(self, observed: list[float]) -> None:
        self.observed = observed

    def shutdown(self, timeout: float) -> None:
        """保存主 worker 获得的剩余预算。"""

        self.observed.append(timeout)


class OrderedShutdownRuntime:
    """记录 ChildRuntime 关闭各资源的顺序和剩余预算。"""

    def __init__(self, name: str, observed: list[tuple[str, float]]) -> None:
        """绑定资源名称和共享顺序记录。

        Args:
            name: 当前测试资源的可读名称。
            observed: 保存关闭顺序和预算的共享列表。
        """

        self._name = name
        self._observed = observed

    def shutdown(self, timeout: float) -> None:
        """记录资源收到的关闭预算。

        Args:
            timeout: ChildRuntime 按总 deadline 计算的剩余秒数。
        """

        self._observed.append((self._name, timeout))


class RecordingToolCommitBroker:
    """记录 ChildRuntime 转交的 Self-Reflection 工具提交回执。"""

    def __init__(self) -> None:
        """初始化空回执列表。"""

        self.commands: list[SelfReflectionToolCommitDecisionCommand] = []

    def apply_decision(
        self,
        command: SelfReflectionToolCommitDecisionCommand,
    ) -> bool:
        """保存一条主进程回执。

        Args:
            command: 需要唤醒子进程等待者的批准或拒绝命令。

        Returns:
            测试固定返回真。
        """

        self.commands.append(command)
        return True


class RuntimeEventRunner(RecordingRunner):
    """保存 Broker 本地事件，供子进程主循环按真实路径排水。"""

    def __init__(self, observed: list[float]) -> None:
        """初始化关闭记录和本地 Runtime Event FIFO。"""

        super().__init__(observed)
        self.events: list[RuntimeEvent] = []

    def drain_events(self) -> list[RuntimeEvent]:
        """按到达顺序取走当前全部本地事件。

        Returns:
            尚未跨进程转发的事件列表。
        """

        drained = list(self.events)
        self.events.clear()
        return drained


class FailingWakeEvent:
    """模拟跨进程事件已写入，但 Bridge 唤醒信号暂时失败。"""

    def set(self) -> None:
        """固定抛出唤醒失败，验证主进程轮询兜底不倒退所有权。"""

        raise RuntimeError("bridge_wakeup_failed")


class DeferredBackgroundRunner:
    """记录 Background Runner 是否在提交排水前被关闭。"""

    def __init__(self) -> None:
        """初始化可用 Runner 和关闭记录。"""

        self.available = True
        self.shutdown_calls: list[float] = []
        self.shutdown_event = Event()

    def start(self, _task: object, _config: object) -> bool:
        """接管一项测试 Activity。

        Args:
            _task: 本测试不读取的后台任务。
            _config: 本测试不读取的执行配置。

        Returns:
            固定返回真，表示 Runner 已接管任务。
        """

        self.available = False
        return True

    def is_available(self) -> bool:
        """返回 Runner 是否仍能接管任务。

        Returns:
            尚未启动任务时返回真。
        """

        return self.available

    def shutdown(self, timeout: float) -> None:
        """记录真正执行的最终关闭。

        Args:
            timeout: Background Executor 剩余的关闭预算。

        Returns:
            无。
        """

        self.shutdown_calls.append(timeout)
        self.shutdown_event.set()


class DelayedCleanupBackgroundRunner(DeferredBackgroundRunner):
    """模拟 shutdown 已返回、但 Runner 资源仍在后台清理的状态。"""

    def __init__(self) -> None:
        """初始化独立的关闭调用和真实清理完成信号。"""

        super().__init__()
        self.cleanup_finished = Event()

    def wait_until_shutdown(self, timeout: float | None = None) -> bool:
        """等待测试显式放行真实 Runner 清理。

        Args:
            timeout: 最长等待秒数；为空时一直等到清理完成。

        Returns:
            清理完成信号已经设置时返回真。
        """

        return self.cleanup_finished.wait(timeout)


class BarrierOnlyCoordinator:
    """模拟 Coordinator 超时返回，但保留真实提交屏障的生命周期。"""

    def __init__(self, barrier: BackgroundCommitDrainBarrier) -> None:
        """绑定共享提交屏障。

        Args:
            barrier: Coordinator 与 Background Executor 共用的排水屏障。
        """

        self._barrier = barrier

    def shutdown(self, timeout: float) -> None:
        """按真实 Coordinator 合同请求排水并允许超时返回。

        Args:
            timeout: ChildRuntime 传入的剩余关闭预算。

        Returns:
            无。
        """

        self._barrier.begin_shutdown(timeout)


def test_child_runtime_shutdown_callbacks_share_one_deadline(monkeypatch: Any) -> None:
    """主 Worker 和附属 Activity Executor 必须共同消费一份关闭总预算。"""

    observed: list[float] = []
    clock = iter((10.0, 10.2, 10.5, 10.9))
    monkeypatch.setattr(child_runtime_module, "monotonic", lambda: next(clock))
    runner = RecordingRunner(observed)
    child_runtime = AssistantChildRuntime(
        runner=runner,  # type: ignore[arg-type]
        shutdown_callbacks=(
            lambda timeout: observed.append(timeout),
            lambda timeout: observed.append(timeout),
        ),
    )

    child_runtime.shutdown(timeout=1.0)

    assert observed == pytest.approx([1.0, 0.8, 0.5])


def test_child_runtime_settles_self_reflection_before_runner_and_executors(
    monkeypatch: Any,
) -> None:
    """Self-Reflection 提交临界区必须先于普通输出和附属执行器关闭。

    Args:
        monkeypatch: 固定每个关闭边界消耗的单调时间。
    """

    observed: list[tuple[str, float]] = []
    clock = iter((10.0, 10.1, 10.3, 10.6))
    monkeypatch.setattr(child_runtime_module, "monotonic", lambda: next(clock))
    coordinator = OrderedShutdownRuntime("self_reflection", observed)
    runner = OrderedShutdownRuntime("runner", observed)
    executor = OrderedShutdownRuntime("executor", observed)
    child_runtime = AssistantChildRuntime(
        runner=runner,  # type: ignore[arg-type]
        self_reflection_coordinator=coordinator,  # type: ignore[arg-type]
        shutdown_callbacks=(executor.shutdown,),
    )

    child_runtime.shutdown(timeout=1.0)

    assert [name for name, _timeout in observed] == [
        "self_reflection",
        "runner",
        "executor",
    ]
    assert [timeout for _name, timeout in observed] == pytest.approx(
        [0.9, 0.7, 0.4]
    )


def test_child_runtime_does_not_clear_background_prepared_commit_after_timeout(
    tmp_path: Path,
) -> None:
    """ChildRuntime 超时后，获批 Background 仍可完成 prepared 接管。

    Args:
        tmp_path: pytest 提供的隔离工作区。
    """

    barrier = BackgroundCommitDrainBarrier()
    background_runner = DeferredBackgroundRunner()
    ordinary_runner = DeferredBackgroundRunner()
    execution_config = AgentExecutionConfig(
        model_config=AgentModelConfig(
            environment={"ANTHROPIC_API_KEY": "test-key"},
            config_dir=tmp_path / "runtime",
            model="claude-sonnet-4-5",
        ),
        permission_mode="acceptEdits",
        max_turns=1,
    )
    background_executor = BackgroundAgentActivityExecutor(
        config_provider=SimpleNamespace(snapshot=lambda **_kwargs: execution_config),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        ordinary_runner=ordinary_runner,
        recovery_runner=background_runner,
        id_factory=lambda _prefix: "background-activity-timeout",
        commit_barrier=barrier,
    )
    accepted, events = background_executor.prepare(
        execution_lane="recovery",
        interaction_session_id="session-a",
        goal="关闭竞态测试",
        deliverable=False,
        requirements="",
        materials=(),
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert barrier.enter("self-reflection:recovery-timeout") is True

    child_runtime = AssistantChildRuntime(
        runner=RecordingRunner([]),  # type: ignore[arg-type]
        self_reflection_coordinator=BarrierOnlyCoordinator(barrier),  # type: ignore[arg-type]
        shutdown_callbacks=(background_executor.shutdown,),
    )
    child_runtime.shutdown(timeout=0.0)

    assert background_runner.shutdown_calls == []
    assert ordinary_runner.shutdown_calls == []
    assert background_executor.has_activity(activity_id) is True
    assert background_executor.commit_prepared(activity_id).kind == "started"

    assert barrier.leave("self-reflection:recovery-timeout") is True
    assert background_runner.shutdown_event.wait(timeout=1.0)
    assert ordinary_runner.shutdown_event.wait(timeout=1.0)
    availability = background_executor.background_availability_snapshot()
    assert availability.ordinary.shutdown is True
    assert availability.recovery.shutdown is True


def test_child_runtime_defers_cua_shutdown_until_background_executor_finishes(
    tmp_path: Path,
) -> None:
    """提交屏障未排空时，官方 CUA Host 关闭不能抢在 Background 之前。"""

    barrier = BackgroundCommitDrainBarrier()
    background_runner = DelayedCleanupBackgroundRunner()
    ordinary_runner = DelayedCleanupBackgroundRunner()
    execution_config = AgentExecutionConfig(
        model_config=AgentModelConfig(
            environment={"ANTHROPIC_API_KEY": "test-key"},
            config_dir=tmp_path / "runtime",
            model="claude-sonnet-4-5",
        ),
        permission_mode="acceptEdits",
        max_turns=1,
    )
    background_executor = BackgroundAgentActivityExecutor(
        config_provider=SimpleNamespace(snapshot=lambda **_kwargs: execution_config),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        ordinary_runner=ordinary_runner,
        recovery_runner=background_runner,
        id_factory=lambda _prefix: "background-cua-shutdown-order",
        commit_barrier=barrier,
    )
    accepted, events = background_executor.prepare(
        execution_lane="recovery",
        interaction_session_id="session-a",
        goal="关闭顺序测试",
        deliverable=False,
        requirements="",
        materials=(),
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert barrier.enter("self-reflection:cua-order") is True
    cua_shutdown_calls: list[str] = []
    cua_shutdown_finished = Event()

    def shutdown_cua_after_background(_timeout: float) -> None:
        """模拟 Assistant 将 CUA Host 停止挂到 Background 完成门之后。"""

        def record_cua_shutdown() -> None:
            """记录 Host 只在两条 Runner 真实清理后停止。"""

            cua_shutdown_calls.append("stopped")
            cua_shutdown_finished.set()

        background_executor.run_after_shutdown(record_cua_shutdown)

    child_runtime = AssistantChildRuntime(
        runner=RecordingRunner([]),  # type: ignore[arg-type]
        self_reflection_coordinator=BarrierOnlyCoordinator(barrier),  # type: ignore[arg-type]
        shutdown_callbacks=(
            background_executor.shutdown,
            shutdown_cua_after_background,
        ),
    )
    child_runtime.shutdown(timeout=0.0)

    assert cua_shutdown_calls == []
    assert background_executor.has_activity(activity_id) is True
    assert background_executor.commit_prepared(activity_id).kind == "started"

    assert barrier.leave("self-reflection:cua-order") is True
    assert background_runner.shutdown_event.wait(timeout=1.0)
    assert ordinary_runner.shutdown_event.wait(timeout=1.0)
    shutdown_monitors = tuple(
        thread
        for thread in enumerate_threads()
        if thread.name == "background-agent-shutdown-monitor" and thread.is_alive()
    )
    assert len(shutdown_monitors) == 1
    assert shutdown_monitors[0].daemon is False
    assert cua_shutdown_calls == []

    background_runner.cleanup_finished.set()
    assert cua_shutdown_finished.wait(timeout=0.05) is False
    ordinary_runner.cleanup_finished.set()
    assert cua_shutdown_finished.wait(timeout=1.0) is True
    assert cua_shutdown_calls == ["stopped"]


def test_child_runtime_routes_tool_commit_decision_to_broker() -> None:
    """同步提交回执只能唤醒 Commit Broker，不能进入普通 generation。"""

    broker = RecordingToolCommitBroker()
    runtime = AssistantChildRuntime(
        runner=RecordingRunner([]),  # type: ignore[arg-type]
        self_reflection_tool_commit_broker=broker,
    )
    command = SelfReflectionToolCommitDecisionCommand(
        commit_request_id="commit-request-1",
        self_reflection_id="self-reflection-1",
        source_activity_id="cognitive-1",
        runtime_epoch=7,
        tool_call_id="tool-call-1",
        tool_name="start_background_work",
        approved=True,
        reason="tool_commit_approved",
        issued_at=10.0,
    )

    runtime.handle_control_command(command)

    assert broker.commands == [command]


def test_child_runtime_hands_commit_to_cross_process_queue_before_wakeup() -> None:
    """真实排水先原子交接提交，再把唤醒失败交给 Bridge 轮询兜底。"""

    runner = RuntimeEventRunner([])
    request_queued = Event()

    def emit_local(event: RuntimeEvent) -> None:
        """把 Broker 请求写入子进程本地 FIFO。"""

        runner.events.append(event)
        request_queued.set()

    broker = SelfReflectionToolCommitBroker(
        emit_event=emit_local,
        response_timeout_seconds=1.0,
        retry_interval_seconds=0.02,
        commit_request_id_factory=lambda: "commit-request-1",
    )
    runtime = AssistantChildRuntime(
        runner=runner,  # type: ignore[arg-type]
        self_reflection_tool_commit_broker=broker,
    )
    cross_process_queue: Queue[RuntimeEvent] = Queue()
    cancellation = Event()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            broker.request_and_wait,
            self_reflection_id="self-reflection-1",
            source_activity_id="cognitive-1",
            runtime_epoch=7,
            tool_call_id="tool-call-1",
            tool_name="start_background_work",
            execution_channel="background",
            canonical_event=_canonical_event(),
            deadline=monotonic() + 1.0,
            cancellation_event=cancellation,
        )
        assert request_queued.wait(timeout=1.0)
        _drain_child_runtime_events(
            runtime,
            event_queue=cross_process_queue,
            bridge_work_event=FailingWakeEvent(),
        )
        forwarded = cross_process_queue.get_nowait()
        cancellation.set()
        assert future.done() is False
        assert broker.apply_decision(_approval()) is True
        decision = future.result(timeout=1.0)

    assert forwarded.type == "foreground.self_reflection_tool_commit_requested"
    assert decision.approved is True


def _canonical_event() -> RuntimeEvent:
    """构造 ChildRuntime 跨进程交接测试使用的 Background canonical 事实。

    Returns:
        身份完整的工具完成事件。
    """

    return RuntimeEvent(
        type="llm.foreground.tool_call_completed",
        source="assistant_runtime",
        runtime_epoch=7,
        produced_at=10.0,
        payload={
            "self_reflection_id": "self-reflection-1",
            "recovery_of_activity_id": "cognitive-1",
            "tool_call_id": "tool-call-1",
            "name": "start_background_work",
            "activity_id": "background-1",
            "state": "accepted",
        },
    )


def _approval() -> SelfReflectionToolCommitDecisionCommand:
    """构造与 ChildRuntime 测试 pending 完全匹配的批准命令。

    Returns:
        主进程已经完成 canonical 登记的批准回执。
    """

    return SelfReflectionToolCommitDecisionCommand(
        commit_request_id="commit-request-1",
        self_reflection_id="self-reflection-1",
        source_activity_id="cognitive-1",
        runtime_epoch=7,
        tool_call_id="tool-call-1",
        tool_name="start_background_work",
        approved=True,
        reason="tool_commit_approved",
        issued_at=11.0,
    )
