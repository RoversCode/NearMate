"""验证 Background Correction 在固定双通道中的原位 handoff。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from companion.background_agent.activity_executor import BackgroundAgentActivityExecutor
from companion.background_agent.agent_session import AgentSessionResumeIdentity
from companion.background_agent.contracts import AgentEvent, BackgroundExecutionLane
from companion.background_agent.execution_lane import BackgroundAgentExecutionLane
from companion.background_agent.execution_slot import ExecutionSlotSubmissionKind
from companion.background_agent.interaction_bridge import AgentInteractionAnswer
from companion.background_agent.model_config import AgentExecutionConfig, AgentModelConfig
from companion.background_agent.skill_models import SkillExecutionSnapshot
from companion.background_agent.workspace import WorkspaceManager
from companion.foreground_tools.activity_correction.service import ActivityCorrectionService
from companion.foreground_tools.orchestration.contracts import ToolFollowupFact


class ControlledRunner:
    """提供可控取消、清理和启动结果的单通道 Runner 替身。"""

    def __init__(self, session_name: str) -> None:
        """初始化 Runner 的可观察状态。

        Args:
            session_name: 本 Runner 独占的产品级 AgentSession 名称。
        """

        self.session_name = session_name
        self.available = True
        self.lane: BackgroundAgentExecutionLane | None = None
        self.started: list[str] = []
        self.tasks_by_activity_id: dict[str, object] = {}
        self.cancel_accepts = True
        self.cleanup_succeeds = True
        self.start_failures: set[str] = set()
        self._pending_cancel: tuple[str, str] | None = None
        self.question_answers: list[object] = []

    def start(self, task: object, execution_config: AgentExecutionConfig) -> bool:
        """按 Activity 编号决定是否真实接管任务。

        Args:
            task: 通道冻结的后台任务。
            execution_config: 本次任务的可信配置快照。

        Returns:
            Runner 接管任务时返回真；测试指定启动失败时返回假。
        """

        _ = execution_config
        activity_id = str(getattr(task, "activity_id", ""))
        if activity_id in self.start_failures:
            return False
        self.started.append(activity_id)
        self.tasks_by_activity_id[activity_id] = task
        self.available = False
        return True

    def is_available(self) -> bool:
        """返回 Runner 是否已经完整释放。"""

        return self.available

    def cancel(self, activity_id: str, *, reason: str = "agent_interrupted") -> bool:
        """记录取消请求，清理由等待入口模拟完成。

        Args:
            activity_id: 需要取消的父 Activity 编号。
            reason: 本次取消的稳定原因。

        Returns:
            当前测试是否接纳取消。
        """

        if not self.cancel_accepts:
            return False
        self._pending_cancel = (activity_id, reason)
        return True

    def wait_until_available(self, _timeout: float = 5.0) -> bool:
        """在允许清理时发布真实取消终态并释放 Runner。

        Args:
            _timeout: 允许等待清理的最长秒数；测试替身不实际等待。

        Returns:
            清理完成时返回真，测试指定超时时返回假。
        """

        if not self.cleanup_succeeds:
            return False
        pending_cancel = self._pending_cancel
        if pending_cancel is None:
            return self.available
        lane = self.lane
        if lane is None:
            raise AssertionError("测试 Runner 尚未绑定执行通道")
        activity_id, reason = pending_cancel
        lane._append_agent_event(
            AgentEvent(
                event_type="cancelled",
                activity_id=activity_id,
                interaction_session_id="session-a",
                payload={"cancellation_reason": reason},
            )
        )
        self._pending_cancel = None
        self.available = True
        return True

    def resume_identity_for_activity(
        self,
        activity_id: str,
    ) -> AgentSessionResumeIdentity | None:
        """返回纠正任务恢复父 SDK 会话所需的身份。

        Args:
            activity_id: 需要恢复会话的父 Activity 编号。

        Returns:
            父任务已被本 Runner 接管时返回恢复身份，否则返回空。
        """

        task = self.tasks_by_activity_id.get(activity_id)
        if task is None:
            return None
        return AgentSessionResumeIdentity(
            agent_session_id=self.session_name,
            sdk_session_id=f"sdk-{self.session_name}",
            workspace_root=str(getattr(task, "workspace_root", "")),
        )

    def answer_question(self, answer: object) -> bool:
        """记录一条只应路由到本 Runner 的资料回答。

        Args:
            answer: 通道校验后的规范化资料答案。

        Returns:
            测试替身固定接纳回答。
        """

        self.question_answers.append(answer)
        return True

    def shutdown(self, _timeout: float) -> None:
        """把测试 Runner 标记为空闲。

        Args:
            _timeout: 允许等待关闭的最长秒数；测试替身不实际等待。
        """

        self.available = True


class StaticConfigProvider:
    """为双通道 Executor 提供稳定的测试配置。"""

    def __init__(self, tmp_path: Path) -> None:
        """创建不会读取外部凭据的测试配置。

        Args:
            tmp_path: pytest 为当前测试创建的隔离目录。
        """

        self.config = AgentExecutionConfig(
            model_config=AgentModelConfig(
                environment={"ANTHROPIC_API_KEY": "test-key"},
                config_dir=tmp_path / "runtime",
                model="claude-sonnet-4-5",
            ),
            permission_mode="acceptEdits",
            max_turns=30,
        )

    def snapshot(
        self,
        *,
        inherited_skill_snapshot: SkillExecutionSnapshot | None = None,
    ) -> AgentExecutionConfig:
        """返回当前配置，并按纠正要求继承父任务 Skill 快照。

        Args:
            inherited_skill_snapshot: 父任务已经冻结的 Skill 能力快照。

        Returns:
            可直接交给 Runner 的稳定执行配置。
        """

        if inherited_skill_snapshot is None:
            return self.config
        return replace(self.config, skill_snapshot=inherited_skill_snapshot)


def test_two_lanes_run_and_route_controls_independently(tmp_path: Path) -> None:
    """双 active 的取消、资料回答、快照和事件都必须按 Activity 隔离。"""

    ordinary_runner, recovery_runner, executor = _make_executor(
        tmp_path,
        ["ordinary-a", "recovery-r"],
    )
    _prepare_and_commit(executor, "ordinary", "普通目标", "ordinary-a")
    _prepare_and_commit(executor, "recovery", "恢复目标", "recovery-r")

    availability = executor.background_availability_snapshot()
    assert availability.active_count == 2
    assert availability.ordinary.busy is True
    assert availability.recovery.busy is True
    assert availability.to_payload()["queue_supported"] is False
    snapshots = executor.activity_snapshots("session-a")
    assert [snapshot.activity_id for snapshot in snapshots] == [
        "ordinary-a",
        "recovery-r",
    ]

    assert executor.cancel_running("ordinary-a", "user_cancelled") is True
    assert ordinary_runner._pending_cancel == ("ordinary-a", "user_cancelled")
    assert recovery_runner._pending_cancel is None

    assert ordinary_runner.lane is not None
    assert recovery_runner.lane is not None
    ordinary_runner.lane._append_agent_event(
        _question_event("ordinary-a", "ordinary-question", "普通任务需要什么？")
    )
    recovery_runner.lane._append_agent_event(
        _question_event("recovery-r", "recovery-question", "恢复任务需要什么？")
    )
    executor.drain_events()
    assert executor.respond_to_work_interaction(
        answer=AgentInteractionAnswer(
            request_id="recovery-question",
            outcome="answer",
            answers={"恢复任务需要什么？": "恢复答案"},
        )
    ) is True
    assert ordinary_runner.question_answers == []
    assert len(recovery_runner.question_answers) == 1

    recovery_runner.lane._append_agent_event(
        _turn_output_event("recovery-r", 1, "恢复通道输出")
    )
    ordinary_runner.lane._append_agent_event(
        _turn_output_event("ordinary-a", 1, "普通通道输出")
    )
    ordered_outputs = [
        event["payload"]["activity_id"]
        for event in executor.drain_events()
        if event["type"] == "background_agent.turn_output"
    ]
    assert ordered_outputs == ["recovery-r", "ordinary-a"]
    executor.shutdown()


def test_same_lane_busy_rejects_but_other_lane_can_start(tmp_path: Path) -> None:
    """恢复槽占用不影响普通准入，两条通道各自忙时使用独立拒绝文案。"""

    _, _, executor = _make_executor(
        tmp_path,
        ["recovery-r", "ordinary-a"],
    )
    _prepare_and_commit(executor, "recovery", "恢复目标", "recovery-r")
    assert executor.background_available() is True
    _prepare_and_commit(executor, "ordinary", "普通目标", "ordinary-a")

    ordinary_accepted, ordinary_events = executor.prepare(
        execution_lane="ordinary",
        interaction_session_id="session-a",
        goal="第二项普通任务",
        deliverable=False,
        requirements="",
        materials=(),
    )
    recovery_accepted, recovery_events = executor.prepare(
        execution_lane="recovery",
        interaction_session_id="session-a",
        goal="第二项恢复任务",
        deliverable=False,
        requirements="",
        materials=(),
    )

    assert ordinary_accepted is False
    assert ordinary_events[0]["payload"]["reason"] == "background_slot_occupied"
    assert ordinary_events[0]["payload"]["safe_message"] == (
        "已有任务正在处理，当前无法开启新任务。"
    )
    assert recovery_accepted is False
    assert recovery_events[0]["payload"]["reason"] == "background_slot_occupied"
    assert recovery_events[0]["payload"]["safe_message"] == (
        "已有一项恢复工作正在处理，这次没有再启动新的后台任务。"
    )
    executor.shutdown()


def test_ordinary_correction_does_not_pause_recovery_lane(tmp_path: Path) -> None:
    """普通任务 B→B' handoff 期间，恢复任务 R 必须持续独立运行。"""

    ordinary_runner, recovery_runner, executor = _make_executor(
        tmp_path,
        ["ordinary-b", "recovery-r", "ordinary-b-prime"],
    )
    _prepare_and_commit(executor, "ordinary", "普通旧目标", "ordinary-b")
    _prepare_and_commit(executor, "recovery", "恢复目标", "recovery-r")

    fact = _commit_background_correction(
        executor,
        parent_id="ordinary-b",
        replacement_id="ordinary-b-prime",
    )

    assert fact.kind == "activity_replaced"
    assert ordinary_runner.started == ["ordinary-b", "ordinary-b-prime"]
    assert recovery_runner.started == ["recovery-r"]
    assert executor.has_activity("recovery-r") is True
    executor.shutdown()


