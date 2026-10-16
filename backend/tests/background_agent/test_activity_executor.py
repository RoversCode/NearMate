from __future__ import annotations

import base64
import shutil
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Barrier, Event, Thread

import pytest

from companion.activities.progress_scheduler import ActivityProgressScheduler
from companion.activities.runtime.models import ACTIVITY_STAGE_CODES
from companion.background_agent.activity_executor import BackgroundAgentActivityExecutor
from companion.background_agent.execution_lane import BackgroundAgentExecutionLane
from companion.background_agent.agent_session import AgentSessionResumeIdentity
from companion.background_agent.config_provider import BackgroundAgentNotConfiguredError
from companion.background_agent.contracts import AgentEvent, AgentMaterial
from companion.background_agent.cua_capability import (
    CuaCapabilityBusyError,
    CuaCapabilitySlot,
)
from companion.background_agent.cua_integration import CUA_SKILL_ID
from companion.background_agent.interaction_bridge import AgentInteractionAnswer
from companion.background_agent.material_access import material_limitations
from companion.background_agent.model_config import AgentExecutionConfig, AgentModelConfig
from companion.background_agent.skill_registry import SkillExecutionSnapshot
from companion.background_agent.task_prompt import render_agent_task_prompt
from companion.background_agent.workspace import WorkspaceManager
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
    CommitDrainStatus,
)


# 本文件只验证单条普通通道；双通道路由由独立协调器测试覆盖。
_ordinary_execution_lane = partial(
    BackgroundAgentExecutionLane,
    lane_kind="ordinary",
)


class StubRunner:
    def __init__(self) -> None:
        self.started_task = None
        self.cancelled_activity_id = None
        self.cancel_reason = None
        self.available = True
        self.start_result = True
        self.activity_sessions: dict[str, str] = {}

    def start(self, task: object, execution_config: AgentExecutionConfig) -> bool:
        if not self.start_result:
            return False
        self.started_task = task
        self.execution_config = execution_config
        self.available = False
        activity_id = getattr(task, "activity_id", "")
        if activity_id:
            self.activity_sessions[activity_id] = "agent-session-a"
        return True

    def is_available(self) -> bool:
        return self.available

    def cancel(self, activity_id: str, *, reason: str = "agent_interrupted") -> bool:
        self.cancelled_activity_id = activity_id
        self.cancel_reason = reason
        return True

    def wait_until_available(self, _timeout: float = 5.0) -> bool:
        return self.available

    def resume_identity_for_activity(
        self,
        activity_id: str,
    ) -> AgentSessionResumeIdentity | None:
        agent_session_id = self.activity_sessions.get(activity_id)
        if agent_session_id is None or self.started_task is None:
            return None
        return AgentSessionResumeIdentity(
            agent_session_id=agent_session_id,
            sdk_session_id="sdk-session-a",
            workspace_root=str(getattr(self.started_task, "workspace_root", "")),
        )

    def answer_interaction(self, _answer: object) -> bool:
        return True

    def answer_question(self, answer: object) -> bool:
        self.question_answer = answer
        return True

    def shutdown(self, _timeout: float) -> None:
        return None


class RecordingEmbeddedCuaRuntime:
    """记录 lane 在启动线性化点准备 Embedded Runtime 的次数。"""

    def __init__(self, *, fail_start: bool = False) -> None:
        """保存是否需要稳定模拟 Host 启动失败。

        Args:
            fail_start: 为真时每次启动都抛出测试异常。
        """

        self.fail_start = fail_start
        self.start_calls = 0

    def start_blocking(self) -> None:
        """记录一次同步启动，按测试配置决定是否失败。

        Raises:
            RuntimeError: `fail_start` 为真时模拟官方 Host 启动失败。
        """

        self.start_calls += 1
        if self.fail_start:
            raise RuntimeError("embedded host start failed")


class ShutdownRecordingRunner(StubRunner):
    """记录 Background Executor 真正完成破坏性关闭的时刻。"""

    def __init__(self) -> None:
        """初始化关闭记录。"""

        super().__init__()
        self.shutdown_calls: list[float] = []
        self.shutdown_event = Event()

    def shutdown(self, timeout: float) -> None:
        """记录 Runner 收到的最终关闭预算。

        Args:
            timeout: Executor 剩余的关闭预算。

        Returns:
            无。
        """

        self.shutdown_calls.append(timeout)
        self.shutdown_event.set()


class FailingShutdownRunner(ShutdownRecordingRunner):
    """记录关闭预算后稳定抛错，用来验证后续资源仍会关闭。"""

    def shutdown(self, timeout: float) -> None:
        """记录预算并模拟 Runner 关闭失败。

        Args:
            timeout: Executor 剩余的关闭预算。

        Raises:
            RuntimeError: 每次调用都模拟底层 Runner 关闭异常。
        """

        super().shutdown(timeout)
        raise RuntimeError("ordinary runner shutdown failed")


class IncrementingMonotonicClock:
    """每次读取都前进一秒，用来观察关闭阶段是否偷取新预算。"""

    def __init__(self) -> None:
        """从零秒开始记录单调时间。"""

        self._current = 0.0

    def __call__(self) -> float:
        """返回当前时间，并让下一次读取前进一秒。

        Returns:
            本次读取看到的单调秒数。
        """

        current = self._current
        self._current += 1.0
        return current


class ShutdownBudgetRecordingBarrier:
    """记录双通道协调器交给提交屏障的剩余关闭预算。"""

    def __init__(self) -> None:
        """初始化空的预算记录。"""

        self.shutdown_timeouts: list[float] = []

    def begin_shutdown(self, timeout: float) -> CommitDrainStatus:
        """记录等待预算并立即报告提交已经排空。

        Args:
            timeout: 协调器从统一截止时间计算出的剩余秒数。

        Returns:
            没有待排水提交的状态。
        """

        self.shutdown_timeouts.append(timeout)
        return CommitDrainStatus(())


def test_dual_lane_shutdown_uses_one_absolute_deadline(tmp_path: Path) -> None:
    """屏障和两条 Runner 必须依次消费同一个截止时间，不能重领预算。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    clock = IncrementingMonotonicClock()
    barrier = ShutdownBudgetRecordingBarrier()
    ordinary_runner = ShutdownRecordingRunner()
    recovery_runner = ShutdownRecordingRunner()
    executor = BackgroundAgentActivityExecutor(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        monotonic_now=clock,
        progress_scheduler=FailingProgressScheduler(),  # type: ignore[arg-type]
        ordinary_runner=ordinary_runner,
        recovery_runner=recovery_runner,
        commit_barrier=barrier,  # type: ignore[arg-type]
    )

    executor.shutdown(timeout=5.0)

    assert barrier.shutdown_timeouts == [4.0]
    assert ordinary_runner.shutdown_calls == [3.0]
    assert recovery_runner.shutdown_calls == [2.0]


def test_dual_lane_shutdown_continues_after_ordinary_runner_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """普通 Runner 关闭异常不能跳过恢复通道和两项共享资源。

    Args:
        monkeypatch: Pytest 提供的临时替换工具。
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    clock = IncrementingMonotonicClock()
    barrier = ShutdownBudgetRecordingBarrier()
    ordinary_runner = FailingShutdownRunner()
    recovery_runner = ShutdownRecordingRunner()
    executor = BackgroundAgentActivityExecutor(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        monotonic_now=clock,
        ordinary_runner=ordinary_runner,
        recovery_runner=recovery_runner,
        commit_barrier=barrier,  # type: ignore[arg-type]
    )
    scheduler_shutdown_calls: list[float] = []
    event_shutdown_calls: list[tuple[float, bool]] = []
    original_scheduler_shutdown = executor._progress_scheduler.shutdown  # noqa: SLF001
    original_event_shutdown = executor._events.shutdown  # noqa: SLF001

    def record_scheduler_shutdown(timeout: float) -> None:
        """记录共享调度器预算并执行真实关闭。

        Args:
            timeout: 协调器剩余的统一关闭预算。

        Returns:
            无返回值。
        """

        scheduler_shutdown_calls.append(timeout)
        original_scheduler_shutdown(timeout)

    def record_event_shutdown(
        timeout: float,
        *,
        preserve_pending_events: bool,
    ) -> None:
        """记录共享事件账本预算并执行真实关闭。

        Args:
            timeout: 协调器剩余的统一关闭预算。
            preserve_pending_events: 是否保留关闭前已经入账的事件。

        Returns:
            无返回值。
        """

        event_shutdown_calls.append((timeout, preserve_pending_events))
        original_event_shutdown(
            timeout,
            preserve_pending_events=preserve_pending_events,
        )

    monkeypatch.setattr(
        executor._progress_scheduler,  # noqa: SLF001
        "shutdown",
        record_scheduler_shutdown,
    )
    monkeypatch.setattr(
        executor._events,  # noqa: SLF001
        "shutdown",
        record_event_shutdown,
    )

    try:
        executor.shutdown(timeout=6.0)
    finally:
        # 红测阶段旧实现会在 ordinary 异常处提前退出；直接调用原方法只负责
        # 收回测试线程，不把补偿调用伪装成生产关闭记录。
        if not scheduler_shutdown_calls:
            original_scheduler_shutdown(0.0)
        if not event_shutdown_calls:
            original_event_shutdown(0.0, preserve_pending_events=True)

    assert barrier.shutdown_timeouts == [5.0]
    assert ordinary_runner.shutdown_calls == [4.0]
    assert recovery_runner.shutdown_calls == [3.0]
    assert scheduler_shutdown_calls == [2.0]
    assert event_shutdown_calls == [(1.0, True)]
    assert executor._shutdown is True  # noqa: SLF001


