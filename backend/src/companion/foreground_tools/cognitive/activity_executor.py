"""管理认知活动的流式执行、进度提醒和前台只读快照。"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import Executor
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from companion.activities.foreground_snapshot import ActivityPhase, ForegroundActivitySnapshot
from companion.activities.progress_reasoning import ProgressReasoningBuffer
from companion.activities.progress_scheduler import (
    ACTIVITY_PROGRESS_INTERVALS_S,
    ActivityProgressScheduler,
    ProgressIntervalPolicy,
    ProgressScheduler,
    ScheduledProgressCall,
)
from companion.activities.runtime.failure_reporting import (
    failure_from_exception,
    failure_from_reason,
)
from companion.activities.runtime.models import (
    ACTIVITY_STAGE_CODES,
    ACTIVITY_STAGE_ORDER,
    ActivityFailure,
)
from companion.activities.runtime.public_text import summarize_activity_goal
from companion.foreground_tools.cognitive.contracts import (
    CognitiveExecutionPlan,
    CognitiveGoalKind,
)
from companion.foreground_tools.cognitive.result_protocol import (
    CognitiveResult,
    CognitiveSuccessResult,
    CognitiveResultProtocolError,
    MAX_COGNITIVE_RESULT_CHARS,
)
from companion.foreground_tools.cognitive.result_resolver import CognitiveResultResolver
from companion.foreground_tools.activity_correction.contracts import (
    CorrectionCancellationOutcome,
    CorrectionEvidence,
    CorrectionReservation,
)
from companion.foreground_tools.background_activity_runtime import BackgroundActivityRuntime
from companion.llm.foreground_models import LlmTextStreamChunk
from companion.observability.logging_config import get_backend_logger
from companion.realtime.cancellation import CancellationToken


CognitiveTaskState = Literal[
    "none",
    "prepared",
    "starting",
    "running",
    "waiting_user",
    "attention",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "rolled_back",
    "rejected",
    "stale",
]

logger = get_backend_logger(__name__)


class _CognitiveOutputLimitReached(RuntimeError):
    """表示流式正文已经超过本地协议容量，必须立即停止读取。"""

# 执行层拿不到可信模型摘要时，只能用稳定码生成不含原始响应的确定性事实。
_EXECUTION_FAILURE_FACTS: dict[str, tuple[str, str]] = {
    "cognitive_result_protocol_failed": (
        "认知结果没有通过结构化协议校验。",
        "Cognitive 因结果协议校验失败而未完成任务。",
    ),
    "provider_unavailable": (
        "认知模型服务当前不可用。",
        "Cognitive 因模型服务不可用而未完成任务。",
    ),
    "provider_timeout": (
        "认知模型服务没有在时限内完成请求。",
        "Cognitive 因模型服务超时而未完成任务。",
    ),
    "request_failed": (
        "认知模型请求没有成功完成。",
        "Cognitive 因模型请求失败而未完成任务。",
    ),
}


class CognitiveTaskExecutorProtocol(Protocol):
    """约束认知任务执行器向活动层提供的流式结果。"""

    def stream(
        self,
        *,
        activity_id: str,
        plan: CognitiveExecutionPlan,
        source_user_text: str,
        cancellation_event: threading.Event | None = None,
    ) -> Iterable[LlmTextStreamChunk]:
        """按完整认知计划执行一次 Knowledge 请求。

        Args:
            activity_id: 当前活动编号，用于关联 Knowledge 请求日志。
            plan: 同一轮全部认知目标合并成的执行计划。
            source_user_text: 触发任务的原始用户正文。
            cancellation_event: 可选的流式请求协作取消信号。

        Returns:
            Knowledge 模型产生的流式文本片段。
        """


@dataclass(frozen=True)
class CognitiveTaskSnapshot:
    """保存认知工具判断“是否已有活动占用”的不可变视图。

    它从运行记录复制而来，刻意不携带流式正文、进度缓冲区和取消令牌。这样工具层
    可以安全判断是否复用或拒绝新请求，而无需持有正在变化的活动记录。

    ``error_text`` 仅用于助手子进程内的诊断，不能作为跨进程或前端契约。
    """

    activity_id: str | None = None
    interaction_session_id: str = ""
    state: CognitiveTaskState = "none"
    kind: CognitiveGoalKind | None = None
    brief: str = ""
    source_user_text: str = ""
    result_text: str = ""
    summary: str = ""
    failure_reason: str | None = None
    error_text: str = ""
    reasoning_token_count: int | None = None
    content_token_count: int | None = None
    started_at: float | None = None
    updated_at: float | None = None

    def to_payload(self) -> dict[str, object]:
        """转换为认知工具既有协议使用的字典。

        Returns:
            用于构造“已有活动运行”结果的进程内载荷，不包含流式正文、进度或取消状态。
        """

        return {
            "activity_id": self.activity_id,
            "interaction_session_id": self.interaction_session_id,
            "state": self.state,
            "kind": self.kind,
            "brief": self.brief,
            "source_user_text": self.source_user_text,
            "result_text": self.result_text,
            "summary": self.summary,
            "failure_reason": self.failure_reason,
            "error_text": self.error_text,
            "reasoning_token_count": self.reasoning_token_count,
            "content_token_count": self.content_token_count,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


@dataclass
class _CognitiveTaskRecord:
    """保存单个认知活动从准备到终态的完整内部状态。

    此对象是执行器唯一可变的活动事实。同一时刻只有一条记录；所有读取、状态转换
    和调度字段更新都必须由 ``CognitiveActivityExecutor._lock`` 保护。
    """

    # 这些字段在 prepare 时确定，之后不改；事件、快照和调度都依赖它们关联同一活动。
    activity_id: str
    interaction_session_id: str
    plan: CognitiveExecutionPlan
    source_user_text: str
    prepared_at: float
    started_at: float | None
    updated_at: float

    # state 决定活动能否继续执行；phase 只决定前台应展示“推理中”还是“收尾中”。
    state: CognitiveTaskState = "prepared"
    phase: ActivityPhase = "reasoning"
    # 这是 Activity Registry 使用的公共阶段。失败时不回退，保留问题发生的位置。
    activity_stage: str = "preparing"
    # 失败时才写入可公开的故障报告，异常堆栈仍只留在日志中。
    failure: ActivityFailure | None = None

    # 推理仍受诊断上限保护；正式正文使用分片完整累积，不能静默丢失开头。
    reasoning_text: str = ""
    content_parts: list[str] = field(default_factory=list)
    result_text: str = ""
    summary: str = ""
    failure_reason: str | None = None
    error_text: str = ""

    # 用量只用于观测，不参与活动状态或调度决策。
    reasoning_token_count: int | None = None
    content_token_count: int | None = None

    # 缓冲区维护待确认和已冻结的推理增量；检查次数决定首次 30 秒、之后 60 秒的节奏。
    progress_reasoning_buffer: ProgressReasoningBuffer = field(
        default_factory=ProgressReasoningBuffer
    )
    progress_check_count: int = 0
    last_progress_at: float | None = None
    schedule_handle: ScheduledProgressCall | None = None

    # 正文出现后取消定时检查，阻止旧回调再为已开始生成结果的活动发送推理进度。
    content_started: bool = False
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)
    finished_event: threading.Event = field(default_factory=threading.Event)

    def to_snapshot(self) -> CognitiveTaskSnapshot:
        """从当前运行记录复制出供认知工具读取的稳定视图。

        调用方必须已持有执行器锁；本方法本身不加锁，以便快照与占用判断处于同一临界区。

        Returns:
            不暴露流式正文、进度缓冲区、定时句柄和取消令牌的不可变快照。
        """

        return CognitiveTaskSnapshot(
            activity_id=self.activity_id,
            interaction_session_id=self.interaction_session_id,
            state=self.state,
            kind=self.plan.primary_kind,
            brief=self.plan.goal_text,
            source_user_text=self.source_user_text,
            result_text=self.result_text,
            summary=self.summary,
            failure_reason=self.failure_reason,
            error_text=self.error_text,
            reasoning_token_count=self.reasoning_token_count,
            content_token_count=self.content_token_count,
            started_at=self.started_at,
            updated_at=self.updated_at,
        )


class CognitiveActivityExecutor:
    """执行后台认知活动，并维护前台可现场读取的权威事实。"""

    def __init__(
        self,
        *,
        task_executor: CognitiveTaskExecutorProtocol,
        result_resolver: CognitiveResultResolver,
        executor: Executor | None = None,
        monotonic_now: Callable[[], float] = time.monotonic,
        id_factory: Callable[[str], str] | None = None,
        event_notifier: Callable[[], None] | None = None,
        progress_scheduler: ProgressScheduler | None = None,
        progress_policy: ProgressIntervalPolicy | None = None,
        max_text_chars: int = 4000,
    ) -> None:
        """初始化后台认知活动执行器。

        Args:
            task_executor: 按统一认知计划执行 Knowledge 请求的流式执行器。
            result_resolver: 执行二分支校验和一次性 Flash 修复的解析管线。
            executor: 承载网络任务的线程执行器。
            monotonic_now: 记录任务时间和计算定时检查使用的单调时钟。
            id_factory: 可替换的任务编号工厂。
            event_notifier: 新事件入队后唤醒运行时的回调。
            progress_scheduler: 进程级共享进度调度器。
            progress_policy: 认知任务的进度退避策略。
            max_text_chars: 推理、错误和单次进度增量的异常容量上限。
        """

        if max_text_chars <= 0:
            raise ValueError("max_text_chars 必须大于零")
        self._task_executor = task_executor
        self._result_resolver = result_resolver
        self._runtime = BackgroundActivityRuntime(
            executor=executor,
            thread_name_prefix="cognitive-activity",
            event_notifier=event_notifier,
        )
        self._monotonic_now = monotonic_now
        self._id_factory = id_factory or (lambda _prefix: str(uuid.uuid4()))
        self._owns_progress_scheduler = progress_scheduler is None
        self._progress_scheduler = progress_scheduler or ActivityProgressScheduler(
            monotonic_now=monotonic_now
        )
        self._progress_policy = progress_policy or ProgressIntervalPolicy(
            ACTIVITY_PROGRESS_INTERVALS_S
        )
        self._max_text_chars = max_text_chars
        self._lock = threading.Lock()
        # 当前执行器只允许一条认知活动占用执行槽位。
        self._active_record: _CognitiveTaskRecord | None = None
        # 纠正准备期间 A 仍然运行，因此 B 不能占用普通活动槽位。这个独立预留只保存
        # 已冻结输入；Foreground 未提交时可直接释放，不会触碰 A。
        self._prepared_correction_record: _CognitiveTaskRecord | None = None
        self._prepared_correction_phase: Literal[
            "prepared",
            "foreground_committed",
            "starting",
        ] | None = None
        self._shutdown = False

    def prepare(
        self,
        *,
        interaction_session_id: str,
        plan: CognitiveExecutionPlan,
        source_user_text: str,
    ) -> tuple[bool, list[dict[str, object]]]:
        """登记一条等待当前生成接受的后台认知活动。

        Args:
            interaction_session_id: 当前交互会话编号。
            plan: 同一轮全部认知目标合并成的后台执行计划。
            source_user_text: 触发任务的原始用户正文。

        Returns:
            是否接受任务，以及同步产生的准备或拒绝事件。
        """

        interaction_session_id = interaction_session_id.strip()
        if not plan.goals:
            raise ValueError("认知计划至少需要一个目标")
        current_time = self._monotonic_now()
        with self._lock:
            if self._shutdown:
                rejected = self._event(
                    "cognitive_task.rejected",
                    {
                        "interaction_session_id": interaction_session_id,
                        "kind": plan.primary_kind,
                        "brief": plan.goal_text,
                        "source_user_text": source_user_text.strip(),
                        "reason": "activity_executor_shutdown",
                        "updated_at": current_time,
                    },
                )
                return False, [rejected]
            running = self._active_record
            if self._prepared_correction_record is not None:
                rejected = self._event(
                    "cognitive_task.rejected",
                    {
                        "activity_id": self._prepared_correction_record.activity_id,
                        "interaction_session_id": interaction_session_id,
                        "kind": plan.primary_kind,
                        "brief": plan.goal_text,
                        "source_user_text": source_user_text.strip(),
                        "reason": "cognitive_task_running",
                        "updated_at": current_time,
                    },
                )
                return False, [rejected]
            if running is not None and running.state in {
                "prepared",
                "starting",
                "running",
                "waiting_user",
                "attention",
                "cancelling",
            }:
                rejected = self._event(
                    "cognitive_task.rejected",
                    {
                        "activity_id": running.activity_id,
                        "interaction_session_id": interaction_session_id,
                        "kind": plan.primary_kind,
                        "brief": plan.goal_text,
                        "source_user_text": source_user_text.strip(),
                        "reason": "cognitive_task_running",
                        "updated_at": current_time,
                    },
                )
                return False, [rejected]
            new_activity_id = self._id_factory("cognitive_task").strip()
            if not new_activity_id:
                return False, [
                    self._event(
                        "cognitive_task.rejected",
                        {
                            "interaction_session_id": interaction_session_id.strip(),
                            "reason": "cognitive_activity_identity_missing",
                            "updated_at": current_time,
                        },
                    )
                ]
            record = _CognitiveTaskRecord(
                activity_id=new_activity_id,
                interaction_session_id=interaction_session_id,
                plan=plan,
                source_user_text=source_user_text.strip(),
                prepared_at=current_time,
                started_at=None,
                updated_at=current_time,
                progress_reasoning_buffer=ProgressReasoningBuffer(self._max_text_chars),
            )
            self._active_record = record
            prepared = self._event("cognitive_task.prepared", self._base_payload(record))
        return True, [prepared]

    def start(self, activity_id: str) -> bool:
        """在工具接受事件成功提交后启动已登记活动。

        Args:
            activity_id: ``prepare`` 返回的认知活动编号。

        Returns:
            任务首次越过启动边界时返回真。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            return False
        with self._lock:
            record = self._record_by_activity_id(normalized_activity_id)
            if self._shutdown or record is None or record.state != "prepared":
                return False
            # 底层 Runtime 还可能拒绝 submit，这个内部状态只占住执行槽，
            # 不能对外写 started 或让 Self-Reflection 误以为任务已运行。
            record.state = "starting"
            record.updated_at = self._monotonic_now()
        try:
            self._runtime.submit(self._run_task, record)
        except Exception as exc:
            logger.exception("认知任务后台执行启动失败 activity_id=%s", record.activity_id)
            self._fail_starting_task_with_exception(
                record,
                exc,
                suppress_notice=True,
            )
            return False
        with self._lock:
            # 异步 Executor 由 submit 返回后的调用线程提交 started；
            # 同步 Executor 可能已由 worker 入口提前提交，两者共用同一锁保证只写一次。
            self._mark_runtime_started_locked(record)
            return record.started_at is not None

    def _mark_runtime_started_locked(self, record: _CognitiveTaskRecord) -> bool:
        """在 Runtime 已接管任务后提交唯一 started 事实。

        Args:
            record: 正处于 starting 窗口的认知任务记录。

        Returns:
            本次首次提交 started 时返回真；任务已被关闭或接管时返回假。
        """

        if (
            self._shutdown
            or self._active_record is not record
            or record.state != "starting"
        ):
            return False
        current_time = self._monotonic_now()
        record.state = "running"
        record.started_at = current_time
        record.updated_at = current_time
        self._append_event(self._event("cognitive_task.started", self._base_payload(record)))
        if record.plan.progress_policy == "reasoning_driven":
            self._schedule_next_reasoning_check_locked(record)
        return True

    def cancel_before_start(self, activity_id: str) -> bool:
        """回滚尚未越过工具接受边界的认知活动。

        Args:
            activity_id: ``prepare`` 返回的认知活动编号。

        Returns:
            成功释放 prepared 占位时返回真。
        """

        with self._lock:
            record = self._record_by_activity_id(activity_id.strip())
            if record is None or record.state != "prepared":
                return False
            if self._active_record is record:
                self._active_record = None
            record.cancellation_token.cancel("generation_not_accepted")
            record.state = "rolled_back"
            record.plan = _clear_plan_evidence(record.plan)
            return True

    def cancel_running(
        self,
        activity_id: str,
        *,
        reason: str = "user_cancelled",
    ) -> bool:
        """请求取消一项已经真实启动的认知任务。

        Args:
            activity_id: 需要取消的认知任务编号。

        Returns:
            首次从 running 进入 cancelling 时返回真。
        """

        with self._lock:
            record = self._active_record
            if record is None or record.activity_id != activity_id.strip():
                return False
            if record.state != "running":
                return False
            record.state = "cancelling"
            record.updated_at = self._monotonic_now()
            self._clear_progress_locked(record)
            self._append_event(
                self._event(
                    "cognitive_task.progress_invalidated",
                    {
                        "activity_id": record.activity_id,
                        "interaction_session_id": record.interaction_session_id,
                        "updated_at": record.updated_at,
                    },
                )
            )

        # 取消会关闭底层 SDK 流并可能触发工作线程回调，不能在执行器锁内执行。
        record.cancellation_token.cancel(reason.strip() or "user_cancelled")
        return True

    def has_activity(self, activity_id: str) -> bool:
        """判断执行器是否仍保存指定 Activity 的运行期事实。

        Args:
            activity_id: 需要核对的 Cognitive Activity 编号。

        Returns:
            当前执行记录与编号匹配时返回真。
        """

        with self._lock:
            return (
                self._active_record is not None
                and self._active_record.activity_id == activity_id.strip()
            )

    def is_activity_correctable(self, activity_id: str) -> bool:
        """判断指定认知 Activity 此刻是否仍可被目标纠正替换。

        Args:
            activity_id: 需要实时核对的旧 Activity 编号。

        Returns:
            Activity 仍在运行且尚未生成最终结果时返回真。
        """

        with self._lock:
            record = self._active_record
            return bool(
                record is not None
                and record.activity_id == activity_id.strip()
                and record.state in {"running", "waiting_user", "attention"}
                and record.phase != "finalizing"
            )

    def correction_evidence(self, activity_id: str) -> CorrectionEvidence:
        """读取旧认知 Activity 仍可安全继承的图片和 URL 证据。

        Args:
            activity_id: 需要读取证据的活跃 Activity 编号。

        Returns:
            当前计划冻结的带标签图片证据和网页来源；目标不匹配时返回空证据。
        """

        with self._lock:
            record = self._active_record
            if record is None or record.activity_id != activity_id.strip():
                return CorrectionEvidence()
            return CorrectionEvidence(
                images=record.plan.images,
                image_materials=record.plan.image_materials,
                source_urls=record.plan.source_urls,
                cognitive_tool_types=tuple(record.plan.effective_cognitive_tool_types),
            )

    def reserve_correction(
        self,
        *,
        based_on_activity_id: str,
        interaction_session_id: str,
        plan: CognitiveExecutionPlan,
        source_user_text: str,
    ) -> CorrectionReservation:
        """在不取消旧 Activity 的前提下预留纠正后的认知任务。

        Args:
            based_on_activity_id: 用户明确要纠正的旧 Activity 编号。
            interaction_session_id: 当前 Foreground 交互会话编号。
            plan: 纠正后的权威认知计划。
            source_user_text: 用户本轮真实纠正文。

        Returns:
            包含接受标记、新 Activity 编号和准备或拒绝事件的结构化结果。
        """

        if not plan.goals:
            raise ValueError("认知计划至少需要一个目标")
        current_time = self._monotonic_now()
        with self._lock:
            active_record = self._active_record
            # Cognitive Correction 只允许把当前认知任务 A 替换为同源任务 B。
            # 任意其它认知任务或另一项纠正预留都不能被这次请求覆盖。
            if (
                self._shutdown
                or self._prepared_correction_record is not None
                or (
                    active_record is not None
                    and active_record.state in {
                        "prepared",
                        "starting",
                        "running",
                        "waiting_user",
                        "attention",
                        "cancelling",
                    }
                    and active_record.activity_id != based_on_activity_id.strip()
                )
            ):
                return CorrectionReservation(
                    accepted=False,
                    events=(
                        self._event(
                            "cognitive_task.rejected",
                            {
                                "activity_id": (
                                    active_record.activity_id
                                    if active_record is not None
                                    else based_on_activity_id.strip()
                                ),
                                "interaction_session_id": interaction_session_id.strip(),
                                "kind": plan.primary_kind,
                                "brief": plan.goal_text,
                                "reason": "cognitive_task_running",
                                "updated_at": current_time,
                            },
                        ),
                    ),
                )
            # 服务层在进入这里前已经检查过一次 A，但模型调用与真正预留之间仍可能
            # 恰好收到终态或取消。相同编号若已不在可纠正状态，必须在锁内拒绝，
            # 不能先制造 B 再等提交阶段才失败。
            if (
                active_record is not None
                and active_record.activity_id == based_on_activity_id.strip()
                and (
                    active_record.state
                    not in {"running", "waiting_user", "attention"}
                    or active_record.phase == "finalizing"
                )
            ):
                return CorrectionReservation(
                    accepted=False,
                    events=(
                        self._event(
                            "cognitive_task.rejected",
                            {
                                "activity_id": active_record.activity_id,
                                "interaction_session_id": interaction_session_id.strip(),
                                "reason": "correction_activity_not_found",
                                "updated_at": current_time,
                            },
                        ),
                    ),
                )
            new_activity_id = self._id_factory("cognitive_task").strip()
            if not new_activity_id:
                return CorrectionReservation(
                    accepted=False,
                    events=(
                        self._event(
                            "cognitive_task.rejected",
                            {
                                "interaction_session_id": interaction_session_id.strip(),
                                "reason": "cognitive_activity_identity_missing",
                                "updated_at": current_time,
                            },
                        ),
                    ),
                )
            record = _CognitiveTaskRecord(
                activity_id=new_activity_id,
                interaction_session_id=interaction_session_id.strip(),
                plan=plan,
                source_user_text=source_user_text.strip(),
                prepared_at=current_time,
                started_at=None,
                updated_at=current_time,
                progress_reasoning_buffer=ProgressReasoningBuffer(self._max_text_chars),
            )
            self._prepared_correction_record = record
            self._prepared_correction_phase = "prepared"
            prepared = self._event("cognitive_task.prepared", self._base_payload(record))
        return CorrectionReservation(
            accepted=True,
            activity_id=record.activity_id,
            events=(prepared,),
        )

    def commit_reserved_correction(self, activity_id: str) -> bool:
        """把 B 标记为 Foreground 已接受、必须启动或失败收口的预留。

        Args:
            activity_id: 纠正准备阶段分配的新 Activity 编号。

        Returns:
            预留存在且已进入 committed 时返回真；关闭或编号不符时返回假。
        """

        with self._lock:
            record = self._prepared_correction_record
            if (
                self._shutdown
                or record is None
                or record.activity_id != activity_id.strip()
                or self._prepared_correction_phase
                not in {"prepared", "foreground_committed"}
            ):
                return False
            self._prepared_correction_phase = "foreground_committed"
            return True

    def start_reserved_correction(self, activity_id: str) -> bool:
        """在 A 清理完成后把独立预留的 B 提升为正式认知任务并启动。

        Args:
            activity_id: 纠正准备阶段分配的新 Activity 编号。

        Returns:
            B 首次越过启动边界时返回真。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            record = self._prepared_correction_record
            active_record = self._active_record
            if (
                self._shutdown
                or record is None
                or record.activity_id != normalized_activity_id
                or record.state != "prepared"
                or self._prepared_correction_phase != "foreground_committed"
                or (
                    active_record is not None
                    and active_record.state
                    in {
                        "prepared",
                        "running",
                        "waiting_user",
                        "attention",
                        "cancelling",
                    }
                )
            ):
                return False
            current_time = self._monotonic_now()
            self._prepared_correction_phase = "starting"
            self._active_record = record
            record.state = "starting"
            record.updated_at = current_time
        try:
            self._runtime.submit(self._run_task, record)
        except Exception as exc:
            logger.exception("认知纠正任务后台执行启动失败 activity_id=%s", record.activity_id)
            self._fail_starting_task_with_exception(
                record,
                exc,
                suppress_notice=True,
            )
            return False
        with self._lock:
            self._mark_runtime_started_locked(record)
            return record.started_at is not None

    def rollback_reserved_correction(self, activity_id: str) -> bool:
        """释放 Foreground 尚未提交的认知纠正预留。

        Args:
            activity_id: 纠正准备阶段分配的新 Activity 编号。

        Returns:
            找到并释放对应预留时返回真。
        """

        with self._lock:
            record = self._prepared_correction_record
            if (
                record is None
                or record.activity_id != activity_id.strip()
                or self._prepared_correction_phase
                not in {"prepared", "foreground_committed"}
            ):
                return False
            self._prepared_correction_record = None
            self._prepared_correction_phase = None
            record.cancellation_token.cancel("generation_not_accepted")
            record.state = "rolled_back"
            record.plan = _clear_plan_evidence(record.plan)
            return True

    def fail_reserved_correction(self, activity_id: str, *, reason: str) -> bool:
        """把已提交但无法启动的认知纠正任务收口为失败终态。

        Args:
            activity_id: 纠正准备阶段分配的新 Activity 编号。
            reason: 清理超时、旧任务竞态终态或启动失败的稳定原因。

        Returns:
            找到并首次关闭对应预留时返回真。
        """

        with self._lock:
            record = self._prepared_correction_record
            if (
                record is None
                or record.activity_id != activity_id.strip()
                or self._prepared_correction_phase
                not in {"foreground_committed", "starting"}
            ):
                return False
            self._prepared_correction_record = None
            self._prepared_correction_phase = None
            current_time = self._monotonic_now()
            runtime_started = record.started_at is not None
            try:
                if runtime_started:
                    # shutdown 会先清空未排水事件，因此已接管的 B 需要在失败前
                    # 重放同一 started 事实。下游 Registry 会幂等吸收已交付的重复事件。
                    record.state = "running"
                    record.updated_at = current_time
                    self._append_event(
                        self._event("cognitive_task.started", self._base_payload(record))
                    )
                record.state = "failed"
                record.failure = failure_from_reason(
                    reason,
                    stage_code=record.activity_stage,
                )
                record.error_text = reason.strip()
                record.updated_at = current_time
                payload = self._terminal_payload(record)
                payload["correction_failure_reason"] = reason.strip()
                payload["suppress_notice"] = True
                if not runtime_started:
                    # Foreground commit 只证明纠正请求已公开，不能代替
                    # Runtime 接管事实；启动前失败必须保留 started_at 为空。
                    payload["execution_phase"] = "before_start"
                self._append_event(self._event("cognitive_task.failed", payload))
            except Exception:
                # 失败收口是提交后最后一道保护，不能再把异常抛回输出车道。
                # 即使事件账本异常，也必须释放本地预留和敏感证据，避免永久占槽。
                logger.exception(
                    "认知纠正任务失败终态写入异常 activity_id=%s reason=%s",
                    record.activity_id,
                    reason,
                )
                record.state = "failed"
                record.failure = failure_from_reason(
                    reason,
                    stage_code=record.activity_stage,
                )
                record.error_text = reason.strip()
                record.updated_at = current_time
            finally:
                record.finished_event.set()
                record.plan = _clear_plan_evidence(record.plan)
            return True

    def cancel_for_correction(
        self,
        activity_id: str,
        *,
        timeout: float = 5.0,
    ) -> CorrectionCancellationOutcome:
        """让旧认知 Activity 真实退出，为同源纠正释放执行槽。

        Args:
            activity_id: 用户明确要纠正的旧 Cognitive Activity 编号。
            timeout: 等待 Knowledge 工作线程退出的最长秒数。

        Returns:
            区分自然终态、取消未接纳、清理超时和清理成功的稳定结果。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            record = self._active_record
            if record is None or record.activity_id != normalized_activity_id:
                return "activity_no_longer_correctable"
            if (
                record.state not in {"running", "waiting_user", "attention"}
                or record.phase == "finalizing"
            ):
                return "activity_no_longer_correctable"
            previous_state = record.state
            # 状态和 phase 在同一把锁内确认并立刻改成 cancelling，避免首个正文
            # 恰好到达后仍被迟到的纠正请求改写为用户取消。
            record.state = "cancelling"
            record.updated_at = self._monotonic_now()
        cancellation_accepted = record.cancellation_token.cancel("user_correction")
        if not cancellation_accepted:
            with self._lock:
                if self._active_record is record and record.state == "cancelling":
                    # token 没有接纳本次纠正时，A 仍归原状态机管理。尤其不能把
                    # waiting_user 或 attention 伪装成 running，也不能提前销毁进度。
                    record.state = previous_state
                    record.updated_at = self._monotonic_now()
            return "cancel_not_accepted"
        with self._lock:
            if self._active_record is record and record.state == "cancelling":
                # 只有取消事实真实建立后，旧任务的进度才应该失效。token 回调可能已
                # 同步推动任务终态；那种情况下终态链路已经负责清理，无需重复入账。
                self._clear_progress_locked(record)
                self._append_event(
                    self._event(
                        "cognitive_task.progress_invalidated",
                        {
                            "activity_id": record.activity_id,
                            "interaction_session_id": record.interaction_session_id,
                            "updated_at": record.updated_at,
                        },
                    )
                )
        record.finished_event.wait(max(timeout, 0.0))
        return "cleaned" if record.finished_event.is_set() else "cleanup_timeout"

    def snapshot(self, interaction_session_id: str) -> CognitiveTaskSnapshot:
        """读取工具层所需的最新任务占用状态。

        Args:
            interaction_session_id: 当前交互会话编号。

        Returns:
            当前任务快照；从未提交任务时返回 none 状态。
        """

        interaction_session_id = interaction_session_id.strip()
        with self._lock:
            record = self._active_record
            if record is None or record.interaction_session_id != interaction_session_id:
                return CognitiveTaskSnapshot(interaction_session_id=interaction_session_id)
            return record.to_snapshot()

    def activity_snapshots(
        self,
        interaction_session_id: str,
    ) -> tuple[ForegroundActivitySnapshot, ...]:
        """读取前台当前可使用的认知活动事实集合。

        Args:
            interaction_session_id: 当前交互会话编号。

        Returns:
            运行中任务的单项只读快照；没有活动时返回空元组。
        """

        with self._lock:
            record = self._active_record
            if (
                record is None
                or record.state
                not in {"running", "waiting_user", "attention", "cancelling"}
            ):
                return ()
            goal = record.plan.goal_text or record.source_user_text
            return (ForegroundActivitySnapshot(
                activity_id=record.activity_id,
                interaction_session_id=record.interaction_session_id,
                source="cognitive",
                category=_cognitive_category(record.plan),
                goal=goal,
                goal_summary=summarize_activity_goal(goal),
                activity_kind=record.plan.progress_kind,
                cognitive_tool_types=tuple(record.plan.effective_cognitive_tool_types),
                state=record.state,
                phase=record.phase,
                stage_code=record.activity_stage,
                progress_reasoning_text=record.progress_reasoning_buffer.frozen_text(),
                progress_sequence=record.progress_reasoning_buffer.latest_sequence,
            ),)

    def acknowledge_progress_notice(self, activity_id: str, sequence: int) -> bool:
        """仅确认与实际前台回复匹配的冻结进度。

        Args:
            activity_id: 已经表达完成的认知任务编号。
            sequence: 本次回复对应的提醒序号。

        Returns:
            当前活动和冻结序号仍匹配并完成清理时返回真。
        """

        with self._lock:
            record = self._record_by_activity_id(activity_id.strip())
            if record is None or record.state != "running":
                return False
            return record.progress_reasoning_buffer.acknowledge_through(sequence)

    def drain_events(self, limit: int = 100) -> list[dict[str, object]]:
        """按预算取出认知任务产生的内部事件。

        Args:
            limit: 本次最多取出的事件数量。

        Returns:
            当前预算内的开始、提醒意图、失效和终态事件。
        """

        if limit <= 0:
            return []
        return self._runtime.drain_events(limit)

    def shutdown(self, timeout: float = 1.0) -> None:
        """停止接收活动、关闭模型流并等待自建资源退出。

        Args:
            timeout: 最长等待自建进度调度线程退出的秒数。
        """

        deadline = time.monotonic() + max(timeout, 0.0)
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            active_record = self._active_record
            correction_record = self._prepared_correction_record
            correction_phase = self._prepared_correction_phase
            committed_correction_id = (
                correction_record.activity_id
                if correction_record is not None
                and correction_phase in {"foreground_committed", "starting"}
                else None
            )
            records = tuple(
                record
                for record in (
                    active_record,
                    (
                        correction_record
                        if committed_correction_id is None
                        else None
                    ),
                )
                if record is not None
            )
            for record in records:
                self._cancel_schedule_locked(record)
                record.progress_check_count = 0
                record.progress_reasoning_buffer.clear_pending()
                # 已提交纠正必须保留完整输入，交给锁外的失败终态构造。
                # 否则 task_input_snapshot 会在事件生成前被提前清空。
                if record is correction_record and committed_correction_id is not None:
                    continue
                record.state = (
                    "rolled_back"
                    if record.state in {"prepared", "starting"}
                    else "failed"
                )
                record.plan = _clear_plan_evidence(record.plan)
            # 先撤销当前记录的身份；这样迟到片段、异常或流结束都不能再写入终态。
            self._active_record = None
            if committed_correction_id is None:
                self._prepared_correction_record = None
                self._prepared_correction_phase = None
            self._runtime.clear_events()

        if committed_correction_id is not None:
            self.fail_reserved_correction(
                committed_correction_id,
                reason="backend_shutdown",
            )
        # 底层 SDK 关闭可能唤醒工作线程，必须在执行器锁外执行。
        for record in records:
            record.cancellation_token.cancel("shutdown")
        if self._owns_progress_scheduler and isinstance(
            self._progress_scheduler, ActivityProgressScheduler
        ):
            self._progress_scheduler.shutdown(timeout=_remaining_timeout(deadline))
        self._runtime.shutdown(
            timeout=_remaining_timeout(deadline),
            preserve_pending_events=committed_correction_id is not None,
        )

    def _run_task(self, record: _CognitiveTaskRecord) -> None:
        """消费 Knowledge 模型流，并为活动生成唯一终态事件。

        Args:
            record: 当前后台认知任务的内部运行记录。
        """

        with self._lock:
            if self._active_record is not record:
                return
            if record.state == "starting":
                self._mark_runtime_started_locked(record)
            # B 可能在 Future 排队期间被用户取消。worker 真正取得执行权时必须
            # 直接完成取消终态，不能把 cancelling + starting 留成永久占位。
            if record.state == "cancelling":
                self._finish_cancelled_locked(record)
                return
            if record.state != "running":
                return
            # worker 获得执行权不等于纠正事务已经完成。直到 B 写入正常终态前，
            # 纠正预留仍保持 starting 阶段，这样 shutdown 才能补齐已启动任务的失败终态。

        try:
            # 请求开始后才进入处理阶段，避免排队或准备时间被误记为模型执行时间。
            self._transition_stage(record, "processing")
            chunks = self._task_executor.stream(
                activity_id=record.activity_id,
                plan=record.plan,
                source_user_text=record.source_user_text,
                cancellation_event=record.cancellation_token,
            )
            for chunk in chunks:
                self._apply_stream_chunk(record, chunk)
            if record.cancellation_token.is_set():
                self._finish_cancelled(record)
                return
            self._complete_task(record)
        except Exception as exc:
            if record.cancellation_token.is_set():
                self._finish_cancelled(record)
                return
            # Provider 异常正文可能包含响应片段或服务端诊断，普通日志只记录安全类型。
            logger.error(
                "认知任务执行失败 activity_id=%s error_type=%s",
                record.activity_id,
                type(exc).__name__,
            )
            self._fail_task_with_exception(record, exc)

    def _transition_stage(self, record: _CognitiveTaskRecord, new_stage: str) -> None:
        """在线程安全边界内推进公共活动阶段。

        Args:
            record: 当前认知任务记录。
            new_stage: 真实执行节点对应的目标阶段。
        """

        with self._lock:
            current = self._active_record
            if current is not record or current.state != "running":
                return
            self._transition_stage_locked(current, new_stage)

    def _transition_stage_locked(self, record: _CognitiveTaskRecord, new_stage: str) -> None:
        """在已持锁状态下推进公共活动阶段并生成阶段变更事件。

        Args:
            record: 已在锁内确认仍运行中的任务记录。
            new_stage: 真实执行节点对应的目标阶段。
        """

        if new_stage not in ACTIVITY_STAGE_CODES:
            # 非法阶段说明执行器内部状态异常。忽略它可避免把正常活动误报为失败，
            # 最终阶段合法性仍由 Activity Registry 复核。
            logger.warning(
                "认知任务公共阶段非法被忽略 activity_id=%s stage=%r",
                record.activity_id,
                new_stage,
            )
            return
        # 重复上报同一阶段不改变事实，也不生成多余事件。
        if new_stage == record.activity_stage:
            return
        # 阶段倒退会破坏前台进度语义，因此记录告警并保留当前阶段；
        # Activity Registry 仍会在公共边界再次复核阶段顺序。
        if ACTIVITY_STAGE_ORDER[new_stage] < ACTIVITY_STAGE_ORDER[record.activity_stage]:
            logger.warning(
                "认知任务公共阶段倒退被忽略 activity_id=%s current=%s target=%s",
                record.activity_id,
                record.activity_stage,
                new_stage,
            )
            return
        record.activity_stage = new_stage
        record.updated_at = self._monotonic_now()
        self._append_event(
            self._event(
                "cognitive_task.stage_changed",
                {
                    "activity_id": record.activity_id,
                    "interaction_session_id": record.interaction_session_id,
                    "stage": new_stage,
                    "updated_at": record.updated_at,
                },
            )
        )

    def _apply_stream_chunk(
        self,
        record: _CognitiveTaskRecord,
        chunk: LlmTextStreamChunk,
    ) -> None:
        """把模型最新返回的流式片段写入当前活动状态。

        用量片段只更新统计；正文片段表示开始生成结果；推理片段则在正文出现前
        积累为弱进度提醒。

        Args:
            record: 当前认知任务的内部运行记录。
            chunk: Knowledge 模型输出的流式片段。
        """

        with self._lock:
            # 仅接受仍属于当前运行活动的片段，避免迟到输出污染下一条活动。
            current = self._active_record
            if current is not record or current.state != "running":
                return

            # 任何有效片段都说明活动仍有进展，应刷新最后活动时间。
            current.updated_at = self._monotonic_now()

            # 用量片段不含可展示内容，只更新模型消耗统计。
            if chunk.kind == "usage":
                current.reasoning_token_count = chunk.reasoning_tokens
                current.content_token_count = chunk.output_tokens
                return

            # 首个正文片段意味着结果开始生成，之后不能再用推理内容打断前台。
            if chunk.kind == "text_delta":
                if not current.content_started:
                    self._enter_content_locked(current)
                current.content_parts.append(chunk.text)
                if sum(len(part) for part in current.content_parts) > MAX_COGNITIVE_RESULT_CHARS:
                    # 超限正文不进入解析器、异常或 Activity 详情，直接按协议故障收口。
                    current.content_parts.clear()
                    self._set_stable_execution_failure_locked(
                        current,
                        failure_code="cognitive_result_protocol_failed",
                    )
                    self._fail_task_locked(
                        current,
                        failure_from_reason(
                            "cognitive_result_protocol_failed",
                            stage_code=current.activity_stage,
                        ),
                        error_text="cognitive_result_protocol_failed",
                    )
                    raise _CognitiveOutputLimitReached()
                return

            # 只有正文尚未出现的非空推理片段才可形成进度提醒。
            if chunk.kind != "reasoning_delta" or current.content_started or not chunk.text:
                return

            if current.plan.progress_policy == "disabled":
                return

            # 保留受限长度的推理历史，供任务结束时的内部兜底处理使用。
            current.reasoning_text = _trim_text(
                current.reasoning_text + chunk.text,
                self._max_text_chars,
            )

            # 同时积累未确认的新增推理，供下一条进度提醒使用。
            current.progress_reasoning_buffer.append_delta(chunk.text)

    def _on_reasoning_deadline(self, record: _CognitiveTaskRecord) -> None:
        """处理一次认知活动进度的定时检查。

        检查期间若积累了新推理，则冻结它并发出提醒；没有增量时不发提醒，
        但仍按稳定间隔安排下一次检查。新推理到达本身永远不触发即时播报。

        Args:
            record: 这次定时检查对应的认知任务记录。
        """

        with self._lock:
            current = self._active_record
            if (
                current is not record
                or current.state != "running"
                or current.content_started
            ):
                return
            current.schedule_handle = None
            current.progress_check_count += 1
            if not current.progress_reasoning_buffer.has_accumulating_text():
                self._schedule_next_reasoning_check_locked(current)
                return
            self._emit_reasoning_progress_locked(current)

    def _emit_reasoning_progress_locked(self, record: _CognitiveTaskRecord) -> None:
        """冻结新增推理，并产生不包含正文的进度提醒。

        Args:
            record: 已在锁内确认仍可产生进度的任务记录。
        """

        # 冻结本次尚未被前台确认的推理增量，确保同一段内容只提醒一次。
        sequence = record.progress_reasoning_buffer.freeze()
        if sequence is None:
            self._schedule_next_reasoning_check_locked(record)
            return
        record.last_progress_at = self._monotonic_now()
        # 事件只携带序号；前台按序号读取冻结内容，避免在事件队列复制推理正文。
        self._append_event(
            self._event(
                "cognitive_task.progress",
                {
                    "activity_id": record.activity_id,
                    "interaction_session_id": record.interaction_session_id,
                    "notice_seq": sequence,
                    "updated_at": record.last_progress_at,
                },
            )
        )
        self._schedule_next_reasoning_check_locked(record)

    def _enter_content_locked(self, record: _CognitiveTaskRecord) -> None:
        """在正文首次出现时废止所有弱进度并停止调度。

        Args:
            record: 已在锁内进入正文阶段的任务记录。
        """

        record.content_started = True
        record.phase = "finalizing"
        record.progress_reasoning_buffer.clear_pending()
        self._cancel_schedule_locked(record)
        # 首个正文片段出现后，公共阶段进入“生成结果”。
        self._transition_stage_locked(record, "generating_result")
        if record.plan.progress_policy == "disabled":
            return
        self._append_event(
            self._event(
                "cognitive_task.progress_invalidated",
                {
                    "activity_id": record.activity_id,
                    "interaction_session_id": record.interaction_session_id,
                    "updated_at": self._monotonic_now(),
                },
            )
        )

    def _complete_task(self, record: _CognitiveTaskRecord) -> None:
        """关闭运行中任务，并把权威结果放入终态事件。

        Args:
            record: 已完成模型流消费的任务记录。
        """

        with self._lock:
            current = self._active_record
            if current is not record:
                return
            # 取消可能恰好发生在流结束后；一旦接受取消，就不能再提交完成态。
            if current.state == "cancelling":
                self._finish_cancelled_locked(current)
                return
            if current.state != "running":
                return
            raw_output = "".join(current.content_parts)

        try:
            cognitive_result = self._result_resolver.resolve(
                activity_id=record.activity_id,
                plan=record.plan,
                raw_output=raw_output,
                cancellation_signal=record.cancellation_token,
            )
        except CognitiveResultProtocolError:
            with self._lock:
                current = self._active_record
                if current is not record:
                    return
                if current.state == "cancelling":
                    self._finish_cancelled_locked(current)
                    return
                if current.state != "running":
                    return
                current.content_parts.clear()
                self._set_stable_execution_failure_locked(
                    current,
                    failure_code="cognitive_result_protocol_failed",
                )
                self._fail_task_locked(
                    current,
                    failure_from_reason(
                        "cognitive_result_protocol_failed",
                        stage_code=current.activity_stage,
                    ),
                    error_text="cognitive_result_protocol_failed",
                )
            return

        with self._lock:
            current = self._active_record
            if current is not record:
                return
            if current.state == "cancelling":
                self._finish_cancelled_locked(current)
                return
            if current.state != "running":
                return
            current.content_parts.clear()
            self._project_validated_result_locked(current, cognitive_result)

    def _project_validated_result_locked(
        self,
        record: _CognitiveTaskRecord,
        result: CognitiveResult,
    ) -> None:
        """把已验证二分支结果投影成唯一 Activity 终态。

        Args:
            record: 已在锁内确认仍运行中的认知任务。
            result: 通过本地二分支校验的模型终态。
        """

        if isinstance(result, CognitiveSuccessResult):
            record.result_text = result.result
            record.summary = result.summary
            record.failure_reason = None
            self._transition_stage_locked(record, "delivering_result")
            record.state = "completed"
            record.updated_at = self._monotonic_now()
            self._clear_progress_locked(record)
            self._append_event(
                self._event("cognitive_task.completed", self._terminal_payload(record))
            )
            record.finished_event.set()
            record.plan = _clear_plan_evidence(record.plan)
            self._release_starting_correction_locked(record)
            return

        record.result_text = ""
        record.summary = ""
        record.failure_reason = result.failure_reason
        # 合法业务失败由执行器生成内部码，模型只能提供经过安全门禁的自然语言原因。
        self._fail_task_locked(
            record,
            failure_from_reason(
                "cognitive_task_failed",
                stage_code=record.activity_stage,
            ),
            error_text="cognitive_task_failed",
        )

    def _fail_task_with_exception(
        self,
        record: _CognitiveTaskRecord,
        exc: BaseException,
        *,
        suppress_notice: bool = False,
    ) -> None:
        """在线程安全边界内把异常任务关闭为失败，并生成公开故障报告。

        Args:
            record: 发生失败的认知任务记录。
            exc: 最靠近真实失败处捕获到的异常。
            suppress_notice: 当前工具二轮已经负责表达启动失败时为真。
        """

        with self._lock:
            if self._active_record is not record:
                return
            if record.state == "cancelling":
                self._finish_cancelled_locked(record)
                return
            if record.state != "running":
                return
            failure = failure_from_exception(exc, stage_code=record.activity_stage)
            self._set_stable_execution_failure_locked(
                record,
                failure_code=failure.code,
            )
            self._fail_task_locked(
                record,
                failure,
                error_text=failure.code,
                suppress_notice=suppress_notice,
            )

    def _fail_starting_task_with_exception(
        self,
        record: _CognitiveTaskRecord,
        exc: BaseException,
        *,
        suppress_notice: bool,
    ) -> None:
        """把 Runtime 未接管的异常收口为启动前失败。

        Args:
            record: 仍占用 starting 提交窗口的认知任务记录。
            exc: 底层 Runtime submit 抛出的异常。
            suppress_notice: 工具二轮已负责表达启动失败时为真。
        """

        with self._lock:
            if self._active_record is not record or record.state != "starting":
                return
            failure = failure_from_exception(exc, stage_code=record.activity_stage)
            self._set_stable_execution_failure_locked(
                record,
                failure_code=failure.code,
            )
            self._fail_task_locked(
                record,
                failure,
                error_text=failure.code,
                suppress_notice=suppress_notice,
                execution_phase="before_start",
            )

    def _set_stable_execution_failure_locked(
        self,
        record: _CognitiveTaskRecord,
        *,
        failure_code: str,
    ) -> None:
        """保存不依赖供应商原始响应的确定性失败事实。

        Args:
            record: 已在锁内确认仍运行中的认知任务。
            failure_code: Failure Reporter 已分类的稳定错误码。
        """

        stable_failure_code = _execution_failure_fact_code(failure_code)
        failure_reason, summary = _EXECUTION_FAILURE_FACTS[stable_failure_code]
        record.result_text = ""
        record.summary = summary
        record.failure_reason = failure_reason

    def _fail_task_locked(
        self,
        record: _CognitiveTaskRecord,
        failure: ActivityFailure,
        *,
        error_text: str = "",
        suppress_notice: bool = False,
        execution_phase: Literal["before_start"] | None = None,
    ) -> None:
        """在已持锁状态下关闭失败任务并生成携带公开故障报告的终态。

        Args:
            record: 需要关闭的认知任务记录。
            failure: Failure Reporter 生成的公开故障报告。
            error_text: 仅供内部诊断使用的异常文本。
            suppress_notice: 当前工具二轮已经负责表达启动失败时为真。
            execution_phase: 只在 Runtime 尚未接管时写入的启动前阶段。
        """

        record.state = "failed"
        record.failure = failure
        record.error_text = _trim_text(error_text, self._max_text_chars)
        record.updated_at = self._monotonic_now()
        self._clear_progress_locked(record)
        terminal_payload = self._terminal_payload(record)
        if suppress_notice:
            terminal_payload["suppress_notice"] = True
        if execution_phase is not None:
            terminal_payload["execution_phase"] = execution_phase
        self._append_event(self._event("cognitive_task.failed", terminal_payload))
        record.finished_event.set()
        record.plan = _clear_plan_evidence(record.plan)
        self._release_starting_correction_locked(record)

    def _finish_cancelled(self, record: _CognitiveTaskRecord) -> None:
        """在执行器真实退出后提交唯一取消终态。

        Args:
            record: 已经结束底层执行的认知任务记录。
        """

        with self._lock:
            if self._active_record is not record or record.state != "cancelling":
                return
            self._finish_cancelled_locked(record)

    def _finish_cancelled_locked(self, record: _CognitiveTaskRecord) -> None:
        """在已持锁状态下提交取消终态并释放不再需要的原始证据。

        Args:
            record: 已确认仍属于当前 Activity Executor 的取消中活动。
        """

        record.state = "cancelled"
        record.updated_at = self._monotonic_now()
        record.result_text = ""
        record.summary = ""
        record.failure_reason = None
        record.error_text = ""
        self._clear_progress_locked(record)
        self._append_event(
            self._event(
                "cognitive_task.cancelled",
                {
                    **self._terminal_payload(record),
                    "cancellation_reason": record.cancellation_token.reason,
                },
            )
        )
        record.finished_event.set()
        record.plan = _clear_plan_evidence(record.plan)
        self._release_starting_correction_locked(record)

    def _release_starting_correction_locked(
        self,
        record: _CognitiveTaskRecord,
    ) -> None:
        """释放已经由 worker 或终态路径接管的 starting 纠正预留。

        Args:
            record: 可能仍占用纠正预留身份的认知任务记录。

        Returns:
            无。记录或阶段不匹配时保持现场不变。
        """

        if (
            self._prepared_correction_record is record
            and self._prepared_correction_phase == "starting"
        ):
            self._prepared_correction_record = None
            self._prepared_correction_phase = None

    def _schedule_next_reasoning_check_locked(self, record: _CognitiveTaskRecord) -> None:
        """根据已发送提醒数量安排下一次推理进度检查。

        Args:
            record: 已在锁内更新进度档位的任务记录。
        """
        if record.plan.progress_policy == "disabled":
            return
        try:
            # 一个活动只保留一个有效定时检查，避免旧回调在新档位下重复提醒。
            self._cancel_schedule_locked(record)
            interval = self._progress_policy.interval_for(record.progress_check_count)
            # 绑定运行记录本身，回调到期时会重新核对当前状态和 Activity 身份。
            record.schedule_handle = self._progress_scheduler.schedule(
                interval,
                lambda current_record=record: self._on_reasoning_deadline(current_record),
            )
        except Exception:
            # 进度提醒只是 Cognitive 的附加表达能力。模型任务已经越过 started 后，
            # 调度器故障只能让本轮不再主动播报进度，不能伪造任务执行失败。
            record.schedule_handle = None
            logger.exception(
                "认知任务进度检查调度失败 activity_id=%s",
                record.activity_id,
            )

    def _clear_progress_locked(self, record: _CognitiveTaskRecord) -> None:
        """取消定时检查，并清空活动的全部临时进度状态。

        Args:
            record: 已在锁内进入正文或终态的任务记录。
        """

        self._cancel_schedule_locked(record)
        record.progress_reasoning_buffer.clear_pending()

    def _cancel_schedule_locked(self, record: _CognitiveTaskRecord) -> None:
        """取消活动当前唯一的定时检查句柄。

        Args:
            record: 已在锁内准备清理调度状态的任务记录。
        """

        handle = record.schedule_handle
        record.schedule_handle = None
        if handle is not None:
            handle.cancel()

    def _record_by_activity_id(self, activity_id: str) -> _CognitiveTaskRecord | None:
        """在已持锁状态下按活动编号读取任务记录。

        Args:
            activity_id: 认知任务编号。

        Returns:
            匹配的任务记录；不存在时返回空。
        """

        record = self._active_record
        if record is None or record.activity_id != activity_id:
            return None
        return record

    def _base_payload(self, record: _CognitiveTaskRecord) -> dict[str, object]:
        """构造认知任务生命周期事件的基础字段。

        Args:
            record: 提供任务身份和目标的运行记录。

        Returns:
            包含任务、会话、类别和目标的基础载荷。
        """

        return {
            "activity_id": record.activity_id,
            "interaction_session_id": record.interaction_session_id,
            "state": record.state,
            "category": _cognitive_category(record.plan),
            "kind": record.plan.primary_kind,
            "brief": record.plan.goal_text,
            "goals": [
                {"kind": goal.kind, "brief": goal.brief}
                for goal in record.plan.goals
            ],
            "cognitive_tool_types": list(record.plan.effective_cognitive_tool_types),
            "reasoning": record.plan.reasoning,
            "web_search": record.plan.web_search,
            "web_extract": record.plan.web_extract,
            "progress_policy": record.plan.progress_policy,
            "based_on_activity_id": record.plan.based_on_activity_id,
            "source_user_text": record.source_user_text,
            "prepared_at": record.prepared_at,
            "started_at": record.started_at,
            "updated_at": record.updated_at,
        }

    def _terminal_payload(self, record: _CognitiveTaskRecord) -> dict[str, object]:
        """构造前台转述终态所需的完整载荷。

        Args:
            record: 已进入终态的认知任务记录。

        Returns:
            包含权威结果与终态信息的完整载荷。
        """

        return {
            **self._base_payload(record),
            "result_text": record.result_text,
            "summary": record.summary,
            "failure_reason": record.failure_reason,
            "task_input_snapshot": {
                "source_user_text": record.source_user_text,
                "goal": record.plan.goal_text,
                "response_goals": [
                    goal.response_goal
                    for goal in record.plan.goals
                    if goal.response_goal
                ],
                "source_urls": list(record.plan.source_urls),
                "encoded_image_count": len(record.plan.images),
                "image_materials": [item.to_payload() for item in record.plan.image_materials],
                "evidence_limitations": list(record.plan.evidence_limitations),
                "reasoning": record.plan.reasoning,
                "web_search": record.plan.web_search,
                "web_extract": record.plan.web_extract,
                "based_on_activity_id": record.plan.based_on_activity_id,
            },
            "failure": record.failure.to_payload() if record.failure is not None else None,
            "reasoning_token_count": record.reasoning_token_count,
            "content_token_count": record.content_token_count,
        }

    def _append_event(self, event: dict[str, object]) -> None:
        """记录事件并唤醒外层事件转发循环。

        Args:
            event: 已完成标准化的内部任务事件。
        """

        self._runtime.append_event(event)

    @staticmethod
    def _event(event_type: str, payload: dict[str, object]) -> dict[str, object]:
        """构造统一的 JSON 友好事件字典。

        Args:
            event_type: 内部事件名称。
            payload: 事件业务载荷。

        Returns:
            可直接进入运行时事件队列的字典。
        """

        return {"type": event_type, "payload": payload}