def test_recovery_correction_inherits_recovery_lane(tmp_path: Path) -> None:
    """恢复任务 R→R' 必须继续使用恢复 Runner，普通任务不受影响。"""

    ordinary_runner, recovery_runner, executor = _make_executor(
        tmp_path,
        ["ordinary-o", "recovery-r", "recovery-r-prime"],
    )
    _prepare_and_commit(executor, "ordinary", "普通目标", "ordinary-o")
    _prepare_and_commit(executor, "recovery", "恢复旧目标", "recovery-r")

    fact = _commit_background_correction(
        executor,
        parent_id="recovery-r",
        replacement_id="recovery-r-prime",
    )

    assert fact.kind == "activity_replaced"
    assert recovery_runner.started == ["recovery-r", "recovery-r-prime"]
    assert ordinary_runner.started == ["ordinary-o"]
    assert executor.has_activity("ordinary-o") is True
    executor.shutdown()


def test_parent_terminal_before_commit_fails_replacement_without_requeue(
    tmp_path: Path,
) -> None:
    """父任务先自然终态时，替代任务必须失败且通道直接回到空闲。"""

    ordinary_runner, _, executor = _make_executor(
        tmp_path,
        ["ordinary-b", "ordinary-b-prime"],
    )
    _prepare_and_commit(executor, "ordinary", "旧目标", "ordinary-b")
    service = ActivityCorrectionService(
        cognitive_executor=None,
        background_executor=executor,
    )
    prepared = service.prepare_background_correction(
        activity_id="ordinary-b",
        interaction_session_id="session-a",
        goal="迟到纠正",
        deliverable=False,
        requirements="",
        materials=(),
        parent_task_context=_parent_context("ordinary-b", "旧目标"),
    )
    reservation = service.take_reservation(prepared.activity_id)
    assert reservation is not None
    _complete_and_release(ordinary_runner, "ordinary-b")

    assert reservation.action.accept() is True
    assert reservation.action.commit_enqueued() is True
    assert reservation.action.run_after_commit() is True
    assert reservation.action.after_commit_fact is not None
    assert reservation.action.after_commit_fact.kind == "activity_replacement_failed"
    assert ordinary_runner.started == ["ordinary-b"]
    assert executor.background_available() is True
    executor.shutdown()