def test_single_lane_shutdown_continues_after_runner_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """独立通道的 Runner 关闭异常也不能跳过自有调度器和事件账本。

    Args:
        monkeypatch: Pytest 提供的临时替换工具。
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    clock = IncrementingMonotonicClock()
    runner = FailingShutdownRunner()
    lane = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        monotonic_now=clock,
        runner=runner,
    )
    scheduler_shutdown_calls: list[float] = []
    event_shutdown_calls: list[tuple[float, bool]] = []
    original_scheduler_shutdown = lane._progress_scheduler.shutdown  # noqa: SLF001
    original_event_shutdown = lane._events.shutdown  # noqa: SLF001

    def record_scheduler_shutdown(timeout: float) -> None:
        """记录自有调度器预算并执行真实关闭。

        Args:
            timeout: 通道剩余的统一关闭预算。

        Returns:
            无返回值。
        """

        scheduler_shutdown_calls.append(timeout)
        original_scheduler_shutdown(timeout)

    def record_event_shutdown(
        timeout: float,
        *,
        preserve_pending_events: bool,
    ) -> None:
        """记录自有事件账本预算并执行真实关闭。

        Args:
            timeout: 通道剩余的统一关闭预算。
            preserve_pending_events: 是否保留关闭前已经入账的事件。

        Returns:
            无返回值。
        """

        event_shutdown_calls.append((timeout, preserve_pending_events))
        original_event_shutdown(
            timeout,
            preserve_pending_events=preserve_pending_events,
        )

    monkeypatch.setattr(
        lane._progress_scheduler,  # noqa: SLF001
        "shutdown",
        record_scheduler_shutdown,
    )
    monkeypatch.setattr(
        lane._events,  # noqa: SLF001
        "shutdown",
        record_event_shutdown,
    )

    try:
        lane.shutdown(timeout=4.0)
    finally:
        if not scheduler_shutdown_calls:
            original_scheduler_shutdown(0.0)
        if not event_shutdown_calls:
            original_event_shutdown(0.0, preserve_pending_events=True)

    assert runner.shutdown_calls == [3.0]
    assert scheduler_shutdown_calls == [2.0]
    assert event_shutdown_calls == [(1.0, True)]
    assert lane._shutdown is True  # noqa: SLF001


class FailingProgressScheduler:
    """模拟进度调度器自身故障，但不影响已经启动的后台任务。"""

    def schedule(self, _delay_seconds: float, _callback: object) -> object:
        """稳定复现调度入口抛错。

        Args:
            _delay_seconds: 本次测试不使用的等待秒数。
            _callback: 本次测试不会执行的到期回调。

        Raises:
            RuntimeError: 每次调用都模拟调度器不可用。
        """

        raise RuntimeError("进度调度器不可用")


class StubConfigProvider:
    """返回由可信用户设置冻结的测试配置。"""

    def __init__(self, tmp_path: Path, permission_mode: str = "acceptEdits") -> None:
        self._config = AgentExecutionConfig(
            model_config=AgentModelConfig(
                environment={"ANTHROPIC_API_KEY": "test-key"},
                config_dir=tmp_path / "runtime",
                model="claude-sonnet-4-5",
            ),
            permission_mode=permission_mode,  # type: ignore[arg-type]
            max_turns=30,
        )

    def snapshot(
        self,
        *,
        inherited_skill_snapshot: SkillExecutionSnapshot | None = None,
    ) -> AgentExecutionConfig:
        """返回当前测试任务配置，并模拟父 Skill 快照继承。

        Args:
            inherited_skill_snapshot: 纠正任务必须沿用的父任务 Skill 快照。

        Returns:
            当前配置；传入父快照时只替换其中的 Skill 快照。
        """

        if inherited_skill_snapshot is None:
            return self._config
        return replace(
            self._config,
            skill_snapshot=inherited_skill_snapshot,
        )

    def replace_skill_snapshot(self, snapshot: SkillExecutionSnapshot) -> None:
        """模拟用户在两项任务之间修改全局 Skill 设置。"""

        self._config = replace(self._config, skill_snapshot=snapshot)


class UnconfiguredProvider:
    """模拟 Console 尚未保存完整后台配置。"""

    def snapshot(
        self,
        *,
        inherited_skill_snapshot: SkillExecutionSnapshot | None = None,
    ) -> AgentExecutionConfig:
        """拒绝冻结任务配置。

        Args:
            inherited_skill_snapshot: 本替身不会使用的父任务 Skill 快照。

        Raises:
            BackgroundAgentNotConfiguredError: 每次调用都表示配置尚未完成。
        """

        _ = inherited_skill_snapshot
        raise BackgroundAgentNotConfiguredError("后台工作配置尚未完成")


class FailingConfigProvider:
    """模拟配置存储发生普通读取异常。"""

    def snapshot(self, **_kwargs: object) -> AgentExecutionConfig:
        """抛出不应穿透 Assistant 控制循环的普通异常。"""

        raise OSError("settings database unavailable")


def _write_managed_cua_skill(asset_root: Path) -> None:
    """创建可由真实 WorkspaceManager 延迟物化的最小官方 Skill 资产。

    Args:
        asset_root: 当前测试独占的产品资产根目录。

    Returns:
        无返回值。
    """

    skill_root = asset_root / ".claude" / "skills" / CUA_SKILL_ID
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: cua-driver\ndescription: 官方 CUA 测试 Skill。\n"
        "version: 0.22.1\n---\n\n说明",
        encoding="utf-8",
    )
    (skill_root / "BROWSER.md").write_text("Browser", encoding="utf-8")
    (skill_root / "WINDOWS.md").write_text("Windows", encoding="utf-8")
    (skill_root / "manifest.json").write_text(
        '{"id":"cua-driver","name":"CUA","summary":"测试",'
        '"category":"工具","version":"0.22.1","default_enabled":true,'
        '"dependencies":[]}',
        encoding="utf-8",
    )


def _cua_config_provider(tmp_path: Path) -> StubConfigProvider:
    """返回启用官方 `cua-driver` 候选 Skill 的测试配置。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        已冻结 CUA 候选 Skill 的配置提供器。
    """

    provider = StubConfigProvider(tmp_path)
    provider.replace_skill_snapshot(
        SkillExecutionSnapshot(
            enabled_skill_ids=(CUA_SKILL_ID,),
            revision=1,
        )
    )
    return provider


def test_dual_lanes_atomically_freeze_one_cua_grant(tmp_path: Path) -> None:
    """两条 lane 同时启动时只给一项 Activity 物化 CUA，另一项仍正常启动。"""

    asset_root = tmp_path / "assets"
    _write_managed_cua_skill(asset_root)
    ordinary_runner = StubRunner()
    recovery_runner = StubRunner()
    capability = CuaCapabilitySlot()
    integration = RecordingEmbeddedCuaRuntime()
    activity_ids = iter(("ordinary-cua", "recovery-cua"))
    executor = BackgroundAgentActivityExecutor(
        config_provider=_cua_config_provider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=asset_root,
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        ordinary_runner=ordinary_runner,
        recovery_runner=recovery_runner,
        cua_integration=integration,  # type: ignore[arg-type]
        cua_capability=capability,
    )
    ordinary_accepted, ordinary_events = executor.prepare(
        execution_lane="ordinary",
        interaction_session_id="session-a",
        goal="普通通道文件任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    recovery_accepted, recovery_events = executor.prepare(
        execution_lane="recovery",
        interaction_session_id="session-a",
        goal="恢复通道文件任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    ordinary_id = str(ordinary_events[0]["payload"]["activity_id"])
    recovery_id = str(recovery_events[0]["payload"]["activity_id"])
    barrier = Barrier(3)
    outcomes: dict[str, object] = {}

    def commit(activity_id: str) -> None:
        """让两条测试线程从同一个屏障并发提交 Activity。"""

        barrier.wait()
        outcomes[activity_id] = executor.commit_prepared(activity_id)

    threads = [
        Thread(target=commit, args=(ordinary_id,)),
        Thread(target=commit, args=(recovery_id,)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2.0)

    assert ordinary_accepted is True
    assert recovery_accepted is True
    assert all(not thread.is_alive() for thread in threads)
    assert {getattr(outcome, "kind") for outcome in outcomes.values()} == {"started"}
    started_tasks = (ordinary_runner.started_task, recovery_runner.started_task)
    granted_tasks = [task for task in started_tasks if task.cua_capability_granted]
    unavailable_tasks = [
        task for task in started_tasks if not task.cua_capability_granted
    ]
    assert len(granted_tasks) == 1
    assert len(unavailable_tasks) == 1
    assert integration.start_calls == 1
    assert capability.snapshot().available is False
    for task in granted_tasks:
        assert CUA_SKILL_ID in task.skill_snapshot.enabled_skill_ids
        assert (
            Path(task.agent_config_dir) / "skills" / CUA_SKILL_ID / "SKILL.md"
        ).is_file()
    for task in unavailable_tasks:
        assert CUA_SKILL_ID not in task.skill_snapshot.enabled_skill_ids
        assert not (Path(task.agent_config_dir) / "skills" / CUA_SKILL_ID).exists()


def test_cua_host_start_failure_degrades_and_releases_grant(tmp_path: Path) -> None:
    """Embedded Host 启动失败时 Activity 仍以无 CUA 模式接管普通工作。"""

    asset_root = tmp_path / "assets"
    _write_managed_cua_skill(asset_root)
    runner = StubRunner()
    capability = CuaCapabilitySlot()
    integration = RecordingEmbeddedCuaRuntime(fail_start=True)
    executor = _ordinary_execution_lane(
        config_provider=_cua_config_provider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=asset_root,
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "host-start-failure",
        runner=runner,
        cua_integration=integration,  # type: ignore[arg-type]
        cua_capability=capability,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="Host 失败后继续文件任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])

    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "started"
    assert integration.start_calls == 1
    assert runner.started_task.cua_capability_granted is False
    assert CUA_SKILL_ID not in runner.started_task.skill_snapshot.enabled_skill_ids
    assert capability.snapshot().available is True


def test_runner_start_failure_releases_frozen_cua_grant(tmp_path: Path) -> None:
    """Runner 拒绝接管时，已经准备好的 Runtime 不能让 Grant 残留。"""

    asset_root = tmp_path / "assets"
    _write_managed_cua_skill(asset_root)
    runner = StubRunner()
    runner.start_result = False
    capability = CuaCapabilitySlot()
    executor = _ordinary_execution_lane(
        config_provider=_cua_config_provider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=asset_root,
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "runner-start-failure",
        runner=runner,
        cua_integration=RecordingEmbeddedCuaRuntime(),  # type: ignore[arg-type]
        cua_capability=capability,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="Runner 拒绝测试",
        deliverable=False,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])

    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "start_failed"
    assert capability.snapshot().available is True


def test_config_materialization_failure_releases_frozen_cua_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """私有配置原子物化失败时，Runner 不启动且 Grant 立即释放。

    Args:
        monkeypatch: Pytest 提供的临时属性替换工具。
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    asset_root = tmp_path / "assets"
    _write_managed_cua_skill(asset_root)
    workspace_manager = WorkspaceManager(
        asset_root=asset_root,
        workspace_root=tmp_path / "workspace",
    )

    def fail_materialization(*_args: object, **_kwargs: object) -> None:
        """模拟原子配置提交前发生磁盘错误。

        Raises:
            OSError: 每次调用都模拟配置目录无法提交。
        """

        raise OSError("agent config materialization failed")

    monkeypatch.setattr(
        workspace_manager,
        "materialize_agent_config",
        fail_materialization,
    )
    runner = StubRunner()
    capability = CuaCapabilitySlot()
    executor = _ordinary_execution_lane(
        config_provider=_cua_config_provider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=workspace_manager,
        id_factory=lambda _prefix: "materialization-failure",
        runner=runner,
        cua_integration=RecordingEmbeddedCuaRuntime(),  # type: ignore[arg-type]
        cua_capability=capability,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="物化失败测试",
        deliverable=False,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])

    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "start_failed"
    assert runner.started_task is None
    assert capability.snapshot().available is True