def _cognitive_category(plan: CognitiveExecutionPlan) -> str:
    """把认知计划转换为前台使用的活动类别。

    Args:
        plan: 已合并的后台认知计划。

    Returns:
        面向前台的稳定活动类别。
    """

    effective_types = plan.effective_cognitive_tool_types
    if len(effective_types) > 1:
        return "综合认知任务"
    if effective_types[0] == "analyze_deeply":
        return "深度分析"
    if effective_types[0] == "lookup_current_fact":
        return "实时信息查询"
    return "当前画面理解"


def _execution_failure_fact_code(activity_failure_code: str) -> str:
    """把公共 Activity 故障归并为稳定的 Cognitive 失败说明。

    Args:
        activity_failure_code: Failure Reporter 已完成安全分类的公共错误码。

    Returns:
        协议故障、Provider 不可用、超时或普通请求失败四类事实之一。
    """

    if activity_failure_code == "cognitive_result_protocol_failed":
        return "cognitive_result_protocol_failed"
    if activity_failure_code == "request_timed_out":
        return "provider_timeout"
    if activity_failure_code in {
        "provider_unavailable",
        "network_unavailable",
        "provider_stream_interrupted",
    }:
        return "provider_unavailable"
    return "request_failed"


def _trim_text(text: str, max_chars: int) -> str:
    """按统一异常容量上限保留最近文本。

    Args:
        text: 需要限制长度的内部文本。
        max_chars: 允许保留的最大字符数。

    Returns:
        未超限时返回原文，超限时返回最近的字符片段。
    """

    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _clear_plan_evidence(plan: CognitiveExecutionPlan) -> CognitiveExecutionPlan:
    """清除任务终态后不再需要的图片和网页原始证据。

    Args:
        plan: 已结束任务持有的认知计划。

    Returns:
        保留目标和能力事实、移除重型证据后的计划。
    """

    return replace(
        plan,
        images=(),
        source_urls=(),
        parent_task_context=None,
    )


def _remaining_timeout(deadline: float) -> float:
    """返回统一关闭截止时间的剩余秒数。

    Args:
        deadline: 基于单调时钟的绝对关闭截止时间。

    Returns:
        不小于零的剩余秒数。
    """

    return max(0.0, deadline - time.monotonic())
