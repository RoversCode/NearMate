"""统一编排 Cognitive 与 Background Work 的 Activity 纠正事务。"""

from __future__ import annotations

from companion.materials.foreground_images import bound_image_evidence

from dataclasses import dataclass, replace
from threading import Lock
from typing import Any, Literal, Protocol, cast

from companion.background_agent.contracts import AgentMaterial
from companion.foreground_tools.activity_correction.contracts import (
    CorrectionCancellationOutcome,
    CorrectionEvidence,
    CorrectionReservation,
)
from companion.foreground_tools.cognitive.contracts import CognitiveExecutionPlan
from companion.foreground_tools.orchestration.transactions import (
    BackgroundActivityReservation,
)
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.foreground_tools.orchestration.contracts import ToolFollowupFact
from companion.observability.logging_config import get_backend_logger


ActivitySource = Literal["cognitive", "background_agent"]
logger = get_backend_logger(__name__)


class _CorrectionExecutor(Protocol):
    """约束纠正事务依赖的通用 Activity 执行接口。"""

    def is_activity_correctable(self, activity_id: str) -> bool: ...

    def cancel_for_correction(
        self,
        activity_id: str,
        *,
        timeout: float = 5.0,
    ) -> CorrectionCancellationOutcome: ...

    def commit_reserved_correction(self, activity_id: str) -> bool: ...

    def start_reserved_correction(self, activity_id: str) -> bool: ...

    def rollback_reserved_correction(self, activity_id: str) -> bool: ...

    def fail_reserved_correction(self, activity_id: str, *, reason: str) -> bool:
        """以不抛异常的幂等端口关闭已提交纠正预留。"""

        ...

class _CognitiveCorrectionExecutor(_CorrectionExecutor, Protocol):
    """约束纠正服务使用的 Cognitive 预留接口。"""

    def reserve_correction(self, **kwargs: Any) -> CorrectionReservation: ...

    def correction_evidence(self, activity_id: str) -> CorrectionEvidence: ...


class _BackgroundCorrectionExecutor(_CorrectionExecutor, Protocol):
    """约束纠正服务使用的 Background Work 预留接口。"""

    def reserve_correction(self, **kwargs: Any) -> CorrectionReservation: ...