def test_correction_atomically_inherits_parent_cua_grant(tmp_path: Path) -> None:
    """父 Runner 清理后 Grant 直接转给 replacement，中间不能被第三项抢走。"""

    asset_root = tmp_path / "assets"
    _write_managed_cua_skill(asset_root)
    activity_ids = iter(("cua-parent", "cua-replacement"))
    runner = StubRunner()
    capability = CuaCapabilitySlot()
    executor = _ordinary_execution_lane(
        config_provider=_cua_config_provider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=asset_root,
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
        cua_integration=RecordingEmbeddedCuaRuntime(),  # type: ignore[arg-type]
        cua_capability=capability,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="父任务",
        deliverable=True,
        requirements="",
        materials=(),
    )
    parent_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True
    assert executor.commit_prepared(parent_id).kind == "started"
    reservation = executor.reserve_correction(
        based_on_activity_id=parent_id,
        interaction_session_id="session-a",
        goal="纠正后的目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    assert reservation.accepted is True
    assert executor.commit_reserved_correction(reservation.activity_id) is True
    executor._append_agent_event(
        AgentEvent(
            event_type="cancelled",
            activity_id=parent_id,
            interaction_session_id="session-a",
            payload={"cancellation_reason": "user_correction"},
        )
    )

    executor._finalize_cua_grant(parent_id)

    assert capability.release(parent_id) is False
    with pytest.raises(CuaCapabilityBusyError):
        capability.acquire("third-activity")
    runner.available = True
    assert executor.start_reserved_correction(reservation.activity_id) is True
    assert runner.started_task.activity_id == reservation.activity_id
    assert runner.started_task.cua_capability_granted is True
    assert CUA_SKILL_ID in runner.started_task.skill_snapshot.enabled_skill_ids


def test_dual_lanes_keep_independent_running_skill_snapshots(tmp_path: Path) -> None:
    """普通和恢复任务并行时，后启动任务不能覆盖前一项的 Skill 配置。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    asset_root = tmp_path / "assets"
    for skill_id in ("skill-a", "skill-b"):
        skill_root = asset_root / ".claude" / "skills" / skill_id
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {skill_id}\ndescription: 双通道隔离测试 Skill。\n---\n\n说明",
            encoding="utf-8",
        )
        (skill_root / "manifest.json").write_text(
            (
                '{"id":"'
                + skill_id
                + '","name":"测试","summary":"测试","category":"测试",'
                '"version":"1.0.0","default_enabled":true,"dependencies":[]}'
            ),
            encoding="utf-8",
        )
    config_provider = StubConfigProvider(tmp_path)
    config_provider.replace_skill_snapshot(
        SkillExecutionSnapshot(enabled_skill_ids=("skill-a",), revision=1)
    )
    ordinary_runner = StubRunner()
    recovery_runner = StubRunner()
    activity_ids = iter(("ordinary-activity", "recovery-activity"))
    executor = BackgroundAgentActivityExecutor(
        config_provider=config_provider,  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=asset_root,
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        ordinary_runner=ordinary_runner,
        recovery_runner=recovery_runner,
    )

    ordinary_accepted, ordinary_events = executor.prepare(
        execution_lane="ordinary",
        interaction_session_id="session-a",
        goal="执行普通任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    config_provider.replace_skill_snapshot(
        SkillExecutionSnapshot(enabled_skill_ids=("skill-b",), revision=2)
    )
    recovery_accepted, recovery_events = executor.prepare(
        execution_lane="recovery",
        interaction_session_id="session-a",
        goal="执行恢复任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    ordinary_activity_id = str(ordinary_events[0]["payload"]["activity_id"])
    recovery_activity_id = str(recovery_events[0]["payload"]["activity_id"])

    assert ordinary_accepted is True
    assert recovery_accepted is True
    assert executor.commit_prepared(ordinary_activity_id).kind == "started"
    assert executor.commit_prepared(recovery_activity_id).kind == "started"
    ordinary_config_root = ordinary_runner.execution_config.model_config.config_dir
    recovery_config_root = recovery_runner.execution_config.model_config.config_dir
    assert ordinary_runner.started_task.workspace_root == recovery_runner.started_task.workspace_root
    assert ordinary_config_root != recovery_config_root
    assert (ordinary_config_root / "skills" / "skill-a" / "SKILL.md").is_file()
    assert not (ordinary_config_root / "skills" / "skill-b").exists()
    assert (recovery_config_root / "skills" / "skill-b" / "SKILL.md").is_file()
    assert not (recovery_config_root / "skills" / "skill-a").exists()
    executor.shutdown()


def _reserve_correction(
    executor: BackgroundAgentExecutionLane,
    **kwargs: object,
) -> tuple[bool, tuple[dict[str, object], ...]]:
    """调用结构化纠正预留接口，并返回场景断言关心的字段。"""

    reservation = executor.reserve_correction(**kwargs)
    return reservation.accepted, reservation.events


def test_correction_cancel_error_restores_previous_activity_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Runner 取消抛错时，旧任务应恢复为可纠正的真实运行状态。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000011",
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="仍在运行的旧目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(activity_id).kind == "started"

    def reject_cancel(_activity_id: str, *, reason: str) -> bool:
        """模拟 Runner 在送出 interrupt 前异常。

        Args:
            _activity_id: 本次测试不使用的 Activity 编号。
            reason: 本次测试不使用的取消原因。

        Raises:
            RuntimeError: 每次调用都模拟取消命令发送失败。
        """

        del reason
        raise RuntimeError("取消命令发送失败")

    monkeypatch.setattr(runner, "cancel", reject_cancel)

    with pytest.raises(RuntimeError, match="取消命令发送失败"):
            executor.slot_cancel_active(activity_id, reason="user_correction")

    assert executor.is_activity_correctable(activity_id) is True
    snapshot = executor.activity_snapshot("session-a")
    assert snapshot is not None
    assert snapshot.state == "running"
    executor.shutdown()


def test_correction_cancel_not_accepted_restores_previous_activity_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Runner 拒绝 interrupt 时返回独立结果，A 仍保持可纠正。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000014",
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="仍在运行的旧目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(activity_id).kind == "started"
    monkeypatch.setattr(runner, "cancel", lambda _activity_id, *, reason: False)

    assert executor.slot_cancel_active(
        activity_id,
        reason="user_correction",
    ) is False
    assert executor.is_activity_correctable(activity_id) is True
    executor.shutdown()


def test_correction_cancel_reports_cleanup_timeout_separately(tmp_path: Path) -> None:
    """interrupt 已接纳但 Runner 未释放时，只能返回真实的清理超时。"""

    runner = StubRunner()
    activity_ids = iter(
        (
            "00000000-0000-4000-8000-000000000015",
            "00000000-0000-4000-8000-000000000016",
        )
    )
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="仍在运行的旧目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(activity_id).kind == "started"

    correction = executor.reserve_correction(
        based_on_activity_id=activity_id,
        interaction_session_id="session-a",
        goal="纠正后的目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    assert correction.accepted is True
    assert executor.commit_reserved_correction(correction.activity_id) is True
    assert executor.cancel_for_correction(activity_id, timeout=0) == "cleanup_timeout"
    executor.shutdown()


def test_background_start_survives_progress_scheduler_failure(tmp_path: Path) -> None:
    """普通任务已被 Runner 接受后，进度调度故障不能推翻启动结果。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000012",
        progress_scheduler=FailingProgressScheduler(),  # type: ignore[arg-type]
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="正常后台任务",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])

    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "started"
    assert getattr(runner.started_task, "activity_id", "") == activity_id
    assert executor.is_activity_correctable(activity_id) is True
    executor.shutdown()