def test_cancel_rejection_keeps_parent_running(tmp_path: Path) -> None:
    """父任务拒绝取消时，B' 失败收口且父任务继续占用原通道。"""

    ordinary_runner, _, executor = _make_executor(
        tmp_path,
        ["ordinary-b", "ordinary-b-prime"],
    )
    _prepare_and_commit(executor, "ordinary", "旧目标", "ordinary-b")
    ordinary_runner.cancel_accepts = False

    fact = _commit_background_correction(
        executor,
        parent_id="ordinary-b",
        replacement_id="ordinary-b-prime",
    )

    assert fact.kind == "activity_replacement_failed"
    assert "没有取消" in fact.safe_message
    assert ordinary_runner.started == ["ordinary-b"]
    assert executor.has_activity("ordinary-b") is True
    executor.shutdown()


def test_cleanup_timeout_never_runs_parent_and_replacement_together(
    tmp_path: Path,
) -> None:
    """父 Runner 清理超时时替代任务失败，不能与父任务并发。"""

    ordinary_runner, _, executor = _make_executor(
        tmp_path,
        ["ordinary-b", "ordinary-b-prime"],
    )
    _prepare_and_commit(executor, "ordinary", "旧目标", "ordinary-b")
    ordinary_runner.cleanup_succeeds = False

    fact = _commit_background_correction(
        executor,
        parent_id="ordinary-b",
        replacement_id="ordinary-b-prime",
    )

    assert fact.kind == "activity_replacement_failed"
    assert "收尾" in fact.safe_message
    assert ordinary_runner.started == ["ordinary-b"]
    executor.shutdown()