@dataclass(frozen=True, slots=True)
class PreparedActivityCorrection:
    """保存一项已预留、但尚未替换旧任务的纠正事务。

    Args:
        old_activity_id: 仍在执行、等待提交后取消的旧 Activity 编号。
        old_source: 旧 Activity 当前由哪类执行器负责。
        new_activity_id: 已经预留输入和资源的新 Activity 编号。
        new_source: 新 Activity 将由哪类执行器负责。
        old_executor: 负责实时复核和取消旧 Activity 的执行器。
        new_executor: 负责释放、失败收口或启动新 Activity 的执行器。
        old_goal: 被取消的旧任务目标。
        new_goal: 准备启动的新任务目标。
        cleanup_timeout: 等待旧 Activity 完成清理的最长秒数。
        commit_barrier: 新任务是 Background Work 时使用的关闭排水屏障。
    """

    old_activity_id: str
    old_source: ActivitySource
    new_activity_id: str
    new_source: ActivitySource
    old_executor: _CorrectionExecutor
    new_executor: _CorrectionExecutor
    old_goal: str
    new_goal: str
    cleanup_timeout: float = 5.0
    commit_barrier: BackgroundCommitDrainBarrier | None = None

    def reservation(self) -> BackgroundActivityReservation:
        """创建可附着到 Foreground 工具事件的幂等提交动作。

        Returns:
            未提交时只释放 B、提交后才取消 A 并启动 B 的 Activity 事务。
        """

        return BackgroundActivityReservation.create(
            source=self.new_source,
            activity_id=self.new_activity_id,
            start_after_commit=self.commit,
            on_rollback=self.rollback,
            commit_barrier=(
                self.commit_barrier
                if self.new_source == "background_agent"
                else None
            ),
        )

    def rollback(self) -> None:
        """释放新 Activity 的预留资源，并保持旧 Activity 继续执行。

        Returns:
            无。
        """

        self.new_executor.rollback_reserved_correction(self.new_activity_id)

    def commit(self) -> ToolFollowupFact:
        """在工具事件成功入队后取消 A，等待清理完成，再启动 B。

        Returns:
            描述旧任务取消情况和新任务启动情况的最终工具事实；失败时同样返回
            可供 Foreground 直接表达的安全事实。
        """

        # 工具事件成功入队后才允许把 B 从 prepared 冻结为 committed。这样 queue
        # put 失败仍能完整回滚 handoff，不会让父任务所在通道永久保留无主的 B。
        try:
            reservation_is_committed = self.new_executor.commit_reserved_correction(
                self.new_activity_id
            )
        except Exception:
            logger.exception(
                "纠正 Activity 无法复核已提交预留 activity_id=%s",
                self.new_activity_id,
            )
            return self._fail_committed_correction("correction_start_failed")
        if not reservation_is_committed:
            # 工具事件已经成功入队，因此 B' 已经是公开承诺。即使执行槽
            # 因父任务先终态而无法冻结 handoff，也必须写启动前失败并释放
            # reservation，不能只返回一条失败文案后把父通道卡住。
            return self._fail_committed_correction("correction_start_failed")

        # 冻结快照只能证明模型调用工具时任务仍活跃。真正提交时必须再看一次，
        # 防止 A 已经自然完成却被迟到的纠正改写成用户取消。
        try:
            old_activity_is_correctable = self.old_executor.is_activity_correctable(
                self.old_activity_id
            )
        except Exception:
            logger.exception(
                "纠正 Activity 无法实时复核旧任务 activity_id=%s",
                self.old_activity_id,
            )
            return self._fail_committed_correction("correction_cancel_failed")
        if not old_activity_is_correctable:
            return self._fail_committed_correction(
                "correction_activity_no_longer_correctable"
            )

        # 只有执行器完成“进入 cancelling + interrupt + 等待清理”后，B 才能启动。
        # 超时后 A 仍沿取消路径收口，B 明确失败，禁止两项任务并发执行。
        try:
            cancellation_outcome = self.old_executor.cancel_for_correction(
                self.old_activity_id,
                timeout=self.cleanup_timeout,
            )
        except Exception:
            logger.exception(
                "纠正 Activity 取消旧任务时失败 activity_id=%s",
                self.old_activity_id,
            )
            return self._fail_committed_correction("correction_cancel_failed")
        failure_reason_by_outcome = {
            "activity_no_longer_correctable": (
                "correction_activity_no_longer_correctable"
            ),
            "cancel_not_accepted": "correction_cancel_failed",
            "cleanup_timeout": "correction_cleanup_timeout",
        }
        if cancellation_outcome != "cleaned":
            return self._fail_committed_correction(
                failure_reason_by_outcome.get(
                    cancellation_outcome,
                    "correction_cancel_failed",
                )
            )

        try:
            new_activity_started = self.new_executor.start_reserved_correction(
                self.new_activity_id
            )
        except Exception:
            logger.exception(
                "纠正 Activity 启动新任务时失败 activity_id=%s",
                self.new_activity_id,
            )
            return self._fail_committed_correction(
                "correction_start_failed",
                old_cancelled=True,
            )
        if new_activity_started:
            return ToolFollowupFact(
                kind="activity_replaced",
                activity_id=self.new_activity_id,
                old_goal=self.old_goal,
                new_goal=self.new_goal,
            )
        return self._fail_committed_correction(
            "correction_start_failed",
            old_cancelled=True,
        )

    def _fail_committed_correction(
        self,
        reason: str,
        *,
        old_cancelled: bool = False,
    ) -> ToolFollowupFact:
        """尽力把已提交 B 收口为失败，且不让补偿异常冲出输出车道。

        Args:
            reason: 写入 B 失败终态的稳定原因码。

        Returns:
            描述旧任务和新任务实际状态的安全事实。
        """

        try:
            self.new_executor.fail_reserved_correction(
                self.new_activity_id,
                reason=reason,
            )
        except Exception:
            logger.exception(
                "纠正 Activity 失败补偿未能完成 activity_id=%s reason=%s",
                self.new_activity_id,
                reason,
            )
        return self._failure_fact(reason, old_cancelled=old_cancelled)

    def _failure_fact(
        self,
        reason: str,
        *,
        old_cancelled: bool,
    ) -> ToolFollowupFact:
        """把纠正提交失败转换为不含内部原因码的用户事实。"""

        safe_message_by_reason = {
            "correction_activity_no_longer_correctable": (
                "旧任务已经结束，新任务没有开始。"
            ),
            "correction_cancel_failed": "旧任务没有取消，新任务没有开始。",
            "correction_cleanup_timeout": "旧任务仍在取消收尾，新任务没有开始。",
            "correction_start_failed": (
                "旧任务已经取消，但新任务没有开始。"
                if old_cancelled
                else "旧任务没有取消，新任务没有开始。"
            ),
        }
        return ToolFollowupFact(
            kind="activity_replacement_failed",
            activity_id=self.new_activity_id,
            old_goal=self.old_goal,
            new_goal=self.new_goal,
            safe_message=safe_message_by_reason.get(
                reason,
                "旧任务没有取消，新任务没有开始。",
            ),
        )