def test_start_failed_before_start_removes_prepared_workspace(tmp_path: Path) -> None:
    """Runner 未接管任务时，启动前失败必须删除任务目录。"""

    runner = StubRunner()
    runner.start_result = False
    activity_id = "00000000-0000-4000-8000-000000000091"
    workspace_manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=tmp_path / "workspace",
    )
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=workspace_manager,
        id_factory=lambda _prefix: activity_id,
        runner=runner,
    )

    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="启动前失败的任务",
        deliverable=True,
        requirements="",
        materials=(),
    )
    assert accepted is True
    assert (workspace_manager.workspace_root / "tasks" / activity_id).is_dir()

    outcome = executor.commit_prepared(activity_id)

    assert outcome.kind == "start_failed"
    assert not (workspace_manager.workspace_root / "tasks" / activity_id).exists()
    executor.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_commit_rejects_config_replaced_by_junction(tmp_path: Path) -> None:
    """普通任务配置在 prepare 后被替换时，commit 必须拒绝且不启动 Runner。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    runner = StubRunner()
    activity_id = "00000000-0000-4000-8000-000000000055"
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: activity_id,
        runner=runner,
    )
    accepted, _events = executor.prepare(
        interaction_session_id="session-a",
        goal="验证私有配置边界",
        deliverable=False,
        requirements="",
        materials=(),
    )
    task = executor._prepared_tasks[activity_id].task  # noqa: SLF001
    agent_config_root = Path(task.agent_config_dir)
    external_root = tmp_path / "external-agent-config"
    external_root.mkdir()
    external_file = external_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    _winapi.CreateJunction(str(external_root), str(agent_config_root))

    try:
        outcome = executor.commit_prepared(activity_id)
    finally:
        executor.shutdown()
        if agent_config_root.is_junction():
            agent_config_root.rmdir()

    assert accepted is True
    assert outcome.kind == "start_failed"
    assert runner.started_task is None
    assert external_file.read_text(encoding="utf-8") == "outside"


def test_background_correction_reservation_rechecks_parent_state(tmp_path: Path) -> None:
    """旧 Background 进入 cancelling 后，不能继续预留纠正任务。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda prefix: (
            "background-parent"
            if prefix == "background_agent" and runner.started_task is None
            else "background-correction"
        ),
        progress_scheduler=FailingProgressScheduler(),  # type: ignore[arg-type]
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="旧后台目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    assert accepted is True
    parent_activity_id = str(events[0]["payload"]["activity_id"])
    assert executor.commit_prepared(parent_activity_id).kind == "started"
    runner.available = True
    assert executor.slot_cancel_active(
        parent_activity_id,
        reason="user_correction",
    ) is True

    correction_accepted, correction_events = _reserve_correction(executor,
        based_on_activity_id=parent_activity_id,
        interaction_session_id="session-a",
        goal="改做新后台目标",
        deliverable=True,
        requirements="",
        materials=(),
    )

    assert correction_accepted is False
    assert correction_events[0]["payload"]["reason"] == "correction_activity_not_found"
    executor.shutdown()


def test_background_correction_rejects_missing_sdk_resume_identity_before_cancel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """父 SDK 编号缺失时不能预留 B，更不能先取消仍在运行的 A。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000071",
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="错误的旧目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    parent_activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True
    assert executor.commit_prepared(parent_activity_id).kind == "started"
    monkeypatch.setattr(
        runner,
        "resume_identity_for_activity",
        lambda _activity_id: None,
    )

    correction_accepted, correction_events = _reserve_correction(executor,
        based_on_activity_id=parent_activity_id,
        interaction_session_id="session-a",
        goal="正确的新目标",
        deliverable=True,
        requirements="",
        materials=(),
    )

    assert correction_accepted is False
    assert correction_events[0]["payload"]["reason"] == (
        "correction_agent_session_unavailable"
    )
    assert runner.cancelled_activity_id is None
    assert executor.is_activity_correctable(parent_activity_id) is True
    executor.shutdown()


def test_cross_source_background_correction_is_rejected(
    tmp_path: Path,
) -> None:
    """Cognitive 父 Activity 不能进入 Background 纠正执行器。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000013",
        progress_scheduler=FailingProgressScheduler(),  # type: ignore[arg-type]
        runner=runner,
    )
    accepted, events = _reserve_correction(executor,
        based_on_activity_id="cognitive-a",
        interaction_session_id="session-a",
        goal="纠正后的后台目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context={"source": "cognitive"},
    )
    assert accepted is False
    assert events[0]["payload"]["reason"] == "correction_activity_not_found"
    assert runner.started_task is None
    executor.shutdown()


