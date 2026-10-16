"""协调普通与 Self-Reflection 恢复两条 Background 执行通道。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from companion.activities.foreground_snapshot import ForegroundActivitySnapshot
from companion.activities.progress_scheduler import (
    ActivityProgressScheduler,
    ProgressIntervalPolicy,
    ProgressScheduler,
)
from companion.background_agent.activity_resources import (
    BACKGROUND_AGENT_UNCONFIGURED_MESSAGE,
)
from companion.background_agent.config_provider import BackgroundAgentConfigProvider
from companion.background_agent.contracts import (
    AgentMaterial,
    BackgroundExecutionLane,
    BackgroundWorkAvailability,
)
from companion.background_agent.cua_integration import CuaIntegration
from companion.background_agent.cua_capability import CuaCapabilitySlot
from companion.background_agent.execution_lane import BackgroundAgentExecutionLane
from companion.background_agent.interaction_bridge import AgentInteractionAnswer
from companion.background_agent.execution_slot import ExecutionSlotSubmissionOutcome
from companion.background_agent.workspace import WorkspaceManager
from companion.foreground_tools.activity_correction.contracts import (
    CorrectionCancellationOutcome,
    CorrectionReservation,
)
from companion.foreground_tools.background_activity_runtime import (
    BackgroundActivityRuntime,
)
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.observability.logging_config import get_backend_logger
from companion.observability.prompt_audit import PromptAuditRecorder


logger = get_backend_logger(__name__)


class BackgroundAgentActivityExecutor:
    """把两条独立单槽通道组合成唯一 Background Activity 入口。

    普通 Foreground 只能提交到 ordinary，Self-Reflection 只能提交到 recovery。
    每条通道拥有独立 Runner 和全部可变执行状态，但事件账本、进度调度器、
    工作区管理器和关闭屏障由本协调器统一管理。

    Args:
        config_provider: 每项新任务读取可信后台配置的来源。
        runtime_dir: Claude Code 私有运行目录。
        workspace_manager: 按 Activity 隔离工作目录的管理器。
        event_notifier: 共享事件成功入账后的唤醒回调。
        id_factory: 可替换的 Activity 编号生成器。
        monotonic_now: 关闭预算和进度调度使用的单调时钟。
        progress_scheduler: 可选进程级共享进度调度器。
        progress_policy: 两条通道共用的进度退避策略。
        ordinary_runner: 普通通道测试 Runner；生产环境为空时创建真实 Runner。
        recovery_runner: 恢复通道测试 Runner；生产环境为空时创建真实 Runner。
        commit_barrier: Background 提交与破坏性关闭共用的排水屏障。
        prompt_audit_recorder: 可选本地 Prompt 审查记录器，透传给两条通道的真实 Runner。
        cua_integration: 官方 Cua Driver 安装、doctor 与 stdio 配置入口。
        cua_capability: 两条 Background lane 共用的全局 CUA Capability Slot。
        terminal_diagnostics_enabled: 是否把完整 SDK 消息和 CLI stderr 打印到开发终端。
    """

    def __init__(
        self,
        *,
        config_provider: BackgroundAgentConfigProvider,
        runtime_dir: Path,
        workspace_manager: WorkspaceManager,
        event_notifier: Callable[[], None] | None = None,
        id_factory: Callable[[str], str] | None = None,
        monotonic_now: Callable[[], float] = time.monotonic,
        progress_scheduler: ProgressScheduler | None = None,
        progress_policy: ProgressIntervalPolicy | None = None,
        ordinary_runner: Any | None = None,
        recovery_runner: Any | None = None,
        commit_barrier: BackgroundCommitDrainBarrier | None = None,
        prompt_audit_recorder: PromptAuditRecorder | None = None,
        cua_integration: CuaIntegration | None = None,
        cua_capability: CuaCapabilitySlot | None = None,
        terminal_diagnostics_enabled: bool = False,
    ) -> None:
        """初始化固定双通道 Background 执行器。

        Args:
            config_provider: 每项新任务读取可信后台配置的来源。
            runtime_dir: Claude Code 私有运行目录。
            workspace_manager: 按 Activity 隔离工作目录的管理器。
            event_notifier: 共享事件成功入账后的唤醒回调。
            id_factory: 可替换的 Activity 编号生成器。
            monotonic_now: 关闭预算和进度调度使用的单调时钟。
            progress_scheduler: 可选进程级共享进度调度器。
            progress_policy: 两条通道共用的进度退避策略。
            ordinary_runner: 普通通道测试 Runner。
            recovery_runner: 恢复通道测试 Runner。
            commit_barrier: Background 提交与破坏性关闭共用的排水屏障。
            prompt_audit_recorder: 可选本地 Prompt 审查记录器，透传给两条通道。
            cua_integration: 官方 Cua Driver 安装、doctor 与 stdio 配置入口。
            cua_capability: 两条 Background lane 共用的全局 CUA Capability Slot。
            terminal_diagnostics_enabled: 是否把完整 SDK 消息和 CLI stderr 打印到开发终端。
        """

        self._monotonic_now = monotonic_now
        self._commit_barrier = commit_barrier
        self._cua_capability = cua_capability
        self._events = BackgroundActivityRuntime(
            thread_name_prefix="background-agent-activity",
            event_notifier=event_notifier,
        )
        self._owns_progress_scheduler = progress_scheduler is None
        self._progress_scheduler = progress_scheduler or ActivityProgressScheduler(
            monotonic_now=monotonic_now
        )
        self._lanes: dict[BackgroundExecutionLane, BackgroundAgentExecutionLane] = {
            "ordinary": BackgroundAgentExecutionLane(
                lane_kind="ordinary",
                config_provider=config_provider,
                runtime_dir=runtime_dir,
                workspace_manager=workspace_manager,
                id_factory=id_factory,
                monotonic_now=monotonic_now,
                progress_scheduler=self._progress_scheduler,
                progress_policy=progress_policy,
                event_runtime=self._events,
                runner=ordinary_runner,
                prompt_audit_recorder=prompt_audit_recorder,
                cua_integration=cua_integration,
                cua_capability=cua_capability,
                terminal_diagnostics_enabled=terminal_diagnostics_enabled,
            ),
            "recovery": BackgroundAgentExecutionLane(
                lane_kind="recovery",
                config_provider=config_provider,
                runtime_dir=runtime_dir,
                workspace_manager=workspace_manager,
                id_factory=id_factory,
                monotonic_now=monotonic_now,
                progress_scheduler=self._progress_scheduler,
                progress_policy=progress_policy,
                event_runtime=self._events,
                runner=recovery_runner,
                prompt_audit_recorder=prompt_audit_recorder,
                cua_integration=cua_integration,
                cua_capability=cua_capability,
                terminal_diagnostics_enabled=terminal_diagnostics_enabled,
            ),
        }
        self._shutdown_lock = threading.RLock()
        self._shutdown_requested = False
        self._shutdown_finalizing = False
        self._shutdown = False
        self._shutdown_deadline: float | None = None
        self._shutdown_callbacks: list[Callable[[], None]] = []

    def prepare(
        self,
        *,
        execution_lane: BackgroundExecutionLane,
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
        """在可信调用方指定的固定通道准备 Background Work。

        Args:
            execution_lane: ordinary 或 recovery；模型参数不能控制该字段。
            interaction_session_id: 触发任务的交互会话编号。
            goal: 后台任务的完整目标。
            deliverable: 是否登记并展示交付成果。
            requirements: 额外成果与执行要求。
            materials: 已授权的正式材料。
            current_scene_bytes: 可选冻结现场图片。
            based_on_activity_id: 可选父任务编号。
            parent_task_context: 已召回并脱敏的父任务事实。
            agent_session_id: Correction 恢复时使用的产品 Session 编号。

        Returns:
            prepare 是否成功，以及 canonical prepared 或 rejected 事件。
        """

        lane = self._lanes.get(execution_lane)
        if lane is None:
            raise ValueError("Background execution_lane 必须是 ordinary 或 recovery")
        return lane.prepare(
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

    def commit_prepared(self, activity_id: str) -> ExecutionSlotSubmissionOutcome:
        """让持有指定 prepared Activity 的通道立即启动任务。

        Args:
            activity_id: 已越过 Foreground 提交边界的 Activity 编号。

        Returns:
            started、start_failed 或 rejected 的唯一初始事实。
        """

        lane = self._lane_for_activity(activity_id)
        if lane is None:
            return ExecutionSlotSubmissionOutcome(
                kind="rejected",
                activity_id=activity_id.strip(),
                reason="background_submission_not_prepared",
            )
        return lane.commit_prepared(activity_id)

    def cancel_before_start(self, activity_id: str) -> bool:
        """回滚指定通道中尚未提交的 prepared Activity。

        Args:
            activity_id: 需要释放的 Activity 编号。

        Returns:
            找到并释放 prepared 资源时返回真。
        """

        lane = self._lane_for_activity(activity_id)
        return lane.cancel_before_start(activity_id) if lane is not None else False

    def cancel_running(self, activity_id: str, reason: str) -> bool:
        """按 Activity 身份取消对应通道的 active 任务。

        Args:
            activity_id: 需要取消的 Background Activity 编号。
            reason: 主进程裁决的稳定取消原因。

        Returns:
            对应 Runner 接纳取消时返回真。
        """

        lane = self._lane_for_activity(activity_id)
        return lane.cancel_running(activity_id, reason) if lane is not None else False

    def has_activity(self, activity_id: str) -> bool:
        """判断任一通道是否仍保留指定 Activity。

        Args:
            activity_id: 需要核对的 Background Activity 编号。

        Returns:
            任务仍在准备、运行、finalizing 或 Correction handoff 时返回真。
        """

        return self._lane_for_activity(activity_id) is not None

    def background_available(self) -> bool:
        """返回普通 Foreground 通道是否可以接单。

        Returns:
            ordinary 通道能够取得 reservation 时返回真；恢复通道占用不影响结果。
        """

        return self._lanes["ordinary"].background_available()

    def background_availability_snapshot(self) -> BackgroundWorkAvailability:
        """读取普通与恢复通道的 canonical 无内容快照。

        Returns:
            两条通道的可用性和真实 active 数量。
        """

        ordinary = self._lanes["ordinary"].background_availability_snapshot()
        recovery = self._lanes["recovery"].background_availability_snapshot()
        return BackgroundWorkAvailability(
            ordinary=ordinary,
            recovery=recovery,
            active_count=sum(
                1 for lane in self._lanes.values() if lane.has_active_task()
            ),
            cua_available=(
                self._cua_capability.snapshot().available
                if self._cua_capability is not None
                else False
            ),
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
        """在父 Activity 所属通道原位准备 Correction replacement。

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
            新 Activity reservation；父任务不存在时返回普通通道的稳定拒绝事实。
        """

        lane = self._lane_for_activity(based_on_activity_id)
        if lane is None:
            lane = self._lanes["ordinary"]
        return lane.reserve_correction(
            based_on_activity_id=based_on_activity_id,
            interaction_session_id=interaction_session_id,
            goal=goal,
            deliverable=deliverable,
            requirements=requirements,
            materials=materials,
            current_scene_bytes=current_scene_bytes,
            parent_task_context=parent_task_context,
        )

    def commit_reserved_correction(self, activity_id: str) -> bool:
        """提交 replacement，并保持它继承父任务通道。

        Args:
            activity_id: Correction prepare 创建的新 Activity 编号。

        Returns:
            所属通道接受 committed handoff 时返回真。
        """

        lane = self._lane_for_activity(activity_id)
        return (
            lane.commit_reserved_correction(activity_id)
            if lane is not None
            else False
        )

    def start_reserved_correction(self, activity_id: str) -> bool:
        """在父 Runner 清理后启动同通道 replacement。

        Args:
            activity_id: committed replacement Activity 编号。

        Returns:
            同通道 Runner 真实接管时返回真。
        """

        lane = self._lane_for_activity(activity_id)
        return lane.start_reserved_correction(activity_id) if lane is not None else False

    def rollback_reserved_correction(self, activity_id: str) -> bool:
        """回滚尚未提交的同通道 Correction replacement。

        Args:
            activity_id: replacement Activity 编号。

        Returns:
            找到并释放 reservation 时返回真。
        """

        lane = self._lane_for_activity(activity_id)
        return lane.rollback_reserved_correction(activity_id) if lane is not None else False

    def fail_reserved_correction(self, activity_id: str, *, reason: str) -> bool:
        """把无法接管父槽位的 replacement 收口为启动前失败。

        Args:
            activity_id: replacement Activity 编号。
            reason: 稳定失败原因。

        Returns:
            所属通道首次写入失败终态时返回真。
        """

        lane = self._lane_for_activity(activity_id)
        return (
            lane.fail_reserved_correction(activity_id, reason=reason)
            if lane is not None
            else False
        )

    def cancel_for_correction(
        self,
        activity_id: str,
        *,
        timeout: float = 5.0,
    ) -> CorrectionCancellationOutcome:
        """取消父任务并等待它所属通道的 Runner 完整清理。

        Args:
            activity_id: 用户明确纠正的父 Activity 编号。
            timeout: 等待父 Runner 释放的最长秒数。

        Returns:
            cleaned、自然终态、取消未接纳或清理超时。
        """

        lane = self._lane_for_activity(activity_id)
        if lane is None:
            return "activity_no_longer_correctable"
        return lane.cancel_for_correction(activity_id, timeout=timeout)

    def is_activity_correctable(self, activity_id: str) -> bool:
        """复核指定 Background Activity 是否仍可被用户纠正。

        Args:
            activity_id: 候选父 Activity 编号。

        Returns:
            所属通道仍允许 handoff 时返回真。
        """

        lane = self._lane_for_activity(activity_id)
        return lane.is_activity_correctable(activity_id) if lane is not None else False

    def respond_to_work_interaction(
        self,
        *,
        answer: AgentInteractionAnswer,
        materials: tuple[AgentMaterial, ...] = (),
    ) -> bool:
        """把资料或权限回答送回持有 request_id 的通道。

        Args:
            answer: 主进程已经 claim 的规范交互回答。
            materials: 本轮新增的正式材料。

        Returns:
            任一通道成功恢复对应 SDK Future 时返回真。
        """

        for lane_kind in ("ordinary", "recovery"):
            if self._lanes[lane_kind].respond_to_work_interaction(
                answer=answer,
                materials=materials,
            ):
                return True
        return False

    def activity_snapshots(
        self,
        interaction_session_id: str,
    ) -> tuple[ForegroundActivitySnapshot, ...]:
        """读取两条通道当前 active 或 finalizing 的 Prompt 快照。

        Args:
            interaction_session_id: 当前 Foreground 交互会话编号。

        Returns:
            固定按 ordinary、recovery 顺序排列的零至两项快照。
        """

        snapshots = (
            self._lanes[lane_kind].activity_snapshot(interaction_session_id)
            for lane_kind in ("ordinary", "recovery")
        )
        return tuple(snapshot for snapshot in snapshots if snapshot is not None)

    def acknowledge_progress_notice(self, activity_id: str, sequence: int) -> bool:
        """按 Activity 身份确认已公开的 Progress 序号。

        Args:
            activity_id: Progress 所属 Background Activity 编号。
            sequence: 已成功表达的冻结序号。

        Returns:
            所属通道接受确认时返回真。
        """

        lane = self._lane_for_activity(activity_id)
        return (
            lane.acknowledge_progress_notice(activity_id, sequence)
            if lane is not None
            else False
        )

    def drain_events(self, limit: int = 100) -> list[dict[str, object]]:
        """按两条通道共享的真实入账顺序排出事件。

        Args:
            limit: 本次最多返回的事件数。

        Returns:
            未超过预算的 Background 来源事件。
        """

        return self._events.drain_events(limit)

    def shutdown(self, timeout: float = 1.0) -> None:
        """停止两条通道准入，并在共享提交排水后统一关闭。

        Args:
            timeout: 两个 Runner 与共享资源共同使用的总关闭预算秒数。

        Returns:
            无；超时的提交会在最后持有者释放后触发终局清理。
        """

        normalized_timeout = max(0.0, timeout)
        with self._shutdown_lock:
            if self._shutdown_requested or self._shutdown:
                return
            self._shutdown_requested = True
            shutdown_deadline = self._monotonic_now() + normalized_timeout
            self._shutdown_deadline = shutdown_deadline
        for lane in self._lanes.values():
            lane.request_shutdown(shutdown_deadline)
        if self._commit_barrier is not None:
            drain_status = self._commit_barrier.begin_shutdown(
                max(0.0, shutdown_deadline - self._monotonic_now())
            )
            if not drain_status.drained:
                logger.warning(
                    "Background 双通道关闭等待提交排水超时 count=%s",
                    len(drain_status.pending_owner_ids),
                )
                self._commit_barrier.add_drain_callback(self._finalize_shutdown)
                return
        self._finalize_shutdown()

    def run_after_shutdown(self, callback: Callable[[], None]) -> None:
        """把依赖 Background 资源已清理的动作挂到关闭完成之后。

        Args:
            callback: Background 两条通道和提交屏障都清理完成后执行的动作。

        Returns:
            无返回值；若关闭已经完成则在当前线程立即执行。

        Raises:
            TypeError: callback 不是可调用对象。
        """

        if not callable(callback):
            raise TypeError("Background shutdown callback 必须可调用")
        run_now = False
        with self._shutdown_lock:
            if self._shutdown:
                run_now = True
            else:
                self._shutdown_callbacks.append(callback)
        if run_now:
            try:
                callback()
            except Exception:
                logger.exception("Background 关闭完成回调执行失败")

    def _lane_for_activity(
        self,
        activity_id: str,
    ) -> BackgroundAgentExecutionLane | None:
        """按运行期 Activity 身份找到唯一所属通道。

        Args:
            activity_id: 需要路由的 Activity 编号。

        Returns:
            唯一持有该身份的通道；不存在时返回空。

        Raises:
            RuntimeError: 两条通道错误地持有同一个 Activity 编号。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            return None
        matching_lanes = tuple(
            lane
            for lane in self._lanes.values()
            if lane.has_activity(normalized_activity_id)
        )
        if len(matching_lanes) > 1:
            raise RuntimeError("同一 Background Activity 不能属于两条执行通道")
        return matching_lanes[0] if matching_lanes else None

    def _finalize_shutdown(self) -> None:
        """在提交屏障排空后按同一截止时间清理两条通道。

        Returns:
            无；重复回调幂等返回。
        """

        with self._shutdown_lock:
            if self._shutdown or self._shutdown_finalizing:
                return
            self._shutdown_finalizing = True
            deadline = (
                self._shutdown_deadline
                if self._shutdown_deadline is not None
                else self._monotonic_now()
            )
        try:
            for lane_kind in ("ordinary", "recovery"):
                try:
                    self._lanes[lane_kind].finalize_shutdown()
                except Exception:
                    # 一条通道关闭失败不能让另一条通道继续产生副作用。
                    logger.exception(
                        "Background 通道关闭失败 lane_kind=%s",
                        lane_kind,
                    )
            if self._owns_progress_scheduler and isinstance(
                self._progress_scheduler,
                ActivityProgressScheduler,
            ):
                try:
                    self._progress_scheduler.shutdown(
                        max(0.0, deadline - self._monotonic_now())
                    )
                except Exception:
                    logger.exception("Background 共享进度调度器关闭失败")
            try:
                self._events.shutdown(
                    max(0.0, deadline - self._monotonic_now()),
                    preserve_pending_events=True,
                )
            except Exception:
                logger.exception("Background 共享事件账本关闭失败")
        finally:
            runners_cleaned = all(
                lane.wait_until_shutdown(0.0) for lane in self._lanes.values()
            )
            if runners_cleaned:
                self._publish_shutdown_complete()
            else:
                threading.Thread(
                    target=self._wait_for_runners_and_publish_shutdown,
                    name="background-agent-shutdown-monitor",
                    daemon=False,
                ).start()

    def _wait_for_runners_and_publish_shutdown(self) -> None:
        """等待两条 Runner 真正退出后再开放 Host.stop 回调门。

        Returns:
            无返回值；等待线程保持子进程存活，直到 Runner 清理完成或父进程总宽限到期。
        """

        for lane in self._lanes.values():
            lane.wait_until_shutdown(None)
        self._publish_shutdown_complete()

    def _publish_shutdown_complete(self) -> None:
        """发布 Background 资源真实清理完成并执行依赖回调。

        Returns:
            无返回值；重复到达时幂等返回。
        """

        with self._shutdown_lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._shutdown_finalizing = False
            shutdown_callbacks = tuple(self._shutdown_callbacks)
            self._shutdown_callbacks.clear()
        for callback in shutdown_callbacks:
            try:
                callback()
            except Exception:
                logger.exception("Background 关闭完成回调执行失败")