class ActivityCorrectionService:
    """分别准备并登记 Cognitive 与 Background Work 的同源纠正事务。

    Args:
        cognitive_executor: Cognitive Activity 执行器。
        background_executor: Background Work Activity 执行器。
        background_commit_barrier: Background 提交与执行器关闭共用的排水屏障。
    """

    def __init__(
        self,
        *,
        cognitive_executor: _CognitiveCorrectionExecutor | None,
        background_executor: _BackgroundCorrectionExecutor | None,
        background_commit_barrier: BackgroundCommitDrainBarrier | None = None,
    ) -> None:
        """保存两类执行器和本轮尚未提交的纠正预留。

        Args:
            cognitive_executor: Cognitive Activity 执行器。
            background_executor: Background Work Activity 执行器。
            background_commit_barrier: 可选的 Background 提交关闭排水屏障。
        """

        self._cognitive_executor = cognitive_executor
        self._background_executor = background_executor
        self._background_commit_barrier = background_commit_barrier
        self._prepared_by_activity_id: dict[str, PreparedActivityCorrection] = {}
        self._lock = Lock()

    def prepare_cognitive_correction(
        self,
        *,
        activity_id: str,
        interaction_session_id: str,
        plan: CognitiveExecutionPlan,
        source_user_text: str,
        parent_task_context: dict[str, object] | None = None,
    ) -> CorrectionReservation:
        """为新的 Cognitive Activity 预留资源，但暂不取消旧 Activity。

        Args:
            activity_id: 用户明确指定的旧 Activity 编号。
            interaction_session_id: 当前 Foreground 交互会话编号。
            plan: 已冻结新目标和父任务关系的 Cognitive 计划。
            source_user_text: 用户本轮真实纠正文。
            parent_task_context: 当前活跃快照提供的旧任务事实。

        Returns:
            包含接受标记、新 Activity 编号和准备或拒绝事件的结构化结果。
        """

        source, old_executor = self._resolve_active_source(
            activity_id,
            interaction_session_id,
            parent_task_context,
            expected_source="cognitive",
        )
        new_executor = self._cognitive_executor
        if source is None or old_executor is None or new_executor is None:
            return CorrectionReservation(
                accepted=False,
                events=(_rejection("cognitive_task.rejected", activity_id),),
            )

        inherited_evidence = old_executor.correction_evidence(activity_id.strip())
        merged_source_urls = _merge_text_values(
            inherited_evidence.source_urls,
            plan.source_urls,
        )
        merged_images, image_limitations = bound_image_evidence(
            (*plan.images, *inherited_evidence.images)
        )
        plan = replace(
            plan,
            images=merged_images,
            image_materials=tuple(dict.fromkeys((*plan.image_materials, *inherited_evidence.image_materials))),
            evidence_limitations=_merge_text_values(plan.evidence_limitations, image_limitations),
            source_urls=merged_source_urls,
            web_extract=bool(merged_source_urls),
        )

        reservation = new_executor.reserve_correction(
            based_on_activity_id=activity_id.strip(),
            interaction_session_id=interaction_session_id,
            plan=plan,
            source_user_text=source_user_text,
        )
        return self._register_prepared(
            reservation=reservation,
            old_activity_id=activity_id,
            old_source=source,
            new_source="cognitive",
            old_executor=old_executor,
            new_executor=new_executor,
            old_goal=_parent_goal(parent_task_context),
            new_goal=plan.goal_text,
        )

    def prepare_background_correction(
        self,
        *,
        activity_id: str,
        interaction_session_id: str,
        goal: str,
        deliverable: bool,
        requirements: str,
        materials: tuple[AgentMaterial, ...],
        current_scene_bytes: bytes | None = None,
        parent_task_context: dict[str, object] | None = None,
    ) -> CorrectionReservation:
        """为新的 Background Work 预留资源，但暂不取消旧 Activity。

        Args:
            activity_id: 用户明确指定的旧 Activity 编号。
            interaction_session_id: 当前 Foreground 交互会话编号。
            goal: 纠正后的权威后台目标。
            deliverable: 是否登记并展示完整交付结果。
            requirements: 新任务的额外验收要求。
            materials: 纠正轮新增的正式材料。
            current_scene_bytes: 当前轮冻结截图；未选择现场时为空。
            parent_task_context: 当前活跃快照提供的旧任务事实。

        Returns:
            包含接受标记、新 Activity 编号和准备或拒绝事件的结构化结果。
        """

        source, old_executor = self._resolve_active_source(
            activity_id,
            interaction_session_id,
            parent_task_context,
            expected_source="background_agent",
        )
        new_executor = self._background_executor
        if (
            source != "background_agent"
            or old_executor is None
            or new_executor is None
        ):
            return CorrectionReservation(
                accepted=False,
                events=(_rejection("background_agent.rejected", activity_id),),
            )

        reservation = new_executor.reserve_correction(
            based_on_activity_id=activity_id.strip(),
            interaction_session_id=interaction_session_id,
            goal=goal,
            deliverable=deliverable,
            requirements=requirements,
            materials=materials,
            current_scene_bytes=current_scene_bytes,
            parent_task_context=parent_task_context,
        )
        return self._register_prepared(
            reservation=reservation,
            old_activity_id=activity_id,
            old_source=source,
            new_source="background_agent",
            old_executor=old_executor,
            new_executor=new_executor,
            old_goal=_parent_goal(parent_task_context),
            new_goal=goal,
        )

    def take_reservation(self, activity_id: str) -> BackgroundActivityReservation | None:
        """取出新 Activity 对应的唯一 Foreground 提交事务。

        Args:
            activity_id: prepare 阶段分配的新 Activity 编号。

        Returns:
            尚未被提交链路取走的事务；不存在或已取走时返回空。
        """

        with self._lock:
            prepared = self._prepared_by_activity_id.pop(activity_id.strip(), None)
        return prepared.reservation() if prepared is not None else None

    def rollback_prepared(self, activity_id: str) -> bool:
        """在 Foreground 提交前取消时释放指定纠正预留。

        Args:
            activity_id: prepare 阶段分配的新 Activity 编号。

        Returns:
            找到并释放预留时返回真；不存在或已移交提交事务时返回假。
        """

        with self._lock:
            prepared = self._prepared_by_activity_id.pop(activity_id.strip(), None)
        if prepared is None:
            return False
        prepared.rollback()
        return True

    def correction_evidence(
        self,
        *,
        activity_id: str,
        parent_task_context: dict[str, object] | None,
    ) -> CorrectionEvidence:
        """读取当前 Cognitive Activity 的正式证据。

        Args:
            activity_id: 用户明确指定的旧 Activity 编号。
            parent_task_context: 当前轮冻结快照提供的来源和身份。

        Returns:
            执行器重新确认后的证据；目标已竞态失效时返回空证据。
        """

        source, executor = self._resolve_active_source(
            activity_id,
            _parent_interaction_session_id(parent_task_context),
            parent_task_context,
            expected_source="cognitive",
        )
        if source != "cognitive" or executor is None:
            return CorrectionEvidence()
        cognitive_executor = cast(_CognitiveCorrectionExecutor, executor)
        return cognitive_executor.correction_evidence(activity_id.strip())

    def _resolve_active_source(
        self,
        activity_id: str,
        interaction_session_id: str,
        parent_task_context: dict[str, object] | None,
        expected_source: ActivitySource,
    ) -> tuple[ActivitySource | None, _CorrectionExecutor | None]:
        """按冻结来源和执行器实时状态确认旧 Activity 仍可纠正。

        Args:
            activity_id: 需要核对归属的旧 Activity 编号。
            interaction_session_id: 当前 Foreground 交互会话编号。
            parent_task_context: 当前轮活跃快照提供的旧任务事实。
            expected_source: 当前纠正工具唯一允许的 Activity 来源。

        Returns:
            仍可纠正的来源与执行器；快照伪造、竞态终态或来源不符时返回空。
        """

        if not isinstance(parent_task_context, dict):
            return None, None
        normalized_activity_id = activity_id.strip()
        context_activity_id = parent_task_context.get("activity_id")
        if not isinstance(context_activity_id, str) or (
            context_activity_id.strip() != normalized_activity_id
        ):
            return None, None
        source = parent_task_context.get("source")
        if source != expected_source:
            return None, None
        context_session_id = parent_task_context.get("interaction_session_id")
        if not isinstance(context_session_id, str) or (
            context_session_id.strip() != interaction_session_id.strip()
        ):
            return None, None
        executor: _CorrectionExecutor | None
        if source == "cognitive":
            executor = self._cognitive_executor
        elif source == "background_agent":
            executor = self._background_executor
        else:
            return None, None
        if executor is None or not executor.is_activity_correctable(normalized_activity_id):
            return None, None
        return source, executor

    def _register_prepared(
        self,
        *,
        reservation: CorrectionReservation,
        old_activity_id: str,
        old_source: ActivitySource,
        new_source: ActivitySource,
        old_executor: _CorrectionExecutor,
        new_executor: _CorrectionExecutor,
        old_goal: str,
        new_goal: str,
    ) -> CorrectionReservation:
        """登记目标执行器已经成功预留的新 Activity。

        Args:
            reservation: 目标执行器返回的结构化预留结果。
            old_activity_id: 仍在执行的旧 Activity 编号。
            old_source: 旧 Activity 来源。
            new_source: 新 Activity 来源。
            old_executor: 负责取消 A 的执行器。
            new_executor: 负责启动或释放 B 的执行器。
            old_goal: 被取消的旧任务目标。
            new_goal: 准备启动的新任务目标。

        Returns:
            保持执行器结果不变；登记冲突时返回拒绝结果。
        """

        if not reservation.accepted:
            return reservation
        new_activity_id = reservation.activity_id
        prepared = PreparedActivityCorrection(
            old_activity_id=old_activity_id.strip(),
            old_source=old_source,
            new_activity_id=new_activity_id,
            new_source=new_source,
            old_executor=old_executor,
            new_executor=new_executor,
            old_goal=old_goal.strip(),
            new_goal=new_goal.strip(),
            commit_barrier=self._background_commit_barrier,
        )
        with self._lock:
            if new_activity_id in self._prepared_by_activity_id:
                new_executor.rollback_reserved_correction(new_activity_id)
                return CorrectionReservation(
                    accepted=False,
                    events=reservation.events,
                )
            self._prepared_by_activity_id[new_activity_id] = prepared
        return reservation