def test_unconfigured_prepare_returns_console_guidance_without_creating_workspace(
    tmp_path: Path,
) -> None:
    """未配置时只能返回安全事实，不能提前创建任务目录或 Activity。"""

    workspace_root = tmp_path / "workspace"
    executor = _ordinary_execution_lane(
        config_provider=UnconfiguredProvider(),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=workspace_root,
        ),
        runner=StubRunner(),
    )

    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="整理录音",
        deliverable=True,
        requirements="",
        materials=(),
    )

    assert accepted is False
    assert events == [
        {
            "type": "background_agent.rejected",
            "payload": {
                "reason": "background_agent_unconfigured",
                "safe_message": (
                    "后台工作尚未配置。请先打开 Console 的“后台工作”设置，填写 "
                    "ANTHROPIC_BASE_URL，并配置 ANTHROPIC_API_KEY 或 "
                    "ANTHROPIC_AUTH_TOKEN。"
                ),
            },
        }
    ]
    assert not (workspace_root / "tasks").exists()
    assert executor.activity_snapshot("session-a") is None
    executor.shutdown()


def test_background_agent_activity_executor_prepares_and_starts_one_task(
    tmp_path: Path,
) -> None:
    runner = StubRunner()
    activity_ids = iter(
        (
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
        )
    )
    recording_path = tmp_path / "recording.wav"
    recording_path.write_bytes(b"audio")
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )

    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="把录音转换成 mp3",
        deliverable=True,
        requirements="保留原文件名",
        materials=(
            AgentMaterial(
                material_id="material-a",
                display_name="recording.wav",
                material_kind="dragged_file",
                media_class="audio",
                local_path=str(recording_path),
                node_kind="file",
            ),
        ),
    )
    activity_id = events[0]["payload"]["activity_id"]

    assert accepted is True
    assert executor.background_available() is False
    assert executor.commit_prepared(activity_id).kind == "started"
    assert runner.started_task.task_output_dir.endswith("outputs")
    assert runner.started_task.permission_mode == "acceptEdits"
    assert material_limitations(runner.started_task.material_catalog) == ()

    second_accepted, second_events = executor.prepare(
        interaction_session_id="session-a",
        goal="第二项任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    assert second_accepted is False
    assert second_events[0]["payload"]["reason"] == "background_slot_occupied"

    # 文件在执行期间失效时，终态必须根据现场重新计算，不能沿用启动时缓存。
    recording_path.unlink()
    executor._append_agent_event(
        AgentEvent(
            event_type="completed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={
                "display_content": "转换完成",
                "delivery_brief": "音频已经转换完成。",
                "output_paths": ["output.mp3"],
            },
        )
    )
    completed = next(
        event
        for event in executor.drain_events()
        if event["type"] == "background_agent.completed"
    )
    assert completed["type"] == "background_agent.completed"
    assert completed["payload"]["source_materials"] == ["recording.wav"]
    assert completed["payload"]["task_input_snapshot"]["material_limitations"] == [
        "recording.wav"
    ]
    # Foreground 已经拿到业务终态，不代表 Runner 已经断开 transport。finalizing 身份仍要
    # 留在通道索引里，否则 shutdown 或验收代码会过早判定清理完成。
    assert executor.has_activity(activity_id) is True

    # Runner 仍在清理前，Activity 必须继续告诉 Foreground 槽位被占用。
    runner.available = True
    executor._on_runner_slot_released(str(activity_id))
    assert executor.has_activity(activity_id) is False
    assert executor.background_available() is True
    assert runner.started_task.activity_id == activity_id

    executor.shutdown()


def test_cancel_before_start_removes_activity_from_correction_index(tmp_path: Path) -> None:
    """未提交的后台占位回滚后，不能被纠正工具当成真实旧任务。"""

    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        runner=StubRunner(),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000099",
    )

    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="尚未提交的任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    activity_id = events[0]["payload"]["activity_id"]
    assert accepted is True
    assert executor.has_activity(activity_id) is True
    assert executor.cancel_before_start(activity_id) is True
    assert executor.has_activity(activity_id) is False
    executor.shutdown()


def test_prepare_normalizes_config_exception_to_rejection(tmp_path: Path) -> None:
    """配置普通异常必须成为 canonical rejection，且不留下任何后台占位。"""

    executor = _ordinary_execution_lane(
        config_provider=FailingConfigProvider(),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        runner=StubRunner(),
    )

    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="转换音频",
        deliverable=True,
        requirements="",
        materials=(),
    )

    assert accepted is False
    assert events[0]["payload"]["reason"] == "background_prepare_failed"
    assert executor.background_available() is False
    executor.shutdown()


def test_prepare_scene_write_exception_discards_unstarted_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """现场写入失败必须删除已建任务目录，并释放 prepared 槽位。"""

    workspace_manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=tmp_path / "workspace",
    )
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=workspace_manager,
        runner=StubRunner(),
        id_factory=lambda _prefix: "background-scene-failed",
    )

    def fail_scene_write(*_args: object, **_kwargs: object) -> Path:
        """模拟现场文件系统写入失败。"""

        raise OSError("scene disk unavailable")

    monkeypatch.setattr(workspace_manager, "write_current_scene", fail_scene_write)

    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="分析现场",
        deliverable=False,
        requirements="",
        materials=(),
        current_scene_bytes=b"png",
    )

    assert accepted is False
    assert events[0]["payload"]["reason"] == "background_prepare_failed"
    assert not (tmp_path / "workspace" / "tasks" / "background-scene-failed").exists()
    assert executor.has_activity("background-scene-failed") is False
    assert executor.background_available() is True
    executor.shutdown()


def test_background_work_freezes_current_scene_bytes_without_reencoding(
    tmp_path: Path,
) -> None:
    """现场截图必须逐字节落盘，并只把冻结文件路径交给执行内核。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000001",
        runner=runner,
    )
    frozen_scene_bytes = b"\x89PNG\r\n\x1a\nexact-current-scene"

    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="把当前屏幕上的文档翻译成英文",
        deliverable=True,
        requirements="完整输出英文译文",
        materials=(),
        current_scene_bytes=frozen_scene_bytes,
    )
    activity_id = str(events[0]["payload"]["activity_id"])

    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "started"
    assert runner.started_task.current_scene_path is not None
    current_scene_path = Path(runner.started_task.current_scene_path)
    assert current_scene_path == (
        tmp_path
        / "workspace"
        / "tasks"
        / "00000000-0000-4000-8000-000000000001"
        / "inputs"
        / "current-scene.png"
    ).resolve()
    assert current_scene_path.read_bytes() == frozen_scene_bytes

    executor.shutdown()


def test_background_agent_progress_uses_shared_backoff_and_invalidates_on_result(
    tmp_path: Path,
) -> None:
    """Turn 输出只在定时检查形成弱进度，工具流水被忽略且终态会撤销进度。"""

    current_time = 0.0
    scheduler = ActivityProgressScheduler(
        monotonic_now=lambda: current_time,
        autostart=False,
    )
    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000001",
        monotonic_now=lambda: current_time,
        progress_scheduler=scheduler,
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="转换音频",
        deliverable=True,
        requirements="",
        materials=(),
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert executor.commit_prepared(activity_id).kind == "started"

    executor._append_agent_event(
        AgentEvent(
            event_type="turn_output",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={"turn_index": 1, "content_text": "已经完成音频格式检查"},
        )
    )
    assert executor.drain_events()[0]["type"] == "background_agent.turn_output"
    current_time = 30.0
    assert scheduler.run_due() == 1
    progress = executor.drain_events()[0]
    assert progress["type"] == "background_agent.progress"
    assert progress["payload"]["notice_seq"] == 1
    snapshot = executor.activity_snapshot("session-a")
    assert snapshot is not None
    assert snapshot.progress_reasoning_text == "已经完成音频格式检查\n"
    assert executor.acknowledge_progress_notice(activity_id, 1) is True

    executor._append_agent_event(
        AgentEvent(
            event_type="turn_output",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={"turn_index": 2, "content_text": "正在生成目标文件"},
        )
    )
    assert executor.drain_events()[0]["type"] == "background_agent.turn_output"
    executor._append_agent_event(
        AgentEvent(
            event_type="stage_changed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={"stage": "generating_result"},
        )
    )
    invalidated, stage_changed = executor.drain_events()
    assert invalidated["type"] == "background_agent.progress_invalidated"
    assert stage_changed["payload"]["stage"] == "generating_result"
    finalizing = executor.activity_snapshot("session-a")
    assert finalizing is not None and finalizing.phase == "finalizing"
    executor.shutdown()


def test_background_activity_snapshot_retains_authoritative_identity_until_release(
    tmp_path: Path,
) -> None:
    """通道保留权威目标，跨会话脱敏由 Foreground 请求边界统一完成。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000001",
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="整理不能泄露给其它会话的材料",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(activity_id).kind == "started"

    running = executor.activity_snapshot("session-a")
    assert running is not None
    assert running.to_payload()["duplicate_tool_calls_forbidden"] is True

    other_session = executor.activity_snapshot("session-b")
    assert other_session is not None
    assert other_session.goal == "整理不能泄露给其它会话的材料"
    assert other_session.progress_reasoning_text == ""
    assert other_session.work_plan_summary is None

    executor._append_agent_event(
        AgentEvent(
            event_type="interaction_requested",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={"request_id": "permission-a", "kind": "permission"},
        )
    )
    waiting = executor.activity_snapshot("session-a")
    assert waiting is not None and waiting.state == "waiting_user"
    assert waiting.to_payload()["duplicate_tool_calls_forbidden"] is True

    assert executor.cancel_running(activity_id, "user_cancelled") is True
    cancelling = executor.activity_snapshot("session-a")
    assert cancelling is not None and cancelling.state == "cancelling"
    assert cancelling.to_payload()["duplicate_tool_calls_forbidden"] is True
    executor.shutdown()


