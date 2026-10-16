"""统一处理鼠标和自然语言提交的工作交互回答。"""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Protocol

from companion.background_agent.contracts import AgentMaterial
from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplication,
    AgentInteractionEventApplyResult,
    BackgroundAgentInteractionRegistry,
    InteractionAnswerValidationError,
    InteractionRequestConflictError,
    InteractionRequestNotFoundError,
    PendingAgentInteraction,
)
from companion.realtime.assistant_events import AssistantControlCommand


_SUBMISSION_REQUESTED = "llm.foreground.work_interaction_submission_requested"
_SUBMISSION_COMPLETED = "llm.foreground.work_interaction_submission_completed"
_SUBMISSION_FAILED = "llm.foreground.work_interaction_submission_failed"
_SUBMISSION_ABANDONED = "llm.foreground.work_interaction_submission_abandoned"


class WorkInteractionRuntime(Protocol):
    """定义主进程向当前助手运行时提交交互回答所需的最小接口。"""

    def answer_background_agent_interaction(
        self,
        *,
        request_id: str,
        outcome: str,
        answers: dict[str, str] | None = None,
        reason: str = "",
        materials: tuple[AgentMaterial, ...] = (),
        user_text: str = "",
        submission_id: str | None = None,
        runtime_epoch: int | None = None,
    ) -> object:
        """把已经通过 Registry claim 的回答送入指定运行时世代。"""

    def reject_background_agent_interaction_submission(
        self,
        *,
        submission_id: str,
        request_id: str,
        reason: str,
        runtime_epoch: int | None = None,
    ) -> AssistantControlCommand:
        """把 Registry 拒绝事实送回指定运行时世代的等待项。"""


