"""在 Backend 主进程编排一次 Cognitive 失败恢复的生命周期。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from json import dumps
from math import isfinite
from threading import RLock
from time import monotonic as monotonic_now
from typing import Any, Protocol

from companion.activities.runtime.models import ACTIVITY_FAILURE_CODES
from companion.activities.runtime.public_text import summarize_activity_goal
from companion.foreground_self_reflection.models import (
    CognitiveRecoverySignal,
    DivertFinalInputCommand,
    ExpressionSettlementDisposition,
    InputDiversionResult,
    PendingRecovery,
    SelfReflectionAdmissionCommand,
    SelfReflectionOutcomeFact,
    SelfReflectionState,
    SelfReflectionToolCommitIdentity,
    UnattendedMaterialReference,
)
from companion.foreground_self_reflection.episode_lifecycle import (
    SelfReflectionEpisodeLifecycle,
)
from companion.foreground_self_reflection.policy import evaluate_cognitive_recovery
from companion.foreground_self_reflection.prompt import (
    RecoveryPromptFacts,
    entry_fallback,
    interruption_reply_fallback,
    render_interruption_reply_instruction,
    render_entry_instruction,
)
from companion.foreground_self_reflection.contracts import (
    AssistantRequestAdmissionLeaseProtocol,
    TakeoverReceipt,
    SelfReflectionEntryContext,
    SelfReflectionInterruptionReplyContext,
    SelfReflectionRecoveryContext,
    SelfReflectionToolCommitDecisionCommand,
)
from companion.foreground_self_reflection.registry import ForegroundAttentionCoordinator
from companion.realtime.assistant_events import (
    AssistantRequest,
    RuntimeEvent,
)
from companion.realtime.assistant_process_handle import AssistantProcessReplacement
from companion.realtime.visible_output_journal import VisibleOutputSettlement
from companion.runtime_memory.models import TaskRecoveryOutcome
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.runtime_memory.unattended_episode import (
    UnattendedEpisodeIdentity,
    UnattendedInterruptionReply,
)
from companion.core.local_clock import local_wall_datetime
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


def _first_identity_mismatch(
    comparisons: tuple[tuple[str, object, object], ...],
) -> tuple[str, object, object] | None:
    """返回一组身份字段中的首个冲突。

    Args:
        comparisons: 按检查顺序排列的字段名、预期值和实际值。

    Returns:
        首个不相等字段；全部一致时返回空。
    """

    for field_name, expected, actual in comparisons:
        if expected != actual:
            return field_name, expected, actual
    return None


class RequestSnapshotPort(Protocol):
    """读取来源 Assistant 请求的最小只读端口。"""

    def get_request(self, request_id: str) -> AssistantRequest | None:
        """按请求编号返回冻结来源请求。

        Args:
            request_id: 创建 Cognitive Activity 的 Foreground 请求编号。

        Returns:
            仍由当前运行时持有的请求快照；不存在时返回空。
        """


class MaterialPort(Protocol):
    """管理 Cognitive 原始材料租约的最小端口。"""

    def retain_turn(self, context_id: str) -> bool:
        """取得一份材料租约。

        Args:
            context_id: 需要继续保留的材料上下文编号。

        Returns:
            材料领域确认租约已经取得时返回真。
        """

    def release_turn(self, context_id: str) -> list[dict[str, object]]:
        """释放一份材料租约。

        Args:
            context_id: 已经完成交接或不再需要的材料上下文编号。

        Returns:
            释放后需要广播的材料变更事件。
        """


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    """保存失败 Cognitive 进入恢复前必须冻结的来源事实。

    Args:
        request: 创建 Cognitive Activity 的原始助手请求快照。
        activity_id: Cognitive Activity 编号。
        protocol_transaction_id: 创建 Activity 的工具事务编号。
        material_context_id: 原请求已经授权的材料上下文编号。
        runtime_epoch: 来源 Assistant Runtime 世代。
        started: Activity 是否已经发布真实 started 事件。
        material_lease_retained: 来源是否已经取得自己的材料租约。
    """

    request: AssistantRequest
    activity_id: str
    protocol_transaction_id: str
    material_context_id: str | None
    runtime_epoch: int
    started: bool = False
    material_lease_retained: bool = False


@dataclass(frozen=True, slots=True)
class _RecoveryTurnSubmission:
    """保存入场表达结算后才能提交的可见 Recovery 请求。

    Args:
        context: Recovery generation 使用的身份、截止时间和失败事实。
        source_request: 提供冻结角色、历史、材料和现场的来源请求。
    """

    context: SelfReflectionRecoveryContext
    source_request: AssistantRequest


@dataclass(frozen=True, slots=True)
class _InterruptionReplyReservation:
    """保存辅助回应从请求创建到 committed settlement 的稳定账本身份。

    Args:
        self_reflection_id: 辅助回应所属自我反思编号。
        episode_id: 辅助回应所属 episode 编号。
        runtime_epoch: 请求所属 Assistant Runtime 世代。
        trigger_input_identity: 触发本次回应的用户输入身份。
        episode_sequence: 启动回应前预留的统一账本顺序号。
        failure_fact_pending: 是否需要由当前提交或 reaper 补写失败事实。
    """

    self_reflection_id: str
    episode_id: str
    runtime_epoch: int
    trigger_input_identity: str
    episode_sequence: int
    failure_fact_pending: bool = False


@dataclass(frozen=True, slots=True)
class _PrimaryOutputReservation:
    """保存 Entry 或 Recovery 输出在 settlement 前占用的生命周期身份。

    Args:
        request_id: 可见输出请求的稳定编号。
        self_reflection_id: 所属自我反思编号。
        episode_id: 所属未处理输入片段编号。
        runtime_epoch: 输出所属 Assistant Runtime 世代。
        request_source: Entry 或 Recovery 输出来源。
    """

    request_id: str
    self_reflection_id: str
    episode_id: str
    runtime_epoch: int
    request_source: str


@dataclass(frozen=True, slots=True)
class _AdmissionCleanup:
    """保存准入副作用失败后仍需重试的资源清理事实。

    字段表示还没有完成的步骤。每完成一步就立即清掉对应字段，后续重试不会
    再次释放已经交还的资源。

    Args:
        snapshot: Attention Admission 产生的身份快照。
        material_context_id: 仍需释放的材料上下文编号。
        release_pin: 是否仍需释放恢复压缩门禁。
        episode_opened: 未处理插话片段是否仍需关闭。
        reason: Episode 关闭后写入 Attention 墓碑的稳定原因。
    """

    snapshot: Any
    material_context_id: str | None
    release_pin: bool
    episode_opened: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _SourceMaterialCleanup:
    """保存普通 Cognitive 终态释放失败后仍需重试的材料租约。

    Args:
        activity_id: 发生终态的来源 Cognitive Activity 编号。
        material_context_id: 需要释放的材料上下文编号。
        runtime_epoch: 来源请求所属的助手运行时世代。
    """

    activity_id: str
    material_context_id: str
    runtime_epoch: int


@dataclass(frozen=True, slots=True)
class _RuntimeTerminationCleanup:
    """保存热替换后必须继续完成的旧世代资源清理事实。

    Args:
        snapshot: 被终止的 Attention 身份快照。
        outcome: 要写入 Task Memory 的冻结恢复结果。
        material_context_id: 仍需释放的材料上下文编号。
        episode_opened: 未处理插话片段是否仍需关闭。
        recovery_pending: Task Memory 恢复关系是否仍需提交。
    """

    snapshot: Any
    outcome: SelfReflectionOutcomeFact
    material_context_id: str | None
    episode_opened: bool = True
    recovery_pending: bool = True


@dataclass(frozen=True, slots=True)
class _ToolCommitRequest:
    """保存主进程已经完成基础协议校验的同步工具提交请求。

    Args:
        identity: Attention 单锁必须冻结和比较的完整提交身份。
        canonical_event: Background 工具需要先登记的 canonical 事件；Inline 为空。
    """

    identity: SelfReflectionToolCommitIdentity
    canonical_event: RuntimeEvent | None


class ForegroundSelfReflectionService:
    """串联恢复准入、插话收集、工具提交和可见输出结算。

    这个类不运行 LLM，也不从自由正文判断成功与否。它只消费结构化运行时事件，
    让 Attention、材料租约、未处理插话历史和可见 Recovery 按固定顺序收口。

    Args:
        attention: 当前运行时唯一的前台注意力状态机。
        runtime_memory: 主进程运行期记忆所有者。
        request_snapshots: 来源请求只读查询端口。
        material_lifecycle: Cognitive 原始材料租约端口。
        current_runtime_epoch: 返回当前助手运行时世代。
        submit_control: 向指定助手世代提交同步工具决定命令。
        submit_expression: 提交 Entry 或 Recovery 可见请求。
        publish_material_event: 发布材料租约释放后产生的主进程瞬时事件。
        publish_episode_committed: 把闭合 episode 放入主进程事件队列的端口。
        runtime_epoch_mutation_lock: 与运行时配置发布共用的异步互斥边界。
    """

    def __init__(
        self,
        *,
        attention: ForegroundAttentionCoordinator,
        runtime_memory: RuntimeMemoryStore,
        request_snapshots: RequestSnapshotPort,
        material_lifecycle: MaterialPort,
        current_runtime_epoch: Callable[[], int],
        submit_control: Callable[[object, int], bool],
        submit_expression: Callable[
            [
                SelfReflectionEntryContext
                | SelfReflectionRecoveryContext
                | SelfReflectionInterruptionReplyContext,
                AssistantRequest,
            ],
            bool,
        ],
        current_foreground_output_identity: (
            Callable[[], tuple[str, str | None] | None] | None
        ) = None,
        publish_material_event: Callable[[dict[str, object]], None] | None = None,
        publish_episode_committed: Callable[[RuntimeEvent], None] | None = None,
        request_recovery_fallback: Callable[[str, int, str], bool] | None = None,
        runtime_epoch_mutation_lock: AbstractAsyncContextManager[object] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定恢复生命周期所需的状态机、记忆、材料和输出端口。

        Args:
            attention: 当前运行时唯一的前台注意力状态机。
            runtime_memory: 主进程运行期记忆所有者。
            request_snapshots: 来源请求只读查询端口。
            material_lifecycle: Cognitive 原始材料租约端口。
            current_runtime_epoch: 返回当前助手运行时世代的函数。
            current_foreground_output_identity: 原子读取当前普通 Foreground 输出身份的
                可选函数；没有活跃普通输出时返回空。
            submit_control: 向指定助手世代提交同步工具决定命令的函数。
            submit_expression: 提交入场或 Recovery 可见请求的函数。
            publish_material_event: 发布材料变更事件的可选函数。
            publish_episode_committed: 发布 episode 原子历史块的可选函数。
            request_recovery_fallback: 请求助手输出域停止模型思考并生成兜底正文的端口。
            runtime_epoch_mutation_lock: 保证 canonical 登记与运行时世代发布
                不能交错的共享异步锁；测试不需要跨模块串行时可为空。
            now: 生成 episode 输入时间的可替换本地时钟。
        """

        self._attention = attention
        self._runtime_memory = runtime_memory
        self._request_snapshots = request_snapshots
        self._material_lifecycle = material_lifecycle
        self._current_runtime_epoch = current_runtime_epoch
        self._current_foreground_output_identity = (
            current_foreground_output_identity or (lambda: None)
        )
        self._submit_control = submit_control
        self._submit_expression = submit_expression
        self._publish_material_event = publish_material_event
        self._request_recovery_fallback = request_recovery_fallback
        self._runtime_epoch_mutation_lock = (
            runtime_epoch_mutation_lock or nullcontext()
        )
        self._now = now or local_wall_datetime
        self._lock = RLock()
        self._episode_lifecycle = SelfReflectionEpisodeLifecycle(
            attention=attention,
            runtime_memory=runtime_memory,
            publish_episode_committed=publish_episode_committed,
            now=self._now,
        )
        self._sources: dict[str, _SourceRecord] = {}
        # 来源请求快照只保留到最终表达提交完成；已收口的 Activity 用墓碑抑制重复失败事件。
        self._settled_source_activity_ids: set[str] = set()
        self._material_contexts: dict[str, str] = {}
        self._pending_outcomes: dict[str, SelfReflectionOutcomeFact] = {}
        self._pending_admission_cleanups: dict[str, _AdmissionCleanup] = {}
        # 这条记录只覆盖“失败已确认、接管尚未完成”的短事务窗口，不新增 Attention 状态。
        self._pending_recoveries: dict[str, PendingRecovery] = {}
        # Recovery 请求必须等入场表达结算后才能进入共享输出 lane。
        self._pending_recovery_turns: dict[str, _RecoveryTurnSubmission] = {}
        self._submitted_recovery_turn_ids: set[str] = set()
        self._pending_deadline_fallbacks: dict[str, tuple[int, str]] = {}
        # 工具结果可以早于无 Thinking 二轮正文形成，但不能提前关闭 Attention。
        self._recovery_outcomes: dict[str, SelfReflectionOutcomeFact] = {}
        self._interruption_reply_reservations: dict[
            str,
            _InterruptionReplyReservation,
        ] = {}
        self._primary_output_reservations: dict[str, _PrimaryOutputReservation] = {}
        self._last_takeover_receipt = TakeoverReceipt(
            status="rejected",
            reason="not_attempted",
        )
        self._pending_source_material_cleanups: dict[str, _SourceMaterialCleanup] = {}
        self._pending_runtime_termination_cleanups: dict[
            str,
            _RuntimeTerminationCleanup,
        ] = {}
        self._active_admission_cleanup_ids: set[str] = set()
        self._active_source_material_cleanup_ids: set[str] = set()
        self._active_runtime_termination_cleanup_ids: set[str] = set()
        self._active_material_handoff_ids: set[str] = set()
        self._settling_outcomes: set[str] = set()
        # canonical 应用已经完整返回后留下墓碑。若批准命令入队失败，Broker 用
        # 同一 commit_request_id 重试时只需重发回执，不能重复登记 TaskResult。
        self._registered_tool_commit_request_ids: set[str] = set()

    def observe_tool_call_completed(self, event: RuntimeEvent) -> bool:
        """登记已接纳的 Cognitive 来源，确保原始材料不会过早释放。

        Args:
            event: `llm.foreground.tool_call_completed` 事件。

        Returns:
            本事件是否属于被本服务登记的 Cognitive 调用。
        """

        payload = event.payload
        tool_name = payload.get("name")
        if not isinstance(tool_name, str) or tool_name not in {
            "lookup_current_fact",
            "analyze_deeply",
            "correct_cognitive_activity",
        }:
            return False
        if payload.get("state") != "accepted":
            return False
        activity_id = _text(payload.get("activity_id"))
        request_id = _text(payload.get("request_id"))
        protocol_transaction_id = _text(payload.get("protocol_transaction_id"))
        if not activity_id or not request_id or not protocol_transaction_id:
            return False
        if not _event_belongs_to_current_epoch(
            event,
            current_runtime_epoch=self._current_runtime_epoch,
        ):
            return False
        request = self._request_snapshots.get_request(request_id)
        if request is None:
            return False
        runtime_epoch = event.runtime_epoch
        assert runtime_epoch is not None
        interaction_session_id = _text(payload.get("interaction_session_id"))
        if interaction_session_id and interaction_session_id != request.interaction_session_id:
            return False
        material_context_id = _material_context_id(payload)
        with self._lock:
            if activity_id in self._pending_source_material_cleanups:
                # 旧终态的释放仍在重试，迟到 acceptance 不能再次取得同一来源租约。
                return True
            if activity_id in self._settled_source_activity_ids:
                # 已经收口的来源只吸收迟到 acceptance，不能重新取得一份租约。
                return True
            existing = self._sources.get(activity_id)
            if existing is not None:
                return (
                    existing.runtime_epoch == runtime_epoch
                    and existing.request.request_id == request.request_id
                    and existing.protocol_transaction_id == protocol_transaction_id
                    and existing.material_context_id == material_context_id
                )
            material_lease_retained = False
            if material_context_id is not None:
                # Cognitive 的普通 Foreground final 可能先于失败终态完成；租约必须在
                # acceptance 事实落账时取得，不能把恢复资格押在一个已经结束的 turn 上。
                try:
                    material_lease_retained = self._material_lifecycle.retain_turn(
                        material_context_id
                    )
                except Exception as exception:
                    logger.warning(
                        "Cognitive 来源材料租约取得失败 activity_id=%s context_id=%s error=%s",
                        activity_id,
                        material_context_id,
                        exception,
                    )
                    return False
                if not material_lease_retained:
                    return False
            self._sources[activity_id] = _SourceRecord(
                request=request,
                activity_id=activity_id,
                protocol_transaction_id=protocol_transaction_id,
                material_context_id=material_context_id,
                runtime_epoch=runtime_epoch,
                material_lease_retained=material_lease_retained,
            )
        return True

    def observe_cognitive_started(self, event: RuntimeEvent) -> bool:
        """记录 Cognitive 已真实启动，未启动的失败永远不能恢复。

        Args:
            event: 候选 `cognitive_task.started` 运行时事件。

        Returns:
            事件身份匹配且来源已被推进为 started 时返回真。
        """

        if event.type != "cognitive_task.started":
            return False
        if not _event_belongs_to_current_epoch(
            event,
            current_runtime_epoch=self._current_runtime_epoch,
        ):
            return False
        activity_id = _text(event.payload.get("activity_id"))
        if not activity_id:
            return False
        with self._lock:
            source = self._sources.get(activity_id)
            if source is None:
                return False
            if not _source_payload_matches(event.payload, source):
                return False
            self._sources[activity_id] = replace(source, started=True)
        return True

    def observe_cognitive_terminal(self, event: RuntimeEvent) -> bool:
        """释放不进入恢复路径的 Cognitive 来源请求快照。

        Args:
            event: 已经成功应用到 Activity Registry 的 Cognitive 非失败终态。

        Returns:
            找到并释放了严格匹配的来源快照时返回真。
        """

        if event.type not in {
            "cognitive_task.completed",
            "cognitive_task.cancelled",
            "cognitive_task.interrupted",
            "cognitive_task.rejected",
        }:
            return False
        if not _event_belongs_to_current_epoch(
            event,
            current_runtime_epoch=self._current_runtime_epoch,
        ):
            return False
        activity_id = _text(event.payload.get("activity_id"))
        if not activity_id:
            return False
        with self._lock:
            source = self._sources.get(activity_id)
            if source is None:
                return False
            if source.runtime_epoch != event.runtime_epoch:
                return False
            if not _source_payload_matches(event.payload, source):
                return False
            self._sources.pop(activity_id, None)
        if source.material_lease_retained and source.material_context_id is not None:
            self._release_source_material(source)
        return True

    @property
    def last_takeover_receipt(self) -> TakeoverReceipt:
        """返回最近一次 Cognitive 失败接管的结构化结果。"""

        with self._lock:
            return self._last_takeover_receipt

    def _set_takeover_receipt(self, receipt: TakeoverReceipt) -> None:
        """记录接管结果，供 Activity Handler 决定唯一的用户表达路径。"""

        with self._lock:
            self._last_takeover_receipt = receipt

    def _observe_cognitive_failed(self, event: RuntimeEvent) -> bool:
        """在同一条失败事件边界内执行一次 Recovery Admission。

        Args:
            event: 已应用到 Activity Registry 的 Cognitive failed 事件。

        Returns:
            Attention 成功接管并抑制普通失败 Notice 时返回真。
        """

        self._set_takeover_receipt(
            TakeoverReceipt(status="rejected", reason="event_not_eligible")
        )
        activity_id = _text(event.payload.get("activity_id"))
        runtime_epoch = event.runtime_epoch
        if event.type != "cognitive_task.failed":
            return False
        if not _event_belongs_to_current_epoch(
            event,
            current_runtime_epoch=self._current_runtime_epoch,
        ):
            return False
        with self._lock:
            source = self._sources.get(activity_id)
            if source is None and activity_id in self._settled_source_activity_ids:
                # Activity 终态事件可能因重试再次到达；墓碑仍需抑制普通失败 Notice。
                self._set_takeover_receipt(
                    TakeoverReceipt(
                        status="accepted",
                        entry_output_accepted=True,
                        reason="duplicate_failure_event",
                    )
                )
                return True
        if source is None:
            return False
        if not _source_payload_matches(event.payload, source):
            return False
        failure_code = _activity_failure_code(event.payload)
        signal = CognitiveRecoverySignal(
            runtime_epoch=runtime_epoch,
            source_activity_id=activity_id,
            source_request_id=source.request.request_id,
            source_protocol_transaction_id=source.protocol_transaction_id,
            activity_started=source.started,
            terminal_state="failed",
            failure_code=failure_code,
        )
        decision = evaluate_cognitive_recovery(signal)
        if not decision.eligible:
            # 终态已经成立且不会进入恢复；完整来源请求到这里已经没有后续消费者。
            self._discard_source(activity_id, release_material=True)
            return False
        with self._episode_lifecycle.transaction():
            return self._admit_eligible_failure(
                event=event,
                source=source,
                signal=signal,
            )

    def _admit_eligible_failure(
        self,
        *,
        event: RuntimeEvent,
        source: _SourceRecord,
        signal: CognitiveRecoverySignal,
    ) -> bool:
        """在统一 Episode 事务内完成准入资源和 Entry 提交。

        Args:
            event: 已通过资格判断的 Cognitive 失败终态。
            source: 失败 Activity 对应的冻结 Foreground 来源。
            signal: 已校验运行时世代和任务身份的恢复信号。

        Returns:
            Entry 已经进入唯一可见输出链时返回真。
        """

        admission = self._episode_lifecycle.admit_and_open(
            SelfReflectionAdmissionCommand(
                runtime_epoch=signal.runtime_epoch,
                source_activity_id=signal.source_activity_id,
                source_request_id=signal.source_request_id,
                source_protocol_transaction_id=signal.source_protocol_transaction_id,
            )
        )
        if admission.status == "duplicate":
            # 来源墓碑已经证明第一次 Admission 成立；重放不能再制造普通失败提醒。
            accepted = admission.snapshot is not None
            self._set_takeover_receipt(
                TakeoverReceipt(
                    status="accepted" if accepted else "rejected",
                    entry_output_accepted=accepted,
                    reason="duplicate_admission" if accepted else "duplicate_without_snapshot",
                )
            )
            return accepted
        if admission.status != "admitted" or admission.snapshot is None:
            self._discard_source(source.activity_id, release_material=True)
            return False
        snapshot = admission.snapshot
        foreground_output_identity = self._safe_current_foreground_output_identity()
        interrupted_request_id = (
            foreground_output_identity[0]
            if foreground_output_identity is not None
            else None
        )
        interrupted_generation_id = (
            foreground_output_identity[1]
            if foreground_output_identity is not None
            else None
        )
        safe_task_target = (
            summarize_activity_goal(event.payload.get("brief"))
            or "用户交代的任务"
        )
        with self._lock:
            self._pending_recoveries[snapshot.self_reflection_id] = PendingRecovery(
                source_activity_id=source.activity_id,
                runtime_epoch=signal.runtime_epoch,
                safe_task_target=safe_task_target,
                untrusted_failure_reason=_text(event.payload.get("failure_reason")),
                interrupted_request_id=interrupted_request_id,
                interrupted_generation_id=interrupted_generation_id,
            )
        pin_retained = False
        try:
            pin_retained = self._runtime_memory.retain_recovery_compaction_pin(
                snapshot.self_reflection_id
            )
            if not pin_retained:
                raise RuntimeError("recovery_compaction_pin_unavailable")
            if source.material_context_id and not source.material_lease_retained:
                # 带材料的来源若在 acceptance 阶段没有拿到租约，不能启动一个必然
                # 无法读取原始目录的伪恢复。
                raise RuntimeError("recovery_material_unavailable")
            with self._lock:
                if source.material_context_id:
                    self._material_contexts[snapshot.self_reflection_id] = (
                        source.material_context_id
                    )
            recovery_context = self._build_recovery_context(source, snapshot, event)
        except Exception:
            self._rollback_admission_resources(
                snapshot,
                material_context_id=(
                    source.material_context_id
                    if source.material_lease_retained
                    else None
                ),
                release_pin=pin_retained,
                reason="recovery_admission_side_effect_failed",
                episode_opened=True,
            )
            with self._lock:
                self._pending_recoveries.pop(snapshot.self_reflection_id, None)
            return False

        # 先推进状态，再投递控制命令，避免子进程瞬间产出的工具提交事件看到 acquiring。
        transition = self._attention.transition_to_reflecting(
            snapshot.self_reflection_id,
            runtime_epoch=signal.runtime_epoch,
        )
        deadline_already_reached = (
            transition.reason == "reflection_deadline_exceeded"
            and transition.snapshot is not None
            and transition.snapshot.outcome is not None
        )
        if transition.status not in {"applied", "duplicate"} and not deadline_already_reached:
            self._rollback_admission_resources(
                snapshot,
                material_context_id=(
                    source.material_context_id
                    if source.material_lease_retained
                    else None
                ),
                release_pin=pin_retained,
                reason="reflection_state_transition_failed",
                episode_opened=True,
            )
            with self._lock:
                self._pending_recoveries.pop(snapshot.self_reflection_id, None)
            return False

        entry = SelfReflectionEntryContext(
            request_id=f"self-reflection-entry-{snapshot.self_reflection_id}",
            self_reflection_id=snapshot.self_reflection_id,
            episode_id=snapshot.episode_id,
            runtime_epoch=snapshot.runtime_epoch,
            source_activity_id=source.activity_id,
            instruction=render_entry_instruction(
                safe_task_target=safe_task_target,
                interrupted_foreground=foreground_output_identity is not None,
            ),
            fallback_text=entry_fallback(
                interrupted_foreground=foreground_output_identity is not None,
            ),
        )
        self._reserve_primary_output(entry)
        # 入场请求自身携带主动抢占原因；只有请求真正进入输出链时，旧 generation
        # 才会被取消，避免接管准备失败时无声地打断普通交流。
        visible_accepted = self._safe_submit_expression(entry, source.request)
        if not visible_accepted:
            self._remove_primary_output(entry.request_id)
            with self._lock:
                self._pending_recoveries.pop(snapshot.self_reflection_id, None)
                self._pending_recovery_turns.pop(snapshot.self_reflection_id, None)
            self._set_takeover_receipt(
                TakeoverReceipt(
                    status="failed",
                    reason="takeover_entry_not_accepted",
                )
            )
            # 已经进入 reflecting 后不能再调用只允许 acquiring 的 abort_admission；
            # 用一次无可见结果的失败收口把 episode、pin、材料和 Attention 一并释放。
            self._settle_outcome(
                SelfReflectionOutcomeFact.failed(
                    self_reflection_id=snapshot.self_reflection_id,
                    source_activity_id=source.activity_id,
                    reason="takeover_entry_not_accepted",
                )
            )
            return False
        with self._lock:
            pending_recovery = self._pending_recoveries.get(
                snapshot.self_reflection_id
            )
            if pending_recovery is not None:
                self._pending_recoveries[snapshot.self_reflection_id] = replace(
                    pending_recovery,
                    submission_status="entry_output_accepted",
                )
            self._pending_recovery_turns[snapshot.self_reflection_id] = (
                _RecoveryTurnSubmission(
                    context=recovery_context,
                    source_request=source.request,
                )
            )
        self._set_takeover_receipt(
            TakeoverReceipt(
                status="accepted",
                entry_output_accepted=True,
                reason="takeover_entry_accepted",
            )
        )
        return True

    def observe_cognitive_failed(
        self,
        event: RuntimeEvent,
    ) -> TakeoverReceipt:
        """观察 Cognitive 业务失败并返回唯一的结构化接管回执。

        Args:
            event: 已经通过 Activity Registry 的 Cognitive failed 终态事件。

        Returns:
            说明 Entry 是否已经建立唯一可见接管路径的接管回执。
        """

        self._observe_cognitive_failed(event)
        return self.last_takeover_receipt

    async def handle_tool_commit_request(
        self,
        event: RuntimeEvent,
        *,
        register_canonical_event: Callable[[RuntimeEvent], Awaitable[None]],
    ) -> bool:
        """在运行时世代稳定期间完成授权、canonical 登记和决定回传。

        Args:
            event: 子进程 Broker 使用稳定身份重发的工具提交请求。
            register_canonical_event: 复用 Router 正常工具事实应用顺序的异步端口。

        Returns:
            请求已被批准或稳定拒绝并成功回执时返回真；旧世代、协议错误、
            canonical 登记异常或控制命令入队失败时返回假。
        """

        # 热替换从候选发布到旧 Runtime 退休始终持有同一把配置锁。工具提交
        # 也覆盖完整主进程事务，避免 canonical 登记后才发现批准目标已换世代。
        async with self._runtime_epoch_mutation_lock:
            if event.type != "foreground.self_reflection_tool_commit_requested":
                return False
            if not _event_belongs_to_current_epoch(
                event,
                current_runtime_epoch=self._current_runtime_epoch,
            ):
                return False
            request = _parse_tool_commit_request(event)
            if request is None:
                return False
            identity = request.identity
            snapshot = self._attention.snapshot(identity.self_reflection_id)
            if snapshot is None:
                return False
            if not _tool_commit_matches_snapshot(request, snapshot):
                return self._safe_submit_control(
                    SelfReflectionToolCommitDecisionCommand(
                        commit_request_id=identity.commit_request_id,
                        self_reflection_id=identity.self_reflection_id,
                        source_activity_id=identity.source_activity_id,
                        runtime_epoch=identity.runtime_epoch,
                        tool_call_id=identity.tool_call_id,
                        tool_name=identity.tool_name,
                        approved=False,
                        reason="canonical_tool_event_invalid",
                        issued_at=monotonic_now(),
                    ),
                    identity.runtime_epoch,
                )

            authorization = self._attention.authorize_tool_commit(identity)
            if authorization.status not in {"applied", "duplicate"}:
                decision_submitted = self._safe_submit_control(
                    SelfReflectionToolCommitDecisionCommand(
                        commit_request_id=identity.commit_request_id,
                        self_reflection_id=identity.self_reflection_id,
                        source_activity_id=identity.source_activity_id,
                        runtime_epoch=identity.runtime_epoch,
                        tool_call_id=identity.tool_call_id,
                        tool_name=identity.tool_name,
                        approved=False,
                        reason=authorization.reason,
                        issued_at=monotonic_now(),
                    ),
                    identity.runtime_epoch,
                )
                return decision_submitted

            canonical_event = request.canonical_event
            if canonical_event is not None:
                with self._lock:
                    canonical_registered = (
                        identity.commit_request_id
                        in self._registered_tool_commit_request_ids
                    )
                if not canonical_registered:
                    try:
                        await register_canonical_event(canonical_event)
                    except Exception:
                        # 不回拒绝，因为 canonical 应用可能只完成了一部分；Broker 会用
                        # 同一完整身份重试，幂等消费者从原边界继续，绝不提前启动工具。
                        logger.exception(
                            "Self-Reflection canonical 工具事实登记失败 commit_request_id=%s",
                            identity.commit_request_id,
                        )
                        return False
                    with self._lock:
                        self._registered_tool_commit_request_ids.add(
                            identity.commit_request_id
                        )
                completion = self._attention.complete_background_tool_commit(identity)
                if completion.status not in {"applied", "duplicate"}:
                    return self._safe_submit_control(
                        SelfReflectionToolCommitDecisionCommand(
                            commit_request_id=identity.commit_request_id,
                            self_reflection_id=identity.self_reflection_id,
                            source_activity_id=identity.source_activity_id,
                            runtime_epoch=identity.runtime_epoch,
                            tool_call_id=identity.tool_call_id,
                            tool_name=identity.tool_name,
                            approved=False,
                            reason=completion.reason,
                            issued_at=monotonic_now(),
                        ),
                        identity.runtime_epoch,
                    )

            return self._safe_submit_control(
                SelfReflectionToolCommitDecisionCommand(
                    commit_request_id=identity.commit_request_id,
                    self_reflection_id=identity.self_reflection_id,
                    source_activity_id=identity.source_activity_id,
                    runtime_epoch=identity.runtime_epoch,
                    tool_call_id=identity.tool_call_id,
                    tool_name=identity.tool_name,
                    approved=True,
                    reason="tool_commit_approved",
                    issued_at=monotonic_now(),
                ),
                identity.runtime_epoch,
            )

    def divert_final_input(
        self,
        command: DivertFinalInputCommand,
    ) -> InputDiversionResult:
        """把 Text 或 Final ASR 原子追加到不可执行 episode。

        Args:
            command: 已经公开、等待 Attention 裁决归属的 Final 输入。

        Returns:
            保留首次接纳、幂等重复或未改道原因的结构化结果。
        """

        result = self._episode_lifecycle.resolve_input_ownership(
            command,
            reserve_ordinary_request=True,
        )
        return self._finish_input_diversion(result)

    def try_divert_reserved_text(
        self,
        command: DivertFinalInputCommand,
    ) -> InputDiversionResult:
        """在文本 Reservation 阶段仅尝试归入已经活跃的 Episode。

        Args:
            command: 刚冻结材料、尚未进入普通请求队列的文本输入。

        Returns:
            已经完整写入 Episode 的归属，或不预留普通请求资格的未改道结果。
        """

        result = self._episode_lifecycle.resolve_input_ownership(
            command,
            reserve_ordinary_request=False,
        )
        return self._finish_input_diversion(result)

    def _finish_input_diversion(
        self,
        result: InputDiversionResult,
    ) -> InputDiversionResult:
        """在 Episode 追加提交后按资格启动一次辅助回应。

        Args:
            result: 生命周期事务已经完成持久化的输入归属。

        Returns:
            保留真实辅助回应提交资格的最终裁决。
        """

        if result.status != "accepted" or not result.interruption_reply_eligible:
            return result
        if self._submit_interruption_reply(result):
            return result
        return replace(result, interruption_reply_eligible=False)

    def _submit_interruption_reply(self, result: InputDiversionResult) -> bool:
        """为一项新接纳输入提交无工具、无 Thinking 的辅助回应。

        Args:
            result: 已成功写入 Runtime Memory episode 的输入裁决。

        Returns:
            顺序号预留和 AssistantRequest 提交都成功时返回真。
        """

        snapshot = result.snapshot
        accepted_input = result.accepted_input
        if snapshot is None or accepted_input is None:
            return False
        with self._lock:
            source = self._sources.get(snapshot.source_activity_id)
        if source is None or source.runtime_epoch != snapshot.runtime_epoch:
            return False

        request_digest = sha256(
            (
                f"{snapshot.self_reflection_id}\0"
                f"{accepted_input.input_identity}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        request_id = f"self-reflection-interruption-{request_digest}"
        with self._episode_lifecycle.transaction():
            episode_sequence = self._attention.reserve_interruption_reply_sequence(
                self_reflection_id=snapshot.self_reflection_id,
                runtime_epoch=snapshot.runtime_epoch,
                request_id=request_id,
                trigger_input_identity=accepted_input.input_identity,
            )
            if episode_sequence is None:
                return False

            context = SelfReflectionInterruptionReplyContext(
                request_id=request_id,
                self_reflection_id=snapshot.self_reflection_id,
                episode_id=snapshot.episode_id,
                runtime_epoch=snapshot.runtime_epoch,
                trigger_input_identity=accepted_input.input_identity,
                instruction=render_interruption_reply_instruction(),
                fallback_text=interruption_reply_fallback(),
            )
            reservation = _InterruptionReplyReservation(
                self_reflection_id=snapshot.self_reflection_id,
                episode_id=snapshot.episode_id,
                runtime_epoch=snapshot.runtime_epoch,
                trigger_input_identity=accepted_input.input_identity,
                episode_sequence=episode_sequence,
            )
            with self._lock:
                self._interruption_reply_reservations[request_id] = reservation
        if self._safe_submit_expression(context, source.request):
            return True
        failed_reservation = replace(reservation, failure_fact_pending=True)
        with self._lock:
            if self._interruption_reply_reservations.get(request_id) == reservation:
                self._interruption_reply_reservations[request_id] = failed_reservation
        self._record_interruption_reply_failure(
            request_id=request_id,
            reservation=failed_reservation,
        )
        return False

    def _record_interruption_reply_failure(
        self,
        *,
        request_id: str,
        reservation: _InterruptionReplyReservation,
    ) -> bool:
        """使用 reservation 身份记录辅助回应的确定性失败事实。

        Args:
            request_id: 已经预留 episode 顺序号的辅助请求编号。
            reservation: 该请求对应的反思、输入和统一账本顺序。

        Returns:
            Runtime Memory 已接受首次或重复失败记录时返回真。
        """

        try:
            # 失败事实和正常正文都必须与 Episode close 共用同一把生命周期锁。
            # 否则热替换恰好在 append 前关闭账本时，会把已经预留的回应吞掉。
            with self._episode_lifecycle.transaction():
                append_status = self._runtime_memory.append_unattended_interruption_reply(
                    UnattendedEpisodeIdentity(
                        self_reflection_id=reservation.self_reflection_id,
                        runtime_epoch=reservation.runtime_epoch,
                        episode_id=reservation.episode_id,
                    ),
                    UnattendedInterruptionReply(
                        request_id=request_id,
                        generation_id=None,
                        trigger_input_identity=reservation.trigger_input_identity,
                        content="",
                        settlement="failed",
                        episode_sequence=reservation.episode_sequence,
                        settled_at=self._now(),
                    ),
                )
        except Exception:
            logger.exception(
                "辅助打扰回应提交失败事实写入 episode 失败 request_id=%s episode_id=%s",
                request_id,
                reservation.episode_id,
            )
            return False
        committed = append_status in {"appended", "duplicate"}
        if committed:
            with self._lock:
                if (
                    self._interruption_reply_reservations.get(request_id)
                    == reservation
                ):
                    self._interruption_reply_reservations.pop(request_id, None)
        return committed

    def _commit_interruption_reply(
        self,
        settlement: VisibleOutputSettlement,
    ) -> ExpressionSettlementDisposition:
        """把辅助回应的 committed settlement 幂等追加到 episode 账本。

        Args:
            settlement: 已完成正文广播和 TTS 排空的辅助输出结算。

        Returns:
            已提交返回 ``committed``；迟到或已消费返回 ``ignored``；
            当前 episode 暂时不能接纳时返回 ``retryable``。
        """

        with self._lock:
            reservation = self._interruption_reply_reservations.get(
                settlement.request_id
            )
        if reservation is None:
            return "ignored"
        if reservation.failure_fact_pending:
            # 一旦判定这条输出不可信，后续重试只能补写同一个失败事实，不能再让
            # 另一份正文借同一 request_id 越过已经确定的失败边界。
            return (
                "committed"
                if self._record_interruption_reply_failure(
                    request_id=settlement.request_id,
                    reservation=reservation,
                )
                else "retryable"
            )
        identity_mismatch = _first_identity_mismatch(
            (
                ("history_policy", "unattended_episode", settlement.history_policy),
                (
                    "request_source",
                    "self_reflection_interruption_reply",
                    settlement.request_source,
                ),
                (
                    "self_reflection_id",
                    reservation.self_reflection_id,
                    settlement.self_reflection_id,
                ),
                ("episode_id", reservation.episode_id, settlement.episode_id),
                (
                    "runtime_epoch",
                    reservation.runtime_epoch,
                    settlement.runtime_epoch,
                ),
                (
                    "trigger_input_identity",
                    reservation.trigger_input_identity,
                    settlement.trigger_input_identity,
                ),
                (
                    "generation_id",
                    True,
                    bool(settlement.generation_id),
                ),
            )
        )
        if identity_mismatch is not None:
            field_name, expected, actual = identity_mismatch
            if not reservation.failure_fact_pending:
                failed_reservation = replace(
                    reservation,
                    failure_fact_pending=True,
                )
                with self._lock:
                    if (
                        self._interruption_reply_reservations.get(
                            settlement.request_id
                        )
                        != reservation
                    ):
                        return "ignored"
                    self._interruption_reply_reservations[
                        settlement.request_id
                    ] = failed_reservation
                reservation = failed_reservation
                logger.error(
                    "辅助回应 settlement 身份冲突，按 reservation 记录失败事实 "
                    "request_id=%s field=%s expected=%r actual=%r",
                    settlement.request_id,
                    field_name,
                    expected,
                    actual,
                )
            return (
                "committed"
                if self._record_interruption_reply_failure(
                    request_id=settlement.request_id,
                    reservation=reservation,
                )
                else "retryable"
            )
        if settlement.outcome == "completed":
            reply_settlement = "completed"
        elif settlement.outcome in {"interrupted", "superseded"}:
            reply_settlement = "interrupted"
        else:
            reply_settlement = "failed"
        with self._episode_lifecycle.transaction():
            # 只有 append 与 reservation 消费处于同一事务，close 才能确定地看到
            # “尚未结算”或“已经写入”两种状态之一，不会形成半提交窗口。
            append_status = self._runtime_memory.append_unattended_interruption_reply(
                UnattendedEpisodeIdentity(
                    self_reflection_id=reservation.self_reflection_id,
                    runtime_epoch=reservation.runtime_epoch,
                    episode_id=reservation.episode_id,
                ),
                UnattendedInterruptionReply(
                    request_id=settlement.request_id,
                    generation_id=settlement.generation_id,
                    trigger_input_identity=reservation.trigger_input_identity,
                    content=settlement.visible_text,
                    settlement=reply_settlement,
                    episode_sequence=reservation.episode_sequence,
                    settled_at=self._now(),
                ),
            )
        if append_status in {"appended", "duplicate"}:
            with self._lock:
                if (
                    self._interruption_reply_reservations.get(
                        settlement.request_id
                    )
                    == reservation
                ):
                    self._interruption_reply_reservations.pop(
                        settlement.request_id,
                        None,
                    )
            return "committed"
        return "retryable"

    def observe_expression_settled(
        self,
        settlement: VisibleOutputSettlement,
    ) -> ExpressionSettlementDisposition:
        """用可见输出结算推进 Recovery，并只在最终结算后关闭状态。

        Args:
            settlement: 已完成广播和所属账本归档的 committed 输出结算。

        Returns:
            当前结算已提交、可以安全忽略或必须由 Journal 重试的明确裁决。
        """

        request_source = settlement.request_source
        with self._lock:
            auxiliary_reservation_exists = (
                settlement.request_id in self._interruption_reply_reservations
            )
            primary_reservation_exists = (
                settlement.request_id in self._primary_output_reservations
            )
        # request_source 本身也可能是损坏字段，所以先用 request_id 找权威 reservation，
        # 再由对应提交路径逐字段判断，不能让伪造来源绕过资源收口。
        if auxiliary_reservation_exists:
            return self._commit_interruption_reply(settlement)
        if not primary_reservation_exists:
            return "ignored"
        retired_primary_settlement = False
        primary_identity_mismatch: tuple[str, object, object] | None = None
        with self._episode_lifecycle.transaction():
            with self._lock:
                primary_reservation = self._primary_output_reservations.get(
                    settlement.request_id
                )
            if primary_reservation is None:
                return "ignored"
            primary_identity_mismatch = _first_identity_mismatch(
                (
                    ("history_policy", "stable_dialogue", settlement.history_policy),
                    (
                        "request_source",
                        primary_reservation.request_source,
                        request_source,
                    ),
                    (
                        "self_reflection_id",
                        primary_reservation.self_reflection_id,
                        settlement.self_reflection_id,
                    ),
                    (
                        "episode_id",
                        primary_reservation.episode_id,
                        settlement.episode_id,
                    ),
                    (
                        "runtime_epoch",
                        primary_reservation.runtime_epoch,
                        settlement.runtime_epoch,
                    ),
                    (
                        "generation_id",
                        True,
                        bool(settlement.generation_id),
                    ),
                )
            )
            if (
                primary_reservation.runtime_epoch != self._current_runtime_epoch()
                or primary_reservation.runtime_epoch != self._attention.runtime_epoch
            ):
                # 旧 Runtime 的输出只负责清理原 reservation；它不能驱动新世代的
                # Recovery 状态转换，但必须唤醒旧 Episode 的 settlement-only reaper。
                self._remove_primary_output(settlement.request_id)
                retired_primary_settlement = True
        if primary_identity_mismatch is not None:
            field_name, expected, actual = primary_identity_mismatch
            logger.error(
                "主输出 settlement 身份冲突，按 reservation 终结 Self-Reflection "
                "request_id=%s field=%s expected=%r actual=%r",
                settlement.request_id,
                field_name,
                expected,
                actual,
            )
        if retired_primary_settlement:
            self._retry_runtime_termination_cleanups()
            return "committed"
        reflection_id = primary_reservation.self_reflection_id
        snapshot = self._attention.snapshot(reflection_id)
        if (
            snapshot is None
            or snapshot.runtime_epoch != primary_reservation.runtime_epoch
            or snapshot.episode_id != primary_reservation.episode_id
        ):
            self._remove_primary_output(settlement.request_id)
            self._retry_runtime_termination_cleanups()
            return "committed"
        if primary_identity_mismatch is not None:
            outcome = SelfReflectionOutcomeFact.failed(
                self_reflection_id=reflection_id,
                source_activity_id=snapshot.source_activity_id,
                reason="assistant_output_identity_conflict",
            )
            disposition = (
                "committed" if self._settle_outcome(outcome) else "retryable"
            )
            if disposition == "committed":
                self._remove_primary_output(settlement.request_id)
            return disposition

        if request_source == "self_reflection_entry":
            # 入场原话只有结算后才确定；随 Recovery 提交一并注入事实，让恢复轮
            # 把自己刚说过的话当作已发生事实，而不是像第一次听到这个请求。
            disposition = (
                "committed"
                if self._submit_recovery_turn(
                    reflection_id,
                    entry_public_text=settlement.visible_text,
                )
                else "retryable"
            )
            if disposition == "committed":
                self._remove_primary_output(settlement.request_id)
            return disposition

        with self._lock:
            outcome = self._recovery_outcomes.get(reflection_id)
        if outcome is None:
            settlement_outcome = settlement.outcome
            if settlement_outcome == "completed":
                outcome = SelfReflectionOutcomeFact.direct_response(
                    self_reflection_id=reflection_id,
                    source_activity_id=snapshot.source_activity_id,
                )
            else:
                outcome = SelfReflectionOutcomeFact.failed(
                    self_reflection_id=reflection_id,
                    source_activity_id=snapshot.source_activity_id,
                    reason=(
                        f"recovery_output_{settlement_outcome}"
                        if settlement_outcome
                        else "recovery_output_unavailable"
                    ),
                )
        disposition = "committed" if self._settle_outcome(outcome) else "retryable"
        if disposition == "committed":
            self._remove_primary_output(settlement.request_id)
        return disposition

    def observe_reply_admission_closed(self, event: RuntimeEvent) -> bool:
        """消费子进程在首段 Recovery 正文前提交的辅助准入关闭事实。

        Args:
            event: 携带完整反思、episode 和运行时身份的内部事件。

        Returns:
            当前 Attention 接受首次或重复关闭时返回真。
        """

        if event.type != "foreground.self_reflection_reply_admission_closed":
            return False
        reflection_id = _text(event.payload.get("self_reflection_id"))
        episode_id = _text(event.payload.get("episode_id"))
        runtime_epoch = event.payload.get("runtime_epoch")
        if type(runtime_epoch) is not int:
            return False
        # 首段正文的关闭事件必须和输入归属共用生命周期锁，否则事件队列与
        # Final ASR 恰好并发时，输入可能在关闭边界两侧被重复裁决。
        with self._episode_lifecycle.transaction():
            return self._attention.close_interruption_reply_admission(
                self_reflection_id=reflection_id,
                runtime_epoch=runtime_epoch,
                episode_id=episode_id,
            )

    def reserve_assistant_request(
        self,
        *,
        request_id: str,
        runtime_epoch: int,
        request_source: str,
    ) -> AssistantRequestAdmissionLeaseProtocol | None:
        """为 System Notice 取得与 Attention Admission 互斥的提交资格。

        Args:
            request_id: 即将进入 Assistant Bridge 的稳定请求编号。
            runtime_epoch: 请求准备时所属 Assistant Runtime 世代。
            request_source: 当前只允许 ``system_notice``。

        Returns:
            没有 Attention 屏障时返回必须提交或回滚的 lease，否则返回空。
        """

        return self._attention.reserve_assistant_request(
            request_id=request_id,
            runtime_epoch=runtime_epoch,
            request_source=request_source,
        )

    def is_attention_active(self, *, runtime_epoch: int) -> bool:
        """判断当前世代是否仍由 Self-Reflection 持有前台注意力。

        Args:
            runtime_epoch: 输入准备归属裁决时观察到的 Assistant Runtime 世代。

        Returns:
            输入仍应进入当前 episode 时返回真。
        """

        return self._attention.is_attention_active(runtime_epoch=runtime_epoch)

    def observe_outcome(self, event: RuntimeEvent) -> bool:
        """冻结工具执行事实，等待同一 Recovery generation 最终结算。

        Args:
            event: 子进程发布的结构化自我反思结果事件。

        Returns:
            结果属于当前世代并成功冻结或已幂等存在时返回真。
        """

        if event.type != "foreground.self_reflection_outcome":
            return False
        if not _event_belongs_to_current_epoch(
            event,
            current_runtime_epoch=self._current_runtime_epoch,
        ):
            return False
        try:
            outcome = SelfReflectionOutcomeFact.from_payload(event.payload)
        except ValueError:
            return False
        snapshot = self._attention.snapshot(outcome.self_reflection_id)
        if snapshot is None or snapshot.source_activity_id != outcome.source_activity_id:
            return False
        with self._lock:
            existing = self._recovery_outcomes.get(outcome.self_reflection_id)
            if existing is not None:
                return existing == outcome
            self._recovery_outcomes[outcome.self_reflection_id] = outcome
        return True

    def reap_expired(self) -> int:
        """请求超时 Recovery 生成 fallback，并重试待清理资源。

        Returns:
            本次请求兜底或清理成功的数量。
        """

        processed = self._retry_source_material_cleanups()
        processed += self._retry_admission_cleanups()
        processed += self._retry_runtime_termination_cleanups()
        processed += self._retry_interruption_reply_failures()
        processed += self._retry_episode_publications()
        with self._lock:
            pending_outcomes = tuple(self._pending_outcomes.values())
        for pending in pending_outcomes:
            if self._settle_outcome(pending):
                processed += 1

        transitions = self._attention.reap_expired()
        for transition in transitions:
            snapshot = transition.snapshot
            if snapshot is None or snapshot.outcome is None:
                continue
            with self._lock:
                self._pending_deadline_fallbacks[snapshot.self_reflection_id] = (
                    snapshot.runtime_epoch,
                    "reflection_deadline_exceeded",
                )
        with self._lock:
            pending_fallbacks = tuple(self._pending_deadline_fallbacks.items())
        for reflection_id, (runtime_epoch, reason) in pending_fallbacks:
            if self._request_recovery_fallback is None:
                continue
            try:
                accepted = self._request_recovery_fallback(
                    reflection_id,
                    runtime_epoch,
                    reason,
                )
            except Exception:
                accepted = False
            if accepted is not False:
                with self._lock:
                    self._pending_deadline_fallbacks.pop(reflection_id, None)
                processed += 1
        return processed

    def _retry_source_material_cleanups(self) -> int:
        """重试普通 Cognitive 终态遗留的材料释放。

        Returns:
            本次成功释放并删除凭据的来源材料数量。
        """

        with self._lock:
            cleanups = tuple(self._pending_source_material_cleanups.items())
        completed = 0
        for activity_id, _cleanup in cleanups:
            with self._lock:
                if activity_id in self._active_source_material_cleanup_ids:
                    continue
                cleanup = self._pending_source_material_cleanups.get(activity_id)
                if cleanup is None:
                    continue
                self._active_source_material_cleanup_ids.add(activity_id)
            try:
                if not self._release_material_context(cleanup.material_context_id):
                    continue
                with self._lock:
                    current = self._pending_source_material_cleanups.get(activity_id)
                    if current == cleanup:
                        self._pending_source_material_cleanups.pop(activity_id, None)
                completed += 1
            finally:
                with self._lock:
                    self._active_source_material_cleanup_ids.discard(activity_id)
        return completed

    def settle_background_material_acceptance(
        self,
        event: RuntimeEvent,
        *,
        material_context_id: str,
        task_result_material_retained: bool,
        adopt_material_lease: Callable[[RuntimeEvent, str], bool],
    ) -> bool:
        """把 Recovery 持有的材料租约交给已接纳的 Background Activity。

        TaskResult 已经取得自己的引用时，本方法释放 Recovery 的那一份；TaskResult
        retain 失败但 Recovery 的引用仍在时，则先让 TaskResult 接管现有引用，再由
        Recovery 放弃所有权。两条路径都不会出现引用计数短暂归零。

        Args:
            event: `start_background_work` 的 canonical acceptance 事件。
            material_context_id: TaskResult 从 acceptance 冻结的材料上下文编号。
            task_result_material_retained: TaskResult 是否已取得自己的新租约。
            adopt_material_lease: TaskResult 接管 Recovery 现有租约的同步端口。

        Returns:
            本事件属于当前 Recovery 且材料所有权已经完成交接时返回真。
        """

        payload = event.payload
        normalized_context_id = material_context_id.strip()
        self_reflection_id = _text(payload.get("self_reflection_id"))
        source_activity_id = _text(payload.get("recovery_of_activity_id"))
        if (
            event.type != "llm.foreground.tool_call_completed"
            or payload.get("name") != "start_background_work"
            or payload.get("state") != "accepted"
            or type(task_result_material_retained) is not bool
            or not normalized_context_id
            or _material_context_id(payload) != normalized_context_id
            or not self_reflection_id
            or not source_activity_id
        ):
            return False
        snapshot = self._attention.snapshot(self_reflection_id)
        if (
            snapshot is None
            or snapshot.source_activity_id != source_activity_id
            or event.runtime_epoch != snapshot.runtime_epoch
        ):
            return False

        with self._lock:
            owned_context_id = self._material_contexts.get(self_reflection_id)
            if owned_context_id != normalized_context_id:
                return False
            if self_reflection_id in self._active_material_handoff_ids:
                return False
            self._active_material_handoff_ids.add(self_reflection_id)
        try:
            if task_result_material_retained:
                if not self._release_material_context(normalized_context_id):
                    return False
            elif not adopt_material_lease(event, normalized_context_id):
                return False
            with self._lock:
                if (
                    self._material_contexts.get(self_reflection_id)
                    == normalized_context_id
                ):
                    self._material_contexts.pop(self_reflection_id, None)
            self._mark_material_lease_transferred(
                source_activity_id,
                normalized_context_id,
            )
            return True
        except Exception:
            # 交接失败时 Recovery 继续持有原租约；重复 acceptance 或结果收口仍可重试/释放。
            return False
        finally:
            with self._lock:
                self._active_material_handoff_ids.discard(self_reflection_id)

    def terminate_runtime_epoch(
        self,
        runtime_epoch: int,
        *,
        new_runtime_epoch: int,
        reason: str = "runtime_reconfigured",
    ) -> None:
        """切换 Attention 到新世代，并释放旧世代尚未交接的恢复资源。

        Args:
            runtime_epoch: 即将被替换的旧助手世代。
            new_runtime_epoch: 已发布的新助手世代。
            reason: 写入终止快照的稳定原因。
        """

        if type(runtime_epoch) is not int or type(new_runtime_epoch) is not int:
            raise ValueError("runtime_epoch 必须是整数")
        if new_runtime_epoch <= runtime_epoch:
            raise ValueError("new_runtime_epoch 必须严格大于旧世代")
        normalized_reason = reason.strip() if isinstance(reason, str) else ""
        if not normalized_reason:
            raise ValueError("reason 不能为空")
        terminated = self._episode_lifecycle.begin_runtime_termination(
            runtime_epoch=runtime_epoch,
            new_runtime_epoch=new_runtime_epoch,
        )
        termination_cleanups: dict[str, _RuntimeTerminationCleanup] = {}
        for transition in terminated:
            snapshot = transition.snapshot
            if snapshot is None:
                continue
            # closing_inputs 已经冻结真实 outcome 时必须沿用它。Task Memory 可能已提交
            # 该结果，热替换不能再用 runtime_reconfigured 制造冲突关系。
            outcome = snapshot.outcome or SelfReflectionOutcomeFact.failed(
                self_reflection_id=snapshot.self_reflection_id,
                source_activity_id=snapshot.source_activity_id,
                reason=normalized_reason,
            )
            termination_cleanups[snapshot.self_reflection_id] = (
                _RuntimeTerminationCleanup(
                    snapshot=snapshot,
                    outcome=outcome,
                    material_context_id=None,
                )
            )

        sources_to_release: list[_SourceRecord] = []
        with self._lock:
            old_sources = {
                activity_id: source
                for activity_id, source in self._sources.items()
                if source.runtime_epoch == runtime_epoch
            }
            active_source_activity_ids = {
                cleanup.snapshot.source_activity_id
                for cleanup in termination_cleanups.values()
            }
            old_source_activity_ids = set(old_sources)
            old_source_activity_ids.update(
                cleanup.snapshot.source_activity_id
                for cleanup in termination_cleanups.values()
            )
            for activity_id in old_source_activity_ids:
                source = old_sources.get(activity_id)
                if (
                    source is not None
                    and activity_id not in active_source_activity_ids
                    and source.material_lease_retained
                    and source.material_context_id is not None
                ):
                    sources_to_release.append(source)
                self._sources.pop(activity_id, None)

            # 已关闭但结果尚未提交的旧世代不在 Attention active 指针中，必须按
            # 来源世代显式删除，防止新 Runtime 的周期 reaper 重新播放旧结果。
            stale_outcome_ids = {
                reflection_id
                for reflection_id, outcome in self._pending_outcomes.items()
                if outcome.source_activity_id in old_source_activity_ids
            }
            for reflection_id in stale_outcome_ids:
                self._pending_outcomes.pop(reflection_id, None)
                self._recovery_outcomes.pop(reflection_id, None)
                self._pending_recovery_turns.pop(reflection_id, None)
                self._submitted_recovery_turn_ids.discard(reflection_id)
                self._pending_deadline_fallbacks.pop(reflection_id, None)

            for reflection_id, cleanup in termination_cleanups.items():
                context_id = self._material_contexts.pop(reflection_id, None)
                if context_id is None:
                    source = old_sources.get(cleanup.snapshot.source_activity_id)
                    if (
                        source is not None
                        and source.material_lease_retained
                        and source.material_context_id is not None
                    ):
                        context_id = source.material_context_id
                self._pending_outcomes.pop(reflection_id, None)
                self._recovery_outcomes.pop(reflection_id, None)
                self._pending_recovery_turns.pop(reflection_id, None)
                self._submitted_recovery_turn_ids.discard(reflection_id)
                self._pending_deadline_fallbacks.pop(reflection_id, None)
                self._pending_runtime_termination_cleanups[reflection_id] = replace(
                    cleanup,
                    material_context_id=context_id,
                )
        for source in sources_to_release:
            self._release_source_material(source)
        # 热替换已经完成线性化；清理失败只能留下分阶段凭据，不能反向阻止新世代。
        self._retry_runtime_termination_cleanups()

    def publish_runtime_candidate(
        self,
        publish_candidate: Callable[[], AssistantProcessReplacement],
        mark_committed: Callable[[AssistantProcessReplacement], None],
    ) -> AssistantProcessReplacement:
        """在候选助手世代发布的同一线性化点终止旧 Attention。

        Args:
            publish_candidate: 已准备好的候选发布函数；调用时不得再等待其它业务锁。
            mark_committed: 稳定句柄交换后立即把外层事务标为已提交的回调。

        Returns:
            Assistant Supervisor 返回的世代交接事实。

        Raises:
            TypeError: 发布动作或提交点回调不可调用。
        """

        # 回调契约必须在稳定句柄交换前确认；交换后只允许提交和尽力清理。
        if not callable(publish_candidate) or not callable(mark_committed):
            raise TypeError("候选发布动作和提交点回调必须可调用")

        with self._episode_lifecycle.transaction():
            replacement = publish_candidate()
            # 外层事务必须在任何旧世代清理之前越过不可回滚提交点。
            mark_committed(replacement)
            previous_epoch = replacement.old_epoch if replacement.old_epoch is not None else self._attention.runtime_epoch
            if previous_epoch == replacement.current_epoch:
                return replacement
            try:
                self.terminate_runtime_epoch(
                    previous_epoch,
                    new_runtime_epoch=replacement.current_epoch,
                )
            except Exception:
                # 稳定句柄已经切换，旧世代收口失败不能把新配置伪装成未提交；
                # 保留新世代并记录诊断，后续清理机制负责继续收口。
                logger.exception(
                    "发布新助手世代后旧 Self-Reflection 收口失败 runtime_epoch=%s",
                    previous_epoch,
                )
            return replacement

    def _settle_outcome(
        self,
        outcome: SelfReflectionOutcomeFact,
    ) -> bool:
        """在最终可见输出结算后关闭 episode、任务记忆和资源。

        Runtime Memory 或材料副作用发生瞬时异常时保持 ``FINALIZING``，重复事件可以
        从同一个幂等边界继续，期间新输入仍进入 episode。``CLOSING_INPUTS`` 只在
        episode 事务锁内短暂存在；在所有权威事实落库前绝不释放普通输入准入。

        Args:
            outcome: 已通过结构校验且身份匹配的唯一恢复结果。

        Returns:
            所有权威事实已经提交且 Attention 已释放时返回真。
        """

        reflection_id = outcome.self_reflection_id
        with self._lock:
            if reflection_id in self._settling_outcomes:
                return False
            self._settling_outcomes.add(reflection_id)
        try:
            snapshot = self._attention.snapshot(reflection_id)
            if snapshot is None:
                return False
            if outcome.source_activity_id != snapshot.source_activity_id:
                # 结果身份必须同时匹配 Attention 快照和来源 Activity，不能让迟到或伪造
                # 的结果把恢复关系写到另一项 Cognitive 任务上。
                return False
            if snapshot.state is SelfReflectionState.TERMINATED:
                return True
            if snapshot.state in {
                SelfReflectionState.CLOSED,
                SelfReflectionState.CLOSED_FAILED,
            }:
                return True
            # 辅助 reservation 的登记和 Recovery begin_close 必须共享生命周期锁。
            # 否则顺序号已经分配但 reservation 尚未登记时，Recovery 会抢先关闭 Episode。
            with self._episode_lifecycle.transaction():
                with self._lock:
                    auxiliary_settlement_pending = any(
                        reservation.self_reflection_id == reflection_id
                        and reservation.runtime_epoch == snapshot.runtime_epoch
                        for reservation in self._interruption_reply_reservations.values()
                    )
                if auxiliary_settlement_pending:
                    # 子进程停止输出只保证不会再产生正文；辅助 settlement 还必须真正写入
                    # episode。不同 generation 的提交 lane 可以独立重试，因此这里把最终
                    # outcome 留给 reaper，绝不能先关闭账本吞掉已经公开的辅助回应。
                    self._remember_pending_outcome(outcome, snapshot.runtime_epoch)
                    return False

                close = self._attention.begin_close(
                    reflection_id,
                    runtime_epoch=snapshot.runtime_epoch,
                    terminal_state=(
                        SelfReflectionState.CLOSED_FAILED
                        if outcome.kind.value == "reflection_failed"
                        else SelfReflectionState.CLOSED
                    ),
                    reason="recovery_outcome_received",
                    outcome=outcome,
                )
                if (
                    close.status == "rejected"
                    and close.reason == "reflection_deadline_exceeded"
                    and close.snapshot is not None
                    and close.snapshot.outcome is not None
                ):
                    outcome = close.snapshot.outcome
                    close = self._attention.begin_close(
                        reflection_id,
                        runtime_epoch=snapshot.runtime_epoch,
                        terminal_state=SelfReflectionState.CLOSED_FAILED,
                        reason="reflection_deadline_exceeded",
                        outcome=outcome,
                    )
            if (
                close.status not in {"applied", "duplicate"}
                or close.snapshot is None
                or close.snapshot.outcome is None
            ):
                return False
            # begin_close 只冻结首次结果并进入 finalizing；重试期间输入仍可继续追加。
            # 后续副作用必须使用 Attention 返回的权威结果，不能相信迟到事件携带的副本。
            outcome = close.snapshot.outcome

            try:
                self._runtime_memory.commit_task_recovery(
                    source_activity_id=outcome.source_activity_id,
                    outcome=_task_recovery_outcome(outcome),
                    recovered_by_activity_id=outcome.recovered_by_activity_id,
                )
            except Exception:
                self._remember_pending_outcome(outcome, snapshot.runtime_epoch)
                return False

            with self._lock:
                if reflection_id in self._active_material_handoff_ids:
                    self._remember_pending_outcome(outcome, snapshot.runtime_epoch)
                    return False
                context_id = self._material_contexts.get(reflection_id)
            if context_id is not None:
                if not self._release_material_context(context_id):
                    self._remember_pending_outcome(outcome, snapshot.runtime_epoch)
                    return False
                with self._lock:
                    self._material_contexts.pop(reflection_id, None)

            # 真正的输入冻结、稳定历史和 Attention 释放共享同一把服务事务锁。
            # CLOSING_INPUTS 因此只存在于这个临界区内部，锁外输入要么仍进入 episode，
            # 要么已经拿到普通 Turn 资格，不会落进半关闭黑洞。
            try:
                completed = self._episode_lifecycle.close_completed_episode(
                    close.snapshot
                )
            except Exception:
                self._remember_pending_outcome(outcome, snapshot.runtime_epoch)
                return False
            if completed.status not in {"applied", "duplicate"}:
                current = self._attention.snapshot(reflection_id)
                if (
                    self._attention.runtime_epoch != snapshot.runtime_epoch
                    or current is None
                    or current.state is SelfReflectionState.TERMINATED
                ):
                    return True
                self._remember_pending_outcome(outcome, snapshot.runtime_epoch)
                return False
            with self._lock:
                self._pending_outcomes.pop(reflection_id, None)
                self._pending_recoveries.pop(reflection_id, None)
                self._pending_recovery_turns.pop(reflection_id, None)
                self._submitted_recovery_turn_ids.discard(reflection_id)
                self._pending_deadline_fallbacks.pop(reflection_id, None)
                self._recovery_outcomes.pop(reflection_id, None)
            self._forget_source(outcome.source_activity_id)
            return True
        finally:
            with self._lock:
                self._settling_outcomes.discard(reflection_id)

    def _retry_episode_publications(self) -> int:
        """重试已经稳定提交、但公共闭合事件暂时未能入队的 episode。

        Returns:
            本轮成功发布并删除补偿凭据的 episode 数量。
        """

        return self._episode_lifecycle.retry_publications()

    def acknowledge_episode_publication(
        self,
        *,
        runtime_epoch: int,
        episode_id: str,
    ) -> bool:
        """确认 Desktop 已成功接收一整块 episode 历史。

        Args:
            runtime_epoch: Episode 所属 Assistant Runtime 世代。
            episode_id: Episode 稳定编号。

        Returns:
            生命周期 outbox 首次或重复确认成功时返回真。
        """

        return self._episode_lifecycle.acknowledge_publication(
            runtime_epoch=runtime_epoch,
            episode_id=episode_id,
        )

    def fail_episode_publication(
        self,
        *,
        runtime_epoch: int,
        episode_id: str,
    ) -> bool:
        """记录 Desktop 公共广播失败并保留 outbox 重试凭据。

        Args:
            runtime_epoch: Episode 所属 Assistant Runtime 世代。
            episode_id: Episode 稳定编号。

        Returns:
            成功解除本次 in-flight 标记时返回真。
        """

        return self._episode_lifecycle.fail_publication(
            runtime_epoch=runtime_epoch,
            episode_id=episode_id,
        )

    def _forget_source(self, activity_id: str) -> None:
        """在最终表达已提交后释放来源快照，并保留失败终态墓碑。

        Args:
            activity_id: 已经完成恢复收口的 Cognitive Activity 编号。
        """

        with self._lock:
            source = self._sources.pop(activity_id, None)
            if source is not None:
                self._settled_source_activity_ids.add(activity_id)

    def _discard_source(
        self,
        activity_id: str,
        *,
        release_material: bool = False,
    ) -> None:
        """丢弃来源快照，并按调用方声明释放尚未转移的材料租约。

        Args:
            activity_id: 要丢弃的 Cognitive Activity 编号。
            release_material: 当前调用方是否还没有执行材料释放。
        """

        with self._lock:
            source = self._sources.pop(activity_id, None)
        if (
            release_material
            and source is not None
            and source.material_lease_retained
            and source.material_context_id is not None
        ):
            self._release_source_material(source)

    def _release_source_material(self, source: _SourceRecord) -> bool:
        """释放来源 Cognitive 持有的材料租约，失败时留下可重试凭据。

        Args:
            source: 已从来源账本取出的冻结记录。

        Returns:
            释放成功或来源本来没有租约时返回真。
        """

        if not source.material_lease_retained or source.material_context_id is None:
            return True
        if self._release_material_context(source.material_context_id):
            return True
        with self._lock:
            self._pending_source_material_cleanups[source.activity_id] = (
                _SourceMaterialCleanup(
                    activity_id=source.activity_id,
                    material_context_id=source.material_context_id,
                    runtime_epoch=source.runtime_epoch,
                )
            )
        return False

    def _release_material_context(self, context_id: str) -> bool:
        """调用材料领域释放租约，并把返回的权威变更事件交给发布端口。

        Args:
            context_id: 需要释放的材料上下文编号。

        Returns:
            材料端口成功完成一次释放调用时返回真。
        """

        try:
            raw_events = self._material_lifecycle.release_turn(context_id)
        except Exception as exception:
            logger.warning(
                "Self-Reflection 材料租约释放失败 context_id=%s error=%s",
                context_id,
                exception,
            )
            return False
        if not isinstance(raw_events, list):
            return True
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            publisher = self._publish_material_event
            if publisher is None:
                continue
            try:
                publisher(dict(event))
            except Exception as exception:
                # 资产释放已经成功，发布失败不能再做第二次 release；上层事件队列
                # 会在自己的重试边界处理这条瞬时通知。
                logger.warning(
                    "Self-Reflection 材料变更事件发布失败 event_type=%s error=%s",
                    event.get("type"),
                    exception,
                )
        return True

    def _mark_material_lease_transferred(
        self,
        activity_id: str,
        context_id: str,
    ) -> None:
        """把来源记录中的材料所有权标记为已经交给 TaskResult。

        Args:
            activity_id: 失去材料所有权的来源 Cognitive Activity 编号。
            context_id: 已由 TaskResult 接管的材料上下文编号。
        """

        with self._lock:
            source = self._sources.get(activity_id)
            if source is None or source.material_context_id != context_id:
                return
            self._sources[activity_id] = replace(
                source,
                material_lease_retained=False,
            )

    def _remember_pending_outcome(
        self,
        outcome: SelfReflectionOutcomeFact,
        runtime_epoch: int,
    ) -> None:
        """仅在来源仍属于当前世代时保留可重试结果。

        Args:
            outcome: 尚未完成资源收口的冻结恢复结果。
            runtime_epoch: 开始本次收口时的 Assistant Runtime 世代。
        """

        with self._lock:
            source = self._sources.get(outcome.source_activity_id)
            if source is None or source.runtime_epoch != runtime_epoch:
                return
            self._pending_outcomes[outcome.self_reflection_id] = outcome

    def _retry_interruption_reply_failures(self) -> int:
        """重试仍未写入 episode 的辅助回应失败事实。

        Returns:
            本次成功补写到 episode 的失败记录数量。
        """

        with self._lock:
            pending = tuple(
                (request_id, reservation)
                for request_id, reservation in self._interruption_reply_reservations.items()
                if reservation.failure_fact_pending
            )
        completed = 0
        for request_id, reservation in pending:
            if self._record_interruption_reply_failure(
                request_id=request_id,
                reservation=reservation,
            ):
                completed += 1
        return completed

    def _retry_admission_cleanups(self) -> int:
        """重试准入副作用失败后的 episode、材料和 pin 清理。

        Returns:
            本次完整清理并删除凭据的失败 Admission 数量。
        """

        with self._lock:
            cleanups = tuple(self._pending_admission_cleanups.items())
        completed = 0
        for reflection_id, _snapshot in cleanups:
            with self._lock:
                if reflection_id in self._active_admission_cleanup_ids:
                    continue
                cleanup = self._pending_admission_cleanups.get(reflection_id)
                if cleanup is None:
                    continue
                self._active_admission_cleanup_ids.add(reflection_id)
            try:
                if cleanup.episode_opened:
                    try:
                        closed = self._episode_lifecycle.abort_admission(
                            cleanup.snapshot,
                            reason=cleanup.reason,
                        )
                    except Exception:
                        continue
                    if not closed:
                        continue
                    cleanup = replace(cleanup, episode_opened=False)
                    with self._lock:
                        self._pending_admission_cleanups[reflection_id] = cleanup
                if cleanup.material_context_id is not None:
                    if not self._release_material_context(
                        cleanup.material_context_id
                    ):
                        continue
                    cleanup = replace(cleanup, material_context_id=None)
                    with self._lock:
                        self._pending_admission_cleanups[reflection_id] = cleanup
                        self._material_contexts.pop(reflection_id, None)
                if cleanup.release_pin:
                    try:
                        self._runtime_memory.release_recovery_compaction_pin(
                            reflection_id
                        )
                    except Exception:
                        continue
                    cleanup = replace(cleanup, release_pin=False)
                    with self._lock:
                        self._pending_admission_cleanups[reflection_id] = cleanup
                with self._lock:
                    self._pending_admission_cleanups.pop(reflection_id, None)
                    self._material_contexts.pop(reflection_id, None)
                completed += 1
            finally:
                with self._lock:
                    self._active_admission_cleanup_ids.discard(reflection_id)
        return completed

    def _retry_runtime_termination_cleanups(self) -> int:
        """按关闭片段、提交关系、释放材料的顺序重试热替换清理。

        Returns:
            本次完整清理并删除凭据的旧世代恢复数量。
        """

        with self._lock:
            cleanups = tuple(self._pending_runtime_termination_cleanups.items())
        completed = 0
        for reflection_id, _snapshot in cleanups:
            with self._lock:
                if reflection_id in self._active_runtime_termination_cleanup_ids:
                    continue
                cleanup = self._pending_runtime_termination_cleanups.get(reflection_id)
                if cleanup is None:
                    continue
                self._active_runtime_termination_cleanup_ids.add(reflection_id)
            try:
                with self._lock:
                    auxiliary_settlement_pending = any(
                        reservation.self_reflection_id == reflection_id
                        and reservation.runtime_epoch == cleanup.snapshot.runtime_epoch
                        for reservation in self._interruption_reply_reservations.values()
                    )
                    primary_settlement_pending = any(
                        reservation.self_reflection_id == reflection_id
                        and reservation.runtime_epoch == cleanup.snapshot.runtime_epoch
                        for reservation in self._primary_output_reservations.values()
                    )
                if auxiliary_settlement_pending or primary_settlement_pending:
                    continue
                if cleanup.episode_opened:
                    try:
                        closed = self._episode_lifecycle.close_terminated_episode(
                            cleanup.snapshot
                        )
                    except Exception:
                        continue
                    if not closed:
                        continue
                    cleanup = replace(cleanup, episode_opened=False)
                    with self._lock:
                        self._pending_runtime_termination_cleanups[reflection_id] = cleanup

                if cleanup.recovery_pending:
                    try:
                        self._runtime_memory.commit_task_recovery(
                            source_activity_id=cleanup.snapshot.source_activity_id,
                            outcome=_task_recovery_outcome(cleanup.outcome),
                            recovered_by_activity_id=(
                                cleanup.outcome.recovered_by_activity_id
                            ),
                        )
                    except Exception:
                        continue
                    cleanup = replace(cleanup, recovery_pending=False)
                    with self._lock:
                        self._pending_runtime_termination_cleanups[
                            reflection_id
                        ] = cleanup

                if cleanup.material_context_id is not None:
                    if not self._release_material_context(
                        cleanup.material_context_id
                    ):
                        continue
                    cleanup = replace(cleanup, material_context_id=None)
                    with self._lock:
                        self._pending_runtime_termination_cleanups[
                            reflection_id
                        ] = cleanup

                with self._lock:
                    self._pending_runtime_termination_cleanups.pop(
                        reflection_id,
                        None,
                    )
                completed += 1
            finally:
                with self._lock:
                    self._active_runtime_termination_cleanup_ids.discard(
                        reflection_id
                    )
        return completed

    def _rollback_admission_resources(
        self,
        snapshot: Any,
        *,
        material_context_id: str | None,
        release_pin: bool,
        reason: str,
        episode_opened: bool = False,
    ) -> None:
        """释放准入阶段副作用，并在失败时留下可重试清理记录。

        Args:
            snapshot: 已经取得 Attention Admission 的身份快照。
            material_context_id: 本次恢复持有的可选材料上下文编号。
            release_pin: 本次失败是否仍需显式释放压缩门禁。
            reason: 写入 Attention 终止墓碑的稳定原因。
            episode_opened: Runtime Memory 是否已经创建未处理插话片段。
        """

        remaining_episode_opened = episode_opened
        if episode_opened:
            try:
                closed = self._episode_lifecycle.abort_admission(
                    snapshot,
                    reason=reason,
                )
            except Exception as exception:
                # 关闭失败会保留在准入清理账本里等待周期重试；这里记录首次失败，
                # 方便定位仍占用中的未处理输入片段，而不是静默吞掉资源异常。
                logger.warning(
                    "Self-Reflection 准入回滚关闭 episode 失败 self_reflection_id=%s error=%s",
                    snapshot.self_reflection_id,
                    exception,
                )
            else:
                if not closed:
                    logger.warning(
                        "Self-Reflection 准入回滚未能终止 Attention self_reflection_id=%s",
                        snapshot.self_reflection_id,
                    )
                else:
                    remaining_episode_opened = False
        remaining_material_context_id = material_context_id
        if material_context_id is not None:
            if self._release_material_context(material_context_id):
                remaining_material_context_id = None
                with self._lock:
                    self._material_contexts.pop(snapshot.self_reflection_id, None)
        remaining_release_pin = release_pin
        if release_pin:
            try:
                self._runtime_memory.release_recovery_compaction_pin(
                    snapshot.self_reflection_id
                )
            except Exception as exception:
                # pin 仍由 Runtime Memory 持有，后续清理会继续重试；记录失败可以区分
                # 正常幂等保留与存储端真实异常。
                logger.warning(
                    "Self-Reflection 准入回滚释放压缩 pin 失败 self_reflection_id=%s error=%s",
                    snapshot.self_reflection_id,
                    exception,
                )
            else:
                remaining_release_pin = False
        if (
            not remaining_episode_opened
            and remaining_material_context_id is None
            and not remaining_release_pin
        ):
            with self._lock:
                self._material_contexts.pop(snapshot.self_reflection_id, None)
        else:
            with self._lock:
                self._pending_admission_cleanups[snapshot.self_reflection_id] = (
                    _AdmissionCleanup(
                        snapshot=snapshot,
                        material_context_id=remaining_material_context_id,
                        release_pin=remaining_release_pin,
                        episode_opened=remaining_episode_opened,
                        reason=reason,
                    )
                )
        with self._lock:
            self._pending_recovery_turns.pop(snapshot.self_reflection_id, None)
            self._submitted_recovery_turn_ids.discard(snapshot.self_reflection_id)
            self._recovery_outcomes.pop(snapshot.self_reflection_id, None)
        if not episode_opened:
            self._attention.abort_admission(
                snapshot.self_reflection_id,
                runtime_epoch=snapshot.runtime_epoch,
                reason=reason,
            )
        self._discard_source(snapshot.source_activity_id)

    def _safe_submit_control(self, command: object, runtime_epoch: int) -> bool:
        """将控制端口异常归一为假，避免事件消费循环被打断。

        Args:
            command: 需要交给助手子进程的隐藏控制命令。
            runtime_epoch: 命令明确针对的助手运行时世代。

        Returns:
            控制端口明确接受命令时返回真。
        """

        try:
            return self._submit_control(command, runtime_epoch) is not False
        except Exception:
            return False

    def _safe_current_foreground_output_identity(
        self,
    ) -> tuple[str, str | None] | None:
        """读取接管前的普通 Foreground 输出身份，并吸收观察端口故障。

        Returns:
            有效的请求与 generation 身份；没有活跃普通输出或端口失败时返回空。
        """

        try:
            identity = self._current_foreground_output_identity()
        except Exception:
            return None
        if identity is None or not isinstance(identity, tuple) or len(identity) != 2:
            return None
        request_id, generation_id = identity
        normalized_request_id = _text(request_id)
        if not normalized_request_id:
            return None
        return normalized_request_id, _text(generation_id) or None

    def _submit_recovery_turn(
        self,
        reflection_id: str,
        *,
        entry_public_text: str = "",
    ) -> bool:
        """在入场表达结算后提交唯一的可见 Recovery 请求。

        Args:
            reflection_id: 已经完成入场表达的自我反思编号。
            entry_public_text: 入场表达已经成功公开并结算的原话；非空时回填进
                Recovery 事实，让恢复轮把自己刚说过的话当作已发生事实。

        Returns:
            Recovery 已经提交、幂等存在或失败事实已经收口时返回真。
        """

        with self._lock:
            if reflection_id in self._submitted_recovery_turn_ids:
                return True
            submission = self._pending_recovery_turns.get(reflection_id)
            if submission is None:
                return False
            self._submitted_recovery_turn_ids.add(reflection_id)

        context = submission.context
        normalized_entry_text = entry_public_text.strip()
        if normalized_entry_text:
            context = replace(
                context,
                prompt_facts=replace(
                    context.prompt_facts,
                    entry_public_text=normalized_entry_text,
                ),
            )

        self._reserve_primary_output(context)
        submitted = self._safe_submit_expression(
            context,
            submission.source_request,
        )
        if submitted:
            with self._lock:
                pending = self._pending_recoveries.get(reflection_id)
                if pending is not None:
                    self._pending_recoveries[reflection_id] = replace(
                        pending,
                        submission_status="recovery_output_accepted",
                    )
            return True

        with self._lock:
            self._submitted_recovery_turn_ids.discard(reflection_id)
        self._remove_primary_output(context.request_id)
        return self._settle_outcome(
            SelfReflectionOutcomeFact.failed(
                self_reflection_id=reflection_id,
                source_activity_id=submission.context.source_activity_id,
                reason="recovery_request_not_accepted",
            )
        )

    def _safe_submit_expression(
        self,
        context: (
            SelfReflectionEntryContext
            | SelfReflectionRecoveryContext
            | SelfReflectionInterruptionReplyContext
        ),
        request: AssistantRequest,
    ) -> bool:
        """提交入场或 Recovery 可见请求，并把端口异常收敛为拒绝。

        Args:
            context: 当前请求的结构化入场或 Recovery 上下文。
            request: 提供角色和可信历史的来源请求快照。

        Returns:
            表达端口接受请求时返回真。
        """

        try:
            return self._submit_expression(context, request) is not False
        except Exception:
            return False

    def _reserve_primary_output(
        self,
        context: SelfReflectionEntryContext | SelfReflectionRecoveryContext,
    ) -> None:
        """在提交主输出前登记 request，防止热替换过早关闭 Episode。

        Args:
            context: Entry 或 Recovery 的完整输出身份。
        """

        request_source = (
            "self_reflection_entry"
            if isinstance(context, SelfReflectionEntryContext)
            else "self_reflection_recovery"
        )
        reservation = _PrimaryOutputReservation(
            request_id=context.request_id,
            self_reflection_id=context.self_reflection_id,
            episode_id=context.episode_id,
            runtime_epoch=context.runtime_epoch,
            request_source=request_source,
        )
        with self._lock:
            existing = self._primary_output_reservations.get(context.request_id)
            if existing is not None and existing != reservation:
                raise ValueError("主输出 request_id 身份冲突")
            self._primary_output_reservations[context.request_id] = reservation

    def _remove_primary_output(self, request_id: str) -> None:
        """删除已经明确提交失败或完成结算的主输出 reservation。

        Args:
            request_id: 需要清理的主输出请求编号。
        """

        with self._lock:
            self._primary_output_reservations.pop(request_id, None)

    def _build_recovery_context(
        self,
        source: _SourceRecord,
        snapshot: Any,
        event: RuntimeEvent,
    ) -> SelfReflectionRecoveryContext:
        """把来源请求和失败事实投影成可见 Recovery 的结构化上下文。

        Args:
            source: 已冻结的 Cognitive 来源请求与材料事实。
            snapshot: Attention Admission 分配的身份和绝对截止时间。
            event: 触发恢复的 Cognitive 失败终态事件。

        Returns:
            可与来源 Foreground 请求一起提交的 Recovery 上下文。
        """

        # 槽位按名词短语设计：只填能力范围本身，不带句号，避免和模板骨架叠出病句。
        facts = RecoveryPromptFacts(
            original_user_request=source.request.user_text,
            previous_method_capability="分析和核查",
            failure_explanation=_text(event.payload.get("failure_reason")) or "刚才的处理没有完成用户目标。",
            tools=(),
        )
        return SelfReflectionRecoveryContext(
            request_id=f"self-reflection-recovery-{snapshot.self_reflection_id}",
            self_reflection_id=snapshot.self_reflection_id,
            episode_id=snapshot.episode_id,
            runtime_epoch=snapshot.runtime_epoch,
            source_activity_id=source.activity_id,
            source_request_id=source.request.request_id,
            source_protocol_transaction_id=source.protocol_transaction_id,
            deadline=snapshot.deadline,
            prompt_facts=facts,
            fallback_text="我重新梳理时出了点问题，暂时没能把这个任务处理好。",
        )

    def _source_request(self, activity_id: str) -> AssistantRequest | None:
        """读取仍由服务持有的 Cognitive 来源请求。

        Args:
            activity_id: 来源 Cognitive Activity 编号。

        Returns:
            冻结请求快照；来源已经收口时返回空。
        """

        with self._lock:
            source = self._sources.get(activity_id)
            return source.request if source is not None else None


def _text(value: object) -> str:
    """把候选值规范化成去除首尾空白的文本。

    Args:
        value: 可能来自事件载荷的任意值。

    Returns:
        字符串的规范化结果；非字符串返回空字符串。
    """

    return value.strip() if isinstance(value, str) else ""


def _event_belongs_to_current_epoch(
    event: RuntimeEvent,
    *,
    current_runtime_epoch: Callable[[], int],
) -> bool:
    """校验事件外层世代仍是当前助手世代。

    Args:
        event: 带可选运行时世代的内部事件。
        current_runtime_epoch: 返回当前主进程权威世代的函数。

    Returns:
        事件携带合法世代且与当前权威世代相同时返回真。
    """

    runtime_epoch = event.runtime_epoch
    if type(runtime_epoch) is not int or runtime_epoch <= 0:
        return False
    try:
        current_epoch = current_runtime_epoch()
    except Exception:
        return False
    return type(current_epoch) is int and current_epoch == runtime_epoch


def _parse_tool_commit_request(event: RuntimeEvent) -> _ToolCommitRequest | None:
    """把跨进程载荷转换成可比较的提交身份和 canonical 事件。

    Args:
        event: 已通过当前运行时世代校验的工具提交请求。

    Returns:
        基础字段、执行通道和 canonical 容器都合法时返回冻结请求，否则返回空。
    """

    payload = event.payload
    commit_request_id = _text(payload.get("commit_request_id"))
    self_reflection_id = _text(payload.get("self_reflection_id"))
    source_activity_id = _text(payload.get("source_activity_id"))
    tool_call_id = _text(payload.get("tool_call_id"))
    tool_name = _text(payload.get("tool_name"))
    execution_channel = payload.get("execution_channel")
    runtime_epoch = event.runtime_epoch
    if (
        not commit_request_id
        or not self_reflection_id
        or not source_activity_id
        or type(runtime_epoch) is not int
        or runtime_epoch <= 0
        or not tool_call_id
        or not tool_name
        or execution_channel not in {"inline", "background"}
    ):
        return None

    canonical_payload = payload.get("canonical_event_payload")
    canonical_produced_at = payload.get("canonical_event_produced_at")
    canonical_event: RuntimeEvent | None = None
    canonical_fingerprint = ""
    if execution_channel == "inline":
        if canonical_payload is not None or canonical_produced_at is not None:
            return None
    else:
        if not isinstance(canonical_payload, dict):
            return None
        if (
            not isinstance(canonical_produced_at, (int, float))
            or isinstance(canonical_produced_at, bool)
            or not isfinite(float(canonical_produced_at))
            or float(canonical_produced_at) < 0
        ):
            return None
        canonical_copy = deepcopy(canonical_payload)
        try:
            serialized_payload = dumps(
                canonical_copy,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return None
        canonical_fingerprint = sha256(
            serialized_payload.encode("utf-8")
        ).hexdigest()
        canonical_event = RuntimeEvent(
            type="llm.foreground.tool_call_completed",
            source="assistant_runtime",
            runtime_epoch=runtime_epoch,
            produced_at=float(canonical_produced_at),
            payload=canonical_copy,
        )

    return _ToolCommitRequest(
        identity=SelfReflectionToolCommitIdentity(
            commit_request_id=commit_request_id,
            self_reflection_id=self_reflection_id,
            source_activity_id=source_activity_id,
            runtime_epoch=runtime_epoch,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            execution_channel=execution_channel,
            canonical_payload_fingerprint=canonical_fingerprint,
        ),
        canonical_event=canonical_event,
    )


def _tool_commit_matches_snapshot(
    request: _ToolCommitRequest,
    snapshot: Any,
) -> bool:
    """核对请求和 canonical 事实没有改写 Admission 冻结身份。

    Args:
        request: 已通过基础协议校验的同步提交请求。
        snapshot: 主进程 Attention 当前冻结快照。

    Returns:
        来源、世代、工具和 Background acceptance 全部一致时返回真。
    """

    identity = request.identity
    if (
        identity.self_reflection_id != snapshot.self_reflection_id
        or identity.source_activity_id != snapshot.source_activity_id
        or identity.runtime_epoch != snapshot.runtime_epoch
    ):
        return False
    canonical_event = request.canonical_event
    if identity.execution_channel == "inline":
        return canonical_event is None
    if canonical_event is None:
        return False
    payload = canonical_event.payload
    result = payload.get("result")
    activity_id = _text(payload.get("activity_id"))
    if not isinstance(result, dict) or not activity_id:
        return False
    return (
        payload.get("state") == "accepted"
        and _text(payload.get("name")) == identity.tool_name
        and _text(payload.get("tool_call_id")) == identity.tool_call_id
        and _text(payload.get("self_reflection_id"))
        == identity.self_reflection_id
        and _text(payload.get("recovery_of_activity_id"))
        == identity.source_activity_id
        and _text(payload.get("request_id")) == snapshot.source_request_id
        and _text(payload.get("protocol_transaction_id"))
        == f"self-reflection-{identity.self_reflection_id}"
        and isinstance(payload.get("deliverable"), bool)
        and result.get("state") == "accepted"
        and _text(result.get("activity_id")) == activity_id
        and _text(result.get("recovery_of_activity_id"))
        == identity.source_activity_id
    )


def _source_payload_matches(
    payload: dict[str, object],
    source: _SourceRecord,
) -> bool:
    """校验生命周期事件不能改写来源请求或交互会话身份。

    Args:
        payload: 需要核对的 Activity 生命周期事件载荷。
        source: prepare 阶段冻结的来源身份。

    Returns:
        所有已提供身份都与冻结来源一致时返回真。
    """

    activity_id = _text(payload.get("activity_id"))
    if activity_id and activity_id != source.activity_id:
        return False
    request_id = _text(payload.get("request_id"))
    if request_id and request_id != source.request.request_id:
        return False
    protocol_transaction_id = _text(payload.get("protocol_transaction_id"))
    if protocol_transaction_id and protocol_transaction_id != source.protocol_transaction_id:
        return False
    interaction_session_id = _text(payload.get("interaction_session_id"))
    if interaction_session_id and interaction_session_id != source.request.interaction_session_id:
        return False
    return True


def _material_context_id(payload: dict[str, object]) -> str | None:
    """读取并规范化事件携带的材料上下文编号。

    Args:
        payload: 可能包含 `turn_material_context_id` 的事件载荷。

    Returns:
        非空材料上下文编号；缺失或无效时返回空。
    """

    value = payload.get("turn_material_context_id")
    return _text(value) or None


def _activity_failure_code(payload: dict[str, object]) -> str | None:
    """读取 Cognitive 终态已经归一化的 Activity 错误码。

    Args:
        payload: 已通过 Activity 身份校验的终态事件载荷。

    Returns:
        合法错误码；故障报告缺失或格式不合格时返回空。
    """

    failure = payload.get("failure")
    if not isinstance(failure, dict):
        return None
    raw_code = failure.get("code")
    if not isinstance(raw_code, str):
        return None
    return raw_code if raw_code in ACTIVITY_FAILURE_CODES else None


def _as_mapping(value: object) -> dict[str, object]:
    """把冻结上下文复制成普通字典，不把可变对象跨边界透传。

    Args:
        value: 字典或提供 `model_dump` 的上下文对象。

    Returns:
        JSON 友好的字典副本；无法转换时返回空字典。
    """

    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    return {}


def _task_recovery_outcome(
    outcome: SelfReflectionOutcomeFact,
) -> TaskRecoveryOutcome:
    """把恢复域事实投影成 Runtime Memory 的任务恢复值对象。

    Args:
        outcome: 已冻结的自我反思结构化结果。

    Returns:
        不包含模型原始正文的任务恢复值对象。
    """

    safe_summary = outcome.safe_message
    if not safe_summary and outcome.tool_fact is not None:
        safe_summary = outcome.tool_fact.safe_result
    if not safe_summary:
        safe_summary = "可见回复已结算。"
    return TaskRecoveryOutcome(
        self_reflection_id=outcome.self_reflection_id,
        kind=outcome.kind.value,
        safe_summary=safe_summary,
        tool_name=(
            outcome.tool_fact.tool_name if outcome.tool_fact is not None else None
        ),
        tool_status=(
            outcome.tool_fact.status.value if outcome.tool_fact is not None else None
        ),
        reason_code=outcome.reason or None,
    )