def _parent_interaction_session_id(
    parent_task_context: dict[str, object] | None,
) -> str:
    """从冻结父任务现场读取交互会话编号。

    Args:
        parent_task_context: 当前轮允许纠正的父 Activity 现场。

    Returns:
        去除首尾空白后的会话编号；现场不完整时返回空串。
    """

    if not isinstance(parent_task_context, dict):
        return ""
    value = parent_task_context.get("interaction_session_id")
    return value.strip() if isinstance(value, str) else ""


def _parent_goal(parent_task_context: dict[str, object] | None) -> str:
    """读取纠正事实需要表达的旧任务目标。

    Args:
        parent_task_context: 当前轮冻结的旧 Activity 现场。

    Returns:
        去除首尾空白的旧任务目标；现场缺失时返回空串。
    """

    if not isinstance(parent_task_context, dict):
        return ""
    value = parent_task_context.get("goal")
    return value.strip() if isinstance(value, str) else ""


def _merge_text_values(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """按来源顺序合并非空文字并去重。

    Args:
        groups: 需要依次合并的不可变文字序列。

    Returns:
        保留首次出现顺序的非空文字。
    """

    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def _rejection(event_type: str, activity_id: str) -> dict[str, object]:
    """构造旧 Activity 已不可纠正时的稳定拒绝事件。

    Args:
        event_type: 目标执行器使用的拒绝事件类型。
        activity_id: 用户指定但已不可纠正的旧 Activity 编号。

    Returns:
        不包含内部错误的稳定拒绝事件。
    """

    return {
        "type": event_type,
        "payload": {
            "activity_id": activity_id.strip(),
            "reason": "correction_activity_not_found",
        },
    }