def test_replacement_start_failure_does_not_modify_other_lane(tmp_path: Path) -> None:
    """B' 启动失败只关闭父通道 handoff，恢复通道 R 保持运行。"""

    ordinary_runner, recovery_runner, executor = _make_executor(
        tmp_path,
        ["ordinary-b", "recovery-r", "ordinary-b-prime"],
    )
    ordinary_runner.start_failures.add("ordinary-b-prime")
    _prepare_and_commit(executor, "ordinary", "旧目标", "ordinary-b")
    _prepare_and_commit(executor, "recovery", "恢复目标", "recovery-r")

    fact = _commit_background_correction(
        executor,
        parent_id="ordinary-b",
        replacement_id="ordinary-b-prime",
    )

    assert fact.kind == "activity_replacement_failed"
    assert "新任务没有开始" in fact.safe_message
    assert recovery_runner.started == ["recovery-r"]
    assert executor.has_activity("recovery-r") is True
    executor.shutdown()


def _make_executor(
    tmp_path: Path,
    activity_ids: list[str],
) -> tuple[ControlledRunner, ControlledRunner, BackgroundAgentActivityExecutor]:
    """创建真实双通道 Executor 和两个互相隔离的可控 Runner。

    Args:
        tmp_path: pytest 为当前测试创建的隔离目录。
        activity_ids: 资源模块应按顺序分配的 Activity 编号。

    Returns:
        普通 Runner、恢复 Runner 和真实双通道 Executor。
    """

    ordinary_runner = ControlledRunner("ordinary-session")
    recovery_runner = ControlledRunner("recovery-session")
    identifiers = iter(activity_ids)
    executor = BackgroundAgentActivityExecutor(
        config_provider=StaticConfigProvider(tmp_path),  # type: ignore[arg-type]
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        id_factory=lambda _prefix: next(identifiers),
        ordinary_runner=ordinary_runner,
        recovery_runner=recovery_runner,
    )
    ordinary_runner.lane = executor._lanes["ordinary"]
    recovery_runner.lane = executor._lanes["recovery"]
    return ordinary_runner, recovery_runner, executor


