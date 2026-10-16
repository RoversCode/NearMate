"""把后台智能体 Runner 投影为 Foreground 可提交的 Activity 执行器。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal

from companion.activities.progress_reasoning import ProgressReasoningBuffer
from companion.activities.progress_scheduler import (
    ACTIVITY_PROGRESS_INTERVALS_S,
    ActivityProgressScheduler,
    ProgressIntervalPolicy,
    ProgressScheduler,
    ScheduledProgressCall,
)
from companion.background_agent.contracts import (
    AgentEvent,
    AgentMaterial,
    AgentTask,
    BackgroundExecutionLane,
    BackgroundLaneAvailability,
)
from companion.background_agent.cua_capability import CuaCapabilitySlot
from companion.background_agent.cua_integration import CUA_SKILL_ID, CuaIntegration
from companion.background_agent.activity_resources import (
    BACKGROUND_AGENT_UNCONFIGURED_MESSAGE,
    ActivityResourcePreparation,
    BackgroundActivityResourceManager,
    PreparedActivityResources,
    merge_agent_materials,
)
from companion.background_agent.config_provider import BackgroundAgentConfigProvider
from companion.background_agent.interaction_bridge import AgentInteractionAnswer
from companion.background_agent.material_access import material_limitations
from companion.background_agent.question_contract import (
    normalize_agent_question_answers,
    normalize_agent_questions,
)
from companion.background_agent.runner import AgentTaskRunner
from companion.background_agent.execution_slot import (
    BackgroundExecutionSlot,
    ExecutionSlotSubmissionOutcome,
)
from companion.background_agent.usage_models import AgentTaskUsageSnapshot
from companion.background_agent.workspace import WorkspaceManager
from companion.foreground_tools.background_activity_runtime import BackgroundActivityRuntime
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.foreground_tools.activity_correction.contracts import (
    CorrectionCancellationOutcome,
    CorrectionReservation,
)
from companion.activities.runtime.failure_reporting import failure_from_reason
from companion.activities.runtime.models import ACTIVITY_STAGE_CODES
from companion.activities.runtime.public_text import summarize_activity_goal
from companion.activities.foreground_snapshot import ForegroundActivitySnapshot
from companion.activities.work_plan import AgentWorkPlan, parse_agent_work_plan
from companion.observability.logging_config import get_backend_logger
from companion.observability.prompt_audit import PromptAuditRecorder


logger = get_backend_logger(__name__)


BACKGROUND_AGENT_ALREADY_RUNNING_MESSAGE = "已有任务正在处理，当前无法开启新任务。"
BACKGROUND_AGENT_RECOVERY_RUNNING_MESSAGE = (
    "已有一项恢复工作正在处理，这次没有再启动新的后台任务。"
)


@dataclass(frozen=True, slots=True)
class _PendingAgentInteraction:
    """保存助手子进程当前可恢复的一条后台交互。

    Args:
        kind: 权限确认或资料问题。
        questions: 资料问题的精确原文；权限请求为空。
    """

    kind: Literal["permission", "question"]
    questions: tuple[str, ...] = ()


class BackgroundAgentExecutionLane:
    """管理后台智能体的单任务槽位、事件账本和取消边界。

    Args:
        lane_kind: ordinary 表示普通 Foreground 通道，recovery 表示恢复专用通道。
        config_provider: 每次新任务读取可信配置的来源。
        runtime_dir: Claude Code 私有运行目录。
        workspace_manager: 默认工作区管理器。
        event_notifier: 新事件入账后的唤醒回调。
        id_factory: Activity 编号生成器。
        monotonic_now: 进度退避使用的单调时钟。
        progress_scheduler: 共享进度调度器。
        progress_policy: 进度退避间隔策略。
        runner: 后台任务 Runner；测试可以传替身。
        event_runtime: 两条通道共用的有序事件账本；为空时由本通道独占创建。
        commit_barrier: Background 提交与执行器关闭共用的排水屏障。
        prompt_audit_recorder: 可选本地 Prompt 审查记录器，透传给真实 Runner。
        cua_integration: 官方 Cua Driver 安装、doctor 与 stdio 配置入口。
        cua_capability: 两条 Background lane 共用的全局 CUA Capability Slot。
        terminal_diagnostics_enabled: 是否把完整 SDK 消息和 CLI stderr 打印到开发终端。
    """

    def __init__(
        self,
        *,
        lane_kind: BackgroundExecutionLane,
        config_provider: BackgroundAgentConfigProvider,
        runtime_dir: Path,
        workspace_manager: WorkspaceManager,
        event_notifier: Callable[[], None] | None = None,
        id_factory: Callable[[str], str] | None = None,
        monotonic_now: Callable[[], float] = time.monotonic,
        progress_scheduler: ProgressScheduler | None = None,
        progress_policy: ProgressIntervalPolicy | None = None,
        runner: Any | None = None,
        event_runtime: BackgroundActivityRuntime | None = None,
        commit_barrier: BackgroundCommitDrainBarrier | None = None,
        prompt_audit_recorder: PromptAuditRecorder | None = None,
        cua_integration: CuaIntegration | None = None,
        cua_capability: CuaCapabilitySlot | None = None,
        terminal_diagnostics_enabled: bool = False,
    ) -> None:
        """初始化后台智能体执行器。

        Args:
            lane_kind: 当前实例负责的固定执行通道。
            config_provider: 每次新任务读取 Console 设置与 SQLite 凭据的可信配置源。
            runtime_dir: SDK 私有配置目录。
            workspace_manager: V1 默认工作区管理器。
            event_notifier: 事件入账后唤醒助手子进程主循环的回调。
            id_factory: 可替换的 Activity 编号生成器。
            monotonic_now: 进度退避使用的单调时钟。
            progress_scheduler: 进程级共享 deadline 调度器。
            progress_policy: 后台智能体弱进度退避间隔。
            runner: 测试可注入的 Runner；生产环境为空时创建真实 SDK Runner。
            event_runtime: 可选共享事件账本；生产双通道必须传入同一实例。
            commit_barrier: 可选的 Background 提交关闭排水屏障。
            prompt_audit_recorder: 可选本地 Prompt 审查记录器，透传给真实 Runner。
            cua_integration: 官方 Cua Driver 安装、doctor 与 stdio 配置入口。
            cua_capability: 两条 Background lane 共用的全局 CUA Capability Slot。
            terminal_diagnostics_enabled: 是否把完整 SDK 消息和 CLI stderr 打印到开发终端。

        Raises:
            ValueError: lane_kind 不是 ordinary 或 recovery。
        """

        if lane_kind not in {"ordinary", "recovery"}:
            raise ValueError("Background lane_kind 必须是 ordinary 或 recovery")
        self._lane_kind = lane_kind
        self._owns_event_runtime = event_runtime is None
        self._events = event_runtime or BackgroundActivityRuntime(
            thread_name_prefix=f"background-agent-{lane_kind}-activity",
            event_notifier=event_notifier,
        )
        self._activity_resources = BackgroundActivityResourceManager(
            config_provider=config_provider,
            workspace_manager=workspace_manager,
            id_factory=id_factory,
        )
        self._monotonic_now = monotonic_now
        self._owns_progress_scheduler = progress_scheduler is None
        self._progress_scheduler = progress_scheduler or ActivityProgressScheduler(
            monotonic_now=monotonic_now
        )
        self._progress_policy = progress_policy or ProgressIntervalPolicy(
            ACTIVITY_PROGRESS_INTERVALS_S
        )
        self._progress_buffer = ProgressReasoningBuffer()
        self._progress_check_count = 0
        self._progress_schedule: ScheduledProgressCall | None = None
        self._pending_interactions: dict[str, _PendingAgentInteraction] = {}
        self._work_plan: AgentWorkPlan | None = None
        self._active_task: AgentTask | None = None
        # Runner 发出终态后仍要等 SDK disconnect 完成；这段时间用同一项 Activity
        # 告诉 Foreground 槽位仍被占用，但不再把它当作正在执行的业务任务。
        self._finishing_task: AgentTask | None = None
        # 普通提交和纠正替代都可能短暂处于 prepared；单槽边界保证同一时间只有
        # 一项普通准备或一项 handoff 替代，不会形成等待队列。
        self._prepared_tasks: dict[str, PreparedActivityResources] = {}
        self._known_tasks: dict[str, AgentTask] = {}
        self._lock = threading.RLock()
        self._shutdown = False
        self._shutdown_finalizing = False
        self._shutdown_requested = False
        self._shutdown_deadline: float | None = None
        self._commit_barrier = commit_barrier
        self._cua_integration = cua_integration
        self._cua_capability = cua_capability
        self._activity_state = "running"
        self._activity_stage = "preparing"
        self._runner = runner or AgentTaskRunner(
            runtime_dir=runtime_dir,
            workspace_manager=workspace_manager,
            event_callback=self._append_agent_event,
            slot_released_callback=self._on_runner_slot_released,
            cua_grant_finalizer=self._finalize_cua_grant,
            thread_name=f"background-agent-{lane_kind}-sdk-loop",
            prompt_audit_recorder=prompt_audit_recorder,
            cua_integration=cua_integration,
            cua_capability=cua_capability,
            terminal_diagnostics_enabled=terminal_diagnostics_enabled,
        )
        self._execution_slot = BackgroundExecutionSlot(self)

    def prepare(
        self,
        *,
        interaction_session_id: str,
        goal: str,
        deliverable: bool,
        requirements: str,
        materials: tuple[AgentMaterial, ...],
        current_scene_bytes: bytes | None = None,
        based_on_activity_id: str | None = None,
        parent_task_context: dict[str, object] | None = None,
        agent_session_id: str | None = None,
    ) -> tuple[bool, list[dict[str, object]]]:
        """登记一项等待 Foreground 接收的后台任务。

        Args:
            interaction_session_id: 触发任务的 Foreground 会话编号。
            goal: 后台任务目标。
            deliverable: 是否登记并展示交付结果。
            requirements: 额外成果要求。
            materials: 本轮材料清单。
            current_scene_bytes: 需要交给执行内核的本轮冻结截图原始字节。
            based_on_activity_id: 可选父任务编号。
            parent_task_context: 父任务的目标、结果、输入和终态事实。
            agent_session_id: 纠正任务优先复用的产品级 AgentSession 编号。

        Returns:
            接受标记和准备事件列表。
        """

        normalized_goal = goal.strip()
        if not normalized_goal:
            return False, [self._event("background_agent.rejected", {"reason": "missing_goal"})]
        admission = self._execution_slot.reserve_submission()
        if not admission.accepted:
            return False, [self._slot_rejection_event(admission.reason)]

        accepted, events = self._prepare_activity_resource(
            interaction_session_id=interaction_session_id,
            goal=normalized_goal,
            deliverable=deliverable,
            requirements=requirements,
            materials=materials,
            current_scene_bytes=current_scene_bytes,
            based_on_activity_id=based_on_activity_id,
            parent_task_context=parent_task_context,
            agent_session_id=agent_session_id,
        )
        if not accepted:
            self._execution_slot.rollback_submission(admission.reservation_id)
            return False, events

        event_payload = events[0].get("payload") if events else None
        activity_id = (
            str(event_payload.get("activity_id") or "").strip()
            if isinstance(event_payload, dict)
            else ""
        )
        if activity_id and self._execution_slot.bind_submission(
            admission.reservation_id,
            activity_id,
        ):
            return True, events

        self._execution_slot.rollback_submission(admission.reservation_id)
        if activity_id:
            self.slot_discard_prepared(activity_id)
        return False, [self._prepare_failed_event()]

    def _prepare_activity_resource(
        self,
        *,
        interaction_session_id: str,
        goal: str,
        deliverable: bool,
        requirements: str,
        materials: tuple[AgentMaterial, ...],
        current_scene_bytes: bytes | None,
        based_on_activity_id: str | None,
        parent_task_context: dict[str, object] | None,
        agent_session_id: str | None,
    ) -> tuple[bool, list[dict[str, object]]]:
        """准备一项独立 Activity 的配置、材料和工作区资源。

        Args:
            interaction_session_id: 触发任务的 Foreground 会话编号。
            goal: 已去除首尾空白的后台任务目标。
            deliverable: 是否登记并展示交付结果。
            requirements: 额外成果要求。
            materials: 本轮正式材料清单。
            current_scene_bytes: 可选冻结现场图片。
            based_on_activity_id: 可选父 Activity 编号。
            parent_task_context: 已脱敏的父任务事实。
            agent_session_id: 可选产品 AgentSession 编号。

        Returns:
            资源准备结果和 canonical prepared 或 rejected 事件。
        """

        with self._lock:
            if self._shutdown or self._shutdown_requested:
                return False, [
                    self._event(
                        "background_agent.rejected",
                        {"reason": "executor_shutdown"},
                    )
                ]
            preparation = self._activity_resources.prepare_new_task(
                interaction_session_id=interaction_session_id,
                goal=goal,
                deliverable=deliverable,
                requirements=requirements,
                materials=materials,
                current_scene_bytes=current_scene_bytes,
                based_on_activity_id=based_on_activity_id,
                parent_task_context=parent_task_context,
                agent_session_id=agent_session_id,
            )
            if not preparation.accepted or preparation.resources is None:
                return False, [self._resource_rejection_event(preparation)]
            resources = preparation.resources
            task = resources.task
            self._prepared_tasks[task.activity_id] = resources
            self._known_tasks[task.activity_id] = task
        return True, [
            self._event(
                "background_agent.prepared",
                {
                    "activity_id": task.activity_id,
                    "interaction_session_id": task.interaction_session_id,
                    "goal": task.goal,
                    "deliverable": task.deliverable,
                    "permission_mode": task.permission_mode,
                    "agent_session_id": task.agent_session_id,
                    "based_on_activity_id": task.based_on_activity_id,
                    "created_at": _now_ms(),
                },
            )
        ]

    def background_available(self) -> bool:
        """判断执行器此刻能否接受一项新的后台工作。

        只读快照检查 shutdown、普通准备、纠正准备、运行、收尾、Runner 和当前
        配置。它不创建工作区，也不占用 Activity；真正派单仍由 ``prepare`` 在
        同一锁下做最终裁决。

        Returns:
            当前确实可以接收下一项后台工作时返回真。
        """

        return self._execution_slot.availability_snapshot().can_accept_submission

    @property
    def lane_kind(self) -> BackgroundExecutionLane:
        """返回本实例唯一负责的执行通道。

        Returns:
            ordinary 或 recovery。
        """

        return self._lane_kind

    def background_availability_snapshot(self) -> BackgroundLaneAvailability:
        """读取当前执行槽的无内容 canonical 可用性快照。

        Returns:
            包含容量、忙碌、Runner、配置和 shutdown 的同锁快照。
        """

        return self._execution_slot.availability_snapshot()

    def _prepare_failed_event(self) -> dict[str, object]:
        """构造不泄漏异常正文的 canonical prepare 拒绝。

        Returns:
            带稳定原因和安全说明的 rejected 事件。
        """

        return self._event(
            "background_agent.rejected",
            {
                "reason": "background_prepare_failed",
                "safe_message": "后台任务没有成功建立。",
            },
        )

    def _resource_rejection_event(
        self,
        preparation: ActivityResourcePreparation,
    ) -> dict[str, object]:
        """把资源准备失败转换成通道统一使用的拒绝事件。

        Args:
            preparation: 资源模块返回的安全失败事实。

        Returns:
            不包含异常正文、配置内容或工作区路径的 rejected 事件。
        """

        payload: dict[str, object] = {
            "reason": preparation.reason or "background_prepare_failed"
        }
        if preparation.safe_message:
            payload["safe_message"] = preparation.safe_message
        return self._event("background_agent.rejected", payload)

    def _slot_rejection_event(self, reason: str) -> dict[str, object]:
        """把执行槽 admission 拒绝转换成 canonical 执行器事件。

        Args:
            reason: 执行槽返回的稳定拒绝原因。

        Returns:
            不包含其它任务目标或材料的 rejected 事件。
        """

        normalized_reason = reason.strip() or "background_agent_rejected"
        safe_message = {
            "background_agent_unconfigured": BACKGROUND_AGENT_UNCONFIGURED_MESSAGE,
            "background_slot_occupied": (
                BACKGROUND_AGENT_RECOVERY_RUNNING_MESSAGE
                if self._lane_kind == "recovery"
                else BACKGROUND_AGENT_ALREADY_RUNNING_MESSAGE
            ),
            "background_agent_unavailable": "后台工作当前不可用。",
            "executor_shutdown": "后台工作当前不可用。",
        }.get(normalized_reason, "后台任务没有启动。")
        return self._event(
            "background_agent.rejected",
            {
                "reason": normalized_reason,
                "safe_message": safe_message,
            },
        )

    def commit_prepared(self, activity_id: str) -> ExecutionSlotSubmissionOutcome:
        """在 Foreground 提交后让当前通道立即尝试启动。

        Args:
            activity_id: 已经完成 Foreground acceptance 的 Activity 编号。

        Returns:
            started、start_failed 或 rejected 的唯一初始事实。
        """

        return self._execution_slot.commit_submission(activity_id)

    def slot_configured(self) -> bool:
        """返回当前配置是否完整可读。

        Returns:
            配置提供器能生成可信快照时返回真。
        """

        with self._lock:
            if self._shutdown or self._shutdown_requested:
                return False
            return self._activity_resources.configured()

    def slot_runner_available(self) -> bool:
        """返回 Runner 当前是否空闲且仍能接管新任务。

        Returns:
            Executor 未关闭且 Runner 报告可用时返回真。
        """

        with self._lock:
            runner_available = (
                not self._shutdown
                and not self._shutdown_requested
                and self._runner.is_available()
            )
            if runner_available and self._finishing_task is not None:
                self._finishing_task = None
            return runner_available

    def slot_start_activity(self, activity_id: str) -> bool:
        """启动一项已提交 Activity，不参与其它通道或 handoff 排序。

        Args:
            activity_id: 执行槽已经选中的 Activity 编号。

        Returns:
            Runner 真正接管任务时返回真；配置、工作区或 Runner 失效时返回假。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            prepared = self._prepared_tasks.get(normalized_activity_id)
            if (
                self._shutdown
                or prepared is None
                or self._active_task is not None
                or self._finishing_task is not None
                or not self._runner.is_available()
            ):
                return False
            task = prepared.task
            # prepare 与 commit 之间可能发生配置热替换或工作区被外部删除。真正
            # 启动前再次验证这些前提，失败就写入真实的 before_start 终态。
            if not self._activity_resources.can_start(task):
                return False
            task, execution_config = self._resolve_cua_start_state(prepared)
            self._prepared_tasks[normalized_activity_id] = PreparedActivityResources(
                task=task,
                execution_config=execution_config,
            )
            try:
                if task.task_mode == "activity_correction":
                    self._activity_resources.can_start(task)
                else:
                    self._activity_resources.materialize_task_config(
                        task,
                        skill_snapshot=task.skill_snapshot,
                    )
            except Exception:
                if task.cua_capability_granted and self._cua_capability is not None:
                    self._cua_capability.release(task.activity_id)
                logger.exception(
                    "后台 Activity 私有配置物化失败 activity_id=%s",
                    task.activity_id,
                )
                return False
            try:
                runner_started = self._runner.start(task, execution_config)
            except Exception:
                logger.exception(
                    "后台智能体 Runner 启动异常 activity_id=%s",
                    task.activity_id,
                )
                runner_started = False
            if runner_started:
                self._prepared_tasks.pop(normalized_activity_id, None)
                self._active_task = task
                self._activity_state = "running"
                self._activity_stage = "preparing"
                self._reset_progress_locked()
                self._schedule_next_progress_check_locked()
                return True
            if task.cua_capability_granted and self._cua_capability is not None:
                self._cua_capability.release(task.activity_id)
        return False

    def _resolve_cua_start_state(
        self,
        prepared: PreparedActivityResources,
    ) -> tuple[AgentTask, Any]:
        """在 Runner 接管前一次性裁决 Grant、Skill 快照和 Runtime 启动。

        Args:
            prepared: prepare 阶段冻结的候选任务资源。

        Returns:
            已冻结 Grant 与有效 Skill 的任务及对应配置。
        """

        task = prepared.task
        execution_config = prepared.execution_config
        candidate_enabled = CUA_SKILL_ID in task.skill_snapshot.enabled_skill_ids
        granted = False
        if candidate_enabled and self._cua_capability is not None:
            if task.task_mode == "activity_correction":
                if task.cua_capability_granted:
                    try:
                        self._cua_capability.acquire(task.activity_id)
                        granted = True
                    except Exception:
                        granted = False
            else:
                try:
                    self._cua_capability.acquire(task.activity_id)
                    granted = True
                except Exception:
                    granted = False
        if granted and self._cua_integration is not None:
            try:
                self._cua_integration.start_blocking()
            except Exception:
                # 官方 Host 启动失败只让本项 Activity 降级为普通任务，不能阻塞文件和
                # Bash 工作；已拿到的 Grant 必须在同一启动边界精确释放。
                logger.warning(
                    "官方 CUA Embedded Host 启动失败，Activity 降级为无 CUA 模式 activity_id=%s",
                    task.activity_id,
                    exc_info=True,
                )
                self._cua_capability.release(task.activity_id)
                granted = False
        if not granted:
            effective_snapshot = type(task.skill_snapshot)(
                enabled_skill_ids=tuple(
                    skill_id
                    for skill_id in task.skill_snapshot.enabled_skill_ids
                    if skill_id != CUA_SKILL_ID
                ),
                revision=task.skill_snapshot.revision,
            )
        else:
            effective_snapshot = task.skill_snapshot
        resolved_task = replace(
            task,
            cua_capability_granted=granted,
            skill_snapshot=effective_snapshot,
        )
        resolved_config = replace(
            execution_config,
            skill_snapshot=effective_snapshot,
        )
        self._known_tasks[task.activity_id] = resolved_task
        return resolved_task, resolved_config

    def _finalize_cua_grant(self, activity_id: str) -> None:
        """在父 Runner 清理末尾释放 Grant 或原子转给纠正 replacement。

        Args:
            activity_id: 已经退休 Turn 并断开 transport 的 Activity ID。

        Returns:
            无返回值；身份不匹配时保持槽位原状。
        """

        if self._cua_capability is None:
            return
        replacement_id = self._execution_slot.committed_replacement_for_parent(
            activity_id
        )
        if replacement_id and self._cua_capability.transfer(activity_id, replacement_id):
            return
        self._cua_capability.release(activity_id)

    def slot_discard_prepared(self, activity_id: str) -> bool:
        """释放一项未启动 Activity 的任务、材料索引和工作区。

        Args:
            activity_id: 需要释放的 prepared Activity 编号。

        Returns:
            本次确实找到并释放资源时返回真。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            prepared = self._prepared_tasks.pop(normalized_activity_id, None)
            if prepared is None:
                return False
            self._known_tasks.pop(normalized_activity_id, None)
        self._activity_resources.discard_unstarted(prepared.task)
        return True

    def slot_terminalize_before_start(
        self,
        activity_id: str,
        *,
        terminal_type: str,
        reason: str,
    ) -> bool:
        """关闭一项已提交但未被 Runner 接管的 Activity。

        Args:
            activity_id: 普通 committed 或 committed handoff Activity 编号。
            terminal_type: failed 或 cancelled。
            reason: 稳定失败或取消原因。

        Returns:
            本次确实找到资源并写入唯一终态时返回真。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            prepared = self._prepared_tasks.pop(normalized_activity_id, None)
            task = (
                prepared.task
                if prepared is not None
                else self._known_tasks.get(normalized_activity_id)
            )
            if task is None:
                return False
        try:
            self._emit_unstarted_terminal(
                task,
                terminal_type=terminal_type,
                reason=reason,
                suppress_notice=True,
            )
        finally:
            # 事件账本异常不能让已移出执行槽的 committed 资源继续出现在
            # 纠正索引里；完整终态写入失败由运行时中断事实兜底。
            with self._lock:
                self._known_tasks.pop(normalized_activity_id, None)
            # 这条路径只处理从未交给 Runner 的任务。目录里可能仍有现场截图和
            # 材料副本，必须和内存索引在同一终态边界清掉，不能只删除字典条目。
            try:
                self._activity_resources.discard_unstarted(task)
            except Exception:
                # 终态事实已经写入；清理异常保留诊断，避免把执行槽的锁和事件
                # 消费循环再次拖入异常。后续进程关闭仍会尝试清理孤儿目录。
                logger.exception(
                    "后台启动前终态工作区清理失败 activity_id=%s",
                    normalized_activity_id,
                )
            if task.cua_capability_granted and self._cua_capability is not None:
                # committed correction 在父 Runner 清理后可能已经把 Grant 转给 B；
                # B 若在启动前终态收口，必须用自己的身份精确释放这份所有权。
                self._cua_capability.release(normalized_activity_id)
        return True

    def cancel_before_start(self, activity_id: str) -> bool:
        """释放尚未越过 Foreground 提交边界的任务占位。

        Args:
            activity_id: 需要回滚的准备任务编号。

        Returns:
            确实释放了对应占位时返回真。
        """

        return self._execution_slot.rollback_submission_by_activity(activity_id)

    def cancel_running(self, activity_id: str, reason: str) -> bool:
        """请求 Runner 中断当前后台任务。

        Args:
            activity_id: 需要中断的运行中 Activity 编号。
            reason: 主进程已经裁决的稳定取消原因。

        Returns:
            Runner 接受中断请求时返回真。
        """

        return self._execution_slot.cancel_activity(activity_id, reason=reason)

    def slot_cancel_active(self, activity_id: str, *, reason: str) -> bool:
        """把 active Activity 的取消命令交给 Runner。

        Args:
            activity_id: 执行槽已确认的 active Activity 编号。
            reason: 首次冻结的稳定取消原因。

        Returns:
            Runner 接纳或已处于同一取消流程时返回真。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            active_task = self._active_task
            if active_task is None or active_task.activity_id != normalized_activity_id:
                return False
            # 第一条取消命令拥有取消原因。进入 cancelling 后，迟到命令只按幂等
            # 成功处理，不能再次下发并覆盖已经冻结的稳定原因。
            if self._activity_state == "cancelling":
                return True
            previous_state = self._activity_state
            self._activity_state = "cancelling"

        cancelled = False
        try:
            cancelled = self._runner.cancel(
                activity_id.strip(),
                reason=reason,
            )
        finally:
            if not cancelled:
                with self._lock:
                    if (
                        self._active_task is active_task
                        and self._activity_state == "cancelling"
                    ):
                        self._activity_state = previous_state
        return cancelled

    def has_activity(self, activity_id: str) -> bool:
        """判断执行器是否仍保留指定后台 Activity 的运行期事实。

        Args:
            activity_id: 需要核对的后台 Activity 编号。

        Returns:
            任务仍在准备、运行或等待纠正提交时返回真；终态释放后返回假。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            return False
        with self._lock:
            if normalized_activity_id in self._known_tasks:
                return True
            return (
                self._finishing_task is not None
                and self._finishing_task.activity_id == normalized_activity_id
            )

    def has_active_task(self) -> bool:
        """判断当前通道是否仍有运行或 finalizing 的任务。

        Returns:
            active 或 finishing 身份仍存在时返回真；prepared 不计入活动数量。
        """

        with self._lock:
            return self._active_task is not None or self._finishing_task is not None

    def is_activity_correctable(self, activity_id: str) -> bool:
        """判断指定后台 Activity 此刻是否仍可被目标纠正替换。

        Args:
            activity_id: 需要实时核对的旧 Activity 编号。

        Returns:
            任务仍活跃、未取消且尚未进入结果生成阶段时返回真。
        """

        return self._execution_slot.is_activity_correctable(activity_id)

    def slot_is_activity_correctable(self, activity_id: str) -> bool:
        """只复核 Executor 持有的 active Activity 执行阶段。

        Args:
            activity_id: 执行槽已确认的 active Activity 编号。

        Returns:
            Runner 仍可中断且尚未进入结果生成阶段时返回真。
        """

        with self._lock:
            task = self._active_task
            return bool(
                task is not None
                and task.activity_id == activity_id.strip()
                and self._activity_state in {"running", "waiting_user", "attention"}
                and self._activity_stage not in {"generating_result", "delivering_result"}
            )

    def reserve_correction(
        self,
        *,
        based_on_activity_id: str,
        interaction_session_id: str,
        goal: str,
        deliverable: bool,
        requirements: str,
        materials: tuple[AgentMaterial, ...],
        current_scene_bytes: bytes | None = None,
        parent_task_context: dict[str, object] | None = None,
    ) -> CorrectionReservation:
        """取得 handoff reservation，并准备纠正后的单项 Activity 资源。

        Args:
            based_on_activity_id: 用户要纠正的旧 active Activity 编号。
            interaction_session_id: 当前 Foreground 交互会话编号。
            goal: 纠正后的完整权威目标。
            deliverable: 是否登记并展示交付结果。
            requirements: 新任务的验收要求。
            materials: 纠正轮新增的正式材料。
            current_scene_bytes: 当前轮冻结截图。
            parent_task_context: 当前交互会话和来源事实。

        Returns:
            成功时返回新 Activity 和 prepared 事件；否则返回稳定拒绝事实。
        """

        admission = self._execution_slot.reserve_handoff(based_on_activity_id)
        if not admission.accepted:
            return CorrectionReservation(
                accepted=False,
                events=(
                    self._event(
                        "background_agent.rejected",
                        {
                            "activity_id": based_on_activity_id.strip(),
                            "reason": admission.reason,
                        },
                    ),
                ),
            )
        reservation = self._prepare_correction_resource(
            based_on_activity_id=based_on_activity_id,
            interaction_session_id=interaction_session_id,
            goal=goal,
            deliverable=deliverable,
            requirements=requirements,
            materials=materials,
            current_scene_bytes=current_scene_bytes,
            parent_task_context=parent_task_context,
        )
        if not reservation.accepted or not reservation.activity_id:
            self._execution_slot.release_handoff_reservation(admission.reservation_id)
            return reservation
        if self._execution_slot.bind_handoff(
            admission.reservation_id,
            reservation.activity_id,
        ):
            return reservation

        self.slot_discard_prepared(reservation.activity_id)
        self._execution_slot.release_handoff_reservation(admission.reservation_id)
        return CorrectionReservation(
            accepted=False,
            events=(self._prepare_failed_event(),),
        )

    def _prepare_correction_resource(
        self,
        *,
        based_on_activity_id: str,
        interaction_session_id: str,
        goal: str,
        deliverable: bool,
        requirements: str,
        materials: tuple[AgentMaterial, ...],
        current_scene_bytes: bytes | None = None,
        parent_task_context: dict[str, object] | None = None,
    ) -> CorrectionReservation:
        """在不取消旧 Activity 的前提下预留纠正后的后台任务。

        Args:
            based_on_activity_id: 用户要纠正的旧 Activity 编号。
            interaction_session_id: 当前 Foreground 交互会话编号。
            goal: 纠正后的权威任务目标。
            deliverable: 是否登记并展示交付结果。
            requirements: 新任务的验收要求。
            materials: 纠正轮新增的正式材料。
            current_scene_bytes: 当前轮冻结截图。
            parent_task_context: 当前交互会话和来源事实，仅用于服务端复核。

        Returns:
            包含接受标记、新 Activity 编号和准备或拒绝事件的结构化结果。
        """

        normalized_parent_id = based_on_activity_id.strip()
        normalized_goal = goal.strip()
        if not normalized_goal:
            return CorrectionReservation(
                accepted=False,
                events=(self._event("background_agent.rejected", {"reason": "missing_goal"}),),
            )
        with self._lock:
            active_task = self._active_task
            old_task = self._known_tasks.get(normalized_parent_id)
            # Background Correction 只允许把当前后台任务 A 替换为同源任务 B。
            if (
                self._shutdown
                or self._finishing_task is not None
                or active_task is None
                or active_task.activity_id != normalized_parent_id
            ):
                return CorrectionReservation(
                    accepted=False,
                    events=(
                        self._event(
                        "background_agent.rejected",
                        {
                            "activity_id": (
                                active_task.activity_id
                                if active_task is not None
                                else normalized_parent_id
                            ),
                            "reason": "background_slot_occupied",
                            "safe_message": BACKGROUND_AGENT_ALREADY_RUNNING_MESSAGE,
                        },
                        ),
                    ),
                )
            if old_task is None or active_task is not old_task:
                return CorrectionReservation(
                    accepted=False,
                    events=(
                        self._event(
                        "background_agent.rejected",
                        {
                            "activity_id": normalized_parent_id,
                            "reason": "correction_activity_not_found",
                        },
                        ),
                    ),
                )
            # 服务层的快照校验和这里的预留不是同一把锁。A 可能在两次检查之间
            # 进入 cancelling、finalizing 或终态；这种情况下直接拒绝，避免留下
            # 一个等提交后才失败的 B 预留。
            if (
                self._activity_state
                not in {"running", "waiting_user", "attention"}
                or self._activity_stage in {"generating_result", "delivering_result"}
            ):
                return CorrectionReservation(
                    accepted=False,
                    events=(
                        self._event(
                        "background_agent.rejected",
                        {
                            "activity_id": normalized_parent_id,
                            "reason": "correction_activity_not_found",
                        },
                        ),
                    ),
                )
            resume_identity = self._runner.resume_identity_for_activity(
                normalized_parent_id
            )
            if (
                resume_identity is None
                or resume_identity.workspace_root
                != str(Path(old_task.workspace_root).resolve(strict=False))
            ):
                return CorrectionReservation(
                    accepted=False,
                    events=(
                        self._event(
                            "background_agent.rejected",
                            {
                                "activity_id": normalized_parent_id,
                                "reason": "correction_agent_session_unavailable",
                            },
                        ),
                    ),
                )
            preparation = self._activity_resources.prepare_correction_task(
                parent_task=old_task,
                interaction_session_id=interaction_session_id,
                goal=normalized_goal,
                deliverable=deliverable,
                requirements=requirements,
                materials=materials,
                current_scene_bytes=current_scene_bytes,
                agent_session_id=resume_identity.agent_session_id,
                expected_sdk_session_id=resume_identity.sdk_session_id,
            )
            if not preparation.accepted or preparation.resources is None:
                return CorrectionReservation(
                    accepted=False,
                    events=(self._resource_rejection_event(preparation),),
                )
            resources = preparation.resources
            task = resources.task
            self._prepared_tasks[task.activity_id] = resources
            self._known_tasks[task.activity_id] = task
        prepared = self._event(
            "background_agent.prepared",
            {
                "activity_id": task.activity_id,
                "interaction_session_id": task.interaction_session_id,
                "goal": task.goal,
                "deliverable": task.deliverable,
                "permission_mode": task.permission_mode,
                "agent_session_id": task.agent_session_id,
                "based_on_activity_id": task.based_on_activity_id,
                "created_at": _now_ms(),
            },
        )
        return CorrectionReservation(
            accepted=True,
            activity_id=task.activity_id,
            events=(prepared,),
        )

    def commit_reserved_correction(self, activity_id: str) -> bool:
        """把 B 标记为 Foreground 已接受、必须启动或失败收口的预留。

        Args:
            activity_id: 纠正准备阶段分配的新 Activity 编号。

        Returns:
            预留存在且已进入 committed 时返回真；关闭或编号不符时返回假。
        """

        return self._execution_slot.commit_handoff(activity_id)

    def start_reserved_correction(self, activity_id: str) -> bool:
        """在 A 清理完成后把独立预留的 B 提升为正式后台任务并启动。

        Args:
            activity_id: 纠正准备阶段分配的新 Activity 编号。

        Returns:
            Runner 真正接管 B 时返回真；本地拒绝接管时返回假。
        """

        return self._execution_slot.start_committed_handoff(activity_id)

    def _emit_unstarted_terminal(
        self,
        task: AgentTask,
        *,
        terminal_type: str,
        reason: str,
        suppress_notice: bool,
    ) -> None:
        """为已提交但未被 Runner 接管的任务写入唯一合法终态。

        Args:
            task: 已经越过 Foreground 提交边界的任务。
            terminal_type: failed 或 cancelled。
            reason: 稳定失败或取消原因。
            suppress_notice: 当前工具二轮是否已经表达同一失败。

        Returns:
            无。

        Raises:
            ValueError: terminal_type 不是允许的启动前终态。
        """

        if terminal_type not in {"failed", "cancelled"}:
            raise ValueError("启动前终态只允许 failed 或 cancelled")
        normalized_reason = reason.strip() or "activity_start_failed"
        terminal_payload: dict[str, object] = {
            "goal": task.goal,
            "deliverable": task.deliverable,
            "based_on_activity_id": task.based_on_activity_id,
            "execution_phase": "before_start",
            "suppress_notice": suppress_notice,
            "source_materials": [
                material.display_name for material in task.material_catalog
            ],
            "task_input_snapshot": {
                "goal": task.goal,
                "task_mode": task.task_mode,
                "agent_session_id": task.agent_session_id,
                "requirements": task.requirements,
                "materials": [
                    material.to_payload() for material in task.material_catalog
                ],
                "material_limitations": list(
                    material_limitations(task.material_catalog)
                ),
                "current_scene_path": task.current_scene_path,
                "based_on_activity_id": task.based_on_activity_id,
            },
        }
        if terminal_type == "failed":
            terminal_payload["error_code"] = normalized_reason
            if task.task_mode == "activity_correction":
                terminal_payload["correction_failure_reason"] = normalized_reason
        else:
            terminal_payload["reason"] = normalized_reason
            terminal_payload["cancellation_reason"] = normalized_reason
        self._append_agent_event(
            AgentEvent(
                event_type=terminal_type,  # type: ignore[arg-type]
                activity_id=task.activity_id,
                interaction_session_id=task.interaction_session_id,
                payload=terminal_payload,
            )
        )

    def rollback_reserved_correction(self, activity_id: str) -> bool:
        """释放 Foreground 尚未提交的后台纠正预留。

        Args:
            activity_id: 纠正准备阶段分配的新 Activity 编号。

        Returns:
            找到并释放对应预留时返回真。
        """

        return self._execution_slot.rollback_handoff(activity_id)

    def fail_reserved_correction(self, activity_id: str, *, reason: str) -> bool:
        """把已提交但无法启动的后台纠正任务收口为失败终态。

        Args:
            activity_id: 纠正准备阶段分配的新 Activity 编号。
            reason: 清理超时、旧任务竞态终态或启动失败的稳定原因。

        Returns:
            找到并首次关闭对应预留时返回真。
        """

        return self._execution_slot.fail_handoff(activity_id, reason=reason)

    def cancel_for_correction(
        self,
        activity_id: str,
        *,
        timeout: float = 5.0,
    ) -> CorrectionCancellationOutcome:
        """让旧后台 Activity 真实退出，同时保留可继续使用的 AgentSession。

        Args:
            activity_id: 用户明确要纠正的旧 Activity 编号。
            timeout: 等待 Runner 释放旧 Turn 的最长秒数。

        Returns:
            区分自然终态、取消未接纳、清理超时和清理成功的稳定结果。
        """

        cancellation_outcome = self._execution_slot.cancel_handoff_parent(activity_id)
        if cancellation_outcome != "accepted":
            return cancellation_outcome
        return (
            "cleaned"
            if self._runner.wait_until_available(timeout)
            else "cleanup_timeout"
        )

    def respond_to_work_interaction(
        self,
        *,
        answer: AgentInteractionAnswer,
        materials: tuple[AgentMaterial, ...] = (),
    ) -> bool:
        """把主进程已经 claim 的权限或资料回答送回同一后台会话。

        Args:
            answer: 已通过主进程 Registry 校验的用户决定和逐问题答案。
            materials: 本轮新增、需要扩展到当前任务访问范围的正式材料。

        Returns:
            对应 SDK 等待请求仍有效并成功安排恢复时返回真。
        """

        normalized_request_id = answer.request_id.strip()
        with self._lock:
            task = self._active_task
            interaction = self._pending_interactions.get(normalized_request_id)
            if task is None or interaction is None:
                return False

            # 权限请求不接收资料答案；资料问题也不能被 allow/deny 冒充处理。
            if interaction.kind == "permission":
                if answer.outcome not in {"allow", "deny", "cancel"}:
                    return False
                return self._runner.answer_interaction(answer)
            if answer.outcome == "cancel":
                return self._runner.answer_interaction(answer)
            if answer.outcome != "answer":
                return False

            normalized_answers = normalize_agent_question_answers(
                interaction.questions,
                answer.answers,
            )
            if normalized_answers is None:
                return False
            updated_catalog = merge_agent_materials(
                task.material_catalog,
                materials,
            )
            updated_task = replace(
                task,
                material_catalog=updated_catalog,
            )
            self._active_task = updated_task
            self._known_tasks[updated_task.activity_id] = updated_task
            normalized_answer = replace(
                answer,
                answers=normalized_answers,
                materials=tuple(materials),
            )

        accepted = self._runner.answer_question(normalized_answer)
        if accepted:
            return True

        # SDK Future 若已经失效，撤回本次材料扩展，等待权威 resolved 或任务终态收口。
        with self._lock:
            if self._active_task is updated_task:
                self._active_task = task
                self._known_tasks[task.activity_id] = task
        return False

    def drain_events(self, limit: int = 100) -> list[dict[str, object]]:
        """按预算排出后台 Agent 事件。

        Args:
            limit: 本次最多排出的事件数量。

        Returns:
            按入账顺序排列的来源事件列表。
        """

        return self._events.drain_events(limit)

    def activity_snapshot(
        self,
        interaction_session_id: str,
    ) -> ForegroundActivitySnapshot | None:
        """返回当前单任务槽位的后台工作事实。

        Args:
            interaction_session_id: 当前 Foreground 轮次的产品交互会话编号。

        Returns:
            当前槽位被占用时返回 Activity 快照；没有占用时返回空。
        """

        with self._lock:
            task = self._active_task or self._finishing_task
            if task is None:
                return None
            state = self._activity_state
            stage = self._activity_stage
            progress_reasoning_text = self._progress_buffer.frozen_text()
            progress_sequence = self._progress_buffer.latest_sequence
            work_plan_summary = (
                self._work_plan.summary() if self._work_plan is not None else None
            )
            is_finishing = self._finishing_task is task
            if is_finishing:
                state = "running"
        return ForegroundActivitySnapshot(
            # 跨会话脱敏由 ForegroundTurnProcessor 在统一边界完成；执行通道只返回
            # 权威原始身份，避免两条通道各自实现一套不同的可见性规则。
            activity_id=task.activity_id,
            interaction_session_id=task.interaction_session_id,
            source="background_agent",
            category="后台工作",
            goal=task.goal,
            goal_summary=summarize_activity_goal(task.goal),
            activity_kind="background_work",
            state=state,  # type: ignore[arg-type]
            phase=(
                "finalizing"
                if is_finishing or stage in {"generating_result", "delivering_result"}
                else "reasoning"
            ),
            stage_code=stage,
            progress_reasoning_text=progress_reasoning_text,
            progress_sequence=progress_sequence,
            work_plan_summary=work_plan_summary,
        )

    def acknowledge_progress_notice(self, activity_id: str, sequence: int) -> bool:
        """确认 Foreground 已经实际表达一条冻结的后台任务进度。

        Args:
            activity_id: 进度所属 Activity 编号。
            sequence: Notice 携带的冻结进度序号。

        Returns:
            当前活动和未确认序号仍匹配时返回真。
        """

        with self._lock:
            if self._active_task is None or self._active_task.activity_id != activity_id.strip():
                return False
            return self._progress_buffer.acknowledge_through(sequence)

    def shutdown(self, timeout: float = 1.0) -> None:
        """请求关闭 Runner，并把已进入提交的资源交给排水屏障保护。

        Args:
            timeout: 等待线程和调度器退出的最长秒数。

        Returns:
            无。
        """

        normalized_timeout = max(0.0, timeout)
        shutdown_deadline = self._monotonic_now() + normalized_timeout
        if not self.request_shutdown(shutdown_deadline):
            return

        # 独立使用通道时由通道自己等待提交屏障；双通道协调器不会把屏障下沉，
        # 它会先同时关闭两条准入，再统一排水并调用 finalize_shutdown。
        if self._commit_barrier is not None:
            drain_status = self._commit_barrier.begin_shutdown(
                max(0.0, shutdown_deadline - self._monotonic_now())
            )
            if not drain_status.drained:
                logger.warning(
                    "Background Executor 关闭等待提交排水超时 count=%s",
                    len(drain_status.pending_owner_ids),
                )
                self._commit_barrier.add_drain_callback(self.finalize_shutdown)
                return
        self.finalize_shutdown()

    def request_shutdown(self, shutdown_deadline: float) -> bool:
        """按协调器给出的绝对截止时间停止本通道准入。

        Args:
            shutdown_deadline: 全部关闭阶段共同遵守的单调时钟绝对截止时间。

        Returns:
            本次首次进入关闭请求态时返回真；重复请求返回假。
        """

        with self._lock:
            if self._shutdown or self._shutdown_requested:
                return False
            self._shutdown_requested = True
            self._shutdown_deadline = max(0.0, shutdown_deadline)

        # 先拒绝新的 prepare，但保留已有 prepared；已经进入提交屏障的 action
        # 仍可调用 commit_prepared，把主进程 COMMITTED 事实落到旧 Runtime。
        self._execution_slot.request_shutdown()
        return True

    def finalize_shutdown(self) -> None:
        """在共享提交屏障排空后完成本通道的破坏性清理。

        Returns:
            无；重复调用幂等返回。
        """

        self._finalize_shutdown()

    def wait_until_shutdown(self, timeout: float | None = None) -> bool:
        """等待本通道 Runner 的线程和清理步骤真实结束。

        Args:
            timeout: 最长等待秒数；为空时一直等待。

        Returns:
            真实 Runner 已完成关闭时返回真；测试替身没有等待端口时视为同步完成。
        """

        wait_for_runner = getattr(self._runner, "wait_until_shutdown", None)
        if wait_for_runner is None:
            return True
        return bool(wait_for_runner(timeout))

    def _finalize_shutdown(self) -> None:
        """在所有 Background 提交排空后执行唯一一次破坏性关闭。

        Returns:
            无。
        """

        with self._lock:
            if self._shutdown or self._shutdown_finalizing:
                return
            self._shutdown_finalizing = True
            deadline = self._shutdown_deadline
        try:
            # 单槽边界先关闭 prepared 和 handoff，保证 Runner 释放 active 时不会在
            # shutdown 窗口重新接单；此时屏障已经确认没有提交持有者。
            try:
                self._execution_slot.shutdown()
            except Exception:
                logger.exception(
                    "Background 单槽关闭失败 lane_kind=%s",
                    self._lane_kind,
                )
            with self._lock:
                orphaned_prepared_tasks = tuple(
                    prepared.task for prepared in self._prepared_tasks.values()
                )
                self._prepared_tasks.clear()
                for task in orphaned_prepared_tasks:
                    self._known_tasks.pop(task.activity_id, None)
            for task in orphaned_prepared_tasks:
                try:
                    self._activity_resources.discard_unstarted(task)
                except Exception:
                    logger.exception(
                        "后台关闭时未启动工作区清理失败 activity_id=%s",
                        task.activity_id,
                    )
            try:
                self._runner.shutdown(
                    max(0.0, deadline - self._monotonic_now())
                    if deadline is not None
                    else 0.0
                )
            except Exception:
                logger.exception(
                    "Background Runner 关闭失败 lane_kind=%s",
                    self._lane_kind,
                )
            if self._owns_progress_scheduler and isinstance(
                self._progress_scheduler,
                ActivityProgressScheduler,
            ):
                try:
                    self._progress_scheduler.shutdown(
                        max(0.0, deadline - self._monotonic_now())
                        if deadline is not None
                        else 0.0
                    )
                except Exception:
                    logger.exception(
                        "Background 进度调度器关闭失败 lane_kind=%s",
                        self._lane_kind,
                    )
            if self._owns_event_runtime:
                try:
                    self._events.shutdown(
                        (
                            max(0.0, deadline - self._monotonic_now())
                            if deadline is not None
                            else 0.0
                        ),
                        preserve_pending_events=True,
                    )
                except Exception:
                    logger.exception(
                        "Background 事件账本关闭失败 lane_kind=%s",
                        self._lane_kind,
                    )
        finally:
            # 完成标记必须晚于所有关闭尝试，否则异常后的第二次调用会永久跳过资源。
            with self._lock:
                self._shutdown = True
                self._shutdown_finalizing = False

    def _append_agent_event(self, event: Any) -> None:
        """把 Runner 领域事件转换成通用事件账本记录。

        Args:
            event: Runner 发出的后台智能体来源事件。

        Returns:
            无。
        """

        payload = dict(event.payload)
        payload.setdefault("interaction_session_id", event.interaction_session_id)
        payload.setdefault("activity_id", event.activity_id)
        with self._lock:
            active_task = self._active_task
            if event.event_type == "turn_output":
                self._record_turn_output_locked(event.activity_id, payload)
            elif event.event_type == "work_plan_changed":
                self._record_work_plan_locked(event.activity_id, payload)
            if event.event_type == "interaction_requested":
                request_id = payload.get("request_id")
                if isinstance(request_id, str) and request_id.strip():
                    kind = payload.get("kind")
                    if kind in {"permission", "question"}:
                        self._pending_interactions[request_id.strip()] = (
                            _PendingAgentInteraction(
                                kind=kind,  # type: ignore[arg-type]
                                questions=(
                                    _question_texts(payload.get("questions"))
                                    if kind == "question"
                                    else ()
                                ),
                            )
                        )
                self._activity_state = "waiting_user"
                self._cancel_progress_schedule_locked()
            elif event.event_type == "interaction_resolved":
                request_id = payload.get("request_id")
                if isinstance(request_id, str):
                    self._pending_interactions.pop(request_id.strip(), None)
                if not self._pending_interactions:
                    self._activity_state = "running"
                    self._schedule_next_progress_check_locked()
            elif event.event_type == "stage_changed":
                stage = payload.get("stage")
                if not isinstance(stage, str) or stage not in ACTIVITY_STAGE_CODES:
                    # Runner 的阶段会进入 Activity 公共契约。非法值必须在来源边界丢弃，
                    # 不能先污染 Foreground 快照，再等主进程解析时才报错。
                    logger.warning(
                        "丢弃非法后台工作阶段 activity_id=%s stage=%r",
                        event.activity_id,
                        stage,
                    )
                    return
                self._activity_stage = stage
                if stage == "generating_result":
                    self._invalidate_progress_locked(event)
            failure_stage = self._activity_stage
        if active_task is not None and active_task.activity_id == event.activity_id:
            payload.setdefault("goal", active_task.goal)
            payload.setdefault("deliverable", active_task.deliverable)
            payload.setdefault(
                "based_on_activity_id",
                active_task.based_on_activity_id,
            )
            if event.event_type in {"completed", "failed", "cancelled"}:
                payload.setdefault(
                    "source_materials",
                    [
                        material.display_name
                        for material in active_task.material_catalog
                    ],
                )
                payload.setdefault(
                    "task_input_snapshot",
                    {
                        "goal": active_task.goal,
                        "task_mode": active_task.task_mode,
                        "agent_session_id": active_task.agent_session_id,
                        "requirements": active_task.requirements,
                        "materials": [
                            material.to_payload()
                            for material in active_task.material_catalog
                        ],
                        "material_limitations": list(
                            material_limitations(active_task.material_catalog)
                        ),
                        "current_scene_path": active_task.current_scene_path,
                        "based_on_activity_id": active_task.based_on_activity_id,
                    },
                )
        payload.setdefault("created_at", _now_ms())
        if event.event_type == "failed" and "failure" not in payload:
            failure = failure_from_reason(
                str(payload.get("error_code") or "agent_execution_error"),
                stage_code=failure_stage,
            )
            payload["failure"] = failure.to_payload()
        if event.event_type in {"completed", "failed", "cancelled"}:
            # Runner 初始化失败或测试替身也必须产生一条零用量不完整记录，避免终态任务
            # 从统计任务数中消失；真实 Runner 提供的过程或终态汇总会保留原值。
            payload.setdefault(
                "internal_usage",
                AgentTaskUsageSnapshot(by_model=(), is_complete=False).to_payload(),
            )
        self._events.append_event({"type": f"background_agent.{event.event_type}", "payload": payload})
        terminalized_active = False
        if event.event_type in {"completed", "failed", "cancelled"}:
            with self._lock:
                # 终态历史由 Task Memory 和事件账本保存。执行器只保留活跃纠正所需
                # 的完整 AgentTask，终态后必须立即删除材料路径和父上下文等运行期事实。
                self._known_tasks.pop(event.activity_id, None)
                if self._active_task is not None and self._active_task.activity_id == event.activity_id:
                    self._pending_interactions.clear()
                    self._reset_progress_locked()
                    self._work_plan = None
                    self._finishing_task = self._active_task
                    self._active_task = None
                    terminalized_active = True
        if terminalized_active:
            self._execution_slot.notify_activity_terminal(event.activity_id)

    def _on_runner_slot_released(self, activity_id: str) -> None:
        """在 Runner 完整清理后释放 finalizing 屏障。

        Args:
            activity_id: Runner 刚刚释放的 Activity 编号。

        Returns:
            无。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            finishing_task = self._finishing_task
            if (
                finishing_task is None
                or finishing_task.activity_id != normalized_activity_id
            ):
                return
            self._finishing_task = None
        self._execution_slot.notify_runner_released(normalized_activity_id)

    def _record_turn_output_locked(
        self,
        activity_id: str,
        payload: dict[str, object],
    ) -> None:
        """把后台智能体本轮可见正文追加到下一次定时 Progress。

        Args:
            activity_id: Turn 输出所属 Activity 编号。
            payload: Runner 发出的 `turn_output` 载荷。

        Returns:
            无。
        """

        if self._active_task is None or self._active_task.activity_id != activity_id:
            return
        content_text = payload.get("content_text")
        if not isinstance(content_text, str) or not content_text.strip():
            return
        self._progress_buffer.append_delta(content_text.strip() + "\n")

    def _record_work_plan_locked(
        self,
        activity_id: str,
        payload: dict[str, object],
    ) -> None:
        """缓存 Runner 已确认的计划，并记录有意义的阶段切换事实。

        Args:
            activity_id: 计划所属 Activity 编号。
            payload: Runner 发出的全量 WorkPlan 载荷。

        Returns:
            无；非法、重复或其它 Activity 的计划不会改变动态环境。
        """

        if self._active_task is None or self._active_task.activity_id != activity_id:
            return
        try:
            work_plan = parse_agent_work_plan(payload.get("work_plan"))
        except ValueError:
            return
        if self._work_plan is not None and work_plan.revision <= self._work_plan.revision:
            return

        previous_summary = self._work_plan.summary() if self._work_plan is not None else None
        self._work_plan = work_plan
        summary = work_plan.summary()
        progress_text = ""
        if summary.current and (
            previous_summary is None
            or summary.current != previous_summary.current
            or summary.completed > previous_summary.completed
        ):
            progress_text = summary.current.rstrip("。") + "。\n"
            if previous_summary is not None and summary.completed > previous_summary.completed:
                progress_text = (
                    f"已完成 {summary.completed}/{summary.total} 项，{progress_text}"
                )
        elif (
            previous_summary is not None
            and summary.completed > previous_summary.completed
        ):
            progress_text = f"计划已完成 {summary.completed}/{summary.total} 项。\n"
        if not progress_text:
            return
        self._progress_buffer.append_delta(progress_text)

    def _on_progress_deadline(self) -> None:
        """在共享调度器到期时冻结新增进度，并继续下一次检查。

        Returns:
            无。
        """

        with self._lock:
            self._progress_schedule = None
            if self._active_task is None or self._activity_state != "running":
                return
            self._progress_check_count += 1
            if not self._progress_buffer.has_accumulating_text():
                self._schedule_next_progress_check_locked()
                return
            self._emit_progress_locked()

    def _emit_progress_locked(self) -> None:
        """冻结新进度并向公共账本写入一条带序号的弱提醒。

        Returns:
            无。
        """

        task = self._active_task
        if task is None:
            return
        sequence = self._progress_buffer.freeze()
        if sequence is None:
            self._schedule_next_progress_check_locked()
            return
        self._events.append_event(
            {
                "type": "background_agent.progress",
                "payload": {
                    "activity_id": task.activity_id,
                    "interaction_session_id": task.interaction_session_id,
                    "goal": task.goal,
                    "notice_seq": sequence,
                    "updated_at": self._monotonic_now(),
                },
            }
        )
        self._schedule_next_progress_check_locked()

    def _schedule_next_progress_check_locked(self) -> None:
        """按首次 30 秒、之后 60 秒的策略安排下一次进度检查。

        Returns:
            无。
        """

        if self._active_task is None or self._activity_state != "running":
            return
        try:
            self._cancel_progress_schedule_locked()
            interval = self._progress_policy.interval_for(self._progress_check_count)
            self._progress_schedule = self._progress_scheduler.schedule(
                interval,
                self._on_progress_deadline,
            )
        except Exception:
            # 进度提醒只是运行中任务的附加表达能力。Runner 已经接单后，即使调度器
            # 暂时不可用，也不能把真实运行中的任务误判为启动失败或破坏事件回调。
            self._progress_schedule = None
            logger.exception(
                "后台工作进度检查调度失败 activity_id=%s",
                self._active_task.activity_id,
            )

    def _invalidate_progress_locked(self, event: Any) -> None:
        """进入结果生成后撤销尚未交付的弱进度。

        Args:
            event: 触发结果生成阶段的 Runner 事件。

        Returns:
            无。
        """

        had_pending_progress = (
            self._progress_buffer.latest_sequence > 0
            or self._progress_buffer.has_accumulating_text()
        )
        self._cancel_progress_schedule_locked()
        self._progress_buffer.clear_pending()
        if not had_pending_progress:
            return
        self._events.append_event(
            {
                "type": "background_agent.progress_invalidated",
                "payload": {
                    "activity_id": event.activity_id,
                    "interaction_session_id": event.interaction_session_id,
                    "updated_at": self._monotonic_now(),
                },
            }
        )

    def _reset_progress_locked(self) -> None:
        """清空单任务进度状态并取消旧 deadline。

        Returns:
            无。
        """

        self._cancel_progress_schedule_locked()
        self._progress_buffer = ProgressReasoningBuffer()
        self._progress_check_count = 0

    def _cancel_progress_schedule_locked(self) -> None:
        """取消当前任务唯一有效的进度 deadline。

        Returns:
            无。
        """

        handle = self._progress_schedule
        self._progress_schedule = None
        if handle is not None:
            handle.cancel()

    @staticmethod
    def _event(event_type: str, payload: dict[str, object]) -> dict[str, object]:
        """构造内部事件字典。

        Args:
            event_type: 完整来源事件名称。
            payload: 事件业务载荷。

        Returns:
            可直接写入后台事件账本的字典。
        """

        return {"type": event_type, "payload": payload}


def _now_ms() -> float:
    """返回当前 Unix 毫秒时间。

    Returns:
        当前 Unix 毫秒时间。
    """

    return time.time() * 1000.0


def _question_texts(value: object) -> tuple[str, ...]:
    """从 AskUserQuestion 载荷提取需要精确匹配的非空问题原文。

    Args:
        value: Runner 传入的 questions 原始列表。

    Returns:
        按 Agent 提问顺序排列的问题原文。
    """

    normalized_questions = normalize_agent_questions(value)
    if normalized_questions is None:
        return ()
    return tuple(str(question["question"]) for question in normalized_questions)
