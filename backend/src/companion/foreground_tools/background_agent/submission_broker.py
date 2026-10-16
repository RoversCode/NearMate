"""等待主进程完成工作交互抢答，并返回后台执行器的真实接收结果。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, RLock
from typing import Literal
from uuid import uuid4

from companion.background_agent.contracts import AgentMaterial
from companion.llm.foreground_models import ForegroundEvent
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import (
    BackgroundAgentInteractionAnswerCommand,
    BackgroundAgentInteractionSubmissionRejectedCommand,
)


_SUBMISSION_POLL_INTERVAL_SECONDS = 0.05
logger = get_backend_logger(__name__)


@dataclass(slots=True)
class _PendingWorkInteractionSubmission:
    """保存一笔仍在等待主进程和后台执行器确认的提交。

    Args:
        submission_id: 本次自然语言回答的唯一编号。
        request_id: 用户正在回答的工作交互请求编号。
        activity_id: 原后台工作的 Activity 编号。
        outcome: 用户明确表达的允许、拒绝、资料回答或取消。
        answers: 资料问题原文到用户答案的映射。
        user_text: 触发 Foreground 工具的用户原话。
        materials: 本轮随回答补充的正式材料。
        turn_material_context_id: 材料生命周期使用的当前轮上下文编号。
        completed: 唤醒正在等待工具结果的 LLM worker。
        delivering: 主进程命令是否已经开始交给后台执行器。
        result: 后台真实接收、抢答失败或超时后的稳定结果。
    """

    submission_id: str
    request_id: str
    activity_id: str
    outcome: Literal["allow", "deny", "answer", "cancel"]
    answers: dict[str, str]
    user_text: str
    materials: tuple[AgentMaterial, ...]
    turn_material_context_id: str | None
    completed: Event = field(default_factory=Event)
    delivering: bool = False
    result: dict[str, object] | None = None


class WorkInteractionSubmissionBroker:
    """让自然语言回答等待主进程 Registry 和后台执行器的权威回执。

    Broker 不判断请求是否还能回答，也不保存产品交互状态。它只负责把一次工具调用
    挂起，等待主进程完成原子 claim，再以后台执行器的布尔返回值结束等待。

    Args:
        event_notifier: 新内部事件入队后唤醒助手子进程主循环的回调。
        timeout_seconds: 一笔提交等待权威回执的最长秒数。
        submission_id_factory: 生成提交编号的函数。
        monotonic_now: 计算等待截止时间的单调时钟。
    """

    def __init__(
        self,
        *,
        event_notifier: Callable[[], None] | None = None,
        timeout_seconds: float = 5.0,
        submission_id_factory: Callable[[], str] = lambda: uuid4().hex,
        monotonic_now: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化空的进程内等待表和事件队列。"""

        if timeout_seconds <= 0:
            raise ValueError("工作交互提交等待时间必须大于零")
        self._event_notifier = event_notifier
        self._timeout_seconds = timeout_seconds
        self._submission_id_factory = submission_id_factory
        self._monotonic_now = monotonic_now
        self._pending: dict[str, _PendingWorkInteractionSubmission] = {}
        self._events: Queue[ForegroundEvent] = Queue()
        self._lock = RLock()

    def submit_and_wait(
        self,
        *,
        request_id: str,
        activity_id: str,
        outcome: Literal["allow", "deny", "answer", "cancel"],
        answers: dict[str, str],
        user_text: str,
        materials: tuple[AgentMaterial, ...],
        turn_material_context_id: str | None,
        cancellation_event: Event | None,
    ) -> dict[str, object]:
        """发起一次主进程裁决，并同步等待后台是否真实接收。

        Args:
            request_id: 当前工作交互请求编号。
            activity_id: 请求所属的后台 Activity 编号。
            outcome: 用户明确表达的回答类型。
            answers: 已完成基础类型校验的逐问题答案。
            user_text: 用户本轮原始话语。
            materials: 本轮新增正式材料。
            turn_material_context_id: 材料生命周期使用的上下文编号。
            cancellation_event: 当前 generation 的取消信号。

        Returns:
            `accepted`、`already_resolved`、`submission_timeout` 或
            `background_unavailable` 对应的安全工具结果。
        """

        submission_id = self._submission_id_factory().strip()
        if not submission_id:
            return _background_unavailable_result(request_id, activity_id)
        pending = _PendingWorkInteractionSubmission(
            submission_id=submission_id,
            request_id=request_id,
            activity_id=activity_id,
            outcome=outcome,
            answers=dict(answers),
            user_text=user_text,
            materials=tuple(materials),
            turn_material_context_id=(
                turn_material_context_id.strip()
                if isinstance(turn_material_context_id, str)
                and turn_material_context_id.strip()
                else None
            ),
        )
        with self._lock:
            if submission_id in self._pending:
                return _background_unavailable_result(request_id, activity_id)
            self._pending[submission_id] = pending

        self._emit(
            "llm.foreground.work_interaction_submission_requested",
            _submission_event_payload(pending),
        )
        deadline = self._monotonic_now() + self._timeout_seconds
        while True:
            remaining_seconds = deadline - self._monotonic_now()
            if remaining_seconds <= 0 or (
                cancellation_event is not None and cancellation_event.is_set()
            ):
                return self._abandon(pending)
            pending.completed.wait(
                timeout=min(
                    _SUBMISSION_POLL_INTERVAL_SECONDS,
                    remaining_seconds,
                )
            )
            with self._lock:
                current = self._pending.get(submission_id)
                if current is not pending or pending.result is None:
                    continue
                result = dict(pending.result)
                self._pending.pop(submission_id, None)
                return result

    def apply_answer_command(
        self,
        command: BackgroundAgentInteractionAnswerCommand,
        *,
        submit_answer: Callable[[BackgroundAgentInteractionAnswerCommand], bool],
    ) -> bool:
        """把主进程已 claim 的命令交给后台执行器，并唤醒等待者。

        Args:
            command: 带 submission_id 的后台交互回答命令。
            submit_answer: 真正调用 `BackgroundAgentActivityExecutor` 的函数。

        Returns:
            后台执行器真实接收本次答案时返回真；迟到或拒收时返回假。
        """

        submission_id = command.submission_id or ""
        with self._lock:
            pending = self._pending.get(submission_id)
            if (
                pending is None
                or pending.request_id != command.request_id.strip()
                or pending.result is not None
                or pending.delivering
            ):
                return False
            pending.delivering = True

        try:
            accepted = submit_answer(command)
        except Exception:
            logger.exception(
                "工作交互回答提交给后台执行器失败 submission_id=%s request_id=%s",
                submission_id,
                pending.request_id,
            )
            accepted = False

        with self._lock:
            current = self._pending.get(submission_id)
            if current is not pending or pending.result is not None:
                return False
            pending.result = (
                _accepted_result(pending)
                if accepted
                else _background_unavailable_result(
                    pending.request_id,
                    pending.activity_id,
                )
            )
            pending.completed.set()

        event_type = (
            "llm.foreground.work_interaction_submission_completed"
            if accepted
            else "llm.foreground.work_interaction_submission_failed"
        )
        event_payload = _submission_event_payload(pending)
        event_payload["receipt"] = (
            "accepted" if accepted else "background_unavailable"
        )
        event_payload["state"] = "accepted" if accepted else "rejected"
        event_payload["name"] = "respond_to_work_interaction"
        self._emit(event_type, event_payload)
        return accepted

    def reject_submission(
        self,
        command: BackgroundAgentInteractionSubmissionRejectedCommand,
    ) -> bool:
        """应用主进程在 Registry claim 阶段给出的拒绝事实。

        Args:
            command: 带 submission_id 和稳定拒绝原因的控制命令。

        Returns:
            命令仍对应当前等待项并成功唤醒时返回真。
        """

        with self._lock:
            pending = self._pending.get(command.submission_id)
            if (
                pending is None
                or pending.request_id != command.request_id.strip()
                or pending.result is not None
                or pending.delivering
            ):
                return False
            receipt = (
                "already_resolved"
                if command.reason == "already_resolved"
                else "background_unavailable"
            )
            pending.result = _rejected_result(
                pending.request_id,
                pending.activity_id,
                receipt,
            )
            pending.completed.set()

        payload = _submission_event_payload(pending)
        payload["receipt"] = receipt
        payload["state"] = "rejected"
        payload["name"] = "respond_to_work_interaction"
        self._emit(
            "llm.foreground.work_interaction_submission_failed",
            payload,
        )
        return True

    def drain_events(self, limit: int = 100) -> list[ForegroundEvent]:
        """非阻塞读取已经产生的内部提交事件。

        Args:
            limit: 本次最多读取的事件数量。

        Returns:
            保持产生顺序的 Foreground 内部事件。
        """

        if limit <= 0:
            return []
        events: list[ForegroundEvent] = []
        for _ in range(limit):
            try:
                events.append(self._events.get_nowait())
            except Empty:
                break
        return events

    def _abandon(
        self,
        pending: _PendingWorkInteractionSubmission,
    ) -> dict[str, object]:
        """让超时或已取消 generation 的等待项失效。"""

        with self._lock:
            current = self._pending.get(pending.submission_id)
            if current is not pending:
                if pending.result is not None:
                    return dict(pending.result)
                return _submission_timeout_result(
                    pending.request_id,
                    pending.activity_id,
                )
            if pending.result is not None:
                result = dict(pending.result)
                self._pending.pop(pending.submission_id, None)
                return result
            self._pending.pop(pending.submission_id, None)

        payload = _submission_event_payload(pending)
        payload["receipt"] = "submission_timeout"
        payload["state"] = "rejected"
        payload["name"] = "respond_to_work_interaction"
        self._emit(
            "llm.foreground.work_interaction_submission_abandoned",
            payload,
        )
        return _submission_timeout_result(
            pending.request_id,
            pending.activity_id,
        )

    def _emit(self, event_type: str, payload: dict[str, object]) -> None:
        """写入一条内部事件，并唤醒负责跨进程转发的主循环。"""

        self._events.put(ForegroundEvent(type=event_type, payload=payload))
        if self._event_notifier is not None:
            self._event_notifier()


