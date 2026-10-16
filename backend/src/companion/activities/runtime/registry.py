"""维护当前 Backend 进程内 Activity 的权威运行时状态。

Registry 只保存本次运行的活动事实：活跃 Activity、有限数量的终态记录、
阶段时间线和单调递增的快照版本号。它不负责执行任务、广播事件或交付
结果卡，避免这些不同生命周期的职责互相阻塞。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from companion.activities.runtime.failure_reporting import runtime_interruption_failure
from companion.activities.runtime.models import (
    ACTIVITY_STAGE_CODES,
    ACTIVITY_STAGE_ORDER,
    ACTIVITY_STAGE_PLAN,
    ActivityCancellationMarkResult,
    ActivityCancelTarget,
    ActivityCompact,
    ActivityCompletionResult,
    ActivityDetail,
    ActivityFailure,
    ActivitySnapshot,
    ActivitySourceChange,
    ActivityStageCode,
    ActivityStagePeriod,
    ActivityTurnOutput,
    ActivityStatus,
    activity_stage_label,
)
from companion.activities.runtime.public_text import summarize_activity_goal
from companion.activities.work_plan import AgentWorkPlan


class ActivityInvariantViolation(RuntimeError):
    """表示来源 Activity Executor 上报了不可能成立的状态变化。

    例如阶段倒退，或失败终态缺少故障报告。这类问题不能伪装成普通
    业务失败，否则会把错误事实写进权威快照。
    """


class ActivityNotFound(LookupError):
    """表示请求的 Activity 不属于当前 Backend 运行。"""


class ActivityStateConflict(RuntimeError):
    """表示 Activity 存在，但当前生命周期不允许目标操作。"""


@dataclass(slots=True)
class _ActivityRecord:
    """保存单个 Activity 的完整内部事实。

    公共快照只能通过 ``compact`` 和 ``detail`` 生成，防止控制来源和运行时
    世代等内部路由字段意外泄漏给 Desktop 或 Console。
    """

    activity_id: str
    kind: str
    type_label: str
    control_source: str
    runtime_epoch: int
    goal: str
    goal_summary: str
    status: ActivityStatus
    started_at: str | None
    updated_at: str
    finished_at: str | None = None
    completion_result: ActivityCompletionResult | None = None
    failure: ActivityFailure | None = None
    failure_reason: str | None = None
    stage_code: ActivityStageCode | None = None
    stage_timeline: list[ActivityStagePeriod] = field(default_factory=list)
    pending_interaction_ids: set[str] = field(default_factory=set)
    work_plan: AgentWorkPlan | None = None
    turn_outputs: list[ActivityTurnOutput] = field(default_factory=list)

    def compact(self) -> ActivityCompact:
        """生成不泄漏内部字段的公共摘要。

        Returns:
            当前记录对应的 ActivityCompact。
        """

        # terminal 可能先于 started 到达，此时没有可关闭的阶段时间线；失败报告
        # 仍记录了真实失败阶段，因此用它保证任务中心能给用户明确的进度位置。
        stage_code: str | None = self.stage_code
        stage_label: str | None = (
            activity_stage_label(stage_code) if stage_code is not None else None
        )
        if stage_code is None and self.failure is not None:
            stage_code = self.failure.stage_code
            stage_label = self.failure.stage_label
        return ActivityCompact(
            activity_id=self.activity_id,
            kind=self.kind,
            type_label=self.type_label,
            status=self.status,
            goal_summary=self.goal_summary,
            failure_summary=(
                self.failure.summary
                if self.status in {"failed", "interrupted"}
                and self.failure is not None
                else None
            ),
            stage_code=stage_code,
            stage_label=stage_label,
            started_at=self.started_at,
            updated_at=self.updated_at,
            finished_at=self.finished_at,
            can_cancel=self.status in {"running", "waiting_user"},
        )

    def detail(self) -> ActivityDetail:
        """生成 Console 使用的公开详情。

        Returns:
            当前记录对应的 ActivityDetail。
        """

        return ActivityDetail(
            compact=self.compact(),
            goal=self.goal,
            stage_plan=ACTIVITY_STAGE_PLAN,
            stage_timeline=tuple(self.stage_timeline),
            result_text=(
                self.completion_result.display_content
                if self.completion_result is not None
                else ""
            ),
            failure=self.failure,
            failure_reason=self.failure_reason,
            work_plan=self.work_plan,
            turn_outputs=tuple(self.turn_outputs),
        )


class ActivityRuntimeRegistry:
    """维护本次 Backend 运行内 Activity 的权威状态。

    同一把可重入锁保护活动索引、阶段时间线、终态淘汰和 revision，确保
    Console 与 Desktop 读到的每份快照都是一个完整时刻的事实，而不是多次
    状态变化拼接出的半成品。
    """

    def __init__(
        self,
        *,
        runtime_id: str,
        max_terminal_records: int = 200,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化当前 Backend 运行的 Activity 状态容器。

        Args:
            runtime_id: 本次 Backend 进程的运行编号，由组合根生成注入。
            max_terminal_records: 本次运行最多保留的终态数量。
            now: 可替换的 UTC 墙钟函数。

        Raises:
            ValueError: runtime_id 为空或终态上限不是正数。
        """

        if not isinstance(runtime_id, str) or not runtime_id.strip():
            raise ValueError("runtime_id 必须是非空字符串")
        if max_terminal_records <= 0:
            raise ValueError("max_terminal_records 必须大于零")
        self._runtime_id = runtime_id
        self._max_terminal_records = max_terminal_records
        self._now = now or (lambda: datetime.now(UTC))
        self._active_by_id: dict[str, _ActivityRecord] = {}
        # OrderedDict 既提供按编号查询，也保留终态到达顺序以执行先进先出淘汰。
        self._terminal_by_id: OrderedDict[str, _ActivityRecord] = OrderedDict()
        # 热替换后拒绝旧世代的迟到事件，避免它们重新污染新运行时的任务中心。
        self._minimum_open_runtime_epoch = 1
        self._revision = 0
        self._lock = threading.RLock()

    def apply(self, change: ActivitySourceChange) -> bool:
        """幂等应用来源适配器产出的单项 Activity 变化。

        Args:
            change: 来源适配器生成的标准变化。

        Returns:
            公共活动事实发生变化时返回真；重复或已经失效的事件返回假。

        Raises:
            ActivityInvariantViolation: 来源破坏同类占用、阶段顺序或故障结构不变量。
        """

        with self._lock:
            # 已被中断的世代不再拥有写入资格。这里必须在分派前拦住迟到事件，
            # 否则 terminal 可能在热替换后重新创建一条已经失效的 Activity。
            if change.runtime_epoch < self._minimum_open_runtime_epoch:
                return False
            if change.change == "started":
                changed = self._apply_started(change)
            elif change.change == "stage_changed":
                changed = self._apply_stage(change)
            elif change.change == "work_plan_changed":
                changed = self._apply_work_plan(change)
            elif change.change == "turn_output":
                changed = self._apply_turn_output(change)
            elif change.change in {"waiting_user_entered", "waiting_user_resumed"}:
                changed = self._apply_waiting_user(change)
            else:
                changed = self._apply_terminal(change)
            if changed:
                self._revision += 1
            return changed

    def cancellation_target(self, activity_id: str) -> ActivityCancelTarget:
        """读取并校验可取消目标。

        Args:
            activity_id: 客户端请求取消的活动编号。

        Returns:
            只包含内部路由所需字段的取消目标。

        Raises:
            ActivityNotFound: 当前运行不存在该活动。
            ActivityStateConflict: 活动不是可取消状态。
        """

        with self._lock:
            record = self._active_by_id.get(activity_id)
            if record is None:
                if activity_id in self._terminal_by_id:
                    raise ActivityStateConflict("activity is already terminal")
                raise ActivityNotFound("activity does not exist in current runtime")
            if record.status not in {"running", "waiting_user"}:
                raise ActivityStateConflict("activity cannot be cancelled in current state")
            return ActivityCancelTarget(
                activity_id=record.activity_id,
                control_source=record.control_source,
                runtime_epoch=record.runtime_epoch,
            )

    def mark_cancelling(self, activity_id: str) -> ActivityCancellationMarkResult:
        """在取消命令成功入队后标记活动为取消中。

        Args:
            activity_id: 已被运行时接受取消命令的活动编号。

        Returns:
            返回已标记、终态已先到达或活动不存在三态结果。
        """

        with self._lock:
            record = self._active_by_id.get(activity_id)
            if record is None:
                if activity_id in self._terminal_by_id:
                    return "already_terminal"
                return "missing"
            # HTTP 重试不会让执行器收到第二次取消命令，因此把重复标记视为成功。
            if record.status == "cancelling":
                return "marked"
            if record.status not in {"running", "waiting_user"}:
                return "missing"
            record.status = "cancelling"
            record.updated_at = self._now_text()
            self._revision += 1
            return "marked"

    def interrupt_active(
        self,
        runtime_epoch: int,
        failure_code: Literal["runtime_reconfigured", "runtime_interrupted"],
    ) -> tuple[ActivityDetail, ...]:
        """把指定运行时世代的活动原子转为中断终态。

        Args:
            runtime_epoch: 已失效的助手运行时世代。
            failure_code: 计划内热替换或意外中断对应的错误码。

        Returns:
            本次关闭的活动详情；没有活动时返回空元组。
        """

        with self._lock:
            self._minimum_open_runtime_epoch = max(
                self._minimum_open_runtime_epoch,
                runtime_epoch + 1,
            )
            interrupted_records = [
                record
                for record in self._active_by_id.values()
                if record.runtime_epoch == runtime_epoch
            ]
            if not interrupted_records:
                return ()
            now_text = self._now_text()
            for record in interrupted_records:
                record.status = "interrupted"
                record.updated_at = now_text
                record.finished_at = now_text
                record.completion_result = None
                # 每条故障报告都保留各自最后所在阶段，不能把整批活动误标成同一阶段。
                record.failure = runtime_interruption_failure(
                    failure_code,
                    stage_code=record.stage_code or "preparing",
                )
                self._close_open_stage(record, now_text)
                self._terminal_by_id[record.activity_id] = record
                self._active_by_id.pop(record.activity_id, None)
            self._bound_terminal_records()
            # 一次运行时故障属于同一领域事务，对外只产生一个新 revision。
            self._revision += 1
            return tuple(record.detail() for record in interrupted_records)

    def desktop_snapshot(self) -> ActivitySnapshot:
        """生成 Desktop 使用的轻量快照。

        Returns:
            全部活跃 Activity 与最近三项终态。
        """

        return self._snapshot(recent_limit=3)

    def console_snapshot(self) -> ActivitySnapshot:
        """生成 Console 使用的完整本次运行快照。

        Returns:
            全部活跃 Activity 与受配置上限约束的终态。
        """

        return self._snapshot(recent_limit=self._max_terminal_records)

    def detail(self, activity_id: str) -> ActivityDetail:
        """按编号读取活动详情。

        Args:
            activity_id: 待读取的活动编号。

        Returns:
            当前活动或终态记录的公开详情。

        Raises:
            ActivityNotFound: 当前运行不存在该活动。
        """

        with self._lock:
            record = self._active_by_id.get(activity_id) or self._terminal_by_id.get(
                activity_id
            )
            if record is None:
                raise ActivityNotFound("activity does not exist in current runtime")
            return record.detail()

    def completion_result(self, activity_id: str) -> ActivityCompletionResult:
        """按编号读取成功 Activity 的结构化完成结果。

        Args:
            activity_id: 待读取的 Activity 编号。

        Returns:
            成功终态保存的完整成果、交付摘要和文件事实。

        Raises:
            ActivityNotFound: 当前运行不存在该 Activity。
            ActivityStateConflict: Activity 尚未成功完成或没有完成结果。
        """

        with self._lock:
            record = self._active_by_id.get(activity_id) or self._terminal_by_id.get(
                activity_id
            )
            if record is None:
                raise ActivityNotFound("activity does not exist in current runtime")
            if record.status != "completed" or record.completion_result is None:
                raise ActivityStateConflict("activity has no completion result")
            return record.completion_result

    def _apply_started(self, change: ActivitySourceChange) -> bool:
        """在已持锁状态下登记真实启动的 Activity 并开启准备阶段。

        Args:
            change: 已通过来源适配器校验的 started 变化。

        Returns:
            新 Activity 成功登记时返回真；重复 started 返回假。

        """

        if (
            change.activity_id in self._active_by_id
            or change.activity_id in self._terminal_by_id
        ):
            return False
        now_text = self._now_text()
        record = _ActivityRecord(
            activity_id=change.activity_id,
            kind=change.kind,
            type_label=change.type_label,
            control_source=change.control_source,
            runtime_epoch=change.runtime_epoch,
            goal=change.goal,
            goal_summary=summarize_activity_goal(change.goal),
            status="running",
            started_at=now_text,
            updated_at=now_text,
            stage_code="preparing",
            stage_timeline=[
                ActivityStagePeriod(
                    stage_code="preparing",
                    stage_label=activity_stage_label("preparing"),
                    started_at=now_text,
                    finished_at=None,
                )
            ],
        )
        self._active_by_id[record.activity_id] = record
        return True

    def _apply_stage(self, change: ActivitySourceChange) -> bool:
        """在已持锁状态下推进 Activity 阶段并维护阶段时间线。

        Args:
            change: 已通过来源适配器校验的 stage_changed 变化。

        Returns:
            阶段实际前进时返回真；重复或无主记录的迟到事件返回假。

        Raises:
            ActivityInvariantViolation: 阶段代码非法或阶段倒退。
        """

        # 终态后的迟到阶段事件幂等吸收，不产生公共变化。
        if change.activity_id in self._terminal_by_id:
            return False
        record = self._active_by_id.get(change.activity_id)
        if record is None:
            # stage_changed 不能独自创建 Activity。未知事件通常来自已淘汰终态的
            # 迟到上报，静默吸收才能保持 started 建立的时间线仍是唯一事实来源。
            return False
        new_stage = change.stage
        if new_stage is None or new_stage not in ACTIVITY_STAGE_CODES:
            raise ActivityInvariantViolation(f"illegal activity stage {new_stage!r}")
        current_index = ACTIVITY_STAGE_ORDER[record.stage_code or "preparing"]
        new_index = ACTIVITY_STAGE_ORDER[new_stage]
        # 阶段倒退属于来源不变量违规；同阶段重复上报幂等忽略。
        if new_index < current_index:
            raise ActivityInvariantViolation(
                f"activity {change.activity_id!r} stage cannot move "
                f"backward from {record.stage_code!r} to {new_stage!r}"
            )
        if new_index == current_index:
            return False
        now_text = self._now_text()
        self._close_open_stage(record, now_text)
        record.stage_timeline.append(
            ActivityStagePeriod(
                stage_code=new_stage,
                stage_label=activity_stage_label(new_stage),
                started_at=now_text,
                finished_at=None,
            )
        )
        record.stage_code = new_stage
        record.updated_at = now_text
        return True

    def _apply_terminal(self, change: ActivitySourceChange) -> bool:
        """在已持锁状态下写入唯一终态，并吸收其后的重复事件。

        Args:
            change: 已通过来源适配器校验的 completed、failed 或 cancelled 变化。

        Returns:
            首次写入该 Activity 终态时返回真；重复终态返回假。

        Raises:
            ActivityInvariantViolation: failed 终态缺少公开故障报告，或 Cognitive 自主失败缺少具体原因。
        """

        if change.activity_id in self._terminal_by_id:
            return False
        if change.change == "failed" and change.failure is None:
            raise ActivityInvariantViolation(
                "failed terminal requires a public failure report"
            )
        if change.change == "completed" and change.completion_result is None:
            raise ActivityInvariantViolation(
                "completed terminal requires a completion result"
            )
        requires_failure_reason = (
            change.change == "failed"
            and change.failure is not None
            and change.failure.code == "cognitive_task_failed"
        )
        has_failure_reason = (
            isinstance(change.failure_reason, str)
            and bool(change.failure_reason.strip())
        )
        if requires_failure_reason != has_failure_reason:
            raise ActivityInvariantViolation(
                "cognitive business failure requires one specific reason"
            )
        now_text = self._now_text()
        record = self._active_by_id.pop(change.activity_id, None)
        if record is None:
            # TaskResult terminal 可先于 started 抵达。先投影公共终态让任务中心可见；
            # 结果卡、Notice 和材料释放仍由独立的交付链路等待完整证明。
            record = _ActivityRecord(
                activity_id=change.activity_id,
                kind=change.kind,
                type_label=change.type_label,
                control_source=change.control_source,
                runtime_epoch=change.runtime_epoch,
                goal=change.goal,
                goal_summary=summarize_activity_goal(change.goal),
                status=change.change,
                started_at=None,
                updated_at=now_text,
            )
        else:
            record.status = change.change
            record.updated_at = now_text
            if change.goal:
                record.goal = change.goal
                record.goal_summary = summarize_activity_goal(change.goal)
            if change.type_label:
                record.type_label = change.type_label
        record.finished_at = now_text
        record.completion_result = (
            change.completion_result if change.change == "completed" else None
        )
        record.failure = change.failure if change.change == "failed" else None
        record.failure_reason = (
            change.failure_reason if change.change == "failed" else None
        )
        # 终态必须封口最后阶段；取消只保留当时进度，不伪造故障报告。
        self._close_open_stage(record, now_text)
        self._terminal_by_id[record.activity_id] = record
        self._bound_terminal_records()
        return True

    def _apply_work_plan(self, change: ActivitySourceChange) -> bool:
        """在已持锁状态下提交更高版本的 Background Agent 全量计划。

        Args:
            change: 已通过来源适配器校验的 WorkPlan 变化。

        Returns:
            更高版本计划写入时返回真；重复、倒退或终态后事件返回假。

        Raises:
            ActivityInvariantViolation: 活跃变化缺少计划快照。
        """

        if change.activity_id in self._terminal_by_id:
            return False
        record = self._active_by_id.get(change.activity_id)
        if record is None:
            return False
        if change.work_plan is None:
            raise ActivityInvariantViolation("work plan change requires a full snapshot")
        current_revision = (
            record.work_plan.revision
            if record.work_plan is not None
            else 0
        )
        if change.work_plan.revision <= current_revision:
            return False
        record.work_plan = change.work_plan
        record.updated_at = self._now_text()
        return True

    def _apply_turn_output(self, change: ActivitySourceChange) -> bool:
        """保存一条按序到达的后台 Agent 纯文本 Turn。"""

        if change.activity_id in self._terminal_by_id:
            return False
        record = self._active_by_id.get(change.activity_id)
        if record is None or change.turn_index is None:
            return False
        if change.turn_index <= 0 or not change.content_text.strip() or not change.created_at.strip():
            return False
        if record.turn_outputs and change.turn_index <= record.turn_outputs[-1].turn_index:
            return False
        record.turn_outputs.append(
            ActivityTurnOutput(
                turn_index=change.turn_index,
                content_text=change.content_text,
                created_at=change.created_at,
            )
        )
        record.updated_at = self._now_text()
        return True

    def _apply_waiting_user(self, change: ActivitySourceChange) -> bool:
        """在不改变执行阶段的前提下进入或退出等待用户状态。"""

        record = self._active_by_id.get(change.activity_id)
        if record is None:
            return False
        request_id = change.interaction_request_id.strip()
        if not request_id:
            return False
        if change.change == "waiting_user_entered":
            if record.status not in {"running", "waiting_user"}:
                return False
            if request_id in record.pending_interaction_ids:
                return False
            record.pending_interaction_ids.add(request_id)
            record.status = "waiting_user"
            record.updated_at = self._now_text()
            return True
        if record.status != "waiting_user" or request_id not in record.pending_interaction_ids:
            return False
        record.pending_interaction_ids.remove(request_id)
        if not record.pending_interaction_ids:
            record.status = "running"
        record.updated_at = self._now_text()
        return True

    def _close_open_stage(self, record: _ActivityRecord, now_text: str) -> None:
        """关闭记录中尚未结束的最后一个阶段。

        Args:
            record: 需要封口阶段时间线的内部记录。
            now_text: 本次状态变化统一使用的 UTC 时间。
        """

        if record.stage_timeline and record.stage_timeline[-1].finished_at is None:
            record.stage_timeline[-1] = replace(
                record.stage_timeline[-1],
                finished_at=now_text,
            )

    def _bound_terminal_records(self) -> None:
        """按终态到达顺序淘汰超过内存上限的记录。"""

        while len(self._terminal_by_id) > self._max_terminal_records:
            self._terminal_by_id.popitem(last=False)

    def _snapshot(self, *, recent_limit: int) -> ActivitySnapshot:
        """在锁内复制、排序并生成对外快照。

        Args:
            recent_limit: 本次快照包含的最大终态数量。

        Returns:
            带当前运行编号和 revision 的不可变公开快照。
        """

        with self._lock:
            active = tuple(
                record.compact()
                for record in sorted(
                    self._active_by_id.values(),
                    key=lambda item: (item.started_at or "", item.activity_id),
                )
            )
            recent_records = sorted(
                self._terminal_by_id.values(),
                key=lambda item: (item.finished_at or "", item.activity_id),
                reverse=True,
            )[:recent_limit]
            return ActivitySnapshot(
                revision=self._revision,
                runtime_id=self._runtime_id,
                active=active,
                recent=tuple(record.compact() for record in recent_records),
            )

    def _now_text(self) -> str:
        """生成带时区的 UTC ISO-8601 时间。

        Returns:
            可直接写入公开 Activity 字段的 UTC 时间文本。
        """

        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(UTC).isoformat()
