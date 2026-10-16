"""裁决单条 Background 执行通道的占用、纠正交接和关闭边界。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

from companion.background_agent.contracts import BackgroundLaneAvailability
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)

ExecutionSlotSubmissionKind = Literal["started", "start_failed", "rejected"]
HandoffCancellationOutcome = Literal[
    "accepted",
    "activity_no_longer_correctable",
    "cancel_not_accepted",
]


class BackgroundActivitySlotPort(Protocol):
    """声明单槽状态机对 Activity 生命周期执行端口的依赖。"""

    def slot_configured(self) -> bool:
        """返回当前配置是否足以准备和启动任务。

        Returns:
            后台配置能够生成可信执行快照时返回真。
        """

    def slot_runner_available(self) -> bool:
        """返回 Runner 是否仍是有效执行资源。

        Returns:
            Runner 没有关闭且能够接管新任务时返回真。
        """

    def slot_start_activity(self, activity_id: str) -> bool:
        """让 Runner 接管一项已经提交的 Activity。

        Args:
            activity_id: 需要开始执行的 Activity 编号。

        Returns:
            Runner 已真实接管任务时返回真。
        """

    def slot_discard_prepared(self, activity_id: str) -> bool:
        """释放一项尚未提交的 Activity 资源。

        Args:
            activity_id: 需要放弃的 prepared Activity 编号。

        Returns:
            本次确实释放 prepared 资源时返回真。
        """

    def slot_terminalize_before_start(
        self,
        activity_id: str,
        *,
        terminal_type: str,
        reason: str,
    ) -> bool:
        """关闭一项已提交但从未启动的 Activity。

        Args:
            activity_id: 需要写入启动前终态的 Activity 编号。
            terminal_type: failed 或 cancelled 等公开终态类型。
            reason: 解释终态来源的稳定原因码。

        Returns:
            终态事实成功写入时返回真。
        """

    def slot_cancel_active(self, activity_id: str, *, reason: str) -> bool:
        """请求当前 active Activity 进入取消流程。

        Args:
            activity_id: 需要取消的 active Activity 编号。
            reason: 传给执行器的稳定取消原因。

        Returns:
            执行器接受取消请求时返回真。
        """

    def slot_is_activity_correctable(self, activity_id: str) -> bool:
        """复核 active Activity 是否仍可被用户纠正。

        Args:
            activity_id: 用户想要纠正的 active Activity 编号。

        Returns:
            任务仍在可纠正执行阶段时返回真。
        """


@dataclass(frozen=True, slots=True)
class ExecutionSlotAdmission:
    """保存一次 prepared capacity 或 handoff 准入结果。

    Args:
        accepted: 是否已经取得 reservation。
        reservation_id: 成功时由单槽状态机分配的一次性编号。
        reason: 拒绝时可供上层归一化的稳定原因。
    """

    accepted: bool
    reservation_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionSlotSubmissionOutcome:
    """保存普通 Background Work 在 commit 后的唯一初始事实。

    Args:
        kind: started、start_failed 或 rejected。
        activity_id: 本次提交对应的 Activity 编号。
        reason: 失败或拒绝时的稳定原因。
    """

    kind: ExecutionSlotSubmissionKind
    activity_id: str
    reason: str = ""


@dataclass(slots=True)
class _HandoffReservation:
    """保存当前通道唯一的活跃纠正交接。"""

    reservation_id: str
    parent_activity_id: str
    replacement_activity_id: str = ""
    committed: bool = False
    parent_slot_released: bool = False


class BackgroundExecutionSlot:
    """管理一条通道的 active 槽位和一次纠正交接。

    Executor 只保存任务、工作区、材料、进度和 Runner 资源；prepared、active、
    finalizing 与 handoff 都在本类的同一把锁中裁决。同一通道没有等待队列。

    Args:
        activity_executor: 负责单项 Activity 资源的执行端口。
    """

    def __init__(
        self,
        activity_executor: BackgroundActivitySlotPort,
    ) -> None:
        """初始化进程内单槽执行边界。

        Args:
            activity_executor: 单项 Activity 资源执行端口。
        """

        self._activity_executor = activity_executor
        self._lock = threading.RLock()
        self._prepared_submissions: dict[str, str] = {}
        self._reservation_by_activity_id: dict[str, str] = {}
        self._active_activity_id: str | None = None
        self._active_finalizing = False
        self._handoff: _HandoffReservation | None = None
        self._shutdown_requested = False
        self._shutdown = False

    def reserve_submission(self) -> ExecutionSlotAdmission:
        """为空闲通道预留一次 prepared 资格。

        Returns:
            成功时返回一次性 reservation；否则返回稳定拒绝原因。
        """

        with self._lock:
            rejection_reason = self._submission_rejection_reason_locked()
            if rejection_reason:
                return ExecutionSlotAdmission(False, reason=rejection_reason)
            reservation_id = f"background-prepared-{uuid4().hex}"
            self._prepared_submissions[reservation_id] = ""
            return ExecutionSlotAdmission(True, reservation_id=reservation_id)

    def bind_submission(self, reservation_id: str, activity_id: str) -> bool:
        """把 prepared capacity reservation 绑定到实际 Activity 资源。

        Args:
            reservation_id: ``reserve_submission`` 返回的一次性编号。
            activity_id: Executor 已经成功准备的 Activity 编号。

        Returns:
            两个身份均有效且首次绑定时返回真。
        """

        normalized_reservation_id = reservation_id.strip()
        normalized_activity_id = activity_id.strip()
        if not normalized_reservation_id or not normalized_activity_id:
            return False
        with self._lock:
            if (
                normalized_reservation_id not in self._prepared_submissions
                or self._prepared_submissions[normalized_reservation_id]
                or normalized_activity_id in self._reservation_by_activity_id
                or self._shutdown_requested
                or self._shutdown
            ):
                return False
            self._prepared_submissions[normalized_reservation_id] = normalized_activity_id
            self._reservation_by_activity_id[normalized_activity_id] = (
                normalized_reservation_id
            )
            return True

    def rollback_submission(self, reservation_id: str) -> bool:
        """归还尚未 commit 的普通任务容量并释放已绑定资源。

        Args:
            reservation_id: 需要回滚的 prepared reservation 编号。

        Returns:
            本次确实移除 reservation 时返回真。
        """

        normalized_reservation_id = reservation_id.strip()
        with self._lock:
            activity_id = self._prepared_submissions.pop(
                normalized_reservation_id,
                None,
            )
            if activity_id is None:
                return False
            if activity_id:
                self._reservation_by_activity_id.pop(activity_id, None)
                self._discard_prepared_locked(activity_id)
            return True

    def rollback_submission_by_activity(self, activity_id: str) -> bool:
        """按 Activity 身份回滚尚未 commit 的普通任务。

        Args:
            activity_id: Executor 已经准备的 Activity 编号。

        Returns:
            找到并释放对应 reservation 时返回真。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            reservation_id = self._reservation_by_activity_id.get(
                normalized_activity_id
            )
        if reservation_id is None:
            return False
        return self.rollback_submission(reservation_id)

    def commit_submission(self, activity_id: str) -> ExecutionSlotSubmissionOutcome:
        """在提交边界后让当前通道立即启动 Activity。

        Args:
            activity_id: Foreground 工具事件已经成功提交的 Activity 编号。

        Returns:
            唯一的 started、start_failed 或 rejected 初始事实。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            reservation_id = self._reservation_by_activity_id.pop(
                normalized_activity_id,
                None,
            )
            if reservation_id is None:
                return ExecutionSlotSubmissionOutcome(
                    kind="rejected",
                    activity_id=normalized_activity_id,
                    reason="background_submission_not_prepared",
                )
            bound_activity_id = self._prepared_submissions.pop(reservation_id, "")
            if bound_activity_id != normalized_activity_id or self._shutdown:
                self._discard_prepared_locked(normalized_activity_id)
                return ExecutionSlotSubmissionOutcome(
                    kind="rejected",
                    activity_id=normalized_activity_id,
                    reason=(
                        "executor_shutdown"
                        if self._shutdown
                        else "background_submission_identity_mismatch"
                    ),
                )

            # 单槽 reservation 已经排除了其它 prepared、active 和 handoff。若提交
            # 时这些事实发生变化，说明关闭或内部不变量已经抢先，不能悄悄等待。
            if self._active_activity_id is not None or self._handoff is not None:
                reason = "background_slot_occupied"
                self._terminalize_before_start_locked(
                    normalized_activity_id,
                    terminal_type="failed",
                    reason=reason,
                )
                return ExecutionSlotSubmissionOutcome(
                    kind="start_failed",
                    activity_id=normalized_activity_id,
                    reason=reason,
                )
            started = self._start_activity_locked(normalized_activity_id)
            return ExecutionSlotSubmissionOutcome(
                kind="started" if started else "start_failed",
                activity_id=normalized_activity_id,
                reason="" if started else "activity_start_failed",
            )

    def reserve_handoff(self, parent_activity_id: str) -> ExecutionSlotAdmission:
        """为当前 active Activity 预留唯一纠正交接资格。

        Args:
            parent_activity_id: 用户明确要纠正的 active Activity 编号。

        Returns:
            父任务仍可纠正且不存在其它 handoff 时返回 reservation。
        """

        normalized_parent_id = parent_activity_id.strip()
        with self._lock:
            if (
                self._shutdown_requested
                or self._shutdown
                or self._handoff is not None
                or self._active_activity_id != normalized_parent_id
                or self._active_finalizing
                or not self._is_activity_correctable_locked(normalized_parent_id)
            ):
                return ExecutionSlotAdmission(
                    False,
                    reason="correction_activity_not_found",
                )
            reservation_id = f"background-handoff-{uuid4().hex}"
            self._handoff = _HandoffReservation(
                reservation_id=reservation_id,
                parent_activity_id=normalized_parent_id,
            )
            return ExecutionSlotAdmission(True, reservation_id=reservation_id)

    def bind_handoff(self, reservation_id: str, replacement_activity_id: str) -> bool:
        """把 handoff reservation 绑定到新 Activity Turn。

        Args:
            reservation_id: ``reserve_handoff`` 返回的一次性编号。
            replacement_activity_id: 纠正后新建的 Activity 编号。

        Returns:
            reservation 匹配且首次绑定时返回真。
        """

        normalized_replacement_id = replacement_activity_id.strip()
        with self._lock:
            handoff = self._handoff
            if (
                handoff is None
                or handoff.reservation_id != reservation_id.strip()
                or handoff.replacement_activity_id
                or not normalized_replacement_id
                or self._shutdown_requested
                or self._shutdown
            ):
                return False
            handoff.replacement_activity_id = normalized_replacement_id
            return True

    def release_handoff_reservation(self, reservation_id: str) -> bool:
        """释放尚未绑定 Activity 资源的 handoff reservation。

        Args:
            reservation_id: ``reserve_handoff`` 返回的一次性编号。

        Returns:
            reservation 匹配、尚未绑定且未提交时返回真。
        """

        with self._lock:
            handoff = self._handoff
            if (
                handoff is None
                or handoff.reservation_id != reservation_id.strip()
                or handoff.replacement_activity_id
                or handoff.committed
            ):
                return False
            self._handoff = None
            return True

    def commit_handoff(self, replacement_activity_id: str) -> bool:
        """复核父任务并冻结它释放后的 active 位置。

        Args:
            replacement_activity_id: 已经越过 Foreground 提交边界的新 Activity。

        Returns:
            首次提交或对同一仍存在的 committed handoff 进行复核时返回真；
            其它编号、已关闭执行槽或未再具备 handoff 时返回假。
        """

        normalized_replacement_id = replacement_activity_id.strip()
        with self._lock:
            handoff = self._handoff
            if (
                self._shutdown
                or handoff is None
                or handoff.replacement_activity_id != normalized_replacement_id
            ):
                return False
            # on_accept 和 after_commit 都会复核同一个提交身份。这里返回幂等成功，
            # 但不替调用方判断父任务是否仍可纠正；后者必须在同一提交流程中单独复核。
            if handoff.committed:
                return True
            if (
                self._active_activity_id != handoff.parent_activity_id
                or self._active_finalizing
                or not self._is_activity_correctable_locked(
                    handoff.parent_activity_id
                )
            ):
                return False
            handoff.committed = True
            return True

    def cancel_handoff_parent(
        self,
        parent_activity_id: str,
    ) -> HandoffCancellationOutcome:
        """在 handoff 已冻结后请求父 Activity 以用户纠正原因取消。

        Args:
            parent_activity_id: handoff 指向的原 active Activity 编号。

        Returns:
            接纳、父任务不再可纠正或取消未接纳三态结果。
        """

        normalized_parent_id = parent_activity_id.strip()
        with self._lock:
            handoff = self._handoff
            if (
                handoff is None
                or not handoff.committed
                or handoff.parent_activity_id != normalized_parent_id
                or self._active_activity_id != normalized_parent_id
                or self._active_finalizing
            ):
                return "activity_no_longer_correctable"
            try:
                accepted = self._activity_executor.slot_cancel_active(
                    normalized_parent_id,
                    reason="user_correction",
                )
            except Exception:
                logger.exception(
                    "Background handoff 父任务取消异常 activity_id=%s",
                    normalized_parent_id,
                )
                accepted = False
            return "accepted" if accepted else "cancel_not_accepted"

    def start_committed_handoff(self, replacement_activity_id: str) -> bool:
        """在父 Runner 完整清理后让 replacement 接管原 active 位置。

        Args:
            replacement_activity_id: handoff 绑定的新 Activity 编号。

        Returns:
            Runner 真正接管 replacement 时返回真。
        """

        normalized_replacement_id = replacement_activity_id.strip()
        with self._lock:
            handoff = self._handoff
            if (
                handoff is not None
                and handoff.committed
                and not handoff.parent_slot_released
                and self._active_activity_id == handoff.parent_activity_id
                and self._active_finalizing
            ):
                try:
                    runner_released = (
                        self._activity_executor.slot_runner_available() is True
                    )
                except Exception:
                    runner_released = False
                if runner_released:
                    self._active_activity_id = None
                    self._active_finalizing = False
                    handoff.parent_slot_released = True
            if (
                self._shutdown
                or handoff is None
                or not handoff.committed
                or not handoff.parent_slot_released
                or handoff.replacement_activity_id != normalized_replacement_id
                or self._active_activity_id is not None
            ):
                return False
            started = self._start_activity_locked(normalized_replacement_id)
            self._handoff = None
            return started

    def committed_replacement_for_parent(self, parent_activity_id: str) -> str | None:
        """读取父 Runner 清理后应原子接管 Grant 的 replacement 身份。

        Args:
            parent_activity_id: 正在完成清理的父 Activity ID。

        Returns:
            已提交且身份匹配的 replacement ID；没有可转移对象时返回空。
        """

        normalized_parent_id = parent_activity_id.strip()
        with self._lock:
            handoff = self._handoff
            if (
                handoff is None
                or not handoff.committed
                or handoff.parent_activity_id != normalized_parent_id
                or not handoff.replacement_activity_id
            ):
                return None
            return handoff.replacement_activity_id

    def rollback_handoff(self, replacement_activity_id: str) -> bool:
        """释放尚未提交的 replacement handoff 和单项资源。

        Args:
            replacement_activity_id: 纠正准备阶段的新 Activity 编号。

        Returns:
            找到未提交 handoff 并释放时返回真。
        """

        normalized_replacement_id = replacement_activity_id.strip()
        with self._lock:
            handoff = self._handoff
            if (
                handoff is None
                or handoff.committed
                or handoff.replacement_activity_id != normalized_replacement_id
            ):
                return False
            self._handoff = None
            self._discard_prepared_locked(normalized_replacement_id)
            return True

    def fail_handoff(self, replacement_activity_id: str, *, reason: str) -> bool:
        """把已公开但无法接管父槽位的 replacement 关闭为启动前失败。

        Args:
            replacement_activity_id: 需要失败收口的新 Activity 编号。
            reason: 纠正竞态、取消失败或清理超时的稳定原因。

        Returns:
            找到并首次关闭 handoff 时返回真。
        """

        normalized_replacement_id = replacement_activity_id.strip()
        normalized_reason = reason.strip() or "correction_start_failed"
        with self._lock:
            handoff = self._handoff
            if (
                handoff is None
                or handoff.replacement_activity_id != normalized_replacement_id
            ):
                return False
            self._handoff = None
            self._terminalize_before_start_locked(
                normalized_replacement_id,
                terminal_type="failed",
                reason=normalized_reason,
            )
            return True

    def cancel_activity(self, activity_id: str, *, reason: str) -> bool:
        """把当前 active Activity 的取消命令交给 Executor。

        Args:
            activity_id: 用户请求取消的 Activity 编号。
            reason: 首次冻结的稳定取消原因。

        Returns:
            active Runner 接纳取消时返回真。
        """

        normalized_activity_id = activity_id.strip()
        normalized_reason = reason.strip() or "user_cancelled"
        with self._lock:
            if self._active_activity_id != normalized_activity_id:
                return False
            try:
                return self._activity_executor.slot_cancel_active(
                    normalized_activity_id,
                    reason=normalized_reason,
                )
            except Exception:
                logger.exception(
                    "Background active 取消异常 activity_id=%s",
                    normalized_activity_id,
                )
                return False

    def is_activity_correctable(self, activity_id: str) -> bool:
        """读取执行槽与生命周期端口共同认可的实时纠正资格。

        Args:
            activity_id: 用户准备纠正的 Activity 编号。

        Returns:
            只有当前 active、非 finalizing 且没有其它父任务 handoff 时返回真；
            当前 Activity 自己的 handoff 在 after_commit 复核期间仍视为可纠正。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            return bool(
                not self._shutdown_requested
                and not self._shutdown
                and (
                    self._handoff is None
                    or self._handoff.parent_activity_id == normalized_activity_id
                )
                and self._active_activity_id == normalized_activity_id
                and not self._active_finalizing
                and self._is_activity_correctable_locked(normalized_activity_id)
            )

    def notify_activity_terminal(self, activity_id: str) -> bool:
        """把 active Activity 转入 finalizing 屏障，但不释放槽位。

        Args:
            activity_id: 已经发布唯一终态的 active Activity 编号。

        Returns:
            本次首次进入 finalizing 时返回真。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            if (
                self._active_activity_id != normalized_activity_id
                or self._active_finalizing
            ):
                return False
            self._active_finalizing = True
            return True

    def notify_runner_released(self, activity_id: str) -> bool:
        """在响应流、交互和 Runner 身份完整清理后释放 active 槽位。

        Args:
            activity_id: 刚刚完成清理的 active Activity 编号。

        Returns:
            身份和 finalizing 状态匹配并首次释放时返回真。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            if (
                self._active_activity_id != normalized_activity_id
                or not self._active_finalizing
            ):
                return False
            self._active_activity_id = None
            self._active_finalizing = False
            handoff = self._handoff
            if (
                handoff is not None
                and handoff.committed
                and handoff.parent_activity_id == normalized_activity_id
            ):
                handoff.parent_slot_released = True
                return True
            return True

    def availability_snapshot(self) -> BackgroundLaneAvailability:
        """读取不包含其它任务目标或材料的 canonical 可用性。

        Returns:
            容量、忙碌、Runner、配置和 shutdown 的同锁快照。
        """

        with self._lock:
            configured = self._configured_locked()
            runner_available = self._runner_available_locked()
            can_accept = bool(
                not self._shutdown_requested
                and not self._shutdown
                and configured
                and runner_available
                and not self._prepared_submissions
                and self._active_activity_id is None
                and self._handoff is None
            )
            return BackgroundLaneAvailability(
                can_accept_submission=can_accept,
                busy=(
                    self._active_activity_id is not None
                    or self._handoff is not None
                    or bool(self._prepared_submissions)
                ),
                runner_available=runner_available,
                configured=configured,
                shutdown=self._shutdown_requested or self._shutdown,
            )

    def shutdown(self) -> None:
        """停止接单并收口 prepared 与 handoff reservation。

        Returns:
            无。
        """

        self.request_shutdown()
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            prepared_activity_ids = tuple(
                activity_id
                for activity_id in self._prepared_submissions.values()
                if activity_id
            )
            handoff = self._handoff
            self._prepared_submissions.clear()
            self._reservation_by_activity_id.clear()
            self._handoff = None

            for activity_id in prepared_activity_ids:
                self._discard_prepared_locked(activity_id)
            if handoff is not None and handoff.replacement_activity_id:
                if handoff.committed:
                    self._terminalize_before_start_locked(
                        handoff.replacement_activity_id,
                        terminal_type="failed",
                        reason="backend_shutdown",
                    )
                else:
                    self._discard_prepared_locked(
                        handoff.replacement_activity_id
                    )

    def request_shutdown(self) -> bool:
        """停止新的 reservation，但暂不清理已有 prepared 资源。

        Returns:
            本次首次进入关闭请求态时返回真；重复调用返回假。
        """

        with self._lock:
            if self._shutdown_requested or self._shutdown:
                return False
            self._shutdown_requested = True
            return True

    def _submission_rejection_reason_locked(self) -> str:
        """在已持锁状态下返回普通 prepare 的首个拒绝原因。

        Returns:
            可接纳时返回空字符串，否则返回稳定拒绝原因。
        """

        if self._shutdown_requested or self._shutdown:
            return "executor_shutdown"
        if not self._runner_available_locked():
            return "background_agent_unavailable"
        if (
            self._prepared_submissions
            or self._active_activity_id is not None
            or self._handoff is not None
        ):
            return "background_slot_occupied"
        return ""

    def _configured_locked(self) -> bool:
        """把配置读取异常归一为不可用。

        Returns:
            执行器确认当前配置可用时返回真。
        """

        try:
            return self._activity_executor.slot_configured() is True
        except Exception:
            logger.warning("Background 执行槽配置可用性读取失败", exc_info=True)
            return False

    def _runner_available_locked(self) -> bool:
        """区分 Runner 忙碌与 Runner 资源不可用。

        Returns:
            已有任务占用 Runner 或空闲 Runner 可以接单时返回真。
        """

        if self._active_activity_id is not None:
            return True
        try:
            return self._activity_executor.slot_runner_available() is True
        except Exception:
            logger.warning("Background 执行槽 Runner 可用性读取失败", exc_info=True)
            return False

    def _is_activity_correctable_locked(self, activity_id: str) -> bool:
        """把 Executor 纠正资格读取异常归一为不可纠正。

        Args:
            activity_id: 需要复核纠正资格的 Activity 编号。

        Returns:
            执行器明确确认任务仍可纠正时返回真。
        """

        try:
            return (
                self._activity_executor.slot_is_activity_correctable(activity_id)
                is True
            )
        except Exception:
            logger.warning(
                "Background 执行槽纠正资格读取失败 activity_id=%s",
                activity_id,
                exc_info=True,
            )
            return False

    def _start_activity_locked(self, activity_id: str) -> bool:
        """占住 active 身份并让 Executor 尝试真实接管。

        Args:
            activity_id: 已经提交且即将占用 active 位置的 Activity 编号。

        Returns:
            执行器真实启动任务时返回真；启动失败时返回假并写入终态。
        """

        self._active_activity_id = activity_id
        self._active_finalizing = False
        try:
            started = self._activity_executor.slot_start_activity(activity_id)
        except Exception:
            logger.exception(
                "Background 执行槽启动异常 activity_id=%s",
                activity_id,
            )
            started = False
        if started:
            return True
        self._active_activity_id = None
        self._active_finalizing = False
        self._terminalize_before_start_locked(
            activity_id,
            terminal_type="failed",
            reason="agent_cli_crash",
        )
        return False

    def _discard_prepared_locked(self, activity_id: str) -> None:
        """释放资源异常只记日志，不让 reservation 永久占住执行槽。

        Args:
            activity_id: 需要释放的 prepared Activity 编号。
        """

        try:
            self._activity_executor.slot_discard_prepared(activity_id)
        except Exception:
            logger.exception(
                "Background prepared 资源释放异常 activity_id=%s",
                activity_id,
            )

    def _terminalize_before_start_locked(
        self,
        activity_id: str,
        *,
        terminal_type: str,
        reason: str,
    ) -> None:
        """写入启动前终态异常时仍释放执行槽本地身份。

        Args:
            activity_id: 需要关闭的未启动 Activity 编号。
            terminal_type: 对外公开的终态类型。
            reason: 解释本次终态的稳定原因码。
        """

        try:
            self._activity_executor.slot_terminalize_before_start(
                activity_id,
                terminal_type=terminal_type,
                reason=reason,
            )
        except Exception:
            logger.exception(
                "Background 启动前终态写入异常 activity_id=%s reason=%s",
                activity_id,
                reason,
            )