def _submission_event_payload(
    pending: _PendingWorkInteractionSubmission,
) -> dict[str, object]:
    """构造主进程裁决和材料登记共用的内部载荷。"""

    payload: dict[str, object] = {
        "submission_id": pending.submission_id,
        "request_id": pending.request_id,
        "activity_id": pending.activity_id,
        "outcome": pending.outcome,
        "answers": dict(pending.answers),
        "user_text": pending.user_text,
        "materials": [material.to_payload() for material in pending.materials],
    }
    if pending.turn_material_context_id:
        payload["turn_material_context_id"] = pending.turn_material_context_id
    return payload


def _accepted_result(
    pending: _PendingWorkInteractionSubmission,
) -> dict[str, object]:
    """构造只在后台真实接收后返回的工具成功事实。"""

    return {
        "state": "accepted",
        "request_id": pending.request_id,
        "activity_id": pending.activity_id,
        "outcome": pending.outcome,
        "answers": dict(pending.answers),
        "safe_message": "本次回应已被当前工作接收。",
    }


def _rejected_result(
    request_id: str,
    activity_id: str,
    reason: str,
) -> dict[str, object]:
    """把主进程或后台拒绝收敛为可安全解释的工具结果。"""

    safe_messages = {
        "already_resolved": "这条工作请求已被其它操作先处理，本次回应没有提交成功。",
        "background_unavailable": "后台当前无法接收这次回应，本次回应没有提交成功。",
    }
    return {
        "state": "rejected",
        "request_id": request_id,
        "activity_id": activity_id,
        "reason": reason,
        "safe_message": safe_messages[reason],
    }


def _submission_timeout_result(
    request_id: str,
    activity_id: str,
) -> dict[str, object]:
    """构造五秒内未获得权威确认时的安全事实。"""

    return {
        "state": "rejected",
        "request_id": request_id,
        "activity_id": activity_id,
        "reason": "submission_timeout",
        "safe_message": "这次回应没有在时限内确认，后台未收到本次回应。",
    }


def _background_unavailable_result(
    request_id: str,
    activity_id: str,
) -> dict[str, object]:
    """构造后台执行器不存在或拒收时的安全事实。"""

    return _rejected_result(
        request_id,
        activity_id,
        "background_unavailable",
    )
