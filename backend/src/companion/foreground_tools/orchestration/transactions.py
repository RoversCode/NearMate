from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock
from typing import Literal
from uuid import uuid4

from companion.observability.logging_config import get_backend_logger
from companion.foreground_tools.orchestration.contracts import ToolFollowupFact
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)


CommitState = Literal["prepared", "accepted", "committed", "rolled_back"]
logger = get_backend_logger(__name__)


class CommitAction:
    """把一次事件提交及其后续副作用收口为只能单向前进的状态机。

    这不是数据库事务。它只负责保证同一份资源的提交流程不会重复执行：
    事件被 generation 接收时进入 accepted，成功入队后进入 committed；
    在入队前失败或取消则进入 rolled_back。提交后的启动等副作用也只会执行一次。
    """

    def __init__(
        self,
        *,
        on_accept: Callable[[], None],
        on_rollback: Callable[[], None] | None = None,
        after_commit: Callable[[], ToolFollowupFact | None] | None = None,
        transaction_id: str | None = None,
        resource_type: str = "",
        resource_id: str = "",
        commit_barrier: BackgroundCommitDrainBarrier | None = None,
    ) -> None:
        """初始化一次资源提交所需的状态和回调。

        Args:
            on_accept: generation 接受事件、但事件尚未入队时执行的动作。
            on_rollback: 事件未成功入队时执行的资源回收动作。
            after_commit: 事件成功入队后执行一次的后续动作，例如启动 Activity。
            transaction_id: 用于串联状态日志的事务编号；未提供时自动生成。
            resource_type: 这次提交保护的资源类别，写入日志便于排查。
            resource_id: 这次提交保护的资源编号，写入日志便于排查。
            commit_barrier: 可选的 Background 关闭排水屏障；提供后，事务从
                accept 持有到 rollback 或 after-commit 完成。
        """

        self._on_accept = on_accept
        self._on_rollback = on_rollback or (lambda: None)
        self._after_commit = after_commit or (lambda: None)
        self._lock = Lock()
        self._state: CommitState = "prepared"
        self._after_commit_running = False
        self._after_commit_done = False
        self._after_commit_fact: ToolFollowupFact | None = None
        self._after_commit_completed = Event()
        self._commit_barrier = commit_barrier
        self._commit_barrier_entered = False
        self.transaction_id = transaction_id or f"commit-{uuid4().hex}"
        self.resource_type = resource_type.strip()
        self.resource_id = resource_id.strip()

    @property
    def state(self) -> CommitState:
        """在线程安全边界内读取当前提交状态。

        Returns:
            prepared、accepted、committed 或 rolled_back 中的当前状态。
        """

        with self._lock:
            return self._state

    def accept(self) -> bool:
        """记录 generation 已接收事件，但暂不视为提交完成。

        此时事件还没有成功入队，因此后续失败仍可调用 rollback 回收资源。

        Returns:
            本次首次从 prepared 进入 accepted 时返回真；其它状态返回假。
        """

        should_leave_barrier = False
        accept_exception: BaseException | None = None
        with self._lock:
            if self._state != "prepared":
                return False
            if self._commit_barrier is not None:
                if not self._commit_barrier.enter(self.transaction_id):
                    return False
                self._commit_barrier_entered = True
            try:
                self._on_accept()
            except BaseException as exception:
                # 接收前动作失败时，事件不可能入队；必须立即回收已预留资源。
                try:
                    self._on_rollback()
                except BaseException as rollback_exception:
                    accept_exception = rollback_exception
                else:
                    accept_exception = exception
                finally:
                    self._state = "rolled_back"
                    self._log_transition("prepared", "rolled_back", "accept_failed")
                    should_leave_barrier = self._commit_barrier_entered
                    self._commit_barrier_entered = False
            else:
                self._state = "accepted"
                self._log_transition("prepared", "accepted", "generation_accepted")
        if should_leave_barrier and self._commit_barrier is not None:
            self._commit_barrier.leave(self.transaction_id)
        if accept_exception is not None:
            raise accept_exception
        return True

    def commit_enqueued(self) -> bool:
        """在事件成功入队后把事务标记为已提交。

        进入 committed 后不再允许 rollback；后续由 run_after_commit 触发启动等动作。

        Returns:
            本次首次从 accepted 进入 committed 时返回真；其它状态返回假。
        """

        with self._lock:
            if self._state != "accepted":
                return False
            self._state = "committed"
            self._log_transition("accepted", "committed", "event_enqueued")
            return True

    def rollback(self) -> bool:
        """回收尚未完成入队提交的资源。

        只允许从 prepared 或 accepted 回滚；已经 committed 的资源已进入正式执行链路，
        必须由该资源自己的取消机制处理。

        Returns:
            本次首次进入 rolled_back 并完成回收时返回真；否则返回假。
        """

        should_leave_barrier = False
        rollback_exception: BaseException | None = None
        with self._lock:
            if self._state not in {"prepared", "accepted"}:
                return False
            previous_state = self._state
            try:
                self._on_rollback()
            except BaseException as exception:
                rollback_exception = exception
            finally:
                self._state = "rolled_back"
                self._log_transition(previous_state, "rolled_back", "event_not_enqueued")
                should_leave_barrier = self._commit_barrier_entered
                self._commit_barrier_entered = False
        if should_leave_barrier and self._commit_barrier is not None:
            self._commit_barrier.leave(self.transaction_id)
        if rollback_exception is not None:
            raise rollback_exception
        return True

    def run_after_commit(self) -> bool:
        """在事件已成功入队后执行一次后续动作。

        回调在锁外执行，避免启动 Activity 等耗时操作阻塞其它状态读取；因此它不属于
        状态转换的原子部分。回调若失败，应由调用方按已提交后的执行失败处理。

        Returns:
            本次首次执行提交后动作时返回真；事务未提交或已执行过时返回假。
        """

        should_run = False
        with self._lock:
            if self._state != "committed" or self._after_commit_done:
                return False
            if self._after_commit_running:
                completion = self._after_commit_completed
            else:
                self._after_commit_running = True
                completion = self._after_commit_completed
                should_run = True

        # 同一事务只有一个线程执行回调；并发调用者在锁外等待它完成，避免既阻塞
        # 状态锁，又在最终 fact 写入前误以为 after_commit 已经结束。
        if not should_run:
            completion.wait()
            return False

        should_leave_barrier = False
        try:
            result = self._after_commit()
        except BaseException:
            with self._lock:
                self._after_commit_running = False
                self._after_commit_done = True
                completion.set()
                should_leave_barrier = self._commit_barrier_entered
                self._commit_barrier_entered = False
            if should_leave_barrier and self._commit_barrier is not None:
                self._commit_barrier.leave(self.transaction_id)
            raise

        with self._lock:
            if isinstance(result, ToolFollowupFact):
                self._after_commit_fact = result
            self._after_commit_running = False
            self._after_commit_done = True
            completion.set()
            should_leave_barrier = self._commit_barrier_entered
            self._commit_barrier_entered = False
        if should_leave_barrier and self._commit_barrier is not None:
            self._commit_barrier.leave(self.transaction_id)
        return True

    @property
    def after_commit_fact(self) -> ToolFollowupFact | None:
        """返回 after_commit 已经产生的最终工具事实。

        Returns:
            已完成的后台工具事实；事务尚未执行或没有事实时返回空。
        """

        with self._lock:
            completion = (
                self._after_commit_completed
                if self._after_commit_running
                else None
            )
        if completion is not None:
            completion.wait()
        with self._lock:
            return self._after_commit_fact

    def _log_transition(
        self,
        previous_state: CommitState,
        next_state: CommitState,
        reason: str,
    ) -> None:
        """记录一次可按事务编号关联的状态变化。

        Args:
            previous_state: 状态变化前的值。
            next_state: 状态变化后的值。
            reason: 本次转换的固定原因标识。
        """

        logger.info(
            "提交事务状态变化 transaction_id=%s resource_type=%s resource_id=%s "
            "previous_state=%s next_state=%s reason=%s",
            self.transaction_id,
            self.resource_type,
            self.resource_id,
            previous_state,
            next_state,
            reason,
        )


