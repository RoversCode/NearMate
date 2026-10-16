"""在主进程按固定优先级分发助手子进程回流事件。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import AbstractSet, Any, Literal

from companion.activities.notice.models import (
    ActivityNoticeInterruptionStrategy,
    NoticeDeliveryCommand,
    NoticeDeliveryResult,
)
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.activities.runtime.adapters import validate_activity_terminal_contract
from companion.activities.terminal_settlement import ActivityTerminalSettlementService
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplyResult,
)
from companion.observability.logging_config import get_backend_logger
from companion.realtime.activity_runtime_event_handler import (
    ACTIVITY_RUNTIME_EVENT_TYPES,
    ActivityRuntimeEventHandler,
)
from companion.realtime.assistant_events import (
    AcknowledgeActivityProgressCommand,
    ActivityCancellationResultCommand,
    RECENT_MATERIAL_OBSERVATION_FIELD,
    RecentMaterialObservation,
    RuntimeEvent,
    RuntimeMemoryCompactionPlanCommand,
    TaskMemoryLookupResultCommand,
    TimedReminderCommandResultCommand,
)
from companion.realtime.assistant_output_settlement_service import (
    AssistantOutputSettlementService,
)
from companion.realtime.runtime_identity import (
    AssistantRequestIdentityConflict,
    AssistantRequestIdentityRegistry,
)
from companion.realtime.visible_output_journal import (
    VisibleOutputJournal,
    VisibleOutputSettlement,
)
from companion.realtime.assistant_output_event_handler import AssistantOutputEventHandler
from companion.realtime.realtime_input_public_event_handler import (
    REALTIME_INPUT_PUBLIC_EVENT_TYPES,
    RealtimeInputPublicEventHandler,
)
from companion.realtime.assistant_request_failure_handler import AssistantRequestFailureHandler
from companion.realtime.task_result_delivery import (
    TaskResultAcceptanceTrackingResult,
    TaskResultDeliveryCoordinator,
)
from companion.realtime.playout_delivery import PlayoutDeliveryRegistry
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.runtime_public_event_publisher import (
    RuntimePublicEventPublishers,
)
from companion.realtime.self_reflection_public_event_handler import (
    SELF_REFLECTION_PUBLIC_EVENT_TYPES,
    SelfReflectionPublicEventHandler,
)
from companion.realtime.turn_material_release_service import TurnMaterialReleaseService
from companion.realtime.timed_reminder_runtime_event_handler import (
    TIMED_REMINDER_RUNTIME_EVENT_TYPES,
    TimedReminderRuntimeEventHandler,
)
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.timed_reminders.service import TimedReminderService


logger = get_backend_logger(__name__)
_USAGE_IDENTITY_LIMIT = 2048

_ASSISTANT_OUTPUT_RUNTIME_EVENT_TYPES = frozenset(
    {
        "assistant.generation_interrupted",
        "assistant.generation_superseded",
        "assistant.audio_chunk",
        "wake_response.audio_chunk",
        "wake_response.audio_finished",
        "assistant.audio_publishing_started",
        "assistant.audio_publishing_cancelled",
        "assistant.tts_segment_failed",
        "assistant.text_chunk",
        "assistant.text_segment_final",
        "assistant.text_final",
        "assistant.audio_finished",
        "assistant.self_reflection_status_changed",
        "assistant.output_status_changed",
    }
)
_INTERNAL_LIFECYCLE_RUNTIME_EVENT_TYPES = frozenset(
    {
        "assistant.state_changed",
    }
)
_INTERNAL_RUNTIME_EVENT_TYPES = frozenset(
    {
        "assistant.generation_started",
        "llm.foreground.authentication_failed",
        "llm.foreground.tool_call_rejected",
        "runtime.internal_error",
        "runtime.internal_crash",
    }
)
_RUNTIME_MEMORY_COMPACTION_EVENT_TYPES = frozenset(
    {
        "llm.foreground.runtime_memory_compaction_completed",
        "llm.foreground.runtime_memory_compaction_failed",
    }
)
_OUTPUT_SETTLEMENT_EVENT_TYPES = frozenset({"assistant.output_settled"})
_TASK_MEMORY_LOOKUP_EVENT_TYPES = frozenset(
    {"llm.foreground.task_memory_lookup_requested"}
)
_MAIN_PROCESS_MATERIAL_EVENT_TYPES = frozenset(
    {
        "materials.assets_released",
        "materials.contexts_changed",
    }
)
_SELF_REFLECTION_RUNTIME_EVENT_TYPES = frozenset(
    {
        "foreground.self_reflection_tool_commit_requested",
        "foreground.self_reflection_outcome",
        "foreground.self_reflection_reply_admission_closed",
    }
) | SELF_REFLECTION_PUBLIC_EVENT_TYPES

RuntimeEventOwner = Literal[
    "request_failure",
    "realtime_input",
    "runtime_memory_compaction",
    "output_settlement",
    "task_memory_lookup",
    "timed_reminder_command",
    "internal_lifecycle",
    "internal_diagnostic",
    "activity",
    "assistant_output",
    "self_reflection",
    "main_process_materials",
]

_STATIC_RUNTIME_EVENT_TYPES_BY_OWNER: dict[RuntimeEventOwner, frozenset[str]] = {
    "realtime_input": REALTIME_INPUT_PUBLIC_EVENT_TYPES,
    "runtime_memory_compaction": _RUNTIME_MEMORY_COMPACTION_EVENT_TYPES,
    "output_settlement": _OUTPUT_SETTLEMENT_EVENT_TYPES,
    "task_memory_lookup": _TASK_MEMORY_LOOKUP_EVENT_TYPES,
    "timed_reminder_command": TIMED_REMINDER_RUNTIME_EVENT_TYPES,
    "internal_lifecycle": _INTERNAL_LIFECYCLE_RUNTIME_EVENT_TYPES,
    "internal_diagnostic": _INTERNAL_RUNTIME_EVENT_TYPES,
    "activity": ACTIVITY_RUNTIME_EVENT_TYPES,
    "assistant_output": _ASSISTANT_OUTPUT_RUNTIME_EVENT_TYPES,
    "self_reflection": _SELF_REFLECTION_RUNTIME_EVENT_TYPES,
    "main_process_materials": _MAIN_PROCESS_MATERIAL_EVENT_TYPES,
}


def validate_runtime_event_owner_sets(
    event_types_by_owner: Mapping[str, AbstractSet[str]],
) -> None:
    """确认静态事件集合之间没有任何重叠。

    Args:
        event_types_by_owner: 处理器名称及其明确拥有的事件名称集合。

    Raises:
        RuntimeError: 同一事件名称被两个处理器同时声明时抛出。
    """

    owners_by_event_type: dict[str, str] = {}
    for owner, event_types in event_types_by_owner.items():
        for event_type in event_types:
            previous_owner = owners_by_event_type.get(event_type)
            if previous_owner is not None:
                raise RuntimeError(
                    "运行时事件归属重叠 "
                    f"event_type={event_type} owners={previous_owner},{owner}"
                )
            owners_by_event_type[event_type] = owner


validate_runtime_event_owner_sets(_STATIC_RUNTIME_EVENT_TYPES_BY_OWNER)


def classify_runtime_event_owner(event: RuntimeEvent) -> RuntimeEventOwner:
    """为一条运行时事件选择唯一处理器。

    请求失败由事件名称和稳定 reason 共同识别；同名 generation 终态在其它 reason 下仍属于
    正常输出生命周期。其余事件必须出现在一个互斥静态集合中，未知事件直接失败。

    Args:
        event: 尚未进入任何业务处理器的运行时事件。

    Returns:
        唯一处理器的稳定名称。

    Raises:
        RuntimeEventRoutingError: 事件没有登记任何所有者时抛出。
    """

    if AssistantRequestFailureHandler.owns(event):
        return "request_failure"
    for owner, event_types in _STATIC_RUNTIME_EVENT_TYPES_BY_OWNER.items():
        if event.type in event_types:
            return owner
    raise RuntimeEventRoutingError(
        event_type=event.type,
        source=event.source,
    )


class RuntimeEventRoutingError(RuntimeError):
    """表示运行时事件没有唯一、明确的主进程处理器。

    Args:
        event_type: 无法归属的事件名称。
        source: 事件生产组件的稳定标识。
        reason_code: 不包含载荷或异常正文的诊断代码。
    """

    def __init__(
        self,
        *,
        event_type: str,
        source: str,
        reason_code: str = "unregistered_runtime_event",
    ) -> None:
        self.event_type = event_type
        self.source = source
        self.reason_code = reason_code
        super().__init__(
            f"运行时事件未归属 event_type={event_type} source={source} "
            f"reason_code={reason_code}"
        )


class AssistantRuntimeEventRouter:
    """把一条回流事件交给唯一应负责它的主进程边界。

    Router 不保存活动、材料或输出状态，也不解释具体业务规则。它只保证分发顺序：
    先收口请求失败，再隔离旧运行时事件，随后依次交给 Activity 处理器或普通输出
    处理器。这个顺序防止热替换后迟到事件污染新运行时，同时不遗漏旧请求的清理。

    生产环境中，活动事件的 Notice 链路不是 Router 直接调用 Coordinator，而是：
    ``ActivityRuntimeEventHandler → LiveKitMediaSession._handle_activity_event →
    AssistantRequestContextPreparer.handle_activity_event → NoticeDeliveryCoordinator``。
    中间的媒体会话包装器还会根据 Coordinator 返回的 supersession 结果取消过期的
    System Notice 请求。这层间接调用让 Router 保持为可独立测试的事件分发器。
    """

    def __init__(
        self,
        *,
        public_event_publishers: RuntimePublicEventPublishers,
        realtime_input_runtime: RealtimeInputRuntime | None = None,
        realtime_input_runtime_provider: Callable[[], RealtimeInputRuntime] | None = None,
        current_input_runtime_identity: Callable[[], tuple[str, int] | None]
        | None = None,
        activity_apply_handler: Callable[
            [str, dict[str, object], int], ActivityApplyResult
        ]
        | None = None,
        activity_broadcast_handler: Callable[[ActivityProjection], Awaitable[object]]
        | None = None,
        current_runtime_epoch: Callable[[], int] | None = None,
        strict_activity_identity: bool = False,
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
        task_result_delivery_coordinator: TaskResultDeliveryCoordinator | None = None,
        playout_delivery_registry: PlayoutDeliveryRegistry | None = None,
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
        output_settled_handler: Callable[[VisibleOutputSettlement], object] | None = None,
        request_identity_registry: AssistantRequestIdentityRegistry | None = None,
        visible_output_journal: VisibleOutputJournal | None = None,
        self_reflection_event_handler: Callable[[RuntimeEvent], object] | None = None,
        task_result_acceptance_handler: Callable[
            [RuntimeEvent, TaskResultAcceptanceTrackingResult], Awaitable[object]
        ]
        | None = None,
        self_reflection_service: object | None = None,
    ) -> None:
        """组装单事件路由所需的 typed handlers。

        Args:
            public_event_publishers: 已按 Interaction、Assistant Output、Activity 和 Materials
                职责绑定的公共发布端口组。
            realtime_input_runtime: 固定的产品 Turn 状态所有者。
            realtime_input_runtime_provider: 延迟读取产品 Turn 状态所有者的端口。
            activity_apply_handler: 应用 Activity 来源事件的同步端口。
            activity_broadcast_handler: 广播 Activity 权威投影的异步端口。
            current_runtime_epoch: 读取当前助手子进程世代的端口。
            activity_interruption_handler: 中断指定世代 Activity 的异步端口。
            activity_notice_interruption_handler: 中断指定世代 Notice 的同步端口。生产环境
                绑定 ``LiveKitMediaSession.interrupt_activity_notices``；它再委托
                ``NoticeDeliveryCoordinator.interrupt_active``，并收束在途 System Notice 请求。
            activity_notice_handler: 把单条 Activity 来源事件写入 Notice 账本的同步端口。
                生产环境中直接传入的是 ``LiveKitMediaSession._handle_activity_event``；它再
                委托 ``AssistantRequestContextPreparer.handle_activity_event``，后者先将
                ``cognitive_task.*`` 或 ``background_agent.*`` 映射为标准 Notice 事实，再
                调用同一应用容器持有的 ``NoticeDeliveryCoordinator.apply_event``。该包装器
                还会条件取消被新进度替代的 System Notice 请求。此端口不广播文本，也不
                创建助手请求；它只更新 Coordinator 的内存 Notice 账本。
            activity_cancellation_handler: 执行主进程权威 Activity 取消的异步端口。
            activity_cancellation_result_handler: 按发起世代返回取消接收事实的同步端口。
            timed_reminder_service: 当前 Backend 运行期唯一的提醒权威服务。
            timed_reminder_command_result_handler: 按发起世代返回提醒写结果的同步端口。
            notice_delivery_handler: 推进 Notice 交付事务的同步端口。生产环境绑定
                ``NoticeDeliveryCoordinator.apply_delivery``，用于请求入队、LLM 正常完成、
                生成取消和输出异常的生命周期闭环；它与 ``activity_notice_handler`` 分别负责
                “登记提醒”与“收口提醒”。
            background_agent_interaction_event_handler: 待答请求的登记与收口端口。
            background_agent_interaction_interruption_handler: 运行时失效时作废待答请求的端口。
            background_agent_usage_handler: 在公开 Activity 投影前保存并剥离后台工作用量的端口。
            recent_material_observation_handler: 消费近期材料成功使用或校验失败事实的
                主进程材料生命周期端口；私有路径不会继续进入 canonical 工具事件。
            turn_material_retain_handler: 取得 TaskResult 材料租约的同步端口。
            turn_material_final_handler: 结束用户轮材料上下文的同步端口。
            turn_material_release_handler: 释放 TaskResult 材料租约的同步端口。
            task_result_delivery_coordinator: 共享的结果卡交付账本。
            playout_delivery_registry: 共享的语音播放交付账本。
            runtime_memory_store: Backend 进程级 Foreground 记忆所有者。
            task_memory_lookup_result_handler: 把任务详情回执送回助手子进程的端口。
            runtime_memory_compaction_plan_handler: 把权威压缩计划送给助手子进程的端口。
            activity_progress_ledger: Backend 进程级 Activity Progress 临时账本。
            progress_acknowledgement_handler: 把已公开 Progress 回执送回子进程的端口。
            output_settled_handler: 完成归档后释放下一轮准入屏障的端口。
            request_identity_registry: Backend 进程级请求身份永久登记表。
            visible_output_journal: Backend 进程级可见输出与永久墓碑账本。
            task_result_acceptance_handler: TaskResult 已确认 acceptance 和材料 retain 后的
                材料租约交接端口。
            self_reflection_service: 主进程前台自我反思编排端口。
        """

        if realtime_input_runtime is None and realtime_input_runtime_provider is None:
            raise ValueError("realtime_input_runtime 或 realtime_input_runtime_provider 必须提供一个")
        runtime_provider = (
            realtime_input_runtime_provider
            if realtime_input_runtime_provider is not None
            else lambda: _require_realtime_input_runtime(realtime_input_runtime)
        )
        material_release_service = TurnMaterialReleaseService(
            finish_turn_materials=turn_material_final_handler,
            broadcast_ephemeral=public_event_publishers.publish_materials_ephemeral,
        )
        # Router 只转交 Notice 端口，不直接持有 NoticeDeliveryCoordinator。这样活动事件的
        # 映射、候选替换及过期 System Notice 取消仍由应用装配的主进程边界统一完成。
        self.activity_event_handler = ActivityRuntimeEventHandler(
            publish_activity=public_event_publishers.publish_activity,
            publish_activity_ephemeral=(
                public_event_publishers.publish_activity_ephemeral
            ),
            publish_materials_ephemeral=(
                public_event_publishers.publish_materials_ephemeral
            ),
            activity_apply_handler=activity_apply_handler,
            activity_broadcast_handler=activity_broadcast_handler,
            activity_interruption_handler=activity_interruption_handler,
            activity_notice_interruption_handler=activity_notice_interruption_handler,
            activity_notice_handler=activity_notice_handler,
            activity_cancellation_handler=activity_cancellation_handler,
            activity_cancellation_result_handler=(
                activity_cancellation_result_handler
            ),
            background_agent_interaction_event_handler=(
                background_agent_interaction_event_handler
            ),
            background_agent_interaction_interruption_handler=(
                background_agent_interaction_interruption_handler
            ),
            retain_turn_materials=turn_material_retain_handler,
            release_turn_materials=turn_material_release_handler,
            task_result_delivery_coordinator=task_result_delivery_coordinator,
            task_result_acceptance_handler=task_result_acceptance_handler,
            self_reflection_service=self_reflection_service,
        )
        self._timed_reminder_event_handler = TimedReminderRuntimeEventHandler(
            service=timed_reminder_service,
            result_handler=timed_reminder_command_result_handler,
        )
        self._request_failure_handler = AssistantRequestFailureHandler(
            publish_interaction=public_event_publishers.publish_interaction,
            publish_assistant_output=(
                public_event_publishers.publish_assistant_output
            ),
            realtime_input_runtime_provider=runtime_provider,
            turn_material_release_service=material_release_service,
            notice_delivery_handler=notice_delivery_handler,
        )
        self._activity_progress_ledger = (
            activity_progress_ledger or ActivityProgressLedger()
        )
        self._activity_terminal_settlement_service = (
            ActivityTerminalSettlementService(
                activity_progress_ledger=self._activity_progress_ledger,
                runtime_memory_store=runtime_memory_store,
                require_canonical_activity_id=strict_activity_identity,
            )
        )
        output_settlement_service = AssistantOutputSettlementService(
            runtime_memory_store=runtime_memory_store,
            activity_progress_ledger=self._activity_progress_ledger,
            progress_acknowledgement_handler=progress_acknowledgement_handler,
            output_settled_handler=output_settled_handler,
            visible_output_journal=visible_output_journal,
        )
        self._output_event_handler = AssistantOutputEventHandler(
            publish_interaction=public_event_publishers.publish_interaction,
            publish_assistant_output=(
                public_event_publishers.publish_assistant_output
            ),
            realtime_input_runtime_provider=runtime_provider,
            activity_event_handler=self.activity_event_handler,
            turn_material_release_service=material_release_service,
            notice_delivery_handler=notice_delivery_handler,
            playout_delivery_registry=playout_delivery_registry,
            output_settlement_service=output_settlement_service,
            output_settlement_committed_handler=(
                self._handle_output_settlement_committed
            ),
        )
        self._realtime_input_event_handler = RealtimeInputPublicEventHandler(
            publish_interaction=public_event_publishers.publish_interaction,
        )
        self._current_input_runtime_identity = (
            current_input_runtime_identity
            if current_input_runtime_identity is not None
            else lambda: _runtime_input_identity(runtime_provider())
        )
        self._current_runtime_epoch = current_runtime_epoch
        self._strict_activity_identity = strict_activity_identity
        self._publish_materials_ephemeral = (
            public_event_publishers.publish_materials_ephemeral
        )
        self._request_identity_registry = request_identity_registry
        self._background_agent_usage_handler = background_agent_usage_handler
        self._recent_material_observation_handler = (
            recent_material_observation_handler
        )
        self._runtime_memory_store = runtime_memory_store
        self._task_memory_lookup_result_handler = task_memory_lookup_result_handler
        self._runtime_memory_compaction_plan_handler = (
            runtime_memory_compaction_plan_handler
        )
        self._self_reflection_event_handler = self_reflection_event_handler
        self._self_reflection_service = self_reflection_service
        self._self_reflection_public_event_handler = (
            SelfReflectionPublicEventHandler(
                publish_interaction=public_event_publishers.publish_interaction,
                acknowledge_publication=(
                    getattr(self_reflection_service, "acknowledge_episode_publication", None)
                    if self_reflection_service is not None
                    else None
                ),
                fail_publication=(
                    getattr(self_reflection_service, "fail_episode_publication", None)
                    if self_reflection_service is not None
                    else None
                ),
            )
        )
        # 子进程退出时可能把同一终态同时留在 Bridge 和 shutdown drain 两个观察窗口；
        # 用世代加 Activity 身份做有界去重，避免旁路收到两次同一份用量。
        self._recorded_usage_identities: OrderedDict[tuple[int | None, str], None] = (
            OrderedDict()
        )

    def cancel_request(self, request_id: str) -> None:
        """在公共输出处理器登记请求级正文取消墓碑。

        Args:
            request_id: 已由领域流程判定失效的助手请求编号。

        Returns:
            None: 后续相同请求的正文会在广播前被吸收。
        """

        self._output_event_handler.cancel_request(request_id)

    async def dispatch(self, event: RuntimeEvent) -> None:
        """按固定优先级为一条回流事件选择唯一处理器。

        处理顺序如下：

        1. 先按显式互斥集合确定唯一所有者，未知事件立即失败；
        2. 主进程材料释放和 Self-Reflection 事实先进入各自控制面；
        3. 请求失败补偿：释放失败请求仍占用的轮次和材料；
        4. 私有用量：在公共投影前保存并从事件中剥离；
        5. 运行时世代校验：丢弃热替换前子进程迟到的普通事件；
        6. 内部生命周期、Activity 或助手输出进入各自处理器。

        Args:
            event: 需要进入主进程状态所有者的运行时事件。
        """

        owner = classify_runtime_event_owner(event)

        if owner == "realtime_input":
            await self._dispatch_realtime_input_event(event)
            return

        if owner == "main_process_materials":
            if event.source != "main_process" or event.runtime_epoch is not None:
                raise RuntimeEventRoutingError(
                    event_type=event.type,
                    source=event.source,
                    reason_code="invalid_main_process_material_event",
                )
            await self._publish_materials_ephemeral(event.to_control_event())
            return

        # 助手世代退出时取回的 backlog 已经失去重新执行资格。它只允许进入后台
        # Activity 的幂等资源收口，不能验证请求身份、写入新记忆或重新触发公开表达。
        if event.retired_runtime_event:
            if owner == "activity":
                event = self._record_internal_usage(event)
                await self.activity_event_handler.reconcile_retired_event(event)
                return
            if owner == "output_settlement":
                # 旧进程 shutdown backlog 与 Bridge 补偿可能同时携带同一结算。
                # Journal 墓碑负责幂等；Router 只允许它完成既有请求收口，不能重新
                # 投影正文、工具或其它旧世代业务事件。
                await self._output_event_handler.handle_settlement(event)
                return
            raise RuntimeEventRoutingError(
                event_type=event.type,
                source=event.source,
                reason_code="retired_event_owner_not_settlement",
            )

        if (
            self._request_identity_registry is not None
            and _requires_assistant_request_identity(event)
        ):
            generation_id = event.payload.get("generation_id")
            self._request_identity_registry.validate_event(
                request_id=str(event.payload["request_id"]),
                runtime_epoch=event.runtime_epoch,
                generation_id=(
                    generation_id if isinstance(generation_id, str) else None
                ),
            )

        # 工具提交请求必须先在主进程完成 Attention 授权和 canonical 应用，随后
        # Service 才能通过控制通道回复批准。它不能走普通异步事实分支。
        if owner == "self_reflection":
            if event.type in SELF_REFLECTION_PUBLIC_EVENT_TYPES:
                await self._self_reflection_public_event_handler.handle(event)
                return
            if event.type == "foreground.self_reflection_reply_admission_closed":
                if self._self_reflection_service is None:
                    raise RuntimeEventRoutingError(
                        event_type=event.type,
                        source=event.source,
                        reason_code="self_reflection_service_unavailable",
                    )
                self._self_reflection_service.observe_reply_admission_closed(event)
                return
            if event.type == "foreground.self_reflection_tool_commit_requested":
                if self._self_reflection_service is None:
                    raise RuntimeEventRoutingError(
                        event_type=event.type,
                        source=event.source,
                        reason_code="self_reflection_service_unavailable",
                    )
                await self._self_reflection_service.handle_tool_commit_request(
                    event,
                    register_canonical_event=self._register_canonical_tool_event,
                )
                return

            # 结果即便来自刚被替换的 Runtime，也必须先交给 Attention 和 episode
            # 账本幂等吸收，不能被通用世代门禁提前丢弃。
            handler = self._self_reflection_event_handler
            if handler is None and self._self_reflection_service is not None:
                handler = self._self_reflection_service.observe_outcome
            if handler is None:
                raise RuntimeEventRoutingError(
                    event_type=event.type,
                    source=event.source,
                    reason_code="self_reflection_handler_unavailable",
                )
            result = handler(event)
            if hasattr(result, "__await__"):
                await result
            return

        # 请求失败事件即使来自旧世代也必须先处理。它只负责按 request_id 释放旧资源；
        # 若先做世代过滤，热替换前的材料租约和实时轮次就没有机会收口。
        if owner == "request_failure":
            handled = await self._request_failure_handler.try_handle(event)
            if not handled:
                raise RuntimeEventRoutingError(
                    event_type=event.type,
                    source=event.source,
                    reason_code="request_failure_handler_rejected_owned_event",
                )
            return

        # Drainer 把必达终态的退避次数放在内部载荷中。业务状态机、任务记忆和 Notice
        # 都只能看到原始来源事实，不能把消费器的重试 bookkeeping 泄漏出去。
        if "_terminal_settlement_retry_count" in event.payload:
            public_payload = dict(event.payload)
            public_payload.pop("_terminal_settlement_retry_count", None)
            event = replace(event, payload=public_payload)
        # 热重配会先用主进程事实收口旧 Activity，再关闭旧 Runner。旧 Runner 随后返回的
        # 终态不能改写公共状态，但其中已收集的 Token 仍应在世代门禁前幂等落库。
        # 无论写入成功与否都先剥离私有字段，后面的公共处理器永远只能看到公开载荷。
        event = self._record_internal_usage(event)

        # 压缩计划本身绑定助手运行时世代。旧运行时的结果必须先释放它拥有的计划，
        # 不能被通用世代门禁直接丢弃，否则 Worker 关闭且没有后续事件时会永久卡住压缩。
        if owner == "runtime_memory_compaction":
            if self._runtime_memory_store is None:
                raise RuntimeEventRoutingError(
                    event_type=event.type,
                    source=event.source,
                    reason_code="runtime_memory_store_unavailable",
                )
            applied = self._handle_runtime_memory_compaction_event(event)
            if applied:
                self._schedule_runtime_memory_compaction(event.produced_at)
            return

        # 可见输出结算是 Bridge 对已接管请求的故障补偿事实。即使来源 runtime 已被热替换，
        # 主进程也必须先归档此前成功广播的正文，再允许下一轮越过顺序屏障。
        if owner == "output_settlement":
            await self._output_event_handler.handle_settlement(event)
            return

        # 失败补偿之外的旧世代事件没有资格影响当前子进程创建的活动、输出或轮次状态。
        if event.runtime_epoch is not None and self._current_runtime_epoch is not None:
            current_epoch = self._current_runtime_epoch()
            if event.runtime_epoch != current_epoch:
                logger.info(
                    "忽略旧助手运行时事件 event_type=%s event_epoch=%s current_epoch=%s",
                    event.type,
                    event.runtime_epoch,
                    current_epoch,
                )
                return

        if event.type == "llm.foreground.tool_call_completed":
            event = await self._consume_recent_material_observation(event)
            await self._register_canonical_tool_event(event)
            return

        # 提醒写命令已经越过工具提交边界，但不是 Activity 或对话记忆事实。
        # 先交给独立权威服务完成，再向原助手世代返回 canonical 结果。
        if owner == "timed_reminder_command":
            await self._timed_reminder_event_handler.handle(event)
            return

        # 运行期记忆只消费已经通过世代门禁的内部事实。MemoryCommit 不属于公开协议，
        # 必须在广播前剥离；终态 task_memory 则必须先于 Notice 候选写入。
        # Activity 一进入真实终态就先从 active 动态环境移出；随后任务记忆和 Notice
        # 都读取同一份 frozen Progress，避免终态先后竞态丢掉已经公开的正文。
        if event.type in {
            "cognitive_task.completed",
            "cognitive_task.failed",
            "cognitive_task.cancelled",
        }:
            # 终态合同必须在任何 Runtime Memory 写入前通过；否则坏事件即使稍后被
            # Activity Registry 吸收，也会先污染后续 Foreground 可见历史。
            validate_activity_terminal_contract(
                event.type,
                event.payload,
                require_canonical_activity_id=self._strict_activity_identity,
            )
        if self._runtime_memory_store is not None:
            self._runtime_memory_store.observe_activity_event(
                event.type,
                event.payload,
            )
        self._activity_terminal_settlement_service.settle_source_terminal(
            event.type,
            event.payload,
        )
        if self._runtime_memory_store is not None:
            self._schedule_runtime_memory_compaction(event.produced_at)

        if owner == "task_memory_lookup":
            self._handle_task_memory_lookup(event)
            return

        # 诊断事实只写本地日志，不改变产品状态，也绝不能进入公共控制协议。
        if owner == "internal_diagnostic":
            logger.info(
                "运行时内部事件已消费 event_type=%s source=%s",
                event.type,
                event.source,
            )
            return

        # assistant.state_changed 仍需释放失败 Turn，但由内部生命周期入口调用输出状态机，
        # 不把它混入可公开的助手输出集合。
        if owner == "internal_lifecycle":
            await self._output_event_handler.handle(event)
            if self._runtime_memory_store is not None:
                self._schedule_runtime_memory_compaction(event.produced_at)
            return

        # Activity 处理器只认 Cognitive、后台工作及其关联的任务结果交付事件；
        # 返回真表示它已完成 Registry、Notice 或结果卡一侧的处理。其中 Notice 事件会经
        # 注入的 activity_notice_handler 写入 NoticeDeliveryCoordinator，而非在 Router 内保存。
        if owner == "activity":
            handled = await self.activity_event_handler.try_handle(event)
            if not handled:
                raise RuntimeEventRoutingError(
                    event_type=event.type,
                    source=event.source,
                    reason_code="activity_handler_rejected_owned_event",
                )
            return

        if owner == "assistant_output":
            try:
                await self._output_event_handler.handle(event)
            except AssistantRequestIdentityConflict:
                raise
            except Exception as exc:
                # 公共输出投影或下游广播失败时，不能把请求留在 Turn 屏障里。先走
                # 统一失败补偿，再用同一执行身份写入无正文的 output_settled 事实。
                logger.error(
                    "助手公共输出处理失败 event_type=%s source=%s exception_type=%s",
                    event.type,
                    event.source,
                    type(exc).__name__,
                )
                await self._compensate_assistant_output_failure(event)
            if self._runtime_memory_store is not None:
                self._schedule_runtime_memory_compaction(event.produced_at)
            return

        raise RuntimeEventRoutingError(
            event_type=event.type,
            source=event.source,
        )

    async def _consume_recent_material_observation(
        self,
        event: RuntimeEvent,
    ) -> RuntimeEvent:
        """消费并剥离工具事件中的一次性近期材料观察。

        Args:
            event: 可能携带私有近期路径的 canonical 工具事件。

        Returns:
            已删除私有字段、可安全交给 Runtime Memory 和 Activity 的事件副本。

        Raises:
            RuntimeEventRoutingError: 私有载荷无效或生产环境未装配处理端口。
        """

        if RECENT_MATERIAL_OBSERVATION_FIELD not in event.payload:
            return event
        sanitized_payload = dict(event.payload)
        raw_observation = sanitized_payload.pop(
            RECENT_MATERIAL_OBSERVATION_FIELD
        )
        sanitized_event = replace(event, payload=sanitized_payload)
        try:
            observation = RecentMaterialObservation.from_payload(raw_observation)
        except ValueError as exception:
            raise RuntimeEventRoutingError(
                event_type=event.type,
                source=event.source,
                reason_code="recent_material_observation_invalid",
            ) from exception
        handler = self._recent_material_observation_handler
        if handler is None:
            raise RuntimeEventRoutingError(
                event_type=event.type,
                source=event.source,
                reason_code="recent_material_observation_handler_unavailable",
            )
        await handler(observation)
        return sanitized_event

    async def _register_canonical_tool_event(self, event: RuntimeEvent) -> None:
        """按普通 Foreground 的固定顺序完整应用一条 canonical 工具事实。

        Args:
            event: 已通过身份和世代门禁的 `tool_call_completed` 事件。

        Raises:
            RuntimeEventRoutingError: 事件类型错误或 Activity owner 拒绝消费时抛出。
        """

        if event.type != "llm.foreground.tool_call_completed":
            raise RuntimeEventRoutingError(
                event_type=event.type,
                source=event.source,
                reason_code="canonical_tool_event_type_invalid",
            )
        if event.runtime_epoch is not None and self._current_runtime_epoch is not None:
            current_epoch = self._current_runtime_epoch()
            if event.runtime_epoch != current_epoch:
                raise RuntimeEventRoutingError(
                    event_type=event.type,
                    source=event.source,
                    reason_code="canonical_tool_event_epoch_mismatch",
                )

        # Runtime Memory、终态结算和 TaskResult acceptance 必须沿用普通工具
        # 事实的同一顺序；任一步异常都让同步 Broker 保持等待并重发同一身份。
        if self._runtime_memory_store is not None:
            self._runtime_memory_store.observe_tool_event(event.payload)
            self._runtime_memory_store.observe_activity_event(
                event.type,
                event.payload,
            )
        self._activity_terminal_settlement_service.settle_source_terminal(
            event.type,
            event.payload,
        )
        if self._runtime_memory_store is not None:
            self._schedule_runtime_memory_compaction(event.produced_at)
        handled = await self.activity_event_handler.try_handle(event)
        if not handled:
            raise RuntimeEventRoutingError(
                event_type=event.type,
                source=event.source,
                reason_code="canonical_tool_event_handler_rejected",
            )

    def _record_internal_usage(self, event: RuntimeEvent) -> RuntimeEvent:
        """把私有用量交给唯一旁路，并从后续业务载荷中剥离。

        Args:
            event: 可能携带供应商私有用量字段的运行时事件。

        Returns:
            已移除 `internal_usage` 的业务事件副本。
        """

        if "internal_usage" not in event.payload:
            return event
        internal_payload = event.payload
        public_payload = dict(internal_payload)
        public_payload.pop("internal_usage", None)
        sanitized_event = replace(event, payload=public_payload)
        activity_id = event.payload.get("activity_id")
        usage_identity = (
            (event.runtime_epoch, activity_id.strip())
            if isinstance(activity_id, str) and activity_id.strip()
            else None
        )
        if usage_identity is not None:
            if usage_identity in self._recorded_usage_identities:
                return sanitized_event
            self._recorded_usage_identities[usage_identity] = None
            self._recorded_usage_identities.move_to_end(usage_identity)
            while len(self._recorded_usage_identities) > _USAGE_IDENTITY_LIMIT:
                self._recorded_usage_identities.popitem(last=False)
        if self._background_agent_usage_handler is not None:
            try:
                self._background_agent_usage_handler(event.type, internal_payload)
            except Exception:
                logger.exception("后台工作用量处理器异常 event_type=%s", event.type)
        return sanitized_event

    async def _compensate_assistant_output_failure(self, event: RuntimeEvent) -> None:
        """收口无法公开的助手输出，不把原始异常带进公共事件。"""

        request_id = event.payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise RuntimeEventRoutingError(
                event_type=event.type,
                source=event.source,
                reason_code="assistant_output_contract_invalid_without_request",
            )

        failure_event = replace(
            event,
            type="assistant.generation_interrupted",
            payload={
                **event.payload,
                "request_id": request_id,
                "reason": "output_unavailable",
                "visible_response_completed": False,
            },
        )
        await self._request_failure_handler.compensate(
            failure_event,
            code="output_unavailable",
        )

        settlement_payload = {
            "settlement_id": (
                f"output-settlement:{request_id}:"
                f"{_clean_text(event.payload.get('generation_id')) or 'unknown'}"
            ),
            "request_id": request_id,
            "generation_id": _clean_text(event.payload.get("generation_id")) or None,
            "history_policy": event.payload.get("history_policy", "stable_dialogue"),
            "activity_progress_references": event.payload.get(
                "activity_progress_references", []
            ),
            "outcome": "output_unavailable",
        }
        for identity_field in (
            "source",
            "self_reflection_id",
            "episode_id",
            "runtime_epoch",
            "trigger_input_identity",
        ):
            if identity_field in event.payload:
                settlement_payload[identity_field] = event.payload[identity_field]
        await self._output_event_handler.handle_settlement(
            replace(
                event,
                type="assistant.output_settled",
                payload=settlement_payload,
            )
        )

    def _handle_output_settlement_committed(
        self,
        settlement: VisibleOutputSettlement,
    ) -> None:
        """在任何写入运行期记忆的可见输出结算后安排压缩。"""

        if settlement.history_policy in {"stable_dialogue", "activity_progress"}:
            self._schedule_runtime_memory_compaction(settlement.settled_at)

    async def _dispatch_realtime_input_event(self, event: RuntimeEvent) -> None:
        """按输入 Runtime 世代隔离瞬时事件，同时保留真实 Final。

        Args:
            event: 已登记的实时输入公共事件。
        """

        event_identity = _payload_input_identity(event.payload)
        current_identity = self._current_input_runtime_identity()
        if event_identity == current_identity:
            await self._realtime_input_event_handler.handle(event)
            return

        # Final transcript 是已经形成的真实用户消息。即使旧 Runtime 在替换后才完成，
        # 也必须进入 Chat；Avatar 会用同一身份拒绝它覆盖当前气泡。
        if event.type == "user.final_transcript" and str(
            event.payload.get("text", "")
        ).strip():
            await self._realtime_input_event_handler.handle(event)
            return

        logger.info(
            "旧输入 Runtime 瞬时事件已吸收 event_type=%s source=%s",
            event.type,
            event.source,
        )

    def _handle_task_memory_lookup(self, event: RuntimeEvent) -> None:
        """读取主进程任务详情并只通过内部控制通道回送。

        Args:
            event: 已通过运行时世代门禁的召回请求事件。

        Returns:
            无。无效请求只记录日志，绝不进入公共广播。
        """

        lookup_id = event.payload.get("lookup_id")
        activity_id = event.payload.get("activity_id")
        purpose = event.payload.get("purpose")
        if not isinstance(lookup_id, str) or not lookup_id.strip():
            logger.warning("忽略缺少 lookup_id 的任务记忆召回事件")
            return
        if not isinstance(activity_id, str) or not activity_id.strip():
            logger.warning("忽略缺少 activity_id 的任务记忆召回事件")
            return
        if purpose not in {"foreground_recall", "task_derivation"}:
            logger.warning("忽略缺少合法 purpose 的任务记忆召回事件")
            return
        if self._task_memory_lookup_result_handler is None:
            logger.warning("任务记忆召回回执端口未配置")
            return

        normalized_activity_id = activity_id.strip()
        result = (
            self._runtime_memory_store.recall_task(
                normalized_activity_id,
                purpose=purpose,
            )
            if self._runtime_memory_store is not None
            else {
                "state": "not_available",
                "activity_id": normalized_activity_id,
                "reason": "runtime_memory_unavailable",
                "safe_message": "这条任务记录当前不可用。",
            }
        )
        try:
            self._task_memory_lookup_result_handler(
                TaskMemoryLookupResultCommand(
                    lookup_id=lookup_id.strip(),
                    activity_id=normalized_activity_id,
                    purpose=purpose,
                    result=dict(result),
                    issued_at=event.produced_at,
                )
            )
        except Exception:
            logger.exception(
                "任务记忆召回回执提交失败 activity_id=%s",
                normalized_activity_id,
            )

    def _handle_runtime_memory_compaction_event(self, event: RuntimeEvent) -> bool:
        """按计划所有者世代收口一笔压缩完成或失败事件。

        Args:
            event: 可能来自当前或刚被替换运行时的压缩事件。

        Returns:
            当前世代的合法摘要确实生效时返回真。
        """

        if self._runtime_memory_store is None:
            return False
        memory_epoch = event.payload.get("memory_epoch")
        plan_id = event.payload.get("plan_id")
        source_message_id = event.payload.get("source_message_id")
        summary = event.payload.get("summary")
        event_runtime_epoch = event.runtime_epoch
        if not isinstance(plan_id, str) or not plan_id.strip():
            logger.warning("忽略缺少 plan_id 的运行期记忆压缩事件")
            return False
        if not isinstance(event_runtime_epoch, int):
            logger.warning("忽略缺少 runtime_epoch 的运行期记忆压缩事件 plan_id=%s", plan_id)
            return False

        current_runtime_epoch = (
            self._current_runtime_epoch()
            if self._current_runtime_epoch is not None
            else None
        )
        if current_runtime_epoch != event_runtime_epoch:
            self._runtime_memory_store.release_compaction_plan(
                plan_id=plan_id,
                owner_runtime_epoch=event_runtime_epoch,
            )
            return False

        if event.type == "llm.foreground.runtime_memory_compaction_failed":
            self._runtime_memory_store.release_compaction_plan(
                plan_id=plan_id,
                owner_runtime_epoch=event_runtime_epoch,
            )
            return False
        if not (
            isinstance(memory_epoch, int)
            and isinstance(source_message_id, int)
            and isinstance(summary, str)
        ):
            self._runtime_memory_store.release_compaction_plan(
                plan_id=plan_id,
                owner_runtime_epoch=event_runtime_epoch,
            )
            logger.warning("拒绝字段不完整的运行期记忆压缩结果 plan_id=%s", plan_id)
            return False
        return self._runtime_memory_store.apply_compaction(
            memory_epoch=memory_epoch,
            plan_id=plan_id,
            source_message_id=source_message_id,
            owner_runtime_epoch=event_runtime_epoch,
            summary=summary,
        )

    def _schedule_runtime_memory_compaction(self, issued_at: float) -> None:
        """在主进程达到阈值时提交唯一压缩计划。

        Args:
            issued_at: 触发规划的运行时事件时间。
        """

        if (
            self._runtime_memory_store is None
            or self._runtime_memory_compaction_plan_handler is None
            or self._current_runtime_epoch is None
        ):
            return
        owner_runtime_epoch = self._current_runtime_epoch()
        if owner_runtime_epoch <= 0:
            return
        plan = self._runtime_memory_store.create_compaction_plan(
            owner_runtime_epoch=owner_runtime_epoch,
        )
        if plan is None:
            return
        try:
            accepted = self._runtime_memory_compaction_plan_handler(
                RuntimeMemoryCompactionPlanCommand(
                    plan=plan,
                    issued_at=issued_at,
                )
            )
            if accepted is False:
                self._runtime_memory_store.release_compaction_plan(
                    plan_id=plan.plan_id,
                    owner_runtime_epoch=owner_runtime_epoch,
                )
        except Exception:
            self._runtime_memory_store.release_compaction_plan(
                plan_id=plan.plan_id,
                owner_runtime_epoch=owner_runtime_epoch,
            )
            logger.exception("运行期记忆压缩计划提交失败 plan_id=%s", plan.plan_id)


def _requires_assistant_request_identity(event: RuntimeEvent) -> bool:
    """判断事件是否必须通过助手请求执行身份校验。

    Args:
        event: 即将进入主进程状态所有者的运行时事件。

    Returns:
        已 claim 的助手输出事件携带非空请求编号时返回真。
    """

    request_id = event.payload.get("request_id")
    is_assistant_output = (
        event.type.startswith("assistant.")
        and event.type != "assistant.request_enqueue_failed"
    )
    return (
        is_assistant_output
        and isinstance(request_id, str)
        and bool(request_id.strip())
    )


def _require_realtime_input_runtime(
    realtime_input_runtime: RealtimeInputRuntime | None,
) -> RealtimeInputRuntime:
    """返回构造期已经验证存在的 RealtimeInputRuntime。"""

    if realtime_input_runtime is None:
        raise RuntimeError("RealtimeInputRuntime 缺失")
    return realtime_input_runtime


def _runtime_input_identity(runtime: RealtimeInputRuntime) -> tuple[str, int] | None:
    """读取输入 Runtime 的稳定比较身份。

    Args:
        runtime: 当前产品输入状态所有者。

    Returns:
        身份完整时返回二元组，否则返回空。
    """

    runtime_id = getattr(runtime, "input_runtime_id", None)
    runtime_epoch = getattr(runtime, "input_runtime_epoch", None)
    if not isinstance(runtime_id, str) or not runtime_id:
        return None
    if not isinstance(runtime_epoch, int) or runtime_epoch <= 0:
        return None
    return runtime_id, runtime_epoch


def _payload_input_identity(payload: dict[str, object]) -> tuple[str, int] | None:
    """从公共载荷读取输入 Runtime 比较身份。

    Args:
        payload: 实时输入事件载荷。

    Returns:
        字段完整时返回二元组，否则返回空并由严格 Handler 报告契约错误。
    """

    runtime_id = payload.get("input_runtime_id")
    runtime_epoch = payload.get("input_runtime_epoch")
    if not isinstance(runtime_id, str) or not isinstance(runtime_epoch, int):
        return None
    return runtime_id, runtime_epoch


def _clean_text(value: object) -> str:
    """把内部身份字段清洗成无首尾空白的字符串。"""

    return value.strip() if isinstance(value, str) else ""