class BackgroundAgentInteractionService:
    """让鼠标和语音回答共用同一个抢答、世代校验和运行时出口。

    Args:
        registry: 当前主进程唯一的待处理工作交互账本。
        runtime_provider: 读取当前可接收控制命令的助手运行时。
        current_runtime_epoch: 读取当前已发布助手运行时世代。
    """

    def __init__(
        self,
        *,
        registry: BackgroundAgentInteractionRegistry,
        runtime_provider: Callable[[], WorkInteractionRuntime],
        current_runtime_epoch: Callable[[], int],
    ) -> None:
        """保存统一回答链所需的三个稳定依赖。"""

        self.registry = registry
        self._runtime_provider = runtime_provider
        self._current_runtime_epoch = current_runtime_epoch
        self._claimed_submissions: dict[str, PendingAgentInteraction] = {}
        self._submission_lock = RLock()

    def apply_runtime_event(
        self,
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> AgentInteractionEventApplyResult:
        """应用后台交互事件，并裁决自然语言回答的权威提交时序。

        Args:
            event_type: 助手子进程回流的事件名称。
            payload: 事件公开载荷；回答细节位于内部工具结果中。
            runtime_epoch: 产生事件的助手运行时世代。

        Returns:
            Registry 接受事件后的 Control 投影；无效或迟到事件返回空。
        """

        if event_type == _SUBMISSION_REQUESTED:
            return self._apply_submission_requested(payload, runtime_epoch)
        if event_type == _SUBMISSION_COMPLETED:
            return self._apply_submission_completed(payload)
        if event_type in {_SUBMISSION_FAILED, _SUBMISSION_ABANDONED}:
            return self._apply_submission_failure(payload)
        return self.registry.apply_runtime_event(event_type, payload, runtime_epoch)

    def _apply_submission_requested(
        self,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> AgentInteractionEventApplication:
        """原子 claim 一次自然语言回答，并把带编号命令送回原运行时。

        Args:
            payload: 子进程冻结的 request、答案、原话和新增材料。
            runtime_epoch: 产生这笔 submission 的助手运行时世代。

        Returns:
            内部事件已消费的空公开投影。
        """

        submission_id = _text(payload.get("submission_id"))
        request_id = _text(payload.get("request_id"))
        activity_id = _text(payload.get("activity_id"))
        pending = self.registry.pending(request_id)
        if (
            not submission_id
            or pending is None
            or pending.activity_id != activity_id
        ):
            self._reject_submission(
                submission_id=submission_id,
                request_id=request_id,
                reason="already_resolved",
                runtime_epoch=runtime_epoch,
            )
            return AgentInteractionEventApplication()

        outcome = _text(payload.get("outcome"))
        answers = _string_mapping(payload.get("answers"))
        materials = _materials_from_payload(payload.get("materials"))
        if answers is None or materials is None:
            self._reject_submission(
                submission_id=submission_id,
                request_id=request_id,
                reason="already_resolved",
                runtime_epoch=runtime_epoch,
            )
            return AgentInteractionEventApplication()

        try:
            claimed, normalized_answers = self.registry.claim_answer(
                request_id=pending.request_id,
                kind=pending.kind,
                outcome=outcome,  # type: ignore[arg-type]
                answers=answers,
                current_runtime_epoch=runtime_epoch,
            )
        except (
            InteractionRequestNotFoundError,
            InteractionRequestConflictError,
            InteractionAnswerValidationError,
            ValueError,
        ):
            self._reject_submission(
                submission_id=submission_id,
                request_id=request_id,
                reason="already_resolved",
                runtime_epoch=runtime_epoch,
            )
            return AgentInteractionEventApplication()

        with self._submission_lock:
            duplicate_submission = submission_id in self._claimed_submissions
            if not duplicate_submission:
                self._claimed_submissions[submission_id] = claimed
        if duplicate_submission:
            self.registry.release_answer_claim(claimed)
            self._reject_submission(
                submission_id=submission_id,
                request_id=request_id,
                reason="already_resolved",
                runtime_epoch=runtime_epoch,
            )
            return AgentInteractionEventApplication()

        try:
            self._runtime_provider().answer_background_agent_interaction(
                request_id=claimed.request_id,
                outcome=outcome,
                answers=normalized_answers,
                reason="",
                materials=materials,
                user_text=_text(payload.get("user_text")),
                submission_id=submission_id,
                runtime_epoch=claimed.runtime_epoch,
            )
        except Exception:
            self._release_submission_claim(submission_id)
            self._reject_submission(
                submission_id=submission_id,
                request_id=request_id,
                reason="background_unavailable",
                runtime_epoch=runtime_epoch,
            )
        return AgentInteractionEventApplication()

    def _apply_submission_completed(
        self,
        payload: dict[str, object],
    ) -> AgentInteractionEventApplyResult:
        """在后台真实接收后收起可操作卡片，并保留 answering 状态。

        Args:
            payload: 子进程返回的 accepted 回执。

        Returns:
            可广播给 Desktop 的 processing 事件；身份不匹配时返回空。
        """

        submission_id = _text(payload.get("submission_id"))
        claimed = self._take_submission_claim(
            submission_id=submission_id,
            request_id=_text(payload.get("request_id")),
            activity_id=_text(payload.get("activity_id")),
        )
        if claimed is None or payload.get("receipt") != "accepted":
            return None
        return AgentInteractionEventApplication(
            control_event={
                "type": "background_agent.interaction_submitted",
                "payload": {
                    "request_id": claimed.request_id,
                    "activity_id": claimed.activity_id,
                },
            }
        )

    def _apply_submission_failure(
        self,
        payload: dict[str, object],
    ) -> AgentInteractionEventApplication:
        """在超时、取消或后台拒收时只释放属于本 submission 的 claim。

        Args:
            payload: 子进程返回的失败或放弃回执。

        Returns:
            内部事件已消费的空公开投影。
        """

        claimed = self._take_submission_claim(
            submission_id=_text(payload.get("submission_id")),
            request_id=_text(payload.get("request_id")),
            activity_id=_text(payload.get("activity_id")),
        )
        if claimed is not None:
            self.registry.release_answer_claim(claimed)
        return AgentInteractionEventApplication()

    def invalidate_runtime_epoch(self, runtime_epoch: int) -> list[dict[str, object]]:
        """清理失效运行时的待答请求和未完成 submission 账目。

        Args:
            runtime_epoch: 已经停止或被热替换的助手运行时世代。

        Returns:
            需要广播给 Desktop 的交互卡收口事件。
        """

        with self._submission_lock:
            stale_submission_ids = [
                submission_id
                for submission_id, claimed in self._claimed_submissions.items()
                if claimed.runtime_epoch == runtime_epoch
            ]
            for submission_id in stale_submission_ids:
                self._claimed_submissions.pop(submission_id, None)
        return self.registry.invalidate_runtime_epoch(runtime_epoch)

    def _take_submission_claim(
        self,
        *,
        submission_id: str,
        request_id: str,
        activity_id: str,
    ) -> PendingAgentInteraction | None:
        """只取走身份完全匹配的 claim，避免迟到事件释放其它回答。"""

        with self._submission_lock:
            claimed = self._claimed_submissions.get(submission_id)
            if (
                claimed is None
                or claimed.request_id != request_id
                or claimed.activity_id != activity_id
            ):
                return None
            return self._claimed_submissions.pop(submission_id)

    def _release_submission_claim(self, submission_id: str) -> bool:
        """释放指定 submission 持有的 Registry 回答权。"""

        with self._submission_lock:
            claimed = self._claimed_submissions.pop(submission_id, None)
        if claimed is None:
            return False
        return self.registry.release_answer_claim(claimed)

    def _reject_submission(
        self,
        *,
        submission_id: str,
        request_id: str,
        reason: str,
        runtime_epoch: int,
    ) -> None:
        """尽力把 claim 失败事实送回原子进程等待项。"""

        if not submission_id or not request_id:
            return
        try:
            self._runtime_provider().reject_background_agent_interaction_submission(
                submission_id=submission_id,
                request_id=request_id,
                reason=reason,
                runtime_epoch=runtime_epoch,
            )
        except Exception:
            # 子进程不可达时 Broker 会在五秒后自行超时，主进程不能伪造成功回执。
            return

    def submit_permission_decision(
        self,
        *,
        request_id: str,
        outcome: str,
    ) -> PendingAgentInteraction:
        """处理 Desktop 鼠标提交的一次明确权限决定。

        Args:
            request_id: 卡片对应的工作交互请求编号。
            outcome: 用户明确选择的允许或拒绝。

        Returns:
            已经进入 answering 状态的不可变请求快照。
        """

        pending = self.registry.pending(request_id)
        if pending is None:
            raise InteractionRequestNotFoundError(request_id)
        if pending.kind != "permission":
            raise InteractionRequestConflictError("interaction kind mismatch")
        if outcome not in {"allow", "deny"}:
            raise InteractionAnswerValidationError(
                "permission outcome must be allow or deny"
            )
        return self._claim_and_submit(
            pending=pending,
            outcome=outcome,
            answers={},
            reason="",
            materials=(),
            user_text="",
            runtime_epoch=self._current_runtime_epoch(),
        )

    def _claim_and_submit(
        self,
        *,
        pending: PendingAgentInteraction,
        outcome: str,
        answers: dict[str, str],
        reason: str,
        materials: tuple[AgentMaterial, ...],
        user_text: str,
        runtime_epoch: int,
    ) -> PendingAgentInteraction:
        """在同一条路径中完成 Registry claim 和 runtime command 提交。"""

        if outcome not in {"allow", "deny", "answer", "cancel"}:
            raise ValueError("invalid work interaction outcome")
        claimed, normalized_answers = self.registry.claim_answer(
            request_id=pending.request_id,
            kind=pending.kind,
            outcome=outcome,  # type: ignore[arg-type]
            answers=answers,
            current_runtime_epoch=runtime_epoch,
        )
        try:
            self._runtime_provider().answer_background_agent_interaction(
                request_id=claimed.request_id,
                outcome=outcome,
                answers=normalized_answers,
                reason=reason,
                materials=materials,
                user_text=user_text,
                submission_id=None,
                runtime_epoch=claimed.runtime_epoch,
            )
        except Exception:
            self.registry.release_answer_claim(claimed)
            raise
        return claimed


def _text(value: object) -> str:
    """把内部事件字段收敛成去除首尾空白的文本。"""

    return value.strip() if isinstance(value, str) else ""


def _string_mapping(value: object) -> dict[str, str] | None:
    """只接受字符串键值组成的回答映射。"""

    if not isinstance(value, dict):
        return None
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        return None
    return dict(value)


def _materials_from_payload(value: object) -> tuple[AgentMaterial, ...] | None:
    """把 Foreground 内部事件中的材料白名单还原为领域对象。"""

    if not isinstance(value, list):
        return None
    materials: list[AgentMaterial] = []
    for item in value:
        try:
            materials.append(AgentMaterial.from_payload(item))
        except ValueError:
            return None
    return tuple(materials)