def _prepare_and_commit(
    executor: BackgroundAgentActivityExecutor,
    execution_lane: BackgroundExecutionLane,
    goal: str,
    activity_id: str,
    expected_kind: ExecutionSlotSubmissionKind = "started",
) -> None:
    """在指定可信通道准备并提交一项 Background Activity。

    Args:
        executor: 双通道真实 Executor。
        execution_lane: ordinary 或 recovery 固定通道。
        goal: 当前任务目标。
        activity_id: 预期分配的 Activity 编号。
        expected_kind: commit 后必须得到的唯一初始事实。
    """

    accepted, events = executor.prepare(
        execution_lane=execution_lane,
        interaction_session_id="session-a",
        goal=goal,
        deliverable=False,
        requirements="",
        materials=(),
    )
    assert accepted is True
    assert events[0]["payload"]["activity_id"] == activity_id
    assert executor.commit_prepared(activity_id).kind == expected_kind


def _commit_background_correction(
    executor: BackgroundAgentActivityExecutor,
    *,
    parent_id: str,
    replacement_id: str,
) -> ToolFollowupFact:
    """经由真实 ActivityCorrectionService 提交一次 Background correction。

    Args:
        executor: 同时持有父任务和 replacement 资源的双通道 Executor。
        parent_id: 当前 active 父 Activity 编号。
        replacement_id: 纠正后新 Activity 的预期编号。

    Returns:
        after_commit 产生的唯一纠正事实。
    """

    service = ActivityCorrectionService(
        cognitive_executor=None,
        background_executor=executor,
    )
    prepared = service.prepare_background_correction(
        activity_id=parent_id,
        interaction_session_id="session-a",
        goal="修正后的目标",
        deliverable=True,
        requirements="",
        materials=(),
        parent_task_context=_parent_context(parent_id, "旧目标"),
    )
    assert prepared.accepted is True
    assert prepared.activity_id == replacement_id
    reservation = service.take_reservation(replacement_id)
    assert reservation is not None
    assert reservation.action.accept() is True
    assert reservation.action.commit_enqueued() is True
    assert reservation.action.run_after_commit() is True
    fact = reservation.action.after_commit_fact
    assert fact is not None
    return fact


def _complete_and_release(runner: ControlledRunner, activity_id: str) -> None:
    """发布真实终态并模拟指定 Runner 完整释放。

    Args:
        runner: 持有目标 Activity 的 Runner。
        activity_id: 当前完成并释放的 Activity 编号。
    """

    lane = runner.lane
    if lane is None:
        raise AssertionError("测试 Runner 尚未绑定执行通道")
    lane._append_agent_event(
        AgentEvent(
            event_type="completed",
            activity_id=activity_id,
            interaction_session_id="session-a",
            payload={"display_content": "完成", "delivery_brief": "任务完成。"},
        )
    )
    runner.available = True
    lane._on_runner_slot_released(activity_id)


def _question_event(
    activity_id: str,
    request_id: str,
    question: str,
) -> AgentEvent:
    """构造一条资料问题 Runner 事件。

    Args:
        activity_id: 问题所属 Activity 编号。
        request_id: 后续回答精确路由使用的请求编号。
        question: Runner 需要用户回答的原始问题。

    Returns:
        可直接交给执行通道的交互请求事件。
    """

    return AgentEvent(
        event_type="interaction_requested",
        activity_id=activity_id,
        interaction_session_id="session-a",
        payload={
            "request_id": request_id,
            "kind": "question",
            "questions": [{"question": question}],
        },
    )


def _turn_output_event(
    activity_id: str,
    turn_index: int,
    content_text: str,
) -> AgentEvent:
    """构造一条用于核对共享账本顺序的 Turn 输出事件。

    Args:
        activity_id: 输出所属 Activity 编号。
        turn_index: 同一 Activity 内的递增 Turn 序号。
        content_text: 本轮纯文本输出。

    Returns:
        可直接交给执行通道的 Turn 输出事件。
    """

    return AgentEvent(
        event_type="turn_output",
        activity_id=activity_id,
        interaction_session_id="session-a",
        payload={
            "turn_index": turn_index,
            "content_text": content_text,
        },
    )


def _parent_context(activity_id: str, goal: str) -> dict[str, object]:
    """构造与真实 Activity 快照一致的纠正父上下文。

    Args:
        activity_id: 当前 active 父 Activity 编号。
        goal: 父任务原目标。

    Returns:
        可供 Correction Service 复核的冻结父上下文。
    """

    return {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "source": "background_agent",
        "goal": goal,
        "state": "running",
        "phase": "reasoning",
    }
