from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from queue import Empty, Queue
from typing import Any, Protocol

from companion.activities.notice.models import (
    ActivityNoticeInterruptionStrategy,
    NoticeDeliveryCommand,
    NoticeDeliveryResult,
)
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.activities.terminal_settlement import (
    ActivityTerminalSettlementRetryableError,
)
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplyResult,
)
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import (
    AcknowledgeActivityProgressCommand,
    ActivityCancellationResultCommand,
    RecentMaterialObservation,
    RuntimeEvent,
)
from companion.realtime.assistant_runtime_event_router import (
    AssistantRuntimeEventRouter,
    RuntimeEventRoutingError,
)
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.playout_delivery import PlayoutDeliveryRegistry
from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
from companion.realtime.task_result_delivery import TaskResultAcceptanceTrackingResult
from companion.realtime.runtime_identity import AssistantRequestIdentityRegistry
from companion.realtime.runtime_identity import AssistantRequestIdentityConflict
from companion.realtime.runtime_public_event_publisher import (
    RuntimePublicEventPublishers,
)
from companion.realtime.visible_output_journal import (
    VisibleOutputJournal,
    VisibleOutputSettlement,
)
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.timed_reminders.service import TimedReminderService
from companion.realtime.assistant_events import (
    RuntimeMemoryCompactionPlanCommand,
    TaskMemoryLookupResultCommand,
    TimedReminderCommandResultCommand,
)

logger = get_backend_logger(__name__)

OUTPUT_SETTLEMENT_RETRY_BASE_DELAY_SECONDS = 0.05
OUTPUT_SETTLEMENT_RETRY_MAX_DELAY_SECONDS = 5.0
ACTIVITY_TERMINAL_RETRY_BASE_DELAY_SECONDS = 0.05
ACTIVITY_TERMINAL_RETRY_MAX_DELAY_SECONDS = 5.0


class WakeResponseAudioOutputLike(Protocol):
    """唤醒回复音频输出边界。"""

    def enqueue_audio_event(self, event: RuntimeEvent) -> None:
        """提交一条唤醒回复音频事件。

        Args:
            event: 需要进入下行音频调度器的运行时事件。
        """


