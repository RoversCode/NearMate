"""验证 Background 单通道无队列执行槽与活跃纠正交接。"""

from __future__ import annotations

from collections import deque

from companion.background_agent.execution_slot import BackgroundExecutionSlot


class RecordingActivityExecutor:
    """记录单槽状态机向 Activity 生命周期端口发出的命令。"""

    def __init__(self) -> None:
        """初始化默认配置可用、Runner 空闲且任务可纠正的替身。"""

        self.configured = True
        self.runner_available = True
        self.correctable = True
        self.start_outcomes: deque[bool] = deque()
        self.started: list[str] = []
        self.discarded: list[str] = []
        self.before_start_terminals: list[tuple[str, str, str]] = []
        self.active_cancellations: list[tuple[str, str]] = []

    def slot_configured(self) -> bool:
        """返回当前配置是否足以准备和启动后台工作。"""

        return self.configured

    def slot_runner_available(self) -> bool:
        """返回 Runner 是否仍是可用的执行资源。"""

        return self.runner_available

    def slot_start_activity(self, activity_id: str) -> bool:
        """记录真实接管尝试并返回预设结果。

        Args:
            activity_id: 执行槽准备交给 Runner 的 Activity 编号。

        Returns:
            没有预设结果时默认接管成功。
        """

        self.started.append(activity_id)
        return self.start_outcomes.popleft() if self.start_outcomes else True

    def slot_discard_prepared(self, activity_id: str) -> bool:
        """记录尚未提交资源的回滚。

        Args:
            activity_id: 需要释放的 Activity 编号。

        Returns:
            测试替身固定返回真。
        """

        self.discarded.append(activity_id)
        return True

    def slot_terminalize_before_start(
        self,
        activity_id: str,
        *,
        terminal_type: str,
        reason: str,
    ) -> bool:
        """记录一条没有伪造 started 的启动前终态。

        Args:
            activity_id: 需要关闭的 committed Activity。
            terminal_type: failed 或 cancelled。
            reason: 稳定失败或取消原因。

        Returns:
            测试替身固定返回真。
        """

        self.before_start_terminals.append((activity_id, terminal_type, reason))
        return True

    def slot_cancel_active(self, activity_id: str, *, reason: str) -> bool:
        """记录当前 active Activity 的取消请求。

        Args:
            activity_id: 当前占用 Runner 的 Activity 编号。
            reason: 首次冻结的取消原因。

        Returns:
            测试替身固定接纳取消。
        """

        self.active_cancellations.append((activity_id, reason))
        return True

    def slot_is_activity_correctable(self, activity_id: str) -> bool:
        """返回父 Activity 当前是否仍有纠正资格。

        Args:
            activity_id: 需要复核的父 Activity 编号。

        Returns:
            当前测试设置的纠正资格。
        """

        _ = activity_id
        return self.correctable


def _prepare_and_commit(slot: BackgroundExecutionSlot, activity_id: str) -> str:
    """准备并提交一项 Background Work。

    Args:
        slot: 当前测试使用的单通道执行槽。
        activity_id: 测试 Activity 编号。

    Returns:
        commit 形成的 started 或 start_failed 事实。
    """

    admission = slot.reserve_submission()
    assert admission.accepted is True
    assert slot.bind_submission(admission.reservation_id, activity_id) is True
    return slot.commit_submission(activity_id).kind


def test_idle_slot_starts_committed_work_immediately() -> None:
    """空闲通道只能产生真实 started，不能产生等待事实。"""

    executor = RecordingActivityExecutor()
    slot = BackgroundExecutionSlot(executor)

    assert _prepare_and_commit(slot, "background-a") == "started"
    assert executor.started == ["background-a"]
    assert slot.availability_snapshot().busy is True


def test_active_slot_rejects_new_submission_without_queueing() -> None:
    """同通道 active 时新任务应在 prepare 立刻稳定拒绝。"""

    slot = BackgroundExecutionSlot(RecordingActivityExecutor())
    assert _prepare_and_commit(slot, "background-a") == "started"

    rejected = slot.reserve_submission()

    assert rejected.accepted is False
    assert rejected.reason == "background_slot_occupied"
    assert slot.availability_snapshot().can_accept_submission is False


