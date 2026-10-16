"""让助手子进程在真实工具提交前同步等待主进程授权。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite
from threading import Event, RLock
from time import monotonic
from typing import Literal
from uuid import uuid4

from companion.foreground_self_reflection.contracts import (
    SelfReflectionToolCommitDecision,
    SelfReflectionToolCommitDecisionCommand,
)
from companion.foreground_self_reflection.models import (
    SELF_REFLECTION_DECISION_BUDGET_SECONDS,
)
from companion.realtime.assistant_events import RuntimeEvent


@dataclass(slots=True)
class _PendingToolCommit:
    """保存一笔仍在等待主进程批准的工具提交。

    Args:
        commit_request_id: 本次同步请求的稳定编号。
        self_reflection_id: 本次自我反思编号。
        source_activity_id: 被恢复的 Cognitive Activity 编号。
        runtime_epoch: 请求所属 Assistant Runtime 世代。
        tool_call_id: 模型产生的唯一工具调用编号。
        tool_name: 本次实际选择的工具名。
        execution_channel: Inline 或 Background 提交路径。
        canonical_event: Background 已准备好的 canonical 事实；Inline 为空。
        forwarded_to_main_process: 请求是否已原子写入跨进程事件队列。
        completed: 主进程回执到达时唤醒等待线程的信号。
        decision: 已通过完整身份校验的唯一回执。
    """

    commit_request_id: str
    self_reflection_id: str
    source_activity_id: str
    runtime_epoch: int
    tool_call_id: str
    tool_name: str
    execution_channel: Literal["inline", "background"]
    canonical_event: RuntimeEvent | None
    forwarded_to_main_process: bool = False
    completed: Event = field(default_factory=Event)
    decision: SelfReflectionToolCommitDecision | None = None


class SelfReflectionToolCommitBroker:
    """持有工具提交 RPC 的等待、重试、取消和迟到回执状态。

    Args:
        emit_event: 把提交请求写入助手子进程本地 Runtime Event Queue 的同步端口。
        response_timeout_seconds: 主进程没有回执时最多等待的秒数。
        retry_interval_seconds: 使用同一请求身份重新发送的时间间隔。
        commit_request_id_factory: 生成稳定提交请求编号的函数。
        monotonic_now: 计算等待时限和重试节奏的单调时钟。

    Broker 不判断 Attention 是否有效，也不执行工具。它只保证同一请求在等待期间
    可以安全重发。Background 请求由子进程主循环原子写入跨进程事件队列后，提交
    裁决权才转交主进程，本地取消或 deadline 不能再删除 pending；Inline 请求没有
    预登记 canonical 事实，仍按本地取消和超时直接收口。
    """

    def __init__(
        self,
        *,
        emit_event: Callable[[RuntimeEvent], None],
        response_timeout_seconds: float = SELF_REFLECTION_DECISION_BUDGET_SECONDS,
        retry_interval_seconds: float = 0.2,
        commit_request_id_factory: Callable[[], str] = lambda: uuid4().hex,
        monotonic_now: Callable[[], float] = monotonic,
    ) -> None:
        """初始化空的 pending 表和可替换时间来源。

        Args:
            emit_event: 把请求写入子进程本地 Runtime Event Queue 的函数。
            response_timeout_seconds: 单笔同步授权的最长本地等待秒数。
            retry_interval_seconds: 主进程未回执时的稳定重发间隔。
            commit_request_id_factory: 生成提交请求编号的函数。
            monotonic_now: 返回单调时间的函数。
        """

        if not _positive_finite(response_timeout_seconds):
            raise ValueError("工具提交回执等待时间必须是大于零的有限数")
        if not _positive_finite(retry_interval_seconds):
            raise ValueError("工具提交重试间隔必须是大于零的有限数")
        self._emit_event = emit_event
        self._response_timeout_seconds = float(response_timeout_seconds)
        self._retry_interval_seconds = float(retry_interval_seconds)
        self._commit_request_id_factory = commit_request_id_factory
        self._monotonic_now = monotonic_now
        self._pending: dict[str, _PendingToolCommit] = {}
        self._lock = RLock()

    def request_and_wait(
        self,
        *,
        self_reflection_id: str,
        source_activity_id: str,
        runtime_epoch: int,
        tool_call_id: str,
        tool_name: str,
        execution_channel: Literal["inline", "background"],
        canonical_event: RuntimeEvent | None,
        deadline: float,
        cancellation_event: Event | None,
    ) -> SelfReflectionToolCommitDecision:
        """发送稳定请求，并等到批准、拒绝、取消或超时。

        Args:
            self_reflection_id: 本次自我反思编号。
            source_activity_id: 被恢复的 Cognitive Activity 编号。
            runtime_epoch: 请求所属 Assistant Runtime 世代。
            tool_call_id: 模型产生的唯一工具调用编号。
            tool_name: 本次实际选择的工具名。
            execution_channel: 工具走 Inline 还是 Background 路径。
            canonical_event: Background 已准备好的 canonical 事件；Inline 为空。
            deadline: Self-Reflection 从 Admission 起固定的绝对截止时间。
            cancellation_event: 热替换、关闭或 deadline 使用的 Recovery generation 信号。

        Returns:
            主进程回执，或本地取消、deadline、等待超时对应的稳定拒绝。
        """

        normalized_fields = tuple(
            value.strip() if isinstance(value, str) else ""
            for value in (
                self_reflection_id,
                source_activity_id,
                tool_call_id,
                tool_name,
            )
        )
        if any(not value for value in normalized_fields):
            return SelfReflectionToolCommitDecision(
                approved=False,
                reason="tool_commit_identity_invalid",
            )
        if type(runtime_epoch) is not int or runtime_epoch <= 0:
            return SelfReflectionToolCommitDecision(
                approved=False,
                reason="tool_commit_identity_invalid",
            )
        if execution_channel not in {"inline", "background"}:
            return SelfReflectionToolCommitDecision(
                approved=False,
                reason="tool_commit_identity_invalid",
            )
        if not _positive_finite(deadline):
            return SelfReflectionToolCommitDecision(
                approved=False,
                reason="tool_commit_identity_invalid",
            )
        if not _canonical_event_matches(
            canonical_event,
            execution_channel=execution_channel,
            runtime_epoch=runtime_epoch,
        ):
            return SelfReflectionToolCommitDecision(
                approved=False,
                reason="canonical_tool_event_invalid",
            )

        commit_request_id = self._commit_request_id_factory().strip()
        if not commit_request_id:
            return SelfReflectionToolCommitDecision(
                approved=False,
                reason="tool_commit_identity_invalid",
            )
        pending = _PendingToolCommit(
            commit_request_id=commit_request_id,
            self_reflection_id=normalized_fields[0],
            source_activity_id=normalized_fields[1],
            runtime_epoch=runtime_epoch,
            tool_call_id=normalized_fields[2],
            tool_name=normalized_fields[3],
            execution_channel=execution_channel,
            canonical_event=canonical_event,
        )
        with self._lock:
            if commit_request_id in self._pending:
                return SelfReflectionToolCommitDecision(
                    approved=False,
                    reason="tool_commit_identity_conflict",
                )
            self._pending[commit_request_id] = pending

        local_deadline = min(
            float(deadline),
            self._monotonic_now() + self._response_timeout_seconds,
        )
        next_emit_at = self._monotonic_now()
        while True:
            decision = self._current_decision(pending)
            if decision is not None:
                return decision

            main_process_owns_background_commit = self._is_forwarded(pending)

            # Background 请求一旦进入跨进程事件队列，主进程就可能已经在登记
            # canonical 事实。此后只能等主进程批准或拒绝，子进程不能再单方面回滚。
            if not main_process_owns_background_commit:
                if cancellation_event is not None and cancellation_event.is_set():
                    abandoned = self._abandon(
                        pending,
                        reason="tool_commit_cancelled",
                    )
                    if abandoned is not None:
                        return abandoned
                    continue

            current_time = self._monotonic_now()
            if (
                not main_process_owns_background_commit
                and current_time >= local_deadline
            ):
                reason = (
                    "reflection_deadline_exceeded"
                    if current_time >= float(deadline)
                    else "tool_commit_authorization_timeout"
                )
                abandoned = self._abandon(pending, reason=reason)
                if abandoned is not None:
                    return abandoned
                continue

            if current_time >= next_emit_at:
                self._emit_request(
                    pending,
                    produced_at=current_time,
                )
                next_emit_at = current_time + self._retry_interval_seconds
            if self._is_forwarded(pending):
                wait_seconds = self._retry_interval_seconds
            else:
                wait_seconds = min(
                    self._retry_interval_seconds,
                    max(0.0, local_deadline - self._monotonic_now()),
                )
            pending.completed.wait(timeout=wait_seconds)

    def forward_request_to_main(
        self,
        event: RuntimeEvent,
        *,
        send_event: Callable[[RuntimeEvent], None],
    ) -> bool:
        """把本地提交请求与跨进程写入合成一个原子所有权交接。

        Args:
            event: 子进程主循环从本地 FIFO 取出的提交请求。
            send_event: 把事件写入主进程跨进程队列的同步端口。

        Returns:
            事件身份仍有效且已经写入跨进程队列时返回真；请求已本地失效、
            身份不匹配或队列写入失败时返回假。
        """

        payload = event.payload if isinstance(event.payload, dict) else {}
        commit_request_id = payload.get("commit_request_id")
        if not isinstance(commit_request_id, str) or not commit_request_id.strip():
            return False
        with self._lock:
            pending = self._pending.get(commit_request_id.strip())
            if not _forwarded_event_matches_pending(event, pending):
                return False
            try:
                # 持有同一把锁完成 put 和 forwarded 标记：本地 deadline 要么先
                # 删除 pending，要么观察到主进程已接管，不存在“已发出却又回滚”。
                send_event(event)
            except Exception:
                return False
            pending.forwarded_to_main_process = True
            return True

    def shutdown(self, *, reason: str = "runtime_shutdown") -> int:
        """拒绝当前 Runtime 内全部等待，让 prepared reservation 有界回滚。

        Args:
            reason: 当前助手子进程终止使用的稳定原因。

        Returns:
            本次首次写入关闭决定并唤醒的 pending 数量。
        """

        normalized_reason = reason.strip() if isinstance(reason, str) else ""
        if not normalized_reason:
            raise ValueError("关闭原因不能为空")
        settled = 0
        with self._lock:
            for pending in self._pending.values():
                if pending.decision is not None:
                    continue
                pending.decision = SelfReflectionToolCommitDecision(
                    approved=False,
                    reason=normalized_reason,
                )
                pending.completed.set()
                settled += 1
        return settled

    def apply_decision(
        self,
        command: SelfReflectionToolCommitDecisionCommand,
    ) -> bool:
        """应用身份完全匹配且仍在等待的主进程回执。

        Args:
            command: 主进程在 Attention 授权和 canonical 登记后发回的决定。

        Returns:
            本次首次保存决定并唤醒等待线程时返回真；迟到或重复时返回假。
        """

        with self._lock:
            pending = self._pending.get(command.commit_request_id)
            if (
                pending is None
                or pending.decision is not None
                or pending.self_reflection_id != command.self_reflection_id
                or pending.source_activity_id != command.source_activity_id
                or pending.runtime_epoch != command.runtime_epoch
                or pending.tool_call_id != command.tool_call_id
                or pending.tool_name != command.tool_name
            ):
                return False
            pending.decision = SelfReflectionToolCommitDecision(
                approved=command.approved,
                reason=command.reason,
            )
            pending.completed.set()
            return True

    def _current_decision(
        self,
        pending: _PendingToolCommit,
    ) -> SelfReflectionToolCommitDecision | None:
        """取出首次回执并让后续迟到命令失去 pending 目标。

        Args:
            pending: 当前等待线程拥有的提交记录。

        Returns:
            已到达的回执；仍在等待时返回空。
        """

        with self._lock:
            current = self._pending.get(pending.commit_request_id)
            if current is not pending or pending.decision is None:
                return None
            decision = pending.decision
            self._pending.pop(pending.commit_request_id, None)
            return decision

    def _is_forwarded(self, pending: _PendingToolCommit) -> bool:
        """判断 Background 请求是否已经越过真实跨进程边界。

        Args:
            pending: 当前等待线程持有的提交记录。

        Returns:
            同一 pending 仍有效且跨进程写入已完成时返回真。
        """

        with self._lock:
            current = self._pending.get(pending.commit_request_id)
            return (
                current is pending
                and pending.forwarded_to_main_process
                and pending.execution_channel == "background"
            )

    def _abandon(
        self,
        pending: _PendingToolCommit,
        *,
        reason: str,
    ) -> SelfReflectionToolCommitDecision | None:
        """在取消或超时时删除 pending，同时尊重已经发生的所有权交接。

        Args:
            pending: 当前等待线程拥有的提交记录。
            reason: 本地取消、deadline 或等待超时原因。

        Returns:
            抢先到达的主进程决定或本地稳定拒绝；Background 已先跨进程
            转发时返回空，调用方必须继续等待权威决定。
        """

        with self._lock:
            current = self._pending.get(pending.commit_request_id)
            if current is pending and pending.decision is not None:
                decision = pending.decision
                self._pending.pop(pending.commit_request_id, None)
                return decision
            if current is pending and pending.forwarded_to_main_process:
                return None
            if current is pending:
                self._pending.pop(pending.commit_request_id, None)
        return SelfReflectionToolCommitDecision(approved=False, reason=reason)

    def _emit_request(
        self,
        pending: _PendingToolCommit,
        *,
        produced_at: float,
    ) -> None:
        """使用同一身份重发一条不产生副作用的授权请求。

        Args:
            pending: 要发送给主进程裁决的冻结提交记录。
            produced_at: 本次重试进入 Runtime Event Queue 的单调时间。

        """

        canonical_event = pending.canonical_event
        payload: dict[str, object] = {
            "commit_request_id": pending.commit_request_id,
            "self_reflection_id": pending.self_reflection_id,
            "source_activity_id": pending.source_activity_id,
            "tool_call_id": pending.tool_call_id,
            "tool_name": pending.tool_name,
            "execution_channel": pending.execution_channel,
            "canonical_event_payload": (
                deepcopy(canonical_event.payload)
                if canonical_event is not None
                else None
            ),
            "canonical_event_produced_at": (
                canonical_event.produced_at
                if canonical_event is not None
                else None
            ),
        }
        try:
            self._emit_event(
                RuntimeEvent(
                    type="foreground.self_reflection_tool_commit_requested",
                    source="assistant_runtime",
                    payload=payload,
                    produced_at=produced_at,
                    runtime_epoch=pending.runtime_epoch,
                )
            )
        except Exception:
            # 队列写入可能与 Runtime 关闭同时发生；保留 pending 后按同一身份重试，
            # 最终仍由取消、Self-Reflection deadline 或本地等待时限收口。
            return


def _forwarded_event_matches_pending(
    event: RuntimeEvent,
    pending: _PendingToolCommit | None,
) -> bool:
    """核对本地 FIFO 事件是否仍是同一笔冻结提交请求。

    Args:
        event: 子进程主循环准备写入跨进程队列的事件。
        pending: Broker 当前按提交编号查到的等待记录。

    Returns:
        外层身份、执行通道和 canonical 快照全部一致时返回真。
    """

    if (
        pending is None
        or pending.decision is not None
        or event.type != "foreground.self_reflection_tool_commit_requested"
        or event.source != "assistant_runtime"
        or event.runtime_epoch != pending.runtime_epoch
        or not isinstance(event.payload, dict)
    ):
        return False
    payload = event.payload
    canonical_event = pending.canonical_event
    return (
        payload.get("commit_request_id") == pending.commit_request_id
        and payload.get("self_reflection_id") == pending.self_reflection_id
        and payload.get("source_activity_id") == pending.source_activity_id
        and payload.get("tool_call_id") == pending.tool_call_id
        and payload.get("tool_name") == pending.tool_name
        and payload.get("execution_channel") == pending.execution_channel
        and payload.get("canonical_event_payload")
        == (
            canonical_event.payload
            if canonical_event is not None
            else None
        )
        and payload.get("canonical_event_produced_at")
        == (
            canonical_event.produced_at
            if canonical_event is not None
            else None
        )
    )


def _canonical_event_matches(
    event: RuntimeEvent | None,
    *,
    execution_channel: Literal["inline", "background"],
    runtime_epoch: int,
) -> bool:
    """核对执行通道是否携带了正确种类的 canonical 事件。

    Args:
        event: Coordinator 已准备好的可选 canonical 事件。
        execution_channel: 当前工具执行通道。
        runtime_epoch: 当前恢复所属 Assistant Runtime 世代。

    Returns:
        Inline 没有提前伪造完成事实，或 Background 事件身份正确时返回真。
    """

    if execution_channel == "inline":
        return event is None
    return (
        isinstance(event, RuntimeEvent)
        and event.type == "llm.foreground.tool_call_completed"
        and event.source == "assistant_runtime"
        and event.runtime_epoch == runtime_epoch
        and isinstance(event.payload, dict)
    )


def _positive_finite(value: object) -> bool:
    """判断一个值是否是严格大于零的有限实数。

    Args:
        value: 需要校验的时间或间隔。

    Returns:
        值可以安全用于单调时间计算时返回真。
    """

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
        and float(value) > 0
    )