def test_background_cancel_preserves_first_explicit_reason(tmp_path: Path) -> None:
    """监督升级先到达后，迟到的普通取消不能覆盖首个取消原因。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000081",
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="把音频转换为 MP3",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(activity_id).kind == "started"

    assert executor.cancel_running(activity_id, "user_correction") is True
    assert executor.cancel_running(activity_id, "user_cancelled") is True

    assert runner.cancelled_activity_id == activity_id
    assert runner.cancel_reason == "user_correction"
    executor.shutdown()


def test_background_agent_work_plan_drives_foreground_summary_and_real_progress(
    tmp_path: Path,
) -> None:
    """真实 TodoList 同时进入动态环境，并按现有退避形成有意义进度。"""

    current_time = 0.0
    scheduler = ActivityProgressScheduler(
        monotonic_now=lambda: current_time,
        autostart=False,
    )
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000001",
        monotonic_now=lambda: current_time,
        progress_scheduler=scheduler,
        runner=StubRunner(),
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="对比两个项目",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(activity_id).kind == "started"

    executor._append_agent_event(
        AgentEvent(
            event_type="work_plan_changed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={
                "work_plan": {
                    "revision": 1,
                    "items": [
                        {
                            "task_id": "1",
                            "subject": "理解参考项目",
                            "active_form": "正在理解参考项目",
                            "status": "completed",
                            "order": 1,
                        },
                        {
                            "task_id": "2",
                            "subject": "核对当前链路",
                            "active_form": "正在核对当前链路",
                            "status": "in_progress",
                            "order": 2,
                        },
                        {
                            "task_id": "3",
                            "subject": "整理借鉴建议",
                            "active_form": "正在整理借鉴建议",
                            "status": "pending",
                            "order": 3,
                        },
                    ],
                }
            },
        )
    )

    plan_event = executor.drain_events()[0]
    assert plan_event["type"] == "background_agent.work_plan_changed"
    snapshot = executor.activity_snapshot("session-a")
    assert snapshot is not None and snapshot.work_plan_summary is not None
    assert snapshot.work_plan_summary.to_payload() == {
        "total": 3,
        "completed": 1,
        "current": "正在核对当前链路",
        "next": "整理借鉴建议",
    }

    current_time = 30.0
    assert scheduler.run_due() == 1
    progress = executor.drain_events()[0]
    assert progress["type"] == "background_agent.progress"
    snapshot = executor.activity_snapshot("session-a")
    assert snapshot is not None
    assert snapshot.progress_reasoning_text == "正在核对当前链路。\n"
    executor.shutdown()


def test_background_agent_work_interaction_validates_kind_and_extends_materials(
    tmp_path: Path,
) -> None:
    """统一命令在子进程校验请求类型，并在恢复资料问题前追加正式材料。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000001",
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="整理采访",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(activity_id).kind == "started"
    executor._append_agent_event(
        AgentEvent(
            event_type="interaction_requested",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={
                "request_id": "question-a",
                "kind": "question",
                "questions": [
                    {"question": "使用哪个版本？"},
                    {"question": "输出什么格式？"},
                ],
            },
        )
    )
    executor.drain_events()

    assert executor.respond_to_work_interaction(
        answer=AgentInteractionAnswer(
            request_id="question-a",
            outcome="answer",
            answers={"使用哪个版本？": "第三版"},
        )
    ) is False
    assert executor.respond_to_work_interaction(
        answer=AgentInteractionAnswer(
            request_id="question-missing",
            outcome="cancel",
            answers={},
        )
    ) is False

    executor._append_agent_event(
        AgentEvent(
            event_type="interaction_requested",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={
                "request_id": "permission-a",
                "kind": "permission",
            },
        )
    )
    executor.drain_events()
    assert executor.respond_to_work_interaction(
        answer=AgentInteractionAnswer(
            request_id="permission-a",
            outcome="answer",
            answers={},
        )
    ) is False
    assert executor.respond_to_work_interaction(
        answer=AgentInteractionAnswer(
            request_id="permission-a",
            outcome="allow",
            answers={},
        )
    ) is True

    supplemental = AgentMaterial(
        material_id="material-new",
        display_name="第三版.md",
        material_kind="dragged_file",
        media_class="text",
        local_path=str(tmp_path / "第三版.md"),
        node_kind="file",
    )
    assert executor.respond_to_work_interaction(
        answer=AgentInteractionAnswer(
            request_id="question-a",
            outcome="answer",
            answers={
                "使用哪个版本？": "第三版",
                "输出什么格式？": "Markdown",
            },
            user_text="第三版，输出 Markdown",
        ),
        materials=(supplemental,),
    ) is True
    assert runner.question_answer.answers == {
        "使用哪个版本？": "第三版",
        "输出什么格式？": "Markdown",
    }
    assert runner.question_answer.materials == (supplemental,)

    replacement_path = tmp_path / "third-edition-replaced.md"
    replacement_path.write_text("替换后的第三版", encoding="utf-8")
    executor._append_agent_event(
        AgentEvent(
            event_type="interaction_requested",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={
                "request_id": "question-b",
                "kind": "question",
                "questions": [{"question": "是否使用重新授权的文件？"}],
            },
        )
    )
    executor.drain_events()
    reauthorized = AgentMaterial(
        material_id="material-new",
        display_name="第三版.md",
        material_kind="dragged_file",
        media_class="text",
        local_path=str(replacement_path),
        node_kind="file",
    )
    assert executor.respond_to_work_interaction(
        answer=AgentInteractionAnswer(
            request_id="question-b",
            outcome="answer",
            answers={"是否使用重新授权的文件？": "是"},
        ),
        materials=(reauthorized,),
    ) is True
    assert runner.question_answer.materials == (reauthorized,)
    assert executor._active_task is not None
    catalog_by_id = {
        material.material_id: material
        for material in executor._active_task.material_catalog
    }
    assert catalog_by_id["material-new"].local_path == str(replacement_path)

    executor._append_agent_event(
        AgentEvent(
            event_type="completed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={
                "display_content": "整理完成",
                "delivery_brief": "采访已经整理完成。",
                "output_paths": [],
            },
        )
    )
    completed = executor.drain_events()[-1]
    assert completed["payload"]["source_materials"] == ["第三版.md"]
    executor.shutdown()