class RuntimeEventQueueDrainer:
    """负责消费运行时事件队列，并把单个事件交给 typed router。

    普通产品事件会交给 AssistantRuntimeEventRouter 处理，内部运行时错误会被记录到本地错误列表，
    供外层监督任务或测试代码读取。它不拥有事件生产者生命周期，由应用级
    ``RuntimeEventService`` 持有并作为 Backend 进程唯一的队列消费边界。
    """

    def __init__(
        self,
        *,
        event_queue: Queue[RuntimeEvent],
        public_event_publishers: RuntimePublicEventPublishers,
        realtime_input_runtime: RealtimeInputRuntime | None = None,
        realtime_input_runtime_provider: Callable[[], RealtimeInputRuntime] | None = None,
        current_input_runtime_identity: Callable[[], tuple[str, int] | None]
        | None = None,
        wake_response_audio_output: WakeResponseAudioOutputLike | None = None,
        foreground_notice_handler: Callable[[], None] | None = None,
        activity_apply_handler: Callable[
            [str, dict[str, object], int], ActivityApplyResult
        ]
        | None = None,
        activity_broadcast_handler: Callable[[ActivityProjection], Awaitable[object]]
        | None = None,
        current_runtime_epoch: Callable[[], int] | None = None,
        strict_activity_identity: bool = False,
        output_available: Callable[[], bool] | None = None,
        activity_interruption_handler: Callable[[int, str], Awaitable[object]] | None = None,
        activity_notice_interruption_handler: Callable[
            [int, ActivityNoticeInterruptionStrategy], object
        ]
        | None = None,
        activity_notice_handler: Callable[[str, dict[str, object], int], None] | None = None,
        activity_cancellation_handler: Callable[[str], Awaitable[None]] | None = None,
        activity_cancellation_result_handler: Callable[
            [ActivityCancellationResultCommand, int], object
        ]
        | None = None,
        timed_reminder_service: TimedReminderService | None = None,
        timed_reminder_command_result_handler: Callable[
            [TimedReminderCommandResultCommand, int], object
        ]
        | None = None,
        background_agent_interaction_event_handler: Callable[
            [str, dict[str, object], int], AgentInteractionEventApplyResult
        ]
        | None = None,
        background_agent_interaction_interruption_handler: Callable[
            [int], list[dict[str, object]]
        ]
        | None = None,
        background_agent_usage_handler: Callable[[str, dict[str, object]], None]
        | None = None,
        recent_material_observation_handler: Callable[
            [RecentMaterialObservation], Awaitable[object]
        ]
        | None = None,
        notice_delivery_handler: Callable[
            [NoticeDeliveryCommand], NoticeDeliveryResult | None
        ]
        | None = None,
        turn_material_retain_handler: Callable[[str], bool] | None = None,
        turn_material_final_handler: Callable[[str], list[dict[str, object]]] | None = None,
        turn_material_release_handler: Callable[[str], list[dict[str, object]]] | None = None,
        playout_delivery_registry: PlayoutDeliveryRegistry | None = None,
        task_result_delivery_coordinator: TaskResultDeliveryCoordinator | None = None,
        runtime_memory_store: RuntimeMemoryStore | None = None,
        task_memory_lookup_result_handler: Callable[
            [TaskMemoryLookupResultCommand], object
        ]
        | None = None,
        runtime_memory_compaction_plan_handler: Callable[
            [RuntimeMemoryCompactionPlanCommand], object
        ]
        | None = None,
        activity_progress_ledger: ActivityProgressLedger | None = None,
        progress_acknowledgement_handler: Callable[
            [AcknowledgeActivityProgressCommand], object
        ]
        | None = None,
        output_settled_handler: Callable[[VisibleOutputSettlement], object]
        | None = None,
        request_identity_registry: AssistantRequestIdentityRegistry | None = None,
        visible_output_journal: VisibleOutputJournal | None = None,
        identity_conflict_handler: Callable[[AssistantRequestIdentityConflict], None]
        | None = None,
        task_result_acceptance_handler: Callable[
            [RuntimeEvent, TaskResultAcceptanceTrackingResult], Awaitable[object]
        ]
        | None = None,
        self_reflection_event_handler: Callable[[RuntimeEvent], object] | None = None,
        self_reflection_service: object | None = None,
    ) -> None:
        """初始化运行时事件队列消费器。

        Args:
            event_queue: 提供待消费运行时事件的统一队列。
            public_event_publishers: 按事件职责绑定的公共发布端口组。
            realtime_input_runtime: 兼容测试注入的固定输入 Runtime。
            realtime_input_runtime_provider: 读取当前输入 Runtime 的进程级 provider。
            current_input_runtime_identity: 读取进程级 Owner 当前输入 Runtime 身份的端口。
            wake_response_audio_output: 当前 ``LiveKitMediaSession`` 持有的
                ``AssistantAudioOutputScheduler``。唤醒回复音频事件先交给它，再发布到
                LiveKit 助手音轨；测试可不传。
            foreground_notice_handler: 当前 ``LiveKitMediaSession`` 的
                ``_enqueue_system_notice_request`` 方法。每次排空非空事件批次后，它从
                ``NoticeDeliveryCoordinator`` 预留的 Notice 中组装一条 System Notice 请求。
            activity_apply_handler: ``ApplicationContainer`` 注入的
                ``ActivityService.apply_source_event``。它把助手子进程发回的
                Activity 来源事件写入 Activity 权威状态机。
            activity_broadcast_handler: ``ApplicationContainer`` 注入的
                ``ActivityService.broadcast_projection``。当 Activity 状态机确有变化时，
                它向控制面广播新的 Activity 投影。
            current_runtime_epoch: ``ApplicationContainer`` 注入的读取函数，实际返回
                ``AssistantProcessSupervisor.client.current_epoch``。Router 用它忽略热替换前
                助手子进程迟到的普通事件。
            output_available: 当前 ``LiveKitMediaSession`` 的
                ``_assistant_output_available`` 方法。它读取本会话
                ``AssistantProcessBridge.is_output_available`` 的健康状态，决定是否还能创建
                System Notice 请求。
            activity_interruption_handler: ``ApplicationContainer`` 注入的
                ``ActivityService.interrupt_active``。助手运行时失效时，它将该世代的
                Activity 收口为对应的中断终态。
            activity_notice_interruption_handler: 当前 ``LiveKitMediaSession`` 的
                ``interrupt_activity_notices`` 方法。它再调用 ``ApplicationContainer`` 注入的
                ``NoticeDeliveryCoordinator.interrupt_active``，并取消已经在 Bridge 中的过期
                System Notice 请求。
            activity_notice_handler: 当前 ``LiveKitMediaSession`` 的
                ``_handle_activity_event`` 方法。它再调用 ``ApplicationContainer`` 注入的
                ``AssistantRequestContextPreparer.handle_activity_event``，把 Activity 事实写入
                ``NoticeDeliveryCoordinator``，并收束被替代的 System Notice 请求。
            activity_cancellation_handler: 主进程执行权威 Activity 取消的异步端口。
            activity_cancellation_result_handler: 按发起运行时世代送回取消回执的同步端口。
            timed_reminder_service: 主进程唯一的定时提醒权威服务。
            timed_reminder_command_result_handler: 按发起世代送回提醒写结果的同步端口。
            background_agent_interaction_event_handler: 后台智能体待答请求的登记与收口端口。
            background_agent_interaction_interruption_handler: 运行时失效时作废待答请求的端口。
            background_agent_usage_handler: 保存并从公开链路剥离后台工作用量的端口。
            recent_material_observation_handler: 消费并广播近期材料状态变化的主进程端口。
            notice_delivery_handler: ``ApplicationContainer`` 注入的
                ``NoticeDeliveryCoordinator.apply_delivery``。它是 Notice 批次从预留、入队到
                正常交付、重排或丢弃的唯一状态写入口。
            turn_material_retain_handler: ``ApplicationContainer`` 注入的
                ``MaterialLifecycle.retain_turn``。后台工作首次确认接收时，它为对应的本轮材料
                上下文取得租约。
            turn_material_final_handler: ``ApplicationContainer`` 注入的
                ``MaterialLifecycle.finish_turn``。普通前台轮次结束时，它清理未被后台活动保留的
                本轮材料上下文。
            turn_material_release_handler: ``ApplicationContainer`` 注入的
                ``MaterialLifecycle.release_turn``。后台工作完成、失败或中断时，它释放此前取得的
                同一份本轮材料上下文租约。
            playout_delivery_registry: 当前 Backend 进程内的语音播放交付账本。
            task_result_delivery_coordinator: 当前 Backend 进程内的结果卡交付账本。
            runtime_memory_store: Backend 进程级 Foreground 记忆所有者。
            task_memory_lookup_result_handler: 把任务详情回执送回助手子进程的端口。
            runtime_memory_compaction_plan_handler: 把压缩计划送给助手子进程的端口。
            activity_progress_ledger: Backend 进程级 Activity Progress 临时账本。
            progress_acknowledgement_handler: 把已登记 Progress 回执送回子进程的端口。
            output_settled_handler: 可见输出归档完成后释放 Turn 准入屏障的端口。
            request_identity_registry: Backend 进程级请求身份永久登记表。
            visible_output_journal: Backend 进程级可见输出与永久墓碑账本。
            identity_conflict_handler: 身份冲突时永久关闭当前 Bridge 的端口。
            task_result_acceptance_handler: Background acceptance 的材料 retain 事实出口。
        """

        # event_queue 是跨 worker/runner 到 websocket 控制面的唯一边界，本类只负责消费不拥有生产生命周期。
        if realtime_input_runtime is None and realtime_input_runtime_provider is None:
            raise ValueError("必须提供 realtime_input_runtime 或 realtime_input_runtime_provider")
        runtime_provider = (
            realtime_input_runtime_provider
            if realtime_input_runtime_provider is not None
            else lambda: realtime_input_runtime  # type: ignore[return-value]
        )
        self.event_queue = event_queue
        self.public_event_publishers = public_event_publishers
        # Router 只处理单事件；批次级 Notice 尝试仍由本消费循环在排水完成后负责。
        self.event_router = AssistantRuntimeEventRouter(
            public_event_publishers=public_event_publishers,
            realtime_input_runtime_provider=runtime_provider,
            current_input_runtime_identity=current_input_runtime_identity,
            activity_apply_handler=activity_apply_handler,
            activity_broadcast_handler=activity_broadcast_handler,
            current_runtime_epoch=current_runtime_epoch,
            strict_activity_identity=strict_activity_identity,
            activity_interruption_handler=activity_interruption_handler,
            activity_notice_interruption_handler=activity_notice_interruption_handler,
            activity_notice_handler=activity_notice_handler,
            activity_cancellation_handler=activity_cancellation_handler,
            activity_cancellation_result_handler=(
                activity_cancellation_result_handler
            ),
            timed_reminder_service=timed_reminder_service,
            timed_reminder_command_result_handler=(
                timed_reminder_command_result_handler
            ),
            background_agent_interaction_event_handler=(
                background_agent_interaction_event_handler
            ),
            background_agent_interaction_interruption_handler=(
                background_agent_interaction_interruption_handler
            ),
            background_agent_usage_handler=background_agent_usage_handler,
            recent_material_observation_handler=(
                recent_material_observation_handler
            ),
            notice_delivery_handler=notice_delivery_handler,
            turn_material_retain_handler=turn_material_retain_handler,
            turn_material_final_handler=turn_material_final_handler,
            turn_material_release_handler=turn_material_release_handler,
            playout_delivery_registry=playout_delivery_registry,
            task_result_delivery_coordinator=task_result_delivery_coordinator,
            runtime_memory_store=runtime_memory_store,
            task_memory_lookup_result_handler=task_memory_lookup_result_handler,
            runtime_memory_compaction_plan_handler=(
                runtime_memory_compaction_plan_handler
            ),
            activity_progress_ledger=activity_progress_ledger,
            progress_acknowledgement_handler=progress_acknowledgement_handler,
            output_settled_handler=output_settled_handler,
            request_identity_registry=request_identity_registry,
            visible_output_journal=visible_output_journal,
            task_result_acceptance_handler=task_result_acceptance_handler,
            self_reflection_event_handler=self_reflection_event_handler,
            self_reflection_service=self_reflection_service,
        )
        # background_task_errors 保存内部 runtime 错误消息，供外层任务或测试查看，不污染前端产品事件协议。
        self.background_task_errors: list[str] = []
        # 唤醒回复音频来自输入侧队列，需要在控制面分发前送入 LiveKit 音频调度器。
        self.wake_response_audio_output = wake_response_audio_output
        self.foreground_notice_handler = foreground_notice_handler
        # 健康真相仍由 Bridge 持有；Drainer 只在事件批次结束时读取，不复制状态。
        self.output_available = output_available
        self.identity_conflict_handler = identity_conflict_handler
        self.current_input_runtime_identity = (
            current_input_runtime_identity
            if current_input_runtime_identity is not None
            else lambda: (
                runtime_provider().input_runtime_id,
                runtime_provider().input_runtime_epoch,
            )
        )

    async def drain_once(self) -> None:
        """非阻塞处理调用开始时的积压事件，新入队事件留待下一轮。"""

        # 初始预算固定本轮最多处理的事件数，避免生产者在 await broadcast 期间持续入队导致本轮长时间不返回。
        initial_backlog = self.event_queue.qsize()
        processed_event_count = 0

        # 按调用开始时的积压数量消费事件；若并发消费者先取空队列，get_nowait 会触发正常返回。
        for _ in range(initial_backlog):
            try:
                event = self.event_queue.get_nowait()
            except Empty:
                # 队列为空是正常边界，代表本轮没有更多运行时事件需要分发。
                break
            # 事件已经出队；单条路由或广播异常只能丢弃当前事件，不能让共享消费任务退出。
            try:
                await self._handle_event(event)
            except Exception as exception:
                diagnostic = f"{type(exception).__name__}: event_type={event.type}"
                if isinstance(exception, AssistantRequestIdentityConflict):
                    self.background_task_errors.append(diagnostic)
                    if self.identity_conflict_handler is not None:
                        self.identity_conflict_handler(exception)
                    logger.error(
                        "助手请求身份冲突，已禁止重试并关闭输出 event_type=%s source=%s "
                        "request_id=%s field=%s expected=%r actual=%r",
                        event.type,
                        event.source,
                        exception.request_id,
                        exception.field,
                        exception.expected,
                        exception.actual,
                    )
                elif isinstance(exception, ActivityTerminalSettlementRetryableError):
                    self._schedule_activity_terminal_retry(event)
                    retry_count = _activity_terminal_retry_count(event) + 1
                    if retry_count == 1:
                        self.background_task_errors.append(diagnostic)
                    logger.error(
                        "Activity 终态结算失败，已安排退避重试 event_type=%s "
                        "source=%s retry_count=%s",
                        event.type,
                        event.source,
                        retry_count,
                    )
                elif event.type == "assistant.output_settled":
                    self._schedule_output_settlement_retry(event)
                    retry_count = _output_settlement_retry_count(event) + 1
                    if retry_count == 1:
                        self.background_task_errors.append(diagnostic)
                    logger.error(
                        "可见输出结算处理失败，已安排退避重试 source=%s error_type=%s retry_count=%s",
                        event.source,
                        type(exception).__name__,
                        retry_count,
                    )
                elif isinstance(exception, RuntimeEventRoutingError):
                    diagnostic = (
                        f"reason_code={exception.reason_code} "
                        f"event_type={exception.event_type} source={exception.source}"
                    )
                    self.background_task_errors.append(diagnostic)
                    logger.error(
                        "运行时事件路由失败 reason_code=%s event_type=%s source=%s",
                        exception.reason_code,
                        exception.event_type,
                        exception.source,
                    )
                else:
                    self.background_task_errors.append(diagnostic)
                    logger.error(
                        "运行时事件处理失败，已隔离当前事件 event_type=%s source=%s error_type=%s",
                        event.type,
                        event.source,
                        type(exception).__name__,
                    )
            processed_event_count += 1

        # 只有真实事件批次才触发一次统一预留；空轮询不能构造 system notice 请求。
        if processed_event_count > 0 and (
            self.output_available is None or self.output_available()
        ):
            try:
                await self._dispatch_foreground_notice()
            except Exception as exception:
                self.background_task_errors.append(
                    f"{type(exception).__name__}: foreground_notice_dispatch"
                )
                logger.error(
                    "前台提醒派发失败，保留 Drainer 消费任务 error_type=%s",
                    type(exception).__name__,
                )

    async def wait_for_events(self, *, timeout: float | None = None) -> bool:
        """等待运行时事件队列出现数据。

        Args:
            timeout: 最长等待秒数，None 表示一直等待。

        Returns:
            等待期间出现事件时返回真，否则返回假。
        """

        if self.event_queue.qsize() > 0:
            return True

        wait_for_data = getattr(self.event_queue, "wait_for_data", None)
        if callable(wait_for_data):
            return await asyncio.to_thread(wait_for_data, timeout)

        return False

    async def _handle_event(self, event: RuntimeEvent) -> None:
        """处理单个运行时事件。

        Args:
            event: 从运行时事件队列取出的事件对象。
        """

        # 子进程内部诊断事件只留在 Backend 日志/测试诊断列表，绝不能把 traceback 或异常正文广播到 UI。
        if event.type in {"runtime.internal_error", "runtime.internal_crash"}:
            if event.type == "runtime.internal_crash":
                exception_type = event.payload.get("exception_type")
                stage = event.payload.get("stage")
                diagnostic = (
                    f"{exception_type}: stage={stage}"
                    if isinstance(exception_type, str) and isinstance(stage, str)
                    else "runtime internal crash"
                )
            else:
                diagnostic = str(event.payload.get("message", "runtime internal error"))
            self.background_task_errors.append(diagnostic)
            return

        if event.type in {"wake_response.audio_chunk", "wake_response.audio_finished"}:
            event_identity = (
                event.payload.get("input_runtime_id"),
                event.payload.get("input_runtime_epoch"),
            )
            if event_identity != self.current_input_runtime_identity():
                logger.info("旧输入 Runtime 唤醒音频已吸收 event_type=%s", event.type)
                return
            if self.wake_response_audio_output is not None:
                self.wake_response_audio_output.enqueue_audio_event(event)
                return

        # 普通事件委托控制面分发器，队列消费侧只保证取出顺序和错误归档。
        await self.event_router.dispatch(event)

    def _schedule_output_settlement_retry(self, event: RuntimeEvent) -> None:
        """把归档失败的必达 settlement 按指数退避重新放回队列。

        Args:
            event: 本次未能完成归档的可见输出结算事件。
        """

        retry_count = _output_settlement_retry_count(event) + 1
        delay_seconds = min(
            OUTPUT_SETTLEMENT_RETRY_BASE_DELAY_SECONDS * (2 ** min(retry_count - 1, 16)),
            OUTPUT_SETTLEMENT_RETRY_MAX_DELAY_SECONDS,
        )
        retry_payload = dict(event.payload)
        retry_payload["_archive_retry_count"] = retry_count
        retry_event = RuntimeEvent(
            type=event.type,
            payload=retry_payload,
            produced_at=event.produced_at,
            source=event.source,
            runtime_epoch=event.runtime_epoch,
        )
        asyncio.get_running_loop().call_later(
            delay_seconds,
            self.event_queue.put,
            retry_event,
        )

    def _schedule_activity_terminal_retry(self, event: RuntimeEvent) -> None:
        """把尚未写完任务记忆的 Activity 终态重新放回必达通道。

        Args:
            event: 本次未能完成 Progress 与任务记忆结算的来源终态。
        """

        retry_count = _activity_terminal_retry_count(event) + 1
        delay_seconds = min(
            ACTIVITY_TERMINAL_RETRY_BASE_DELAY_SECONDS
            * (2 ** min(retry_count - 1, 16)),
            ACTIVITY_TERMINAL_RETRY_MAX_DELAY_SECONDS,
        )
        retry_payload = dict(event.payload)
        retry_payload["_terminal_settlement_retry_count"] = retry_count
        retry_event = RuntimeEvent(
            type=event.type,
            payload=retry_payload,
            produced_at=event.produced_at,
            source=event.source,
            runtime_epoch=event.runtime_epoch,
        )
        asyncio.get_running_loop().call_later(
            delay_seconds,
            self.event_queue.put,
            retry_event,
        )

    async def _dispatch_foreground_notice(self) -> None:
        """在当前非空事件批次排空后尝试提交一批待表达 Notice。"""

        if self.foreground_notice_handler is None:
            return
        self.foreground_notice_handler()
        logger.debug("前台提醒入队尝试结束")


def _output_settlement_retry_count(event: RuntimeEvent) -> int:
    """读取内部 settlement 归档重试次数。

    Args:
        event: 可能携带内部重试字段的运行时事件。

    Returns:
        非负重试次数；缺失、布尔值或非法值按零处理。
    """

    value = event.payload.get("_archive_retry_count")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value

def _activity_terminal_retry_count(event: RuntimeEvent) -> int:
    """读取内部 Activity 终态结算重试次数。"""

    value = event.payload.get("_terminal_settlement_retry_count")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value
