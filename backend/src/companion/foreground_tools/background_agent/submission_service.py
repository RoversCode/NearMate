"""提供 Foreground 原生派单与自我反思恢复共用的 Background Work 提交能力。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock
from typing import Protocol

from companion.background_agent.activity_resources import (
    BACKGROUND_AGENT_UNCONFIGURED_MESSAGE,
)
from companion.background_agent.contracts import AgentMaterial
from companion.background_agent.contracts import (
    BackgroundExecutionLane,
    BackgroundWorkAvailability,
)
from companion.background_agent.execution_slot import (
    ExecutionSlotSubmissionOutcome,
)
from companion.background_agent.execution_lane import (
    BACKGROUND_AGENT_ALREADY_RUNNING_MESSAGE,
)
from companion.foreground_tools.orchestration.contracts import ToolFollowupFact
from companion.foreground_tools.orchestration.transactions import (
    BackgroundActivityReservation,
)
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


@dataclass(frozen=True, slots=True)
class BackgroundWorkSubmission:
    """冻结一项与 Foreground 模型对象无关的新 Background Work。

    Args:
        operation_id: 本次原生派单或自我反思恢复操作的稳定编号。
        execution_lane: 由可信调用方指定的 ordinary 或 recovery 通道。
        tool_call_id: 本次真实工具执行身份。
        protocol_transaction_id: 本次工具协议事务身份。
        interaction_session_id: 用户动作所属交互会话编号。
        goal: Background Work 真正要完成的目标。
        deliverable: 是否要求登记并展示交付成果。
        requirements: 成果和执行方式的补充要求。
        materials: 已经经过材料系统授权的正式材料。
        current_scene_bytes: 需要交给后台的冻结现场图片字节。
        based_on_activity_id: 用户明确派生任务时的父 Activity 编号。
        parent_task_context: 已由主进程召回并脱敏的父任务事实。
    """

    operation_id: str
    execution_lane: BackgroundExecutionLane
    tool_call_id: str
    protocol_transaction_id: str
    interaction_session_id: str
    goal: str
    deliverable: bool
    requirements: str
    materials: tuple[AgentMaterial, ...]
    current_scene_bytes: bytes | None = None
    based_on_activity_id: str | None = None
    parent_task_context: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RejectedBackgroundWork:
    """保存 canonical prepare 的稳定拒绝结果。

    Args:
        payload: 可交给工具协议但不包含异常正文的拒绝载荷。
    """

    payload: dict[str, object]


class PreparedBackgroundWork:
    """保存已占位 Background Work 及其一次性提交事务。

    Args:
        submission: 产生该 Activity 的冻结领域输入。
        activity_id: prepare 阶段分配的 Background Activity 编号。
        acceptance_payload: 可交给工具协议的 accepted 事实。
        reservation: 负责 commit 后启动或提交前回滚的一次性事务。
    """

    def __init__(
        self,
        *,
        submission: BackgroundWorkSubmission,
        activity_id: str,
        acceptance_payload: dict[str, object],
        reservation: BackgroundActivityReservation,
    ) -> None:
        """保存一次准备成功且只能被领取一次的后台提交。

        Args:
            submission: 创建该 Activity 的冻结业务输入。
            activity_id: prepare 阶段已经分配的 Activity 编号。
            acceptance_payload: 可以交给工具协议的 accepted 事实。
            reservation: 提交后启动或提交前回滚的一次性事务。
        """

        self.submission = submission
        self.activity_id = activity_id
        self.acceptance_payload = dict(acceptance_payload)
        self._reservation = reservation
        self._claimed = False
        self._lock = Lock()

    def claim_reservation(self) -> BackgroundActivityReservation:
        """把一次性启动/回滚事务交给唯一提交协调器。

        Returns:
            首次领取时返回 Background Activity reservation。

        Raises:
            RuntimeError: 同一 Prepared 结果被两个提交者重复领取。
        """

        with self._lock:
            if self._claimed:
                raise RuntimeError("Background Work 提交事务已经被领取")
            self._claimed = True
            return self._reservation

    def rollback_unclaimed(self) -> bool:
        """在提交协调器接管前释放尚未启动的 Activity 占位。

        Returns:
            本次确实完成回滚时返回真；已领取时返回假。
        """

        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            reservation = self._reservation
        return reservation.rollback()


BackgroundWorkSubmissionResult = PreparedBackgroundWork | RejectedBackgroundWork


class BackgroundAgentActivityExecutorProtocol(Protocol):
    """声明 canonical Background Work 服务依赖的最小执行器接口。"""

    def prepare(self, **kwargs: object) -> tuple[bool, list[dict[str, object]]]:
        """准备一项尚未提交的新 Background Work。

        Args:
            **kwargs: 创建 Activity 和冻结材料所需的完整参数。

        Returns:
            是否准备成功，以及可以安全公开的拒绝或状态事件。
        """

        ...

    def commit_prepared(self, activity_id: str) -> ExecutionSlotSubmissionOutcome:
        """在提交边界之后让执行槽立即启动指定 Activity。

        Args:
            activity_id: 已经完成 prepare 的 Activity 编号。

        Returns:
            执行通道产生的 started、failed 或 rejected 事实。
        """

        ...

    def cancel_before_start(self, activity_id: str) -> bool:
        """释放尚未越过提交边界的 Activity 占位。

        Args:
            activity_id: 需要回滚 prepare 的 Activity 编号。

        Returns:
            本次确实释放占位时返回真。
        """

        ...

    def background_available(self) -> bool:
        """返回当前提交边界是否可以接受一项新任务。

        Returns:
            当前配置和容量允许继续 prepare 时返回真。
        """

        ...

    def background_availability_snapshot(self) -> BackgroundWorkAvailability:
        """返回不包含任务内容的 canonical 可用性快照。

        Returns:
            普通、恢复两条固定通道和当前活动数量。
        """

        ...


class BackgroundWorkSubmissionService:
    """准备一项新 Background Work，并绑定唯一 commit 后启动语义。

    Args:
        activity_executor: 负责槽位裁决、Activity prepare 和真实启动的执行器。
        commit_barrier: Background 提交与执行器关闭共用的排水屏障。
    """

    def __init__(
        self,
        activity_executor: BackgroundAgentActivityExecutorProtocol | None,
        commit_barrier: BackgroundCommitDrainBarrier | None = None,
    ) -> None:
        """绑定负责真实 prepare、commit 和容量裁决的后台执行器。

        Args:
            activity_executor: 后台功能未配置时为空，否则为 canonical 执行器端口。
            commit_barrier: 可选的 Background 提交关闭排水屏障。
        """

        self._activity_executor = activity_executor
        self._commit_barrier = commit_barrier

    def background_available(self) -> bool:
        """读取 Background canonical 提交边界的实时可用性。

        Returns:
            执行器、Runner、配置和所有占用槽位均可用时返回真；任何普通读取
            异常都返回假，由 ``prepare`` 在并发变化后继续做最终裁决。
        """

        executor = self._activity_executor
        if executor is None:
            return False
        availability = getattr(executor, "background_available", None)
        if not callable(availability):
            return False
        try:
            return availability() is True
        except Exception as exception:
            logger.warning(
                "Background availability 读取失败 error_type=%s",
                type(exception).__name__,
            )
            return False

    def availability_snapshot(self) -> BackgroundWorkAvailability | None:
        """读取 Foreground 可安全表达的 canonical Background 可用性。

        Returns:
            执行器存在且快照读取成功时返回结构化状态；否则返回空。
        """

        executor = self._activity_executor
        if executor is None:
            return None
        snapshot_reader = getattr(
            executor,
            "background_availability_snapshot",
            None,
        )
        if not callable(snapshot_reader):
            return None
        try:
            snapshot = snapshot_reader()
        except Exception:
            logger.warning("Background availability 快照读取失败", exc_info=True)
            return None
        return snapshot if isinstance(snapshot, BackgroundWorkAvailability) else None

    def prepare(
        self,
        submission: BackgroundWorkSubmission,
        *,
        cancellation_event: Event | None = None,
    ) -> BackgroundWorkSubmissionResult:
        """校验领域输入、占住后台槽位并创建一次性提交事务。

        Args:
            submission: 与 Foreground 模型对象解耦的完整后台工作输入。
            cancellation_event: 调用方在 prepare 前后的协作式取消信号。

        Returns:
            成功时返回 PreparedBackgroundWork；否则返回稳定拒绝事实。
        """

        invalid_identity = _first_blank_identity(submission)
        if invalid_identity is not None:
            return RejectedBackgroundWork(
                _rejected(
                    "background_submission_identity_missing",
                    "后台任务缺少稳定执行身份。",
                )
            )
        if cancellation_event is not None and cancellation_event.is_set():
            return RejectedBackgroundWork(
                _rejected(
                    "generation_cancelled",
                    "当前回复已经取消，后台工作没有启动。",
                )
            )
        if self._activity_executor is None:
            return RejectedBackgroundWork(
                _rejected("background_agent_unavailable", "后台工作当前不可用。")
            )

        normalized_goal = submission.goal.strip()
        if not normalized_goal:
            return RejectedBackgroundWork(
                _rejected("missing_goal", "需要先明确后台工作要完成什么。")
            )
        try:
            accepted, events = self._activity_executor.prepare(
                execution_lane=submission.execution_lane,
                interaction_session_id=submission.interaction_session_id.strip(),
                goal=normalized_goal,
                deliverable=submission.deliverable,
                requirements=submission.requirements,
                materials=tuple(submission.materials),
                current_scene_bytes=submission.current_scene_bytes,
                based_on_activity_id=submission.based_on_activity_id,
                parent_task_context=(
                    dict(submission.parent_task_context)
                    if submission.parent_task_context is not None
                    else None
                ),
            )
        except Exception as exception:
            logger.warning(
                "Background prepare 未经执行器归一化 operation_id=%s error_type=%s",
                submission.operation_id,
                type(exception).__name__,
            )
            return RejectedBackgroundWork(
                _rejected(
                    "background_prepare_failed",
                    "后台任务没有成功建立。",
                )
            )
        event_payload = _first_event_payload(
            events,
            "background_agent.prepared" if accepted else "background_agent.rejected",
        )
        if not accepted:
            return RejectedBackgroundWork(_rejection_payload(event_payload))

        activity_id = str(event_payload.get("activity_id") or "").strip()
        if not activity_id:
            return RejectedBackgroundWork(
                _rejected(
                    "background_activity_identity_missing",
                    "后台任务没有成功建立。",
                )
            )
        if cancellation_event is not None and cancellation_event.is_set():
            self._activity_executor.cancel_before_start(activity_id)
            return RejectedBackgroundWork(
                _rejected(
                    "generation_cancelled",
                    "当前回复已经取消，后台工作没有启动。",
                )
            )

        acceptance_payload: dict[str, object] = {
            "state": "accepted",
            "activity_id": activity_id,
            "goal": normalized_goal,
            "deliverable": submission.deliverable,
        }
        if submission.based_on_activity_id:
            acceptance_payload["based_on_activity_id"] = (
                submission.based_on_activity_id
            )

        def start_after_commit() -> ToolFollowupFact:
            """只用 Runner 的真实接管结果生成用户可表达事实。

            Returns:
                started、rejected 或 start_failed 对应的 canonical 事实。
            """

            try:
                outcome = self._activity_executor.commit_prepared(activity_id)
            except Exception:
                logger.exception(
                    "Background Work 提交后启动异常 operation_id=%s activity_id=%s",
                    submission.operation_id,
                    activity_id,
                )
                outcome = ExecutionSlotSubmissionOutcome(
                    kind="start_failed",
                    activity_id=activity_id,
                    reason="activity_start_failed",
                )
            if outcome.kind == "started":
                return ToolFollowupFact(
                    kind="activity_started",
                    activity_id=activity_id,
                    goal=normalized_goal,
                )
            if outcome.kind == "rejected":
                return ToolFollowupFact(
                    kind="tool_rejected",
                    activity_id=activity_id,
                    goal=normalized_goal,
                    safe_message="这项任务没有启动。",
                )
            if outcome.kind == "start_failed":
                return ToolFollowupFact(
                    kind="activity_start_failed",
                    activity_id=activity_id,
                    goal=normalized_goal,
                    safe_message="这项任务没有启动。",
                )
            raise RuntimeError("Background 执行槽返回了未知提交事实")

        reservation = BackgroundActivityReservation.create(
            source="background_agent",
            activity_id=activity_id,
            start_after_commit=start_after_commit,
            on_rollback=lambda: self._activity_executor.cancel_before_start(activity_id),
            transaction_id=submission.operation_id,
            commit_barrier=self._commit_barrier,
        )
        return PreparedBackgroundWork(
            submission=submission,
            activity_id=activity_id,
            acceptance_payload=acceptance_payload,
            reservation=reservation,
        )


def _first_blank_identity(submission: BackgroundWorkSubmission) -> str | None:
    """返回第一项空白提交身份，全部有效时返回空。

    Args:
        submission: 需要检查稳定身份的后台工作输入。

    Returns:
        第一项空白字段名；全部有效时返回空。
    """

    identities = {
        "operation_id": submission.operation_id,
        "tool_call_id": submission.tool_call_id,
        "protocol_transaction_id": submission.protocol_transaction_id,
        "interaction_session_id": submission.interaction_session_id,
    }
    return next((name for name, value in identities.items() if not value.strip()), None)


def _first_event_payload(
    events: list[dict[str, object]],
    event_type: str,
) -> dict[str, object]:
    """读取指定 prepare 事件的第一份字典载荷。

    Args:
        events: 执行器在 prepare 阶段产生的事件列表。
        event_type: 需要匹配的事件类型。

    Returns:
        第一份匹配的字典载荷；没有匹配时返回空字典。
    """

    for event in events:
        if event.get("type") != event_type:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _rejection_payload(event_payload: dict[str, object]) -> dict[str, object]:
    """把执行器拒绝事件归一为稳定工具结果。

    Args:
        event_payload: 执行器公开的拒绝事件载荷。

    Returns:
        不包含内部异常正文的工具拒绝结果。
    """

    reason = str(event_payload.get("reason") or "background_agent_rejected").strip()
    if reason == "background_slot_occupied":
        return {
            "state": "already_running",
            "reason": reason,
            "safe_message": _safe_message(
                event_payload,
                fallback=BACKGROUND_AGENT_ALREADY_RUNNING_MESSAGE,
            ),
        }
    if reason == "background_agent_unconfigured":
        return _rejected(reason, BACKGROUND_AGENT_UNCONFIGURED_MESSAGE)
    return {
        "state": "rejected",
        "activity_id": event_payload.get("activity_id"),
        "reason": reason,
        "safe_message": _safe_message(
            event_payload,
            fallback="后台任务没有启动。",
        ),
    }


def _rejected(reason: str, safe_message: str) -> dict[str, object]:
    """构造不泄漏内部执行细节的统一拒绝结果。

    Args:
        reason: 可供程序判断的稳定拒绝原因。
        safe_message: 可以直接交给用户表达的安全说明。

    Returns:
        canonical rejected 工具结果。
    """

    return {"state": "rejected", "reason": reason, "safe_message": safe_message}


def _safe_message(payload: dict[str, object], *, fallback: str) -> str:
    """读取执行器提供的安全说明，缺失时使用稳定兜底。

    Args:
        payload: 可能包含安全说明的执行器载荷。
        fallback: 缺少有效说明时使用的固定文本。

    Returns:
        去除首尾空白后的安全说明。
    """

    value = payload.get("safe_message")
    return value.strip() if isinstance(value, str) and value.strip() else fallback