def test_background_correction_inherits_initial_supplemental_and_current_materials(
    tmp_path: Path,
) -> None:
    """后台纠正 B 应继承 A 的初始材料、运行期补充材料和纠正轮材料。"""

    initial_path = tmp_path / "initial.pdf"
    supplemental_path = tmp_path / "supplemental.md"
    correction_path = tmp_path / "correction.csv"
    initial_path.write_text("initial", encoding="utf-8")
    supplemental_path.write_text("supplemental", encoding="utf-8")
    correction_path.write_text("correction", encoding="utf-8")
    activity_ids = iter(
        (
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
        )
    )
    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )
    initial = AgentMaterial(
        "initial",
        "initial.pdf",
        "dragged_file",
        "mixed_or_document",
        str(initial_path),
        "file",
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="错误目标",
        deliverable=True,
        requirements="",
        materials=(initial,),
    )
    old_activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(old_activity_id).kind == "started"
    executor._append_agent_event(
        AgentEvent(
            event_type="interaction_requested",
            activity_id=old_activity_id,
            interaction_session_id="session-a",
            payload={
                "request_id": "question-a",
                "kind": "question",
                "questions": [{"question": "还需要什么？"}],
            },
        )
    )
    executor.drain_events()
    supplemental = AgentMaterial(
        "supplemental",
        "supplemental.md",
        "dragged_file",
        "text",
        str(supplemental_path),
        "file",
    )
    assert executor.respond_to_work_interaction(
        answer=AgentInteractionAnswer(
            request_id="question-a",
            outcome="answer",
            answers={"还需要什么？": "使用补充文件"},
        ),
        materials=(supplemental,),
    ) is True
    current = AgentMaterial(
        "correction",
        "correction.csv",
        "dragged_file",
        "text",
        str(correction_path),
        "file",
    )

    correction_accepted, correction_events = _reserve_correction(executor,
        based_on_activity_id=old_activity_id,
        interaction_session_id="session-a",
        goal="正确目标",
        deliverable=True,
        requirements="输出报告",
        materials=(current,),
        parent_task_context={"activity_id": old_activity_id, "source": "background_agent"},
    )

    assert correction_accepted is True
    new_activity_id = str(correction_events[0]["payload"]["activity_id"])
    prepared = executor._prepared_tasks[new_activity_id].task
    assert [material.material_id for material in prepared.turn_materials] == [
        "correction",
    ]
    assert [material.material_id for material in prepared.material_catalog] == [
        "initial",
        "supplemental",
        "correction",
    ]
    prompt = render_agent_task_prompt(prepared)
    assert "correction.csv" in prompt
    assert "initial.pdf" not in prompt
    assert "supplemental.md" not in prompt
    assert prepared.agent_session_id == "agent-session-a"
    assert prepared.expected_sdk_session_id == "sdk-session-a"
    assert prepared.based_on_activity_id == old_activity_id
    assert runner.cancelled_activity_id is None
    task_root = tmp_path / "workspace" / "tasks" / new_activity_id
    assert task_root.is_dir()
    assert executor.rollback_reserved_correction(new_activity_id) is True
    assert not task_root.exists()
    assert executor.is_activity_correctable(old_activity_id) is True
    executor.shutdown()


def test_background_correction_inherits_parent_skill_snapshot(
    tmp_path: Path,
) -> None:
    """全局 Skill 开关变化后，恢复父会话的纠正任务仍使用父任务快照。"""

    activity_ids = iter(
        (
            "00000000-0000-4000-8000-000000000021",
            "00000000-0000-4000-8000-000000000022",
        )
    )
    config_provider = StubConfigProvider(tmp_path)
    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=config_provider,  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="旧目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    old_activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True and executor.commit_prepared(old_activity_id).kind == "started"
    parent_snapshot = runner.started_task.skill_snapshot
    config_provider.replace_skill_snapshot(SkillExecutionSnapshot(revision=99))

    reservation = executor.reserve_correction(
        based_on_activity_id=old_activity_id,
        interaction_session_id="session-a",
        goal="新目标",
        deliverable=True,
        requirements="",
        materials=(),
    )

    assert reservation.accepted is True
    prepared_resources = executor._prepared_tasks[reservation.activity_id]
    prepared_task = prepared_resources.task
    prepared_config = prepared_resources.execution_config
    assert prepared_task.skill_snapshot == parent_snapshot
    assert prepared_config.skill_snapshot == parent_snapshot
    executor.shutdown()


def test_cognitive_to_background_correction_is_rejected(tmp_path: Path) -> None:
    """Cognitive 父 Activity 不能进入 Background 纠正执行器。"""

    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000003",
        runner=StubRunner(),
    )
    accepted, events = _reserve_correction(executor,
        based_on_activity_id="00000000-0000-4000-8000-000000000001",
        interaction_session_id="session-a",
        goal="根据旧图片生成报告",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context={"source": "cognitive"},
    )

    assert accepted is False
    assert events[0]["payload"]["reason"] == "correction_activity_not_found"
    executor.shutdown()


def test_cross_source_correction_does_not_create_committed_activity(
    tmp_path: Path,
) -> None:
    """跨来源 Background 纠正必须在预留前拒绝。"""

    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000004",
        runner=StubRunner(),
    )
    accepted, events = _reserve_correction(executor,
        based_on_activity_id="00000000-0000-4000-8000-000000000001",
        interaction_session_id="session-a",
        goal="正确目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context={"source": "cognitive"},
    )
    assert accepted is False
    assert events[0]["payload"]["reason"] == "correction_activity_not_found"
    executor.shutdown()


def test_committed_background_failure_port_releases_reservation_when_ledger_raises(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """失败事件账本异常时，收口端口也不能抛错或永久占住 B。"""

    activity_ids = iter(
        (
            "00000000-0000-4000-8000-000000000007-old",
            "00000000-0000-4000-8000-000000000007-new",
        )
    )
    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )
    accepted_old, old_events = executor.prepare(
        interaction_session_id="session-a",
        goal="旧目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    old_activity_id = str(old_events[0]["payload"]["activity_id"])
    assert accepted_old is True and executor.commit_prepared(old_activity_id).kind == "started"
    accepted, events = _reserve_correction(executor,
        based_on_activity_id=old_activity_id,
        interaction_session_id="session-a",
        goal="正确目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context={
            "activity_id": old_activity_id,
            "source": "background_agent",
            "interaction_session_id": "session-a",
        },
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert executor.commit_reserved_correction(activity_id) is True

    def reject_event(_event: object) -> None:
        """模拟提交后事件账本突然不可写。"""

        raise RuntimeError("事件账本不可写")

    monkeypatch.setattr(executor, "_append_agent_event", reject_event)

    assert executor.fail_reserved_correction(
        activity_id,
        reason="correction_cancel_failed",
    ) is True
    assert executor.has_activity(activity_id) is False
    assert executor.fail_reserved_correction(
        activity_id,
        reason="correction_cancel_failed",
    ) is False
    executor.shutdown()


def test_shutdown_compensates_foreground_committed_background_correction(
    tmp_path: Path,
) -> None:
    """Foreground 已接受的 B 在 shutdown 时必须失败收口，不能被当作未提交文件删除。

    Args:
        tmp_path: pytest 提供的隔离工作区。

    Returns:
        无返回值；终态和 Grant 释放由断言验证。
    """

    activity_ids = iter(
        (
            "00000000-0000-4000-8000-000000000005-old",
            "00000000-0000-4000-8000-000000000005-new",
        )
    )
    asset_root = tmp_path / "assets"
    _write_managed_cua_skill(asset_root)
    runner = StubRunner()
    capability = CuaCapabilitySlot()
    executor = _ordinary_execution_lane(
        config_provider=_cua_config_provider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=asset_root,
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
        cua_integration=RecordingEmbeddedCuaRuntime(),  # type: ignore[arg-type]
        cua_capability=capability,
    )
    accepted_old, old_events = executor.prepare(
        interaction_session_id="session-a",
        goal="旧目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    old_activity_id = str(old_events[0]["payload"]["activity_id"])
    assert accepted_old is True and executor.commit_prepared(old_activity_id).kind == "started"
    assert capability.snapshot().available is False
    accepted, events = _reserve_correction(executor,
        based_on_activity_id=old_activity_id,
        interaction_session_id="session-a",
        goal="正确目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context={
            "activity_id": old_activity_id,
            "source": "background_agent",
            "interaction_session_id": "session-a",
        },
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    assert executor.commit_reserved_correction(activity_id) is True
    # 父 Runner 的 finally 已经把全局 Grant 原子转给 committed replacement。
    assert executor._finalize_cua_grant(old_activity_id) is None  # noqa: SLF001
    assert capability.snapshot().available is False

    executor.shutdown()

    events = executor.drain_events()
    assert [event["type"] for event in events] == ["background_agent.failed"]
    failed = events[0]
    assert failed["payload"]["correction_failure_reason"] == "backend_shutdown"
    assert executor.has_activity(activity_id) is False
    assert capability.snapshot().available is True


def test_shutdown_rolls_back_uncommitted_background_correction(tmp_path: Path) -> None:
    """Foreground 尚未接受的 B 在 shutdown 时只回滚工作区，不得伪造公共终态。"""

    activity_ids = iter(
        (
            "00000000-0000-4000-8000-000000000006-old",
            "00000000-0000-4000-8000-000000000006-new",
        )
    )
    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )
    accepted_old, old_events = executor.prepare(
        interaction_session_id="session-a",
        goal="旧目标",
        deliverable=False,
        requirements="",
        materials=(),
    )
    old_activity_id = str(old_events[0]["payload"]["activity_id"])
    assert accepted_old is True and executor.commit_prepared(old_activity_id).kind == "started"
    accepted, events = _reserve_correction(executor,
        based_on_activity_id=old_activity_id,
        interaction_session_id="session-a",
        goal="正确目标",
        deliverable=False,
        requirements="",
        materials=(),
        parent_task_context={
            "activity_id": old_activity_id,
            "source": "background_agent",
            "interaction_session_id": "session-a",
        },
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])
    task_root = tmp_path / "workspace" / "tasks" / activity_id
    assert task_root.is_dir()

    executor.shutdown()

    assert not task_root.exists()
    assert executor.drain_events() == []


