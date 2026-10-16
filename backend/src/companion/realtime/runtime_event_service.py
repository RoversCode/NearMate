"""持有 Backend 进程唯一的 RuntimeEvent 消费循环。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any

from companion.activities.notice.models import (
    ActivityNoticeInterruptionStrategy,
    NoticeDeliveryCommand,
    NoticeDeliveryResult,
)
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplyResult,
)
from companion.core.broadcast import BroadcastHub
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import (
    AcknowledgeActivityProgressCommand,
    ActivityCancellationResultCommand,
    RuntimeEvent,
    RuntimeMemoryCompactionPlanCommand,
    TaskMemoryLookupResultCommand,
    TimedReminderCommandResultCommand,
)
from companion.realtime.runtime_identity import AssistantRequestIdentityConflict
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger
from companion.realtime.assistant_runtime_event_router import RuntimeEventRoutingError
from companion.realtime.notify_queue import NotifyQueue
from companion.realtime.playout_delivery import PlayoutDeliveryRegistry
from companion.realtime.runtime_event_queue_drainer import (
    RuntimeEventQueueDrainer,
    WakeResponseAudioOutputLike,
)
from companion.realtime.runtime_identity import AssistantRequestIdentityRegistry
from companion.realtime.runtime_public_event_publisher import RuntimePublicEventPublishers
from companion.realtime.task_result_delivery import (
    TaskResultAcceptanceTrackingResult,
    TaskResultDeliveryCoordinator,
)
from companion.realtime.visible_output_journal import (
    VisibleOutputJournal,
    VisibleOutputSettlement,
)
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.timed_reminders.service import TimedReminderService

if TYPE_CHECKING:
    from companion.core.application.assistant_request_context_preparer import (
        AssistantRequestContextPreparer,
    )
    from companion.core.application.material_lifecycle import MaterialLifecycleService
    from companion.core.application.turn_admission import TurnAdmissionSequencer


logger = get_backend_logger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeEventSessionBindings:
    """当前媒体会话临时提供给进程级 Drainer 的操作端口。

    这些端口只描述媒体资源操作，不保存事件账本。会话重建时旧端口会被撤销，
    进程级 Router、Journal、材料和 Turn 状态仍由应用容器继续持有。
    """

    wake_response_audio_output: WakeResponseAudioOutputLike | None = None
    foreground_notice_handler: Callable[[], None] | None = None
    activity_notice_handler: Callable[[str, dict[str, object], int], None] | None = None
    activity_notice_interruption_handler: Callable[
        [int, ActivityNoticeInterruptionStrategy], object
    ] | None = None
    output_available: Callable[[], bool] | None = None
    task_memory_lookup_result_handler: (
        Callable[[TaskMemoryLookupResultCommand], object] | None
    ) = None
    runtime_memory_compaction_plan_handler: Callable[
        [RuntimeMemoryCompactionPlanCommand], object
    ] | None = None
    progress_acknowledgement_handler: Callable[
        [AcknowledgeActivityProgressCommand], object
    ] | None = None
    identity_conflict_handler: (
        Callable[[AssistantRequestIdentityConflict], None] | None
    ) = None
    output_settled_handler: Callable[[VisibleOutputSettlement], object] | None = None


class _DynamicWakeResponseAudioOutput:
    """把唤醒音频交给当前会话；断连期间没有媒体端口时直接吸收。"""

    def __init__(self, owner: "RuntimeEventService") -> None:
        self._owner = owner

    def enqueue_audio_event(self, event: RuntimeEvent) -> None:
        bindings = self._owner._bindings_snapshot()
        if bindings is None or bindings.wake_response_audio_output is None:
            return
        bindings.wake_response_audio_output.enqueue_audio_event(event)


class RuntimeEventService:
    """管理 Backend 进程内唯一的运行时事件队列消费者。

    Args:
        event_queue: 所有输入 Worker、Bridge 和输出 lane 共用的队列。
        hub: 控制面广播中枢，用于绑定固定领域 Publisher。
        realtime_input_runtime_owner: 动态读取当前输入 Runtime 的所有者。
        assistant_process_supervisor: 动态读取当前助手 Bridge 的所有者。
        assistant_request_context_preparer: Activity Notice 事实写入端口。
        material_lifecycle: 本轮材料生命周期服务。
        activity_apply_handler: Activity 权威状态应用端口。
        activity_broadcast_handler: Activity 公共投影端口。
        activity_interruption_handler: Activity 运行时中断端口。
        activity_cancellation_handler: 复用 ActivityService 的权威取消端口。
        activity_cancellation_result_handler: 按发起世代把取消回执送回子进程的端口。
        background_agent_interaction_event_handler: 后台工作交互状态端口。
        background_agent_interaction_interruption_handler: 后台工作交互失效端口。
        background_agent_usage_handler: 私有用量记录端口。
        notice_delivery_handler: Notice 交付账本端口。
        task_result_delivery_coordinator: 结果卡交付账本。
        playout_delivery_registry: 音频播放交付账本。
        runtime_memory_store: 运行期记忆账本。
        activity_progress_ledger: Activity Progress 账本。
        turn_admission: Turn 顺序屏障。
        request_identity_registry: 请求身份永久注册表。
        visible_output_journal: 可见正文与结算墓碑。
        assistant_output_health_ledger: 助手输出健康快照。
        task_result_acceptance_handler: TaskResult 完成材料 retain 后的所有权交接端口。
        self_reflection_service: 主进程自我反思状态和恢复资源控制面。
    """

    def __init__(
        self,
        *,
        event_queue: NotifyQueue[RuntimeEvent],
        hub: BroadcastHub,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
        assistant_process_supervisor: AssistantProcessSupervisor,
        assistant_request_context_preparer: "AssistantRequestContextPreparer",
        material_lifecycle: "MaterialLifecycleService",
        activity_apply_handler: Callable[[str, dict[str, object], int], ActivityApplyResult],
        activity_broadcast_handler: Callable[[ActivityProjection], Awaitable[object]],
        activity_interruption_handler: Callable[[int, str], Awaitable[object]],
        background_agent_interaction_event_handler: Callable[
            [str, dict[str, object], int], AgentInteractionEventApplyResult
        ],
        background_agent_interaction_interruption_handler: Callable[
            [int], list[dict[str, object]]
        ],
        background_agent_usage_handler: Callable[[str, dict[str, object]], None],
        notice_delivery_handler: Callable[
            [NoticeDeliveryCommand], NoticeDeliveryResult | None
        ],
        task_result_delivery_coordinator: TaskResultDeliveryCoordinator,
        playout_delivery_registry: PlayoutDeliveryRegistry,
        runtime_memory_store: RuntimeMemoryStore,
        activity_progress_ledger: ActivityProgressLedger,
        turn_admission: "TurnAdmissionSequencer",
        request_identity_registry: AssistantRequestIdentityRegistry,
        visible_output_journal: VisibleOutputJournal,
        assistant_output_health_ledger: AssistantOutputHealthLedger,
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
        task_result_acceptance_handler: Callable[
            [RuntimeEvent, TaskResultAcceptanceTrackingResult], Awaitable[object]
        ]
        | None = None,
        self_reflection_event_handler: Callable[[RuntimeEvent], object] | None = None,
        self_reflection_service: object | None = None,
    ) -> None:
        self.event_queue = event_queue
        self._realtime_input_runtime_owner = realtime_input_runtime_owner
        self._assistant_process_supervisor = assistant_process_supervisor
        self._assistant_request_context_preparer = assistant_request_context_preparer
        self._notice_delivery_handler = notice_delivery_handler
        self._assistant_output_health_ledger = assistant_output_health_ledger
        self._self_reflection_service = self_reflection_service
        self._turn_admission = turn_admission
        self._bindings_lock = RLock()
        self._bindings: RuntimeEventSessionBindings | None = None
        self._task: asyncio.Task[None] | None = None
        self._drain_lock = asyncio.Lock()
        self._wake_output = _DynamicWakeResponseAudioOutput(self)

        publishers = RuntimePublicEventPublishers(
            publish_interaction=hub.publisher("interaction").publish,
            publish_assistant_output=hub.publisher("assistant_output").publish,
            publish_activity=hub.publisher("activity").publish,
            publish_activity_ephemeral=hub.publisher("activity").publish_ephemeral,
            publish_materials_ephemeral=hub.publisher("materials").publish_ephemeral,
        )
        self.drainer = RuntimeEventQueueDrainer(
            event_queue=event_queue,
            public_event_publishers=publishers,
            realtime_input_runtime_provider=realtime_input_runtime_owner.require_for_livekit,
            current_input_runtime_identity=realtime_input_runtime_owner.current_runtime_identity,
            wake_response_audio_output=self._wake_output,
            foreground_notice_handler=self._dispatch_foreground_notice,
            activity_notice_handler=self._handle_activity_notice,
            background_agent_interaction_event_handler=background_agent_interaction_event_handler,
            background_agent_interaction_interruption_handler=(
                background_agent_interaction_interruption_handler
            ),
            background_agent_usage_handler=background_agent_usage_handler,
            recent_material_observation_handler=(
                material_lifecycle.observe_recent_materials
            ),
            activity_apply_handler=activity_apply_handler,
            activity_broadcast_handler=activity_broadcast_handler,
            current_runtime_epoch=lambda: assistant_process_supervisor.client.current_epoch,
            strict_activity_identity=True,
            output_available=self._output_available,
            activity_interruption_handler=activity_interruption_handler,
            activity_cancellation_handler=activity_cancellation_handler,
            activity_cancellation_result_handler=(
                activity_cancellation_result_handler
            ),
            timed_reminder_service=timed_reminder_service,
            timed_reminder_command_result_handler=(
                timed_reminder_command_result_handler
            ),
            activity_notice_interruption_handler=self._interrupt_activity_notices,
            notice_delivery_handler=notice_delivery_handler,
            turn_material_retain_handler=material_lifecycle.retain_turn,
            turn_material_final_handler=material_lifecycle.finish_turn,
            turn_material_release_handler=material_lifecycle.release_turn,
            playout_delivery_registry=playout_delivery_registry,
            task_result_delivery_coordinator=task_result_delivery_coordinator,
            runtime_memory_store=runtime_memory_store,
            task_memory_lookup_result_handler=self._submit_control,
            runtime_memory_compaction_plan_handler=self._submit_control,
            activity_progress_ledger=activity_progress_ledger,
            progress_acknowledgement_handler=self._submit_control,
            output_settled_handler=self._handle_output_settled,
            request_identity_registry=request_identity_registry,
            visible_output_journal=visible_output_journal,
            identity_conflict_handler=self._handle_identity_conflict,
            task_result_acceptance_handler=task_result_acceptance_handler,
            self_reflection_event_handler=self_reflection_event_handler,
            self_reflection_service=self_reflection_service,
        )

    def _handle_output_settled(
        self,
        settlement: VisibleOutputSettlement,
    ) -> None:
        """先让 Self-Reflection 按 reservation 裁决，再释放 Turn 顺序屏障。

        Args:
            settlement: 已完成正文广播与历史策略归档的可见输出结算。
        """

        if self._self_reflection_service is not None:
            observe_expression_settled = getattr(
                self._self_reflection_service,
                "observe_expression_settled",
                None,
            )
            if callable(observe_expression_settled):
                # request_source 也是待校验字段，不能拿它决定是否进入身份校验。
                # 普通输出没有 reservation，Service 会明确返回 ignored。
                disposition = observe_expression_settled(settlement)
                if disposition == "retryable":
                    # Journal 只有在业务账本真正接纳结算后才能写永久墓碑。这里抛错会
                    # 让同一 output settlement 回到既有退避重试 lane，而不会释放 Turn。
                    raise RuntimeError("self_reflection_settlement_retry_required")
                if disposition not in {"committed", "ignored"}:
                    raise ValueError("未知 Self-Reflection settlement 裁决")
        self._turn_admission.handle_output_settled(settlement)
        bindings = self._bindings_snapshot()
        if bindings is not None and bindings.output_settled_handler is not None:
            bindings.output_settled_handler(settlement)

    def bind_session(self, bindings: RuntimeEventSessionBindings) -> None:
        """发布当前媒体会话的临时操作端口。"""

        with self._bindings_lock:
            self._bindings = bindings

    def unbind_session(self, bindings: RuntimeEventSessionBindings) -> None:
        """只撤销仍属于当前会话的端口，避免旧会话覆盖新会话。"""

        with self._bindings_lock:
            if self._bindings is bindings:
                self._bindings = None

    async def start(self) -> None:
        """启动唯一消费循环；重复启动不会创建第二个消费者。"""

        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="runtime-event-service")

    async def stop(self) -> None:
        """先排空现有事件，再停止消费任务。"""

        await self.drain_once()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def drain_once(self) -> None:
        """串行排空一批队列事件，防止生命周期回调与后台循环并发消费。"""

        async with self._drain_lock:
            await self.drainer.drain_once()

    def interrupt_task_result_deliveries(self, runtime_epoch: int) -> list[dict[str, object]]:
        """中断指定世代的 TaskResult 交付并释放材料。"""

        return self.drainer.event_router.activity_event_handler.interrupt_task_result_deliveries(
            runtime_epoch
        )

    def cancel_request(self, request_id: str) -> None:
        """同步阻止指定请求的迟到正文进入公共事件流。

        Args:
            request_id: 已由媒体或领域协调器确认需要停止的请求编号。

        Returns:
            None: 取消事实直接写入唯一 Runtime Event Router。
        """

        self.drainer.event_router.cancel_request(request_id)

    async def _run(self) -> None:
        """持续等待队列通知并排水；单事件错误由 Drainer 隔离。"""

        while True:
            try:
                has_events = await self.drainer.wait_for_events(timeout=0.1)
                # 先排空已经到达的正文、工具事实和 settlement，再运行 deadline reaper；
                # 否则 reaper 可能抢在 canonical/首段正文事件前错误触发 fallback。
                if has_events:
                    await self.drain_once()
                self_reflection_service = getattr(self, "_self_reflection_service", None)
                if self_reflection_service is not None:
                    reap_expired = getattr(
                        self_reflection_service,
                        "reap_expired",
                        None,
                    )
                    if callable(reap_expired):
                        reap_expired()
            except Exception:
                # 这是进程内唯一的事件消费循环，单轮失败绝不能杀死它，否则后续
                # settlement 和 Notice 只会在队列里堆积且无人发现；让一拍再继续，
                # 避免同样的瞬时报错把循环打成空转。
                logger.exception("运行时事件循环单轮处理失败，继续消费")
                await asyncio.sleep(0.1)

    def _bindings_snapshot(self) -> RuntimeEventSessionBindings | None:
        with self._bindings_lock:
            return self._bindings

    def _output_available(self) -> bool:
        bindings = self._bindings_snapshot()
        if bindings is not None and bindings.output_available is not None:
            return bindings.output_available()
        return self._assistant_output_health_ledger.snapshot().available

    def _dispatch_foreground_notice(self) -> None:
        bindings = self._bindings_snapshot()
        if bindings is None or bindings.foreground_notice_handler is None:
            return
        bindings.foreground_notice_handler()

    def _handle_activity_notice(
        self,
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> None:
        bindings = self._bindings_snapshot()
        if bindings is not None and bindings.activity_notice_handler is not None:
            bindings.activity_notice_handler(event_type, payload, runtime_epoch)
            return
        self._assistant_request_context_preparer.handle_activity_event(
            event_type,
            payload,
            runtime_epoch,
        )

    def _interrupt_activity_notices(
        self,
        runtime_epoch: int,
        strategy: ActivityNoticeInterruptionStrategy,
    ) -> object:
        bindings = self._bindings_snapshot()
        if bindings is not None and bindings.activity_notice_interruption_handler is not None:
            return bindings.activity_notice_interruption_handler(runtime_epoch, strategy)
        return self._assistant_request_context_preparer.interrupt_activity_notices(
            runtime_epoch,
            strategy,
        )

    def _submit_control(self, command: Any) -> object:
        bindings = self._bindings_snapshot()
        if bindings is not None:
            handler = (
                bindings.task_memory_lookup_result_handler
                if isinstance(command, TaskMemoryLookupResultCommand)
                else bindings.runtime_memory_compaction_plan_handler
                if isinstance(command, RuntimeMemoryCompactionPlanCommand)
                else bindings.progress_acknowledgement_handler
            )
            if handler is not None:
                return handler(command)
        try:
            return self._assistant_process_supervisor.require_for_output().submit_control(command)
        except Exception:
            logger.warning(
                "当前没有可用助手 Bridge，内部控制回执已吸收 command_type=%s",
                type(command).__name__,
            )
            return False

    def _handle_identity_conflict(self, exception: AssistantRequestIdentityConflict) -> None:
        bindings = self._bindings_snapshot()
        if bindings is None or bindings.identity_conflict_handler is None:
            return
        bindings.identity_conflict_handler(exception)
