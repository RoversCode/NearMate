"""协调活动提醒从领域事实到前台交付的进程内状态机。

活动事件经 ``event_mapper`` 转为标准事实后，由 ``apply_event()`` 登记为提醒候选；
构造用户请求或独立 System Notice 时，``build_notice_context()`` 原子预留一批候选；
Bridge 与助手输出链路再通过 ``apply_delivery()`` 回报入队、LLM 完成、取消和异常终态。
本协调器只维护候选、批次和交付账本：不生成前台回复、不直接写队列，也不直接取消
请求。来源事件替换产生的请求编号由 ``LiveKitMediaSession`` 负责条件取消。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from companion.activities.notice.delivery_policy import (
    notice_lane_key,
    notice_sort_key,
    select_notice_batch,
    should_replace_pending_progress,
)
from companion.activities.notice.delivery_state import (
    ActiveActivityNoticeState,
    InteractionSessionNoticeState,
    NoticeDeliveryState,
    NoticeReadMode,
    NoticeRecord,
    NoticeRequestState,
    NoticeState,
    NoticeStrength,
)
from companion.activities.notice.models import (
    ActivityInteractionNoticeInvalidation,
    ActivityEventApplyResult,
    ActivityLifecycleEvent,
    ActivityNoticeInterruptionStrategy,
    ActivityProgressNoticeIntent,
    ActivityProgressNoticeInvalidation,
    ActivityTerminalState,
    CognitiveResultNoticeSnapshot,
    InformationRequestNoticeSnapshot,
    MappedActivityEvent,
    NoticeDeliveryCommand,
    NoticeDeliveryResult,
    NoticeContext,
    NoticeIntent,
    NoticeSnapshot,
    PermissionRequestNoticeSnapshot,
    ProgressReportNoticeSnapshot,
    TerminalStatusNoticeSnapshot,
    WorkResultNoticeSnapshot,
)
from companion.activities.runtime.failure_reporting import failure_notice_text
from companion.observability.logging_config import get_backend_logger
from companion.observability.structured_logging import log_event


logger = get_backend_logger(__name__)


@dataclass
class NoticeBatchLease:
    """表示一批已预留提醒在“成功入队”与“入队前失败”之间的唯一结论。

    预留发生在构造助手请求时，但此时请求尚未被 Bridge 接管。提交方必须在
    ``queue.put`` 成功后调用 ``commit_enqueued()``，或在此前失败时调用 ``rollback()``；
    两条路径互斥且幂等，避免迟到回滚把已进入输出链路的提醒重新放回候选池。

    Args:
        request_id: 本批提醒绑定的助手请求编号。
        coordinator: 持有提醒主账本的协调器。
    """

    request_id: str
    coordinator: NoticeDeliveryCoordinator
    _state: Literal["prepared", "enqueued", "rolled_back"] = field(
        default="prepared",
        init=False,
        repr=False,
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def state(self) -> str:
        """返回该批次尚待入队、已入队或已回滚的稳定状态。"""

        with self._lock:
            return self._state

    def commit_enqueued(self) -> bool:
        """在请求成功交给 Bridge 后，把预留批次确认成已入队。

        Bridge 的即时入队通知与本方法可能在同一成功路径上都到达；协调器的交付
        转换本身是幂等的，因此重复确认不会改变结果。
        """

        with self._lock:
            if self._state != "prepared":
                return False
            self.coordinator.apply_delivery(
                NoticeDeliveryCommand(
                    request_id=self.request_id,
                    outcome="request_enqueued",
                )
            )
            self._state = "enqueued"
            return True

    def rollback(self) -> bool:
        """在请求尚未入队时归还批次，使仍有效的提醒可被下次请求领取。"""

        with self._lock:
            if self._state != "prepared":
                return False
            self.coordinator.apply_delivery(
                NoticeDeliveryCommand(
                    request_id=self.request_id,
                    outcome="request_enqueue_failed",
                )
            )
            self._state = "rolled_back"
            return True


class NoticeDeliveryCoordinator:
    """维护每个交互会话的提醒候选、请求批次和独立输出占用。

    弱进度只保留各来源最新的一条候选；强提醒保留到 LLM 正常完成或异常终止。同一
    交互会话内，普通生命周期按来源串行领取；每个权限或资料请求使用自己的交付车道，
    允许同一 Activity 的并列问题同批表达。所有可变账本都在同一把锁中更新，以保证
    事件替换、批次预留与请求收口之间不会出现双重投递、倒序领取或错误复活。它是
    交付协调者，不是输出执行者。
    """

    def __init__(
        self,
        *,
        max_provider_protocol_retries: int = 1,
        monotonic_now: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化提醒协调器。

        Args:
            max_provider_protocol_retries: 强提醒因厂商协议失败允许重新入队的次数。
            monotonic_now: 协调器本地排序和租约使用的单调时钟。
        """

        if max_provider_protocol_retries < 0:
            raise ValueError("max_provider_protocol_retries 不能小于零")
        self._max_provider_protocol_retries = max_provider_protocol_retries
        # 交付顺序和租约计时不能受系统时钟校准影响。
        self._monotonic_now = monotonic_now
        # Activity 事件、Bridge 回报和媒体回调可并发进入，必须共用同一锁边界。
        self._lock = threading.RLock()
        # 状态聚合在单一账本，避免多份索引在异常回滚时失去一致。
        self._state = NoticeDeliveryState()
        # 义务事实与 Notice 请求状态共用上面的锁；本账本不另建重试队列。

    def apply_delivery(self, command: NoticeDeliveryCommand) -> NoticeDeliveryResult:
        """根据输出链路回报收口已预留的提醒批次。

        ``request_enqueued`` 确认 Bridge 已接管批次；随后 LLM 完成、用户打断、
        输出不可用和厂商协议失败，分别决定提醒应结束、重排或丢弃。这里是
        请求交付状态唯一的写入口，调用方不应直接修改 NoticeRecord。

        Args:
            command: 带明确请求身份和交付结果的 Notice 命令。

        Returns:
            请求身份是否有效，以及实际选择的交付结果。
        """

        request_id = command.request_id.strip()
        if not request_id:
            return NoticeDeliveryResult(
                accepted=False,
                request_id="",
                outcome=command.outcome,
            )

        transitions = {
            # Bridge 已接管请求：预留批次不能再按“尚未入队”回滚。
            "request_enqueued": self._handle_request_enqueued,
            # 请求未能进入 Bridge：归还仍有效的提醒，供下一次请求重新领取。
            "request_enqueue_failed": self._handle_request_enqueue_failed,
            # LLM 已正常产出非空 final：强弱提醒和独立播报占用都立即结束。
            "llm_output_completed": self._handle_llm_output_completed,
            # 用户打断输出：强提醒重排，弱进度丢弃，避免旧进度打断后续对话。
            "delivery_cancelled": self._handle_delivery_cancelled,
            # Bridge 或输出链路已永久不可用：全部丢弃，不能像普通取消一样重排。
            "output_unavailable": self._handle_output_unavailable,
            # 厂商协议失败：弱进度丢弃，强提醒只在有限预算内重新入候选池。
            "provider_protocol_failed": self._handle_provider_protocol_failed,
            # 外层已确认旧请求被替换：删除过期项，归还同批仍有效的提醒。
            "superseded": self._handle_superseded,
        }
        with self._lock:
            released_terminal_activity_keys = transitions[command.outcome](request_id)
        logger.info(
            "Notice 交付命令已应用 request_id=%s outcome=%s",
            request_id,
            command.outcome,
        )
        return NoticeDeliveryResult(
            accepted=True,
            request_id=request_id,
            outcome=command.outcome,
            released_terminal_activity_keys=released_terminal_activity_keys,
        )

    def apply_event(
        self,
        event: MappedActivityEvent,
        runtime_epoch: int,
    ) -> ActivityEventApplyResult:
        """登记活动事件，并返回需要由外层条件取消的过期请求。

        进度只更新弱候选；启动记录中断时所需的最小活动事实；终态和用户注意事件
        形成强提醒。若新事实替换了已被某请求预留的旧提醒，返回该请求编号；媒体
        会话随后根据请求是否仍可取消执行补偿，本类不直接触碰 Bridge。

        Args:
            event: 来源映射器生成的标准事件。
            runtime_epoch: 事件所属助手运行时世代。

        Returns:
            因提醒过期而应尝试条件取消的请求编号。
        """

        interaction_session_id = event.interaction_session_id.strip()
        activity_id = event.activity_id.strip()
        source = event.source.strip()
        if not interaction_session_id or not activity_id or not source:
            return ActivityEventApplyResult()

        with self._lock:
            if runtime_epoch < self._state.minimum_open_runtime_epoch:
                return ActivityEventApplyResult()
            if isinstance(event, ActivityProgressNoticeIntent):
                return self._apply_progress(event, runtime_epoch)
            if isinstance(event, ActivityProgressNoticeInvalidation):
                return self._apply_progress_invalidation(event, runtime_epoch)
            if isinstance(event, ActivityInteractionNoticeInvalidation):
                return self._apply_interaction_invalidation(event, runtime_epoch)
            if event.lifecycle == "started":
                key = (runtime_epoch, interaction_session_id, source, activity_id)
                # 活动编号在一个运行时世代内唯一；终态先到时，迟到的 started 不能把
                # 已封口的活动重新打开。新世代天然使用不同 key，不需要删除旧终态。
                if key in self._state.terminal_activity_keys:
                    return ActivityEventApplyResult()
                # 记录中断投影和进度去重所需的最小公开事实，不保存执行正文。
                self._state.active_activities_by_key[key] = ActiveActivityNoticeState(
                    category=event.category,
                    goal=event.goal,
                )
                return ActivityEventApplyResult()
            if event.lifecycle == "terminal":
                return self._apply_terminal(event, runtime_epoch)
            return self._apply_interaction(event, runtime_epoch)

    def interrupt_active(
        self,
        runtime_epoch: int,
        strategy: ActivityNoticeInterruptionStrategy = "silent",
    ) -> ActivityEventApplyResult:
        """收口一个失效运行时世代的活动，并阻止其迟到事件重新打开候选。

        计划内热替换使用 ``silent``：只清理在途提醒。意外中断使用 ``notify``：
        除清理外，为每项已 started 的真实活动补一条 ``interrupted`` 强终态。提高
        最小可接收世代后，旧子进程晚到的进度或终态都会被忽略。

        Args:
            runtime_epoch: 已失效的助手运行时世代。
            strategy: silent 只废止在途提醒并封住迟到事件；
                notify 再为每项真实活动生成一次 interrupted 强终态。

        Returns:
            已被中断事实替代、需要尝试取消的提醒请求编号。
        """

        with self._lock:
            self._state.minimum_open_runtime_epoch = max(
                self._state.minimum_open_runtime_epoch,
                runtime_epoch + 1,
            )
            request_ids: set[str] = set()
            interrupted = [
                (key, activity_state)
                for key, activity_state in self._state.active_activities_by_key.items()
                if key[0] == runtime_epoch
            ]
            for key, activity_state in sorted(interrupted, key=lambda item: item[0]):
                _, interaction_session_id, source, activity_id = key
                self._state.terminal_activity_keys.add(key)
                request_ids.update(
                    self._supersede_notices(
                        interaction_session_id=interaction_session_id,
                        source=source,
                        activity_id=activity_id,
                        runtime_epoch=runtime_epoch,
                        intents={
                            "progress_report",
                            "cognitive_result",
                            "work_result",
                            "terminal_status",
                            "permission_request",
                            "information_request",
                        },
                    )
                )
                self._state.active_activities_by_key.pop(key, None)
                if strategy == "notify":
                    # 意外中断必须明确告知任务未能完成；计划内热替换不播报。
                    self._append_notice(
                        activity_id=activity_id,
                        interaction_session_id=interaction_session_id,
                        source=source,
                        runtime_epoch=runtime_epoch,
                        intent="terminal_status",
                        strength="strong",
                        sequence=0,
                        category=activity_state.category,
                        goal=activity_state.goal,
                        status_context=failure_notice_text("runtime_interrupted"),
                        terminal_state="interrupted",
                    )

            return _apply_result(request_ids)

    def create_batch_lease(self, request_id: str) -> NoticeBatchLease:
        """为即将提交的助手请求创建批次提交/回滚句柄。

        Args:
            request_id: 即将绑定提醒的助手请求编号。

        Returns:
            可在 queue.put 后提交或在异常时回滚的事务句柄。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ValueError("notice batch lease 必须绑定非空 request_id")
        return NoticeBatchLease(
            request_id=normalized_request_id,
            coordinator=self,
        )

    def build_notice_context(
        self,
        interaction_session_id: str,
        *,
        mode: NoticeReadMode,
        request_id: str | None = None,
    ) -> NoticeContext:
        """为一次助手请求预留可交付的提醒，并构造其 Notice 上下文。

        ``system_notice`` 可领取强提醒和弱进度，作为独立前台回复；``user_request``
        只附着强提醒，避免弱进度打断用户当前问题；``preview`` 不占用任何记录。
        活动快照由子进程执行器在真正构造 Prompt 前现场读取，不能由这里伪造。

        Args:
            interaction_session_id: 当前交互会话编号。
            mode: 预览、用户请求附着或 system notice 预留模式。
            request_id: 消费提醒时使用的助手请求编号。

        Returns:
            本轮原子预留的提醒批次；Activity 快照由助手执行层现场读取。
        """

        interaction_session_id = interaction_session_id.strip()
        with self._lock:
            notices = self._reserve_batch(interaction_session_id, mode=mode, request_id=request_id)
        # 活动快照在子进程构造 Prompt 前读取；这里仅返回已预留的 Notice 事实。
        return {
            "notice_batch": [_notice_payload(notice) for notice in notices],
        }

    def _handle_llm_output_completed(
        self,
        request_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """在 LLM 正常产出非空 final 后结束本请求绑定的全部提醒。

        Notice 只跟踪 LLM 是否完成这次表达生成，不等待控制面广播、TTS 或音频播放。
        因此无论本次请求承载强批次还是弱批次，其 Notice 和独立 System Notice 占用
        都在同一锁内立即清账。
        """

        if request_id not in self._state.requests_by_id:
            return ()
        notices = self._request_notices(request_id)
        released_activity_keys = _terminal_activity_keys(notices)
        for notice in notices:
            self._remove_notice(notice)
        self._close_request(request_id)
        return released_activity_keys

    def _handle_request_enqueued(
        self,
        request_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """把准备态批次确认成 Bridge 已接管的已入队批次。"""

        request_state = self._state.requests_by_id.get(request_id)
        if request_state is None or request_state.stage == "enqueued":
            return ()
        request_state.stage = "enqueued"
        return ()

    def _handle_request_enqueue_failed(
        self,
        request_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """只回滚仍在准备态的批次，归还其中尚未过期的提醒。"""

        request_state = self._state.requests_by_id.get(request_id)
        if request_state is None or request_state.stage != "prepared":
            return ()
        for notice in self._request_notices(request_id):
            notice.request_id = None
            if notice.state == "superseded":
                notice.state = "dropped"
                self._remove_notice(notice)
                continue
            notice.state = "pending"
            self._offer_pending_notice(notice)
        self._close_request(request_id)
        return ()

    def _handle_delivery_cancelled(
        self,
        request_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """按用户打断语义重排仍有效的强提醒，并丢弃弱进度与过期事实。"""

        request_state = self._state.requests_by_id.get(request_id)
        if request_state is None:
            return ()
        request_notices = self._request_notices(request_id)
        # 状态更新取消和带尾注 final 来自不同线程，谁先进入主进程事件队列不固定。
        # 只要独立批次已经含 superseded 项，这次中断就必须按状态更新语义归还同批
        # 仍有效的弱提醒；否则普通 delivery_cancelled 会把它们当成用户打断直接丢掉。
        if (
            request_state.mode == "system_notice"
            and any(notice.state == "superseded" for notice in request_notices)
        ):
            return self._handle_superseded(request_id)
        released_notices: list[NoticeRecord] = []
        for notice in request_notices:
            notice.request_id = None
            if notice.state == "superseded":
                # 更新事实已经明确淘汰这条提醒，用户取消旧请求不能把过期强事实复活。
                notice.state = "dropped"
                released_notices.append(notice)
                self._remove_notice(notice)
                continue
            if notice.strength == "strong":
                notice.state = "pending"
                self._offer_pending_notice(notice)
            else:
                notice.state = "dropped"
                self._remove_notice(notice)
        self._close_request(request_id)
        return _terminal_activity_keys(released_notices)

    def _handle_output_unavailable(
        self,
        request_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """在输出链路永久不可用时收口请求，并保留尚未表达的恢复结果。"""

        if request_id not in self._state.requests_by_id:
            return ()
        notices = self._request_notices(request_id)
        released_notices: list[NoticeRecord] = []
        for notice in notices:
            notice.request_id = None
            notice.state = "dropped"
            released_notices.append(notice)
            self._remove_notice(notice)
        self._close_request(request_id)
        return _terminal_activity_keys(released_notices)

    def _handle_provider_protocol_failed(
        self,
        request_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """在厂商协议失败时丢弃弱进度，并在预算内重排强提醒。

        重排预算按单条提醒累计，而非按请求累计：强批次中的提醒可各自获得有限
        补偿；弱批次和已被新事实替换的提醒不重试，避免过期信息在输出异常后反复出现。
        """

        if request_id not in self._state.requests_by_id:
            return ()
        released_notices: list[NoticeRecord] = []
        for notice in self._request_notices(request_id):
            notice.request_id = None
            if notice.state == "superseded":
                # 协议失败只补偿仍然有效的事实；被更新事实替换的强提醒也必须直接删除。
                notice.state = "dropped"
                released_notices.append(notice)
                self._remove_notice(notice)
                action = "drop_superseded"
            else:
                notice.provider_protocol_failure_count += 1
                should_retry = (
                    notice.strength == "strong"
                    and notice.provider_protocol_failure_count
                    <= self._max_provider_protocol_retries
                )
                if should_retry:
                    notice.state = "pending"
                    self._offer_pending_notice(notice)
                    action = "retry"
                else:
                    notice.state = "dropped"
                    released_notices.append(notice)
                    self._remove_notice(notice)
                    action = "drop"
            log_event(
                logger,
                logging.WARNING,
                "notice_provider_protocol_failed",
                request_id=request_id,
                notice_intent=notice.intent,
                notice_strength=notice.strength,
                failure_count=notice.provider_protocol_failure_count,
                action=action,
            )
        self._close_request(request_id)
        return _terminal_activity_keys(released_notices)

    def _handle_superseded(
        self,
        request_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """在外层确认旧请求已不再输出后，按每条提醒的当前事实完成对账。

            → 解除 Notice 与旧 request_id 的绑定
            → 删除过期 Notice，归还仍有效 Notice

        Args:
            request_id: 已被外层确认淘汰的助手请求编号。
        """

        # 请求可能已由其他终态收口；迟到的 superseded 回报不应影响任何新请求。
        if request_id not in self._state.requests_by_id:
            return ()
        released_notices: list[NoticeRecord] = []
        for notice in self._request_notices(request_id):
            # 旧请求已不会输出，先断开归属，随后按该 Notice 的真实有效性决定去向。
            notice.request_id = None
            if notice.state == "superseded":
                # 新进度或终态已经取代它，不能因为请求取消而重新放回候选池。
                notice.state = "dropped"
                released_notices.append(notice)
                self._remove_notice(notice)
            else:
                # 这是同批但未被替换的提醒；归还后仍可由后续请求正常表达。
                notice.state = "pending"
                self._offer_pending_notice(notice)
        # 释放请求账本及其可能占用的独立 System Notice 输出位。
        self._close_request(request_id)
        return _terminal_activity_keys(released_notices)

    def has_active_system_notice(self, interaction_session_id: str) -> bool:
        """判断会话输出链路是否仍被 system notice 占用。

        Args:
            interaction_session_id: 需要检查的交互会话编号。

        Returns:
            存在尚未完成输出闭环的提醒请求时返回真。
        """

        with self._lock:
            session_state = self._state.sessions_by_id.get(
                interaction_session_id.strip()
            )
            if session_state is None or session_state.system_request_id is None:
                return False
            request_state = self._state.requests_by_id.get(
                session_state.system_request_id
            )
            return request_state is not None and request_state.stage == "enqueued"

    def permission_interaction_request_ids(
        self,
        request_id: str,
    ) -> tuple[str, ...]:
        """读取指定助手请求当前占用的权限交互内部编号。

        Args:
            request_id: 已完成 Notice 批次预留的助手请求编号。

        Returns:
            去重并稳定排序后的权限交互请求编号；请求不存在时返回空元组。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return ()
        with self._lock:
            request_state = self._state.requests_by_id.get(normalized_request_id)
            if request_state is None:
                return ()
            session_state = self._state.sessions_by_id.get(
                request_state.interaction_session_id
            )
            if session_state is None:
                return ()
            return tuple(
                sorted(
                    {
                        notice.interaction_request_id
                        for notice in session_state.notices_by_id.values()
                        if notice.request_id == normalized_request_id
                        and notice.intent == "permission_request"
                        and notice.interaction_request_id
                    }
                )
            )

    def has_pending_notice(self, interaction_session_id: str) -> bool:
        """判断会话是否存在可被新 System Notice 请求预留的提醒。

        Args:
            interaction_session_id: 需要检查的交互会话编号。

        Returns:
            存在未被来源车道阻塞、可供独立播报领取的提醒时返回真。
        """

        with self._lock:
            interaction_session_id = interaction_session_id.strip()
            session_state = self._state.sessions_by_id.get(interaction_session_id)
            if session_state is None:
                return False
            # 已有独立请求时，本会话不能再预留第二条 System Notice；即使其它来源
            # 仍有 pending，也要等现有请求收口后再报告为可领取。
            if session_state.system_request_id is not None:
                return False
            return bool(
                select_notice_batch(
                    session_state,
                    mode="system_notice",
                )
            )

    def _apply_progress(
        self,
        event: ActivityProgressNoticeIntent,
        runtime_epoch: int,
    ) -> ActivityEventApplyResult:
        """仅接受更新的弱进度，并替换同一活动尚未完成交付的旧进度。

        ``sequence`` 是来源执行器给出的单调版本。旧版本直接忽略；新版本若已被
        某个请求领取，则把该请求标为需要条件取消，而不是在此处直接打断输出。

        Args:
            event: 新产生的弱进度提醒意图。

        Returns:
            需要取消的过期提醒请求集合。
        """

        # 世代隔离避免热替换后同名活动的迟到事件相互污染。
        key = (
            runtime_epoch,
            event.interaction_session_id,
            event.source,
            event.activity_id,
        )

        # 终态是该活动在本世代的最终事实，任何迟到进度都不能重新打开提醒。
        if key in self._state.terminal_activity_keys:
            return ActivityEventApplyResult()  # 表示本次事件没有产生任何后续 Notice 动作

        activity_state = self._state.active_activities_by_key.setdefault(
            key,
            ActiveActivityNoticeState(),
        )

        # 同一版本的重放不应制造新的候选或取消动作。
        if event.sequence <= activity_state.latest_progress_sequence:
            return ActivityEventApplyResult()

        # 保存最新版本，作为后续乱序事件的比较基准。
        activity_state.latest_progress_sequence = event.sequence

        # 待领取的旧候选直接删除；已进入请求链路的旧项转为 superseded。
        request_ids = self._supersede_notices(
            interaction_session_id=event.interaction_session_id,
            source=event.source,
            activity_id=event.activity_id,
            runtime_epoch=runtime_epoch,
            intents={"progress_report"},
        )

        # 新进度先留在候选池，只有下次可用的前台请求才会真正领取它。
        self._append_notice(
            activity_id=event.activity_id,
            interaction_session_id=event.interaction_session_id,
            source=event.source,
            runtime_epoch=runtime_epoch,
            intent="progress_report",
            strength="weak",
            sequence=event.sequence,
        )

        # 外层据此执行条件取消，协调器自身不持有输出队列。
        return _apply_result(request_ids)

    def _apply_progress_invalidation(
        self,
        event: ActivityProgressNoticeInvalidation,
        runtime_epoch: int,
    ) -> ActivityEventApplyResult:
        """在正文或更权威事实出现时废止同一活动的所有弱进度。

        Args:
            event: 指定需要废止弱进度的活动身份。

        Returns:
            需要取消的过期提醒请求集合。
        """

        request_ids = self._supersede_notices(
            interaction_session_id=event.interaction_session_id,
            source=event.source,
            activity_id=event.activity_id,
            runtime_epoch=runtime_epoch,
            intents={"progress_report"},
        )
        return _apply_result(request_ids)

    def _apply_terminal(
        self,
        event: ActivityLifecycleEvent,
        runtime_epoch: int,
    ) -> ActivityEventApplyResult:
        """将活动收口为唯一终态，并以强提醒替换同活动的在途信息。

        首条终态封住后续进度和重复终态，并清掉这项 Activity 的全部旧候选。普通终态
        会形成需要最终表达的强提醒；静默终态只完成账本收口，不追加用户可见提醒。

        Args:
            event: 活动完成或失败的生命周期事件。

        Returns:
            因终态替换而需要取消的提醒请求集合。
        """

        # 映射器必须明确说明成功成果或未完成终态，协调器不再通过状态猜表达意图。
        if event.terminal_state is None or event.intent not in {
            "cognitive_result",
            "work_result",
            "terminal_status",
        }:
            return ActivityEventApplyResult()
        if event.intent in {"cognitive_result", "work_result"}:
            if event.terminal_state != "completed":
                return ActivityEventApplyResult()
            if event.intent == "cognitive_result" and not event.result_context.strip():
                return ActivityEventApplyResult()
        elif event.terminal_state not in {
            "failed",
            "rejected",
            "cancelled",
            "interrupted",
        }:
            return ActivityEventApplyResult()

        # 世代、会话、来源和活动编号共同定义一条活动的终态去重键。
        key = (
            runtime_epoch,
            event.interaction_session_id,
            event.source,
            event.activity_id,
        )

        # 来源重放或乱序不应制造第二次完成、失败或取消提醒。
        if key in self._state.terminal_activity_keys:
            return ActivityEventApplyResult()

        # 先写终态账本，再清活动状态，保证并发事件看到一致的收口事实。
        self._state.terminal_activity_keys.add(key)
        self._state.active_activities_by_key.pop(key, None)

        # 终态是这项活动的最终事实，此前进度和用户介入要求都已经过期。只按完整
        # Activity 身份淘汰，不能误删同来源另一项活动仍需按 FIFO 交付的终态。
        request_ids = self._supersede_notices(
            interaction_session_id=event.interaction_session_id,
            source=event.source,
            activity_id=event.activity_id,
            runtime_epoch=runtime_epoch,
            intents={
                "progress_report",
                "cognitive_result",
                "work_result",
                "terminal_status",
                "permission_request",
                "information_request",
            },
        )

        # 用户纠正已经由当前 Foreground 轮表达替换事实。旧任务仍需完整收口，
        # 但不能再追加一条普通“已取消”提醒，否则会产生重复播报。
        if not event.suppress_notice:
            self._append_notice(
                activity_id=event.activity_id,
                interaction_session_id=event.interaction_session_id,
                source=event.source,
                runtime_epoch=runtime_epoch,
                intent=event.intent,
                strength="strong",
                sequence=0,
                category=event.category,
                goal=event.goal,
                activity_kind=event.activity_kind,
                result_context=event.result_context,
                status_context=event.status_context,
                terminal_state=event.terminal_state,
            )

        # 已领取的旧提醒由外层按请求实际状态条件取消。
        return _apply_result(request_ids)

    def _apply_interaction(
        self,
        event: ActivityLifecycleEvent,
        runtime_epoch: int,
    ) -> ActivityEventApplyResult:
        """登记一条权限或资料请求，同时保留同 Activity 的其它并列请求。

        Args:
            event: 需要用户注意或补充信息的生命周期事件。

        Returns:
            因交互请求替换旧进度而需要取消的请求集合。
        """

        if event.lifecycle != "waiting_user" or event.intent not in {
            "permission_request",
            "information_request",
        }:
            return ActivityEventApplyResult()
        key = (
            runtime_epoch,
            event.interaction_session_id,
            event.source,
            event.activity_id,
        )
        # 终态已经封口后，迟到的介入事件不再描述当前事实，不能重新产生提醒。
        if key in self._state.terminal_activity_keys:
            return ActivityEventApplyResult()
        if not event.interaction_request_id:
            return ActivityEventApplyResult()
        if event.intent == "permission_request" and (
            not event.action_summary or event.grant_scope != "activity"
        ):
            return ActivityEventApplyResult()
        if event.intent == "information_request" and not event.questions:
            return ActivityEventApplyResult()

        session_state = self._state.sessions_by_id.get(event.interaction_session_id)
        if session_state is not None and any(
            notice.runtime_epoch == runtime_epoch
            and notice.source == event.source
            and notice.activity_id == event.activity_id
            and notice.interaction_request_id == event.interaction_request_id
            for notice in session_state.notices_by_id.values()
        ):
            return ActivityEventApplyResult()

        # 用户需要介入时，普通进度已经失去表达价值；其它 request_id 对应的是并列问题，
        # 必须继续保留，不能再像旧 waiting_user 语义那样互相覆盖。
        request_ids = self._supersede_notices(
            interaction_session_id=event.interaction_session_id,
            source=event.source,
            activity_id=event.activity_id,
            runtime_epoch=runtime_epoch,
            intents={"progress_report"},
        )
        self._append_notice(
            activity_id=event.activity_id,
            interaction_session_id=event.interaction_session_id,
            source=event.source,
            runtime_epoch=runtime_epoch,
            intent=event.intent,
            strength="strong",
            sequence=0,
            category=event.category,
            goal=event.goal,
            activity_kind=event.activity_kind,
            interaction_request_id=event.interaction_request_id,
            action_summary=event.action_summary,
            grant_scope=event.grant_scope,
            questions=event.questions,
        )
        return _apply_result(request_ids)

    def _apply_interaction_invalidation(
        self,
        event: ActivityInteractionNoticeInvalidation,
        runtime_epoch: int,
    ) -> ActivityEventApplyResult:
        """精确废止已经被鼠标、语音或文字处理的一条交互提醒。

        独立 System Notice 若仍未出字，需要由外层条件取消；附着在真实用户轮的
        Notice 只标记过期，不能取消用户正在完成的回答 generation。

        Args:
            event: 已解决请求的稳定 Activity 和 request_id 身份。
            runtime_epoch: 事件所属助手运行时世代。

        Returns:
            只包含独立 System Notice 请求编号的取消建议。
        """

        session_state = self._state.sessions_by_id.get(event.interaction_session_id)
        if session_state is None:
            return ActivityEventApplyResult()

        superseded_system_request_ids: set[str] = set()
        for notice in list(session_state.notices_by_id.values()):
            if (
                notice.runtime_epoch != runtime_epoch
                or notice.source != event.source
                or notice.activity_id != event.activity_id
                or notice.interaction_request_id != event.interaction_request_id
                or notice.intent
                not in {"permission_request", "information_request"}
            ):
                continue
            request_id = notice.request_id
            if request_id is None:
                notice.state = "dropped"
                self._remove_notice(notice)
                continue
            notice.state = "superseded"
            request_state = self._state.requests_by_id.get(request_id)
            if request_state is not None and request_state.mode == "system_notice":
                superseded_system_request_ids.add(request_id)
        return _apply_result(superseded_system_request_ids)

    def _append_notice(
        self,
        *,
        activity_id: str,
        interaction_session_id: str,
        source: str,
        runtime_epoch: int,
        intent: NoticeIntent,
        strength: NoticeStrength,
        sequence: int,
        category: str = "",
        goal: str = "",
        activity_kind: str = "",
        result_context: str = "",
        status_context: str = "",
        terminal_state: ActivityTerminalState | None = None,
        interaction_request_id: str = "",
        action_summary: str = "",
        grant_scope: Literal["activity"] | None = None,
        questions: tuple[dict[str, object], ...] = (),
    ) -> NoticeRecord:
        """将一个已确认的活动事实写入会话账本，并尝试成为可预留候选。

        强提醒直接留在会话的待领取集合；弱进度还要与同一来源的当前候选竞争，
        以保证前台最多看到该来源最新的一条进度。

        Args:
            activity_id: 提醒关联的活动编号。
            interaction_session_id: 提醒所属交互会话编号。
            source: 活动来源类型。
            runtime_epoch: 提醒所属助手运行时世代。
            intent: 提醒的显式表达意图。
            strength: 提醒的强弱等级。
            sequence: 弱进度对应的来源版本；强提醒固定为零。
            category: 面向 foreground 的活动类别。
            goal: 活动目标摘要。
            activity_kind: 选择 Notice 表达规则所需的稳定任务类别。
            result_context: Cognitive 结果正文；后台成功摘要由稍后的投影器添加。
            status_context: 可供 Foreground 转述的安全终态原因。
            terminal_state: 活动终态；非终态提醒为空。
            interaction_request_id: 只用于交互提醒精确失效的内部请求编号。
            action_summary: 权限请求允许进入 Foreground 的安全中文操作摘要。
            grant_scope: 权限请求覆盖的固定 Activity 范围。
            questions: 资料请求的逐问题白名单事实。

        Returns:
            已写入唯一账本的 Notice 记录。
        """

        notice_id = f"notice-{self._state.next_notice_number}"
        self._state.next_notice_number += 1
        notice = NoticeRecord(
            notice_id=notice_id,
            activity_id=activity_id,
            interaction_session_id=interaction_session_id,
            source=source,
            runtime_epoch=runtime_epoch,
            intent=intent,
            strength=strength,
            sequence=sequence,
            created_at=self._monotonic_now(),
            category=category.strip(),
            goal=goal.strip(),
            activity_kind=activity_kind.strip(),
            result_context=result_context.strip(),
            status_context=status_context.strip(),
            terminal_state=terminal_state,
            interaction_request_id=interaction_request_id.strip(),
            action_summary=action_summary.strip(),
            grant_scope=grant_scope,
            questions=tuple(dict(question) for question in questions),
        )
        session_state = self._state.sessions_by_id.setdefault(
            interaction_session_id,
            InteractionSessionNoticeState(),
        )
        session_state.notices_by_id[notice_id] = notice
        # 竞争唯一候选槽
        self._offer_pending_notice(notice)
        return notice

    def _reserve_batch(
        self,
        interaction_session_id: str,
        *,
        mode: NoticeReadMode,
        request_id: str | None,
    ) -> list[NoticeRecord]:
        """按读取模式挑选候选，并原子绑定给一个即将提交的助手请求。

        预留同时完成三件事：从每个未占用来源最多选一条当前优先级最高的提醒、从
        弱进度候选槽移除它们、建立 ``request_id → notice`` 归属。之后任何成功、
        取消或失败都能沿请求身份精确收口；同一会话同一时间最多允许一个独立
        System Notice 占用输出。

        Args:
            interaction_session_id: 目标交互会话编号。
            mode: 当前请求的提醒预留模式。
            request_id: 接收本批提醒的助手请求编号。

        Returns:
            已原子绑定到请求的有序提醒记录。
        """

        # 预览不创建请求归属，因此不能改变候选状态。
        if not interaction_session_id or mode == "preview":
            return []

        # 没有请求身份就无法在输出终态时精确归还或删除这批提醒。
        normalized_request_id = (
            request_id.strip() if isinstance(request_id, str) else ""
        )
        if not normalized_request_id:
            return []

        # 请求上下文可能因重试构造而重复读取；必须复用首次预留结果。
        if normalized_request_id in self._state.requests_by_id:
            return self._request_notices(normalized_request_id)

        session_state = self._state.sessions_by_id.get(interaction_session_id)
        if session_state is None:
            return []

        # 单会话独立播报串行化，避免两个 System Notice 同时竞争同一输出通道。
        if mode == "system_notice" and session_state.system_request_id is not None:
            return []

        # 独立播报按策略取得一个强度的批次；用户请求只能携带强提醒，避免弱进度
        # 挤占主回复。选择函数只读状态，真正的请求归属仍在下面统一提交。
        selected = select_notice_batch(
            session_state,
            mode=mode,
        )
        if not selected:
            return []

        # 独立播报等待入队确认；附着用户请求则已属于该轮主回复。
        target_state: NoticeState = (
            "reserved" if mode == "system_notice" else "attached_user"
        )

        self._state.requests_by_id[normalized_request_id] = NoticeRequestState(
            request_id=normalized_request_id,
            interaction_session_id=interaction_session_id,
            mode=mode,
        )
        for notice in selected:
            # 已预留项不能继续作为弱进度候选被另一请求重复领取。
            if (
                session_state.pending_progress_notice_id_by_source.get(notice.source)
                == notice.notice_id
            ):
                session_state.pending_progress_notice_id_by_source.pop(
                    notice.source,
                    None,
                )
            # 表示“已保留播报资格、正在构造和提交请求”，还不表示“已经在工作”
            notice.state = target_state
            notice.request_id = normalized_request_id

        # 此处仅完成预留，Bridge 接管成功后才由交付命令推进为 enqueued。
        if mode == "system_notice":
            session_state.system_request_id = normalized_request_id
        return selected

    def _supersede_notices(
        self,
        *,
        interaction_session_id: str,
        source: str,
        activity_id: str,
        runtime_epoch: int,
        intents: set[NoticeIntent],
    ) -> set[str]:
        """以更新事实替换旧提醒，并返回已领取旧项所属的请求编号。

        待领取项从账本删除即可；已经绑定请求的项不能悄悄消失，必须标为
        ``superseded`` 并交由外层条件取消，避免旧文本已在输出链路中仍被播出。

        Args:
            interaction_session_id: 提醒所属交互会话编号。
            source: 活动来源类型。
            activity_id: 需要淘汰旧提醒的活动编号。
            runtime_epoch: 需要淘汰提醒所属的助手运行时世代。
            intents: 本次允许淘汰的显式意图集合。

        Returns:
            已经进入生成链路、需要尝试取消的请求编号集合。
        """

        request_ids: set[str] = set()
        session_state = self._state.sessions_by_id.get(interaction_session_id)
        if session_state is None:
            return request_ids
        # 目前存在的notice，要么去掉，要么superseded
        matches = [
            notice
            for notice in session_state.notices_by_id.values()
            if notice.source == source
            and notice.activity_id == activity_id
            and notice.runtime_epoch == runtime_epoch
            and notice.intent in intents
            and notice.state not in {"dropped", "superseded"}
        ]
        for notice in matches:
            if notice.state == "pending":
                # 尚未被请求领取的候选没有外部可见副作用，可以立即清理。
                notice.state = "dropped"
                self._remove_notice(notice)
                continue
            # 已经有请求归属时保留记录，等待原请求按自己的生命周期完成账本收口。
            notice.state = "superseded"
            request_id = notice.request_id
            if request_id is None:
                continue
            request_state = self._state.requests_by_id.get(request_id)
            # 只有独立 System Notice 可以因事实失效而被主动取消。附着在真实用户轮的
            # Notice 只是这轮 Prompt 的一小部分，取消整个请求会连用户自己的回答一起杀掉。
            if request_state is not None and request_state.mode == "system_notice":
                request_ids.add(request_id)
        return request_ids

    def _remove_notice(self, notice: NoticeRecord) -> None:
        """从会话账本和可能引用它的弱进度候选槽中删除一条提醒。

        Args:
            notice: 需要从协调器状态中移除的提醒记录。
        """

        session_state = self._state.sessions_by_id.get(
            notice.interaction_session_id
        )
        if session_state is None:
            return
        if (
            session_state.pending_progress_notice_id_by_source.get(notice.source)
            == notice.notice_id
        ):
            session_state.pending_progress_notice_id_by_source.pop(
                notice.source,
                None,
            )
        session_state.notices_by_id.pop(notice.notice_id, None)
        self._remove_empty_session(notice.interaction_session_id)

    def _offer_pending_notice(self, notice: NoticeRecord) -> None:
        """让待领取弱进度竞争同来源的唯一候选槽。

        强提醒不进入弱候选槽，最终由来源车道和全局优先级共同选择。弱进度只有胜过
        现有候选时才保留，这使重复调度或快速更新最多留下值得表达的最新进展。

        Args:
            notice: 已经处于 pending 状态、等待重新入槽的提醒记录。
        """

        # 已预留或已废止的记录不能重新参与候选选择。
        if notice.state != "pending":
            return

        # 强提醒的 Activity 级替换已在事件入口完成；此槽只负责弱进度候选竞争。
        if notice.strength == "strong":
            return

        session_state = self._state.sessions_by_id.get(notice.interaction_session_id)
        if session_state is None:
            return
        current_notice_id = session_state.pending_progress_notice_id_by_source.get(
            notice.source
        )

        # 首次进度直接成为该来源可被批次选择的候选。
        if current_notice_id is None or current_notice_id == notice.notice_id:
            session_state.pending_progress_notice_id_by_source[notice.source] = (
                notice.notice_id
            )
            return

        # 读取当前候选，检查槽位是否仍指向有效的待领取记录。
        current_notice = session_state.notices_by_id.get(current_notice_id)

        # 槽位可能被回滚、替换或交付清理；失效引用由当前记录直接修复。
        if current_notice is None or current_notice.state != "pending":
            session_state.pending_progress_notice_id_by_source[notice.source] = (
                notice.notice_id
            )
            return

        if should_replace_pending_progress(notice, current_notice):
            # 新版本优先级更高或同级更晚，旧候选不再有表达价值。
            current_notice.state = "dropped"
            self._remove_notice(current_notice)
            session_state.pending_progress_notice_id_by_source[notice.source] = (
                notice.notice_id
            )
            return

        # 当前候选仍更值得表达，丢弃本次弱进度即可。
        notice.state = "dropped"
        self._remove_notice(notice)

    def _request_notices(self, request_id: str) -> list[NoticeRecord]:
        """读取并稳定排序已绑定请求的提醒，供所有交付终态使用。

        Args:
            request_id: 助手请求编号。

        Returns:
            按业务优先级和创建顺序排列的提醒记录。
        """

        request_state = self._state.requests_by_id.get(request_id)
        if request_state is None:
            return []
        session_state = self._state.sessions_by_id.get(
            request_state.interaction_session_id
        )
        if session_state is None:
            return []
        notices = [
            notice
            for notice in session_state.notices_by_id.values()
            if notice.request_id == request_id
        ]
        notices.sort(key=notice_sort_key)
        return notices

    def _close_request(self, request_id: str) -> None:
        """把已经无需继续追踪的一次助手请求从 Notice 账本中收掉。

        一批 Notice 无论是正常播完、入队前失败、被用户打断还是被新事实替代，后续都
        不应再按这个 ``request_id`` 查找或处理。若它是独立 System Notice，还要释放
        “这个交互会话正在独立播报”的占位，让下一批提醒可以申请输出。

        Args:
            request_id: 已经完成或补偿收口的助手请求编号。
        """

        # 先取走请求账本，同时保留它所属会话，供后续释放独立播报占位。
        request_state = self._state.requests_by_id.pop(request_id, None)
        if request_state is None:
            return
        session_state = self._state.sessions_by_id.get(
            request_state.interaction_session_id
        )
        if session_state is None:
            return
        # 迟到的旧请求只能释放自己的占位，绝不能误放开后来新请求占用的输出链路。
        if session_state.system_request_id == request_id:
            session_state.system_request_id = None
        # 提醒和独立播报占位都清空后，整个会话的 Notice 状态也不必继续保留。
        self._remove_empty_session(request_state.interaction_session_id)

    def _remove_empty_session(self, interaction_session_id: str) -> None:
        """删除既无提醒也无 System Notice 占用的空会话状态。

        Args:
            interaction_session_id: 需要尝试清理的交互会话编号。
        """

        session_state = self._state.sessions_by_id.get(interaction_session_id)
        if session_state is None:
            return
        if session_state.notices_by_id or session_state.system_request_id is not None:
            return
        self._state.sessions_by_id.pop(interaction_session_id, None)