def test_background_agent_rejects_submission_until_runner_cleanup_finishes(
    tmp_path: Path,
) -> None:
    """公开终态后仍占槽，SDK Runner 清理完成前新任务必须拒绝。"""

    runner = StubRunner()
    activity_ids = iter(
        (
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
        )
    )
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="第一项任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "started"
    executor._append_agent_event(
        AgentEvent(
            event_type="completed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={
                "display_content": "完成",
                "delivery_brief": "任务已经完成。",
                "output_paths": [],
            },
        )
    )

    rejected, rejection_events = executor.prepare(
        interaction_session_id="session-a",
        goal="第二项任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    assert rejected is False
    assert rejection_events[0]["payload"]["reason"] == "background_slot_occupied"

    finalizing = executor.activity_snapshot("session-b")
    assert finalizing is not None
    assert finalizing.phase == "finalizing"
    assert finalizing.stage_code in ACTIVITY_STAGE_CODES
    assert finalizing.goal == "第一项任务"
    assert finalizing.to_payload()["duplicate_tool_calls_forbidden"] is True

    runner.available = True
    executor._on_runner_slot_released(activity_id)
    next_accepted, next_events = executor.prepare(
        interaction_session_id="session-a",
        goal="第二项任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    next_activity_id = str(next_events[0]["payload"]["activity_id"])
    assert next_accepted is True
    assert executor.commit_prepared(next_activity_id).kind == "started"
    assert runner.started_task.activity_id == next_activity_id
    running = executor.activity_snapshot("session-b")
    assert running is not None and running.phase == "reasoning"


def test_invalid_runner_stage_never_enters_activity_or_foreground_snapshot(
    tmp_path: Path,
) -> None:
    """Runner 写错阶段名时应在来源边界丢弃，不能污染公共 Activity 契约。"""

    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="验证阶段边界",
        deliverable=False,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "started"

    executor._append_agent_event(
        AgentEvent(
            event_type="stage_changed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={"stage": "finishing"},
        )
    )

    assert executor.drain_events() == []
    snapshot = executor.activity_snapshot("session-a")
    assert snapshot is not None
    assert snapshot.stage_code == "preparing"
    executor.shutdown()


def test_background_agent_terminalizes_committed_task_when_runner_rejects_start(
    tmp_path: Path,
) -> None:
    """提交后 Runner 意外拒绝启动时，任务必须失败收口并释放占位。"""

    runner = StubRunner()
    runner.start_result = False
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000001",
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="需要收口的任务",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])

    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "start_failed"
    emitted = executor.drain_events()
    assert [event["type"] for event in emitted] == ["background_agent.failed"]
    assert emitted[-1]["payload"]["failure"]["code"] == "agent_cli_crash"
    assert emitted[-1]["payload"]["suppress_notice"] is True
    assert emitted[-1]["payload"]["internal_usage"] == {
        "is_complete": False,
        "reference_cost_usd": None,
        "by_model": [],
    }
    assert executor.activity_snapshot("session-a") is None
    executor.shutdown()


def test_background_correction_start_returns_false_when_runner_rejects(
    tmp_path: Path,
) -> None:
    """旧任务清理后 Runner 拒绝 B 时，纠正事实必须是启动失败。"""

    activity_ids = iter(("background-old", "background-new"))
    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="错误目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    old_activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True
    assert executor.commit_prepared(old_activity_id).kind == "started"

    reservation = executor.reserve_correction(
        based_on_activity_id=old_activity_id,
        interaction_session_id="session-a",
        goal="正确目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    assert reservation.accepted is True
    assert executor.commit_reserved_correction(reservation.activity_id) is True

    executor._append_agent_event(
        AgentEvent(
            event_type="cancelled",
            activity_id=old_activity_id,
            interaction_session_id="session-a",
            payload={"cancellation_reason": "user_correction"},
        )
    )
    runner.available = True
    runner.start_result = False

    assert executor.start_reserved_correction(reservation.activity_id) is False
    new_activity_events = [
        event
        for event in executor.drain_events()
        if event["payload"].get("activity_id") == reservation.activity_id
    ]
    assert [event["type"] for event in new_activity_events] == ["background_agent.failed"]
    assert new_activity_events[-1]["payload"]["suppress_notice"] is True
    assert executor.has_activity(reservation.activity_id) is False
    executor.shutdown()


def test_background_correction_with_empty_id_factory_does_not_reserve_activity(
    tmp_path: Path,
) -> None:
    """纠正编号生成失败时应直接拒绝，且不创建工作区或预留任务。"""

    activity_ids = iter(("background-old", ""))
    runner = StubRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(activity_ids),
        runner=runner,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="旧目标",
        deliverable=True,
        requirements="",
        materials=(),
    )
    old_activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True
    assert executor.commit_prepared(old_activity_id).kind == "started"

    reservation = executor.reserve_correction(
        based_on_activity_id=old_activity_id,
        interaction_session_id="session-a",
        goal="正确目标",
        deliverable=True,
        requirements="",
        materials=(),
    )

    assert reservation.accepted is False
    assert reservation.activity_id == ""
    assert reservation.events[0]["payload"]["reason"] == (
        "background_activity_identity_missing"
    )
    assert not executor._prepared_tasks
    assert all(
        task_id != "" for task_id in executor._known_tasks
    )
    executor.shutdown()


def test_background_agent_failure_report_uses_current_activity_stage(
    tmp_path: Path,
) -> None:
    """结果生成期间失败时，故障报告不能谎报为处理阶段。"""

    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000001",
        runner=StubRunner(),
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="生成结果后失败的任务",
        deliverable=True,
        requirements="",
        materials=(),
    )
    activity_id = str(events[0]["payload"]["activity_id"])
    assert accepted is True
    assert executor.commit_prepared(activity_id).kind == "started"

    executor._append_agent_event(
        AgentEvent(
            event_type="stage_changed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={"stage": "generating_result"},
        )
    )
    executor._append_agent_event(
        AgentEvent(
            event_type="failed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={"error_code": "agent_execution_error"},
        )
    )

    failed = next(
        event for event in executor.drain_events() if event["type"] == "background_agent.failed"
    )
    assert failed["payload"]["failure"]["stage_code"] == "generating_result"
    executor.shutdown()


def test_shutdown_defers_prepared_cleanup_until_background_commit_drains(
    tmp_path: Path,
) -> None:
    """关闭超时不能清理已经进入提交临界区的 prepared Activity。"""

    barrier = BackgroundCommitDrainBarrier()
    runner = ShutdownRecordingRunner()
    executor = _ordinary_execution_lane(
        config_provider=StubConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: "00000000-0000-4000-8000-000000000099",
        runner=runner,
        commit_barrier=barrier,
    )
    accepted, events = executor.prepare(
        interaction_session_id="session-a",
        goal="等待主进程批准后启动",
        deliverable=False,
        requirements="",
        materials=(),
    )
    assert accepted is True
    activity_id = str(events[0]["payload"]["activity_id"])

    assert barrier.enter("self-reflection:recovery-1") is True
    executor.shutdown(timeout=0.0)

    # ChildRuntime 已经继续执行 shutdown，但 Background Executor 仍必须保留
    # prepared 身份，让获批 worker 随后完成 commit_prepared。
    assert runner.shutdown_calls == []
    assert activity_id in executor._prepared_tasks  # noqa: SLF001
    assert executor.commit_prepared(activity_id).kind == "started"

    assert barrier.leave("self-reflection:recovery-1") is True
    assert runner.shutdown_event.wait(timeout=1.0)
    assert activity_id not in executor._prepared_tasks  # noqa: SLF001