@dataclass(frozen=True)
class BackgroundActivityReservation:
    """保存一个已准备、但必须等前台确认后才能启动的后台 Activity。

    Activity 在 prepare 阶段已经拿到编号并占住执行器，但此时还没有运行。
    action 负责把两种后续路径绑在一起：工具事件成功入队后启动 Activity；
    事件未被接收或中途取消时，释放这个尚未启动的占位。

    Args:
        source: Activity 的来源类型，例如 task_result 或 cognitive。
        activity_id: prepare 阶段分配的 Activity 编号。
        action: 保证启动或释放只会执行一次的提交事务。
    """

    source: str
    activity_id: str
    action: CommitAction

    @classmethod
    def create(
        cls,
        *,
        source: str,
        activity_id: str,
        start_after_commit: Callable[[], ToolFollowupFact | None],
        on_rollback: Callable[[], None],
        transaction_id: str | None = None,
        commit_barrier: BackgroundCommitDrainBarrier | None = None,
    ) -> BackgroundActivityReservation:
        """为一个 prepared 状态的后台 Activity 创建提交与回滚动作。

        这个方法不启动 Activity。它只记录：前台成功接收工具事件后该调用什么
        函数启动；前台没有接收时又该调用什么函数释放占位。

        Args:
            source: Activity 的来源类型。
            activity_id: 已处于 prepared 状态的 Activity 编号。
            start_after_commit: 工具事件成功入队后调用的启动动作。
            on_rollback: 工具事件未能入队时调用的占位释放动作。
            transaction_id: 可选调用方稳定操作编号；缺失时生成内部事务编号。
            commit_barrier: 可选的 Background 关闭排水屏障。

        Returns:
            带有尚未提交事务的 Activity 占位对象。
        """

        normalized_source = source.strip()
        normalized_activity_id = activity_id.strip()
        return cls(
            source=normalized_source,
            activity_id=normalized_activity_id,
            action=CommitAction(
                # Activity reservation 在 prepare 阶段已经建立；generation accept
                # 只改变 CommitAction 本身，不能提前把业务资源冻结成 committed。
                on_accept=lambda: None,
                on_rollback=on_rollback,
                after_commit=start_after_commit,
                transaction_id=(transaction_id or f"background-{uuid4().hex}"),
                resource_type=normalized_source,
                resource_id=normalized_activity_id,
                commit_barrier=commit_barrier,
            ),
        )

    def rollback(self) -> bool:
        """释放还未提交启动的 Activity 占位。

        重复调用安全；已经提交并启动的 Activity 不会被这条回滚路径影响。

        Returns:
            本次确实完成占位释放时返回真；此前已提交或已回滚时返回假。
        """

        return self.action.rollback()