def test_prepared_reservation_owns_the_only_slot_until_rollback() -> None:
    """尚未 commit 的资源也必须阻止同通道第二项 prepare。"""

    executor = RecordingActivityExecutor()
    slot = BackgroundExecutionSlot(executor)
    reservation = slot.reserve_submission()
    assert reservation.accepted is True
    assert slot.bind_submission(reservation.reservation_id, "background-a") is True

    assert slot.reserve_submission().reason == "background_slot_occupied"
    assert slot.rollback_submission(reservation.reservation_id) is True
    assert executor.discarded == ["background-a"]
    assert slot.reserve_submission().accepted is True


def test_start_failure_terminalizes_before_start_and_releases_slot() -> None:
    """Runner 拒绝接管时应写真实启动前失败，并立即归还通道。"""

    executor = RecordingActivityExecutor()
    executor.start_outcomes.append(False)
    slot = BackgroundExecutionSlot(executor)

    assert _prepare_and_commit(slot, "background-a") == "start_failed"
    assert executor.before_start_terminals == [
        ("background-a", "failed", "agent_cli_crash")
    ]
    assert slot.availability_snapshot().can_accept_submission is True


def test_finalizing_keeps_slot_busy_until_runner_releases() -> None:
    """业务终态不能提前释放仍在断开连接的 Runner。"""

    slot = BackgroundExecutionSlot(RecordingActivityExecutor())
    assert _prepare_and_commit(slot, "background-a") == "started"
    assert slot.notify_activity_terminal("background-a") is True
    assert slot.reserve_submission().reason == "background_slot_occupied"

    assert slot.notify_runner_released("background-a") is True
    assert slot.reserve_submission().accepted is True


def test_committed_handoff_inherits_parent_slot() -> None:
    """replacement 只能在父 Runner 完整清理后接管同一通道。"""

    executor = RecordingActivityExecutor()
    slot = BackgroundExecutionSlot(executor)
    assert _prepare_and_commit(slot, "background-a") == "started"
    handoff = slot.reserve_handoff("background-a")
    assert handoff.accepted is True
    assert slot.bind_handoff(handoff.reservation_id, "background-a-prime") is True
    assert slot.commit_handoff("background-a-prime") is True
    assert slot.cancel_handoff_parent("background-a") == "accepted"
    assert slot.reserve_submission().reason == "background_slot_occupied"

    assert slot.notify_activity_terminal("background-a") is True
    assert slot.notify_runner_released("background-a") is True
    assert executor.started == ["background-a"]
    assert slot.start_committed_handoff("background-a-prime") is True
    assert executor.started == ["background-a", "background-a-prime"]


def test_committed_handoff_recheck_is_idempotent() -> None:
    """acceptance 与 after_commit 对同一 handoff 的复核必须幂等。"""

    slot = BackgroundExecutionSlot(RecordingActivityExecutor())
    assert _prepare_and_commit(slot, "background-a") == "started"
    handoff = slot.reserve_handoff("background-a")
    assert handoff.accepted is True
    assert slot.bind_handoff(handoff.reservation_id, "background-a-prime") is True

    assert slot.commit_handoff("background-a-prime") is True
    assert slot.commit_handoff("background-a-prime") is True
    assert slot.is_activity_correctable("background-a") is True


def test_parent_terminal_wins_uncommitted_handoff_race() -> None:
    """父任务先终态时 replacement 必须失败，不能转成普通任务。"""

    executor = RecordingActivityExecutor()
    slot = BackgroundExecutionSlot(executor)
    assert _prepare_and_commit(slot, "background-a") == "started"
    handoff = slot.reserve_handoff("background-a")
    assert handoff.accepted is True
    assert slot.bind_handoff(handoff.reservation_id, "background-a-prime") is True

    assert slot.notify_activity_terminal("background-a") is True
    assert slot.commit_handoff("background-a-prime") is False
    assert slot.fail_handoff(
        "background-a-prime",
        reason="correction_activity_no_longer_correctable",
    ) is True
    assert executor.before_start_terminals == [
        (
            "background-a-prime",
            "failed",
            "correction_activity_no_longer_correctable",
        )
    ]


def test_shutdown_discards_prepared_resource_and_rejects_new_work() -> None:
    """关闭会清理未提交资源，并永久拒绝当前通道的新准入。"""

    executor = RecordingActivityExecutor()
    slot = BackgroundExecutionSlot(executor)
    reservation = slot.reserve_submission()
    assert reservation.accepted is True
    assert slot.bind_submission(reservation.reservation_id, "background-a") is True

    slot.shutdown()

    assert executor.discarded == ["background-a"]
    assert slot.reserve_submission().reason == "executor_shutdown"
