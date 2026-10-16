from __future__ import annotations

from dataclasses import dataclass

import pytest

from companion.background_agent.contracts import (
    BackgroundLaneAvailability,
    BackgroundWorkAvailability,
)
from companion.background_agent.execution_slot import (
    ExecutionSlotSubmissionKind,
    ExecutionSlotSubmissionOutcome,
)
from companion.foreground_tools.background_agent.submission_service import (
    BackgroundWorkSubmission,
    BackgroundWorkSubmissionService,
    PreparedBackgroundWork,
)
from companion.foreground_tools.orchestration.contracts import ToolFollowupFact


@dataclass
class RecordingSubmissionExecutor:
    """模拟执行槽提交结果，并记录提交边界是否只执行一次。

    Args:
        outcome: commit 时应返回的 canonical 执行槽事实。
        raise_on_commit: 是否在 commit 边界模拟执行器异常。
    """

    outcome: ExecutionSlotSubmissionOutcome
    raise_on_commit: bool = False

    def __post_init__(self) -> None:
        """初始化可观察的调用记录。

        Returns:
            无。
        """

        self.commit_calls: list[str] = []
        self.cancel_calls: list[str] = []

    def prepare(self, **kwargs: object) -> tuple[bool, list[dict[str, object]]]:
        """返回一项已经取得 prepared 容量的 Activity。

        Args:
            **kwargs: Submission Service 传入的冻结任务参数。

        Returns:
            接受标记和唯一 prepared 事件。
        """

        del kwargs
        return True, [
            {
                "type": "background_agent.prepared",
                "payload": {"activity_id": self.outcome.activity_id},
            }
        ]

    def commit_prepared(self, activity_id: str) -> ExecutionSlotSubmissionOutcome:
        """返回测试指定的执行槽 canonical 事实。

        Args:
            activity_id: 已经越过 Foreground 提交边界的 Activity 编号。

        Returns:
            测试预先指定的执行槽结果。
        """

        self.commit_calls.append(activity_id)
        if self.raise_on_commit:
            raise RuntimeError("测试用提交异常")
        return self.outcome

    def cancel_before_start(self, activity_id: str) -> bool:
        """记录提交前回滚。

        Args:
            activity_id: 需要释放 prepared 容量的 Activity 编号。

        Returns:
            测试替身始终返回真。
        """

        self.cancel_calls.append(activity_id)
        return True

    def background_available(self) -> bool:
        """返回测试执行器仍可接单。

        Returns:
            始终返回真。
        """

        return True

    def background_availability_snapshot(self) -> BackgroundWorkAvailability:
        """返回不包含任务内容的 canonical 可用性快照。

        Returns:
            两条通道都空闲且可接单的固定快照。
        """

        return BackgroundWorkAvailability(
            ordinary=BackgroundLaneAvailability(
                can_accept_submission=True,
                busy=False,
                runner_available=True,
                configured=True,
                shutdown=False,
            ),
            recovery=BackgroundLaneAvailability(
                can_accept_submission=True,
                busy=False,
                runner_available=True,
                configured=True,
                shutdown=False,
            ),
            active_count=0,
        )


@pytest.mark.parametrize(
    ("slot_kind", "expected_fact"),
    [
        (
            "started",
            ToolFollowupFact(
                kind="activity_started",
                activity_id="background-a",
                goal="整理录音",
            ),
        ),
        (
            "start_failed",
            ToolFollowupFact(
                kind="activity_start_failed",
                activity_id="background-a",
                goal="整理录音",
                safe_message="这项任务没有启动。",
            ),
        ),
        (
            "rejected",
            ToolFollowupFact(
                kind="tool_rejected",
                activity_id="background-a",
                goal="整理录音",
                safe_message="这项任务没有启动。",
            ),
        ),
    ],
)
def test_after_commit_maps_each_slot_outcome_to_one_canonical_fact(
    slot_kind: ExecutionSlotSubmissionKind,
    expected_fact: ToolFollowupFact,
) -> None:
    """提交服务只能把执行槽的唯一结果映射成一份最终工具事实。

    Args:
        slot_kind: 执行槽本次返回的 canonical 结果类型。
        expected_fact: Submission Service 必须映射出的工具事实。

    Returns:
        无。
    """

    executor = RecordingSubmissionExecutor(
        ExecutionSlotSubmissionOutcome(
            kind=slot_kind,
            activity_id="background-a",
        )
    )
    prepared = _prepare_submission(executor)
    action = prepared.claim_reservation().action

    assert action.accept() is True
    assert action.commit_enqueued() is True
    assert action.run_after_commit() is True
    assert action.run_after_commit() is False
    assert action.after_commit_fact == expected_fact
    assert executor.commit_calls == ["background-a"]
    assert executor.cancel_calls == []


def test_after_commit_exception_becomes_start_failed_fact() -> None:
    """提交边界异常必须收口成安全失败事实，不能逃出控制循环。

    Returns:
        无。
    """

    executor = RecordingSubmissionExecutor(
        ExecutionSlotSubmissionOutcome(kind="started", activity_id="background-a"),
        raise_on_commit=True,
    )
    prepared = _prepare_submission(executor)
    action = prepared.claim_reservation().action

    assert action.accept() is True
    assert action.commit_enqueued() is True
    assert action.run_after_commit() is True
    assert action.after_commit_fact == ToolFollowupFact(
        kind="activity_start_failed",
        activity_id="background-a",
        goal="整理录音",
        safe_message="这项任务没有启动。",
    )


def test_availability_snapshot_preserves_both_lane_facts() -> None:
    """Foreground 读取的快照必须完整保留双通道无内容事实。

    Returns:
        无。
    """

    executor = RecordingSubmissionExecutor(
        ExecutionSlotSubmissionOutcome(kind="started", activity_id="background-a")
    )

    assert BackgroundWorkSubmissionService(executor).availability_snapshot() == (
        BackgroundWorkAvailability(
            ordinary=BackgroundLaneAvailability(
                can_accept_submission=True,
                busy=False,
                runner_available=True,
                configured=True,
                shutdown=False,
            ),
            recovery=BackgroundLaneAvailability(
                can_accept_submission=True,
                busy=False,
                runner_available=True,
                configured=True,
                shutdown=False,
            ),
            active_count=0,
        )
    )


def _prepare_submission(
    executor: RecordingSubmissionExecutor,
) -> PreparedBackgroundWork:
    """通过正式服务准备一项测试 Background Work。

    Args:
        executor: 返回指定执行槽事实的测试执行器。

    Returns:
        已经持有一次性提交事务的 prepared 结果。
    """

    result = BackgroundWorkSubmissionService(executor).prepare(
        BackgroundWorkSubmission(
            execution_lane="ordinary",
            operation_id="operation-a",
            tool_call_id="tool-a",
            protocol_transaction_id="transaction-a",
            interaction_session_id="session-a",
            goal="整理录音",
            deliverable=True,
            requirements="输出清晰的纪要",
            materials=(),
        )
    )
    assert isinstance(result, PreparedBackgroundWork)
    return result