def _apply_result(
    request_ids: set[str],
    *,
    released_terminal_activity_keys: tuple[tuple[str, str], ...] = (),
) -> ActivityEventApplyResult:
    """把去重后的过期请求编号转换为交给媒体会话的不可变结果。

    Args:
        request_ids: 需要取消的助手请求编号集合。

    Returns:
        按稳定顺序保存请求编号的事件应用结果。
    """

    return ActivityEventApplyResult(
        superseded_request_ids=tuple(sorted(request_ids)),
        released_terminal_activity_keys=released_terminal_activity_keys,
    )


def _terminal_activity_keys(
    notices: list[NoticeRecord],
) -> tuple[tuple[str, str], ...]:
    """提取已经完成表达或明确删除的终态 Activity 身份。

    Args:
        notices: 本次交付转换真正移除的 Notice 记录。

    Returns:
        去重并稳定排序的来源与 Activity 编号。
    """

    terminal_intents = {
        "cognitive_result",
        "work_result",
        "terminal_status",
    }
    return tuple(
        sorted(
            {
                (notice.source, notice.activity_id)
                for notice in notices
                if notice.intent in terminal_intents
            }
        )
    )


def _notice_payload(notice: NoticeRecord) -> NoticeSnapshot:
    """将内部账本记录投影为可交给前台请求构造器的公开载荷。

    弱进度只暴露身份和版本，绝不携带推理或正文；强提醒才携带经来源适配器筛选的
    类别、目标和终态文案。请求归属、租约阶段和失败重试计数始终留在协调器内部。

    Args:
        notice: 需要暴露给 foreground 的内部提醒记录。

    Returns:
        不包含内部租约状态的提醒载荷。
    """

    if notice.intent == "progress_report":
        return ProgressReportNoticeSnapshot(
            notice_id=notice.notice_id,
            activity_id=notice.activity_id,
            source=notice.source,
            intent="progress_report",
            sequence=notice.sequence,
        )
    common = {
        "notice_id": notice.notice_id,
        "activity_id": notice.activity_id,
        "source": notice.source,
        "category": notice.category,
        "goal": notice.goal,
    }
    if notice.activity_kind:
        common["activity_kind"] = notice.activity_kind
    if notice.intent == "cognitive_result":
        return CognitiveResultNoticeSnapshot(
            intent="cognitive_result",
            result_context=notice.result_context,
            **common,
        )
    if notice.intent == "work_result":
        return WorkResultNoticeSnapshot(
            intent="work_result",
            **common,
        )
    if notice.intent == "terminal_status":
        return TerminalStatusNoticeSnapshot(
            intent="terminal_status",
            terminal_state=notice.terminal_state or "interrupted",
            **(
                {"status_context": notice.status_context}
                if notice.status_context
                else {}
            ),
            **common,
        )
    if notice.intent == "permission_request":
        return PermissionRequestNoticeSnapshot(
            intent="permission_request",
            action_summary=notice.action_summary,
            grant_scope="activity",
            **common,
        )
    return InformationRequestNoticeSnapshot(
        intent="information_request",
        questions=[dict(question) for question in notice.questions],
        **common,
    )
