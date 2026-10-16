"""把助手子进程的活动事件投影为主进程的任务中心、提醒和结果交付。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from companion.activities.notice.models import ActivityNoticeInterruptionStrategy
from companion.activities.terminal_settlement import (
    ActivityTerminalSettlementRetryableError,
)
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.activities.runtime.registry import ActivityNotFound, ActivityStateConflict
from companion.activities.runtime.service import ActivityRuntimeUnavailable
from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplyResult,
)
from companion.observability.logging_config import get_backend_logger
from companion.realtime.task_result_delivery import (
    TaskResultAcceptanceTrackingResult,
    TaskResultDeliveryCoordinator,
    TaskResultTerminalDelivery,
    public_background_agent_started,
)
from companion.realtime.assistant_events import (
    ActivityCancellationResultCommand,
    RuntimeEvent,
)

if TYPE_CHECKING:
    from companion.foreground_self_reflection.service import ForegroundSelfReflectionService


logger = get_backend_logger(__name__)

_ACTIVITY_PROGRESS_EVENTS = frozenset(
    {
        "cognitive_task.progress",
        "cognitive_task.progress_invalidated",
        "background_agent.progress",
        "background_agent.progress_invalidated",
    }
)
_BACKGROUND_AGENT_TERMINAL_EVENTS = frozenset(
    {
        "background_agent.completed",
        "background_agent.failed",
        "background_agent.cancelled",
    }
)
_COGNITIVE_ACTIVITY_EVENTS = frozenset(
    {
        "cognitive_task.started",
        "cognitive_task.prepared",
        "cognitive_task.stage_changed",
        "cognitive_task.completed",
        "cognitive_task.failed",
        "cognitive_task.cancelled",
        "cognitive_task.interrupted",
        "cognitive_task.rejected",
    }
)
_BACKGROUND_AGENT_ACTIVITY_EVENTS = frozenset(
    {
        "background_agent.started",
        "background_agent.completed",
        "background_agent.failed",
        "background_agent.cancelled",
        "background_agent.interrupted",
        "background_agent.work_plan_changed",
        "background_agent.stage_changed",
        "background_agent.turn_output",
        "background_agent.interaction_requested",
        "background_agent.interaction_resolved",
        "background_agent.permission_granted",
    }
)
_WORK_INTERACTION_EVENTS = frozenset(
    {
        "llm.foreground.work_interaction_submission_requested",
        "llm.foreground.work_interaction_submission_completed",
        "llm.foreground.work_interaction_submission_failed",
        "llm.foreground.work_interaction_submission_abandoned",
    }
)
_ACTIVITY_CANCELLATION_EVENTS = frozenset(
    {"llm.foreground.activity_cancellation_requested"}
)
_RETIRED_BACKGROUND_EVENT_TYPES = frozenset(
    {
        "background_agent.started",
        "background_agent.completed",
        "background_agent.failed",
        "background_agent.cancelled",
    }
)

ACTIVITY_RUNTIME_EVENT_TYPES = frozenset(
    {
        "assistant.runtime_unavailable",
        "llm.foreground.tool_call_completed",
        *_ACTIVITY_PROGRESS_EVENTS,
        *_COGNITIVE_ACTIVITY_EVENTS,
        *_BACKGROUND_AGENT_ACTIVITY_EVENTS,
        *_WORK_INTERACTION_EVENTS,
        *_ACTIVITY_CANCELLATION_EVENTS,
    }
)


def _clean_text(value: object) -> str:
    """把跨进程未知字段收窄为去除首尾空白的文字。

    Args:
        value: 尚未确认类型的事件字段。

    Returns:
        字符串字段清理后的内容；其它类型返回空字符串。
    """

    return value.strip() if isinstance(value, str) else ""


class ActivityRuntimeEventHandler:
    """协调活动的三类主进程后果，但不保存活动权威状态。

    活动权威状态由 Registry 持有，本类通过注入端口把来源事件应用到 Registry 并广播
    快照。除此之外，它还负责两条不能简单按事件到达顺序处理的链路：

    - Cognitive：生命周期变化后依次更新任务中心、收口来源资源、广播快照、写入 Notice 账本；
    - 后台工作：终态可先进入任务中心，但结果卡、Notice 和材料释放必须等待
      acceptance、started 和发起它的 Foreground 请求收口；
    - 运行时中断：终止同一世代的活动、按策略收口 Notice，并释放尚未交付的材料租约。

    因此这里是“来源事件 → 主进程后果”的编排层，不是事件队列消费者，也不重写
    Notice Coordinator、Activity Registry 或 TaskResult Delivery 的内部状态机。

    Notice 的生产路径为 ``_apply_activity_notice() →
    LiveKitMediaSession._handle_activity_event() →
    AssistantRequestContextPreparer.handle_activity_event() →
    NoticeDeliveryCoordinator.apply_event()``。媒体会话在这条路径上消费 Coordinator 返回的
    supersession 结果，取消已被新进度替代的 System Notice 请求；本类只投递活动事实。
    """

    def __init__(
        self,
        *,
        publish_activity: Callable[[dict[str, Any]], Awaitable[None]],
        publish_activity_ephemeral: Callable[[dict[str, Any]], Awaitable[None]],
        publish_materials_ephemeral: Callable[[dict[str, Any]], Awaitable[None]],
        activity_apply_handler: Callable[
            [str, dict[str, object], int], ActivityApplyResult
        ]
        | None = None,
        activity_broadcast_handler: Callable[[ActivityProjection], Awaitable[object]]
        | None = None,
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
        background_agent_interaction_event_handler: Callable[
            [str, dict[str, object], int], AgentInteractionEventApplyResult
        ]
        | None = None,
        background_agent_interaction_interruption_handler: Callable[
            [int], list[dict[str, object]]
        ]
        | None = None,
        retain_turn_materials: Callable[[str], bool] | None = None,
        release_turn_materials: Callable[[str], list[dict[str, object]]] | None = None,
        task_result_delivery_coordinator: TaskResultDeliveryCoordinator | None = None,
        task_result_acceptance_handler: Callable[
            [RuntimeEvent, TaskResultAcceptanceTrackingResult], Awaitable[object]
        ]
        | None = None,
        self_reflection_service: "ForegroundSelfReflectionService" | None = None,
    ) -> None:
        """初始化活动投影、提醒和成果交付的边界端口。

        Args:
            publish_activity: 广播公开 TaskResult 交付事件的 Activity 领域端口。
            publish_activity_ephemeral: 广播交互卡片事件的 Activity 瞬时端口。
            publish_materials_ephemeral: 广播材料释放快照的 Materials 瞬时端口。
            activity_apply_handler: 把来源事件应用到权威 Registry 的同步端口。
                生产环境由 ``ApplicationContainer.create_livekit_media_session`` 注入
                ``ActivityService.apply_source_event``。
            activity_broadcast_handler: 广播权威 Activity 快照的异步端口。
                生产环境由 ``ApplicationContainer.create_livekit_media_session`` 注入
                ``ActivityService.broadcast_projection``。
            activity_interruption_handler: 中断指定运行时世代全部 Activity 的异步端口。
                生产环境由 ``ApplicationContainer.create_livekit_media_session`` 注入
                ``ActivityService.interrupt_active``。
            activity_notice_interruption_handler: 按策略中断 Activity Notice 的同步端口。
                生产环境由 ``LiveKitMediaSession`` 注入自身的
                ``LiveKitMediaSession.interrupt_activity_notices``；该方法调用
                ``NoticeDeliveryCoordinator.interrupt_active``，并收束在途 System Notice 请求。
            activity_notice_handler: 把单条 Activity 来源事件交给 Notice 链路的同步端口。
                生产环境由 ``LiveKitMediaSession`` 注入自身的
                ``LiveKitMediaSession._handle_activity_event``；该方法调用
                ``AssistantRequestContextPreparer.handle_activity_event``，最终调用
                ``NoticeDeliveryCoordinator.apply_event``。Coordinator 返回的 supersession
                结果由媒体会话消费；本处理器不直接收到该返回值，也不取消请求。
            activity_cancellation_handler: 复用 `ActivityService.cancel()` 的权威取消端口。
            activity_cancellation_result_handler: 按原始运行时世代把结构化回执送回子进程的端口。
            background_agent_interaction_event_handler: 登记或收口待答请求的主进程端口。
            background_agent_interaction_interruption_handler: 作废旧世代待答请求并生成
                卡片收口事件的主进程端口。
            retain_turn_materials: 为 TaskResult 取得材料租约的同步端口。
                生产环境由 ``ApplicationContainer.create_livekit_media_session`` 注入
                ``MaterialLifecycle.retain_turn``。
            release_turn_materials: 释放 TaskResult 材料租约的同步端口。
                生产环境由 ``ApplicationContainer.create_livekit_media_session`` 注入
                ``MaterialLifecycle.release_turn``。
            task_result_delivery_coordinator: 可选的进程内结果卡交付账本；生产环境由组合根
                共享它，以便重连首帧读取已经成功交付的结果卡。
            task_result_acceptance_handler: 首次或重复 acceptance 处理完成后的事实出口。
                它只接收协调器已经确认的材料 retain 结果，不自行读取材料状态。
        """

        if (activity_apply_handler is None) != (activity_broadcast_handler is None):
            raise ValueError("Activity 应用端口与广播端口必须同时提供")
        self._publish_activity = publish_activity
        self._publish_activity_ephemeral = publish_activity_ephemeral
        self._publish_materials_ephemeral = publish_materials_ephemeral
        self._activity_apply_handler = activity_apply_handler
        self._activity_broadcast_handler = activity_broadcast_handler
        self._activity_interruption_handler = activity_interruption_handler
        self._activity_notice_interruption_handler = activity_notice_interruption_handler
        # 这是“写入提醒事实”的端口，不是“立即生成或播报一段提醒文本”的端口。
        # 具体文本由后续 System Notice Foreground 请求根据 Coordinator 预留的批次生成。
        self._activity_notice_handler = activity_notice_handler
        self._activity_cancellation_handler = activity_cancellation_handler
        self._activity_cancellation_result_handler = (
            activity_cancellation_result_handler
        )
        self._background_agent_interaction_event_handler = (
            background_agent_interaction_event_handler
        )
        self._background_agent_interaction_interruption_handler = (
            background_agent_interaction_interruption_handler
        )
        self._task_result_delivery = task_result_delivery_coordinator or TaskResultDeliveryCoordinator(
            retain_material=retain_turn_materials,
            release_material=release_turn_materials,
        )
        self._task_result_acceptance_handler = task_result_acceptance_handler
        self._self_reflection_service = self_reflection_service
        # Activity Registry 先于 Notice 端口推进。若 Notice 写入瞬时失败，重放事件会
        # 被 Registry 判为重复；这份短账本保证重复路径仍能完成唯一一次 Notice 交付。
        self._pending_cognitive_notices: dict[
            tuple[int, str, str],
            RuntimeEvent,
        ] = {}
        # Notice 写入失败不能依赖原始 Activity 事件再次到达；Drainer 会隔离普通异常，
        # 所以这里自己保留一次性重试账本，直到同一身份成功交给 Notice 链路。
        self._cognitive_notice_retry_counts: dict[tuple[int, str, str], int] = {}
        self._scheduled_cognitive_notice_retries: set[tuple[int, str, str]] = set()

    async def try_handle(self, event: RuntimeEvent) -> bool:
        """识别并处理一条属于 Activity 域的回流事件。

        分支顺序就是业务优先级：进度事件只更新 Notice，避免无意义地刷新任务中心；
        Cognitive 事件按“Registry → 来源资源 → 快照 → Notice”顺序处理（恢复接管时 Notice
        抑制会提前到快照广播之前）；TaskResult 则根据其
        acceptance、started 和 terminal 的乱序到达规则进入交付协调器。返回假表示
        这不是活动事件，外层 Router 应继续交给普通助手输出处理器。

        Args:
            event: 已通过当前运行时世代过滤的事件。

        Returns:
            事件属于 Activity 事件族并已消费时返回真。
        """

        # 运行时意外中断不是某一条活动自己的失败，需要按 runtime_epoch 收口整批活动。
        if event.type == "assistant.runtime_unavailable":
            await self._handle_runtime_unavailable(event)
            return True
        if event.type in _ACTIVITY_CANCELLATION_EVENTS:
            await self._handle_activity_cancellation(event)
            return True
        # 弱进度不改变 Activity 生命周期；只更新 Notice 候选，不能反复广播任务中心快照。
        if event.type in _ACTIVITY_PROGRESS_EVENTS:
            self._apply_activity_notice(event)
            return True
        if event.type in {
            "background_agent.interaction_requested",
            "background_agent.interaction_resolved",
        }:
            if event.runtime_epoch is None:
                return True
            interaction_application = None
            if self._background_agent_interaction_event_handler is not None:
                interaction_application = self._background_agent_interaction_event_handler(
                    event.type,
                    event.payload,
                    event.runtime_epoch,
                )
                if interaction_application is None:
                    return True
            # 同一事件既推进任务中心状态，也驱动交互卡片和 Notice 账本。requested
            # 创建待表达请求；resolved 不创建新提醒，而是按 request_id 精确废止旧提醒。
            apply_result = self._apply_activity(event)
            if apply_result.disposition == "changed":
                await self._broadcast_activity(apply_result.projection)
            self._apply_activity_notice(event)
            control_event = (
                interaction_application.control_event
                if interaction_application is not None
                else event.to_control_event()
            )
            if control_event is not None:
                await self._publish_activity_ephemeral(control_event)
            return True
        if event.type == "background_agent.permission_granted":
            # Gate 已在 Runner 内生效；媒体会话据此触发鼠标短确认，不改写 Activity 生命周期。
            self._apply_activity_notice(event)
            return True
        if event.type in _WORK_INTERACTION_EVENTS:
            if (
                event.runtime_epoch is None
                or self._background_agent_interaction_event_handler is None
            ):
                return True
            interaction_application = self._background_agent_interaction_event_handler(
                event.type,
                event.payload,
                event.runtime_epoch,
            )
            if interaction_application is None:
                return True
            if interaction_application.control_event is not None:
                await self._publish_activity_ephemeral(
                    interaction_application.control_event
                )
            # 材料只在后台执行器返回 accepted 后取得租约。requested 只说明主进程
            # 正在裁决，failed/abandoned 则明确表示后台没有接收本次补充。
            if event.type == "llm.foreground.work_interaction_submission_completed":
                self._task_result_delivery.track_supplemental_material(event)
            return True
        # 剩余 Cognitive 事件是 started、阶段变化和终态，统一走 Registry 投影链路。
        if event.type in _COGNITIVE_ACTIVITY_EVENTS:
            await self._handle_cognitive_event(event)
            return True
        # 后台工作仍以自己的来源事件更新任务中心；只有需要交付成果的任务才额外
        # 投影任务结果卡，并沿用四条件协调与材料租约。
        if event.type == "background_agent.started":
            await self._handle_background_agent_started(event)
            return True
        if event.type in _BACKGROUND_AGENT_TERMINAL_EVENTS:
            await self._handle_background_agent_terminal(event)
            return True
        # 计划快照只服务于任务中心和 Foreground 动态环境。真正需要说给用户听的阶段切换
        # 已由 Executor 另行生成退避进度事件，这里不能再把原始清单变化送进 Notice。
        if event.type in {
            "background_agent.work_plan_changed",
            "background_agent.stage_changed",
        }:
            apply_result = self._apply_activity(event)
            if apply_result.disposition == "changed":
                await self._broadcast_activity(apply_result.projection)
            return True
        if event.type == "background_agent.turn_output":
            # Turn 正文只写入 Activity 详情；控制 WebSocket 只收到 snapshot_changed 信号，
            # Console 随后通过 HTTP 重取详情，避免正文进入通用事件广播链路。
            apply_result = self._apply_activity(event)
            if apply_result.disposition == "changed":
                await self._broadcast_activity(apply_result.projection)
            return True
        if event.type in _BACKGROUND_AGENT_ACTIVITY_EVENTS:
            await self._handle_cognitive_event(event)
            return True
        # 此事件是 TaskResult 的 acceptance 证明，不是公开 Activity 生命周期事件。
        # terminal 若已抢先到达，记录 acceptance 后可能立即满足交付条件。
        if event.type == "llm.foreground.tool_call_completed":
            if self._self_reflection_service is not None:
                self._self_reflection_service.observe_tool_call_completed(event)
            tracking_result = self._task_result_delivery.track_tool_acceptance(event)
            if (
                tracking_result.accepted
                and self._task_result_acceptance_handler is not None
            ):
                await self._task_result_acceptance_handler(event, tracking_result)
            if tracking_result.terminal_delivery is not None:
                await self._deliver_task_result_terminal(
                    tracking_result.terminal_delivery
                )
            return True
        return False

    async def _handle_activity_cancellation(self, event: RuntimeEvent) -> None:
        """执行 Foreground 已提交的精确取消，并返回结构化权威回执。

        Args:
            event: 子进程在工具事件提交后发出的取消请求。

        Returns:
            无返回值；处理结果通过原始运行时世代的控制命令回传。
        """

        cancellation_id = _clean_text(event.payload.get("cancellation_id"))
        activity_id = _clean_text(event.payload.get("activity_id"))
        state: Literal["cancellation_requested", "rejected"] = "rejected"
        reason = "invalid_cancellation_request"
        safe_message = "这次取消请求无效，任务没有被取消。"

        if (
            cancellation_id
            and activity_id
            and event.runtime_epoch is not None
            and self._activity_cancellation_handler is not None
        ):
            try:
                await self._activity_cancellation_handler(activity_id)
            except ActivityNotFound:
                reason = "activity_not_found"
                safe_message = "这项任务已不存在，无法取消。"
            except ActivityStateConflict:
                reason = "activity_state_conflict"
                safe_message = "这项任务已经结束、正在取消或当前不能取消。"
            except ActivityRuntimeUnavailable:
                reason = "activity_runtime_unavailable"
                safe_message = "当前运行时无法接收取消请求，任务没有确认停止。"
            except Exception:
                logger.exception(
                    "Foreground Activity 取消执行失败 activity_id=%s runtime_epoch=%s",
                    activity_id,
                    event.runtime_epoch,
                )
                reason = "activity_cancellation_failed"
                safe_message = "这项任务没有确认进入停止流程。"
            else:
                state = "cancellation_requested"
                reason = "accepted"
                safe_message = "取消请求已接收，任务正在停止。"
        elif self._activity_cancellation_handler is None:
            reason = "activity_runtime_unavailable"
            safe_message = "当前运行时无法接收取消请求，任务没有确认停止。"

        if event.runtime_epoch is None or self._activity_cancellation_result_handler is None:
            logger.warning(
                "Activity 取消回执无法投递 cancellation_id=%s activity_id=%s",
                cancellation_id,
                activity_id,
            )
            return
        command = ActivityCancellationResultCommand(
            cancellation_id=cancellation_id,
            activity_id=activity_id,
            state=state,
            reason=reason,
            safe_message=safe_message,
            issued_at=time.monotonic(),
        )
        try:
            self._activity_cancellation_result_handler(
                command,
                event.runtime_epoch,
            )
        except Exception:
            logger.warning(
                "Activity 取消回执投递失败 cancellation_id=%s activity_id=%s runtime_epoch=%s",
                cancellation_id,
                activity_id,
                event.runtime_epoch,
                exc_info=True,
            )

    async def reconcile_retired_event(self, event: RuntimeEvent) -> None:
        """收口旧助手世代的后台 backlog，而不重新创建公共 Activity。

        退出时子进程可能已经把 started 或终态写入 IPC 队列，但主进程还没来得及消费。
        这些事实不能重新派发或恢复；只需按世代关闭已有 TaskResult 状态并释放真实材料。

        Args:
            event: 带有 `retired_runtime_event=True` 和旧 `runtime_epoch` 的退出事件。
        """

        if event.type not in _RETIRED_BACKGROUND_EVENT_TYPES:
            logger.info(
                "忽略不允许收口的退役事件 event_type=%s runtime_epoch=%s",
                event.type,
                event.runtime_epoch,
            )
            return
        if event.runtime_epoch is None:
            return
        # 同一世代的第一条退役事件就关闭全部未交付 TaskResult；后续 started/terminal
        # 只会命中已经移除的状态，不会重复释放材料或生成结果卡。
        for release_event in self._task_result_delivery.interrupt_runtime_epoch(
            event.runtime_epoch
        ):
            await self._publish_materials_ephemeral(release_event)

    async def acknowledge_task_result_request(self, runtime_epoch: int, request_id: str) -> None:
        """标记发起 TaskResult 的 Foreground 请求已收口，并开放待交付结果。

        TaskResult 不能在用户还没听到“已接单”的同一前台请求中抢先展示结果。该请求
        正常结束或被取消后，协调器会检查此前已到达的 terminal 是否因此获得交付资格。

        Args:
            runtime_epoch: Foreground 请求所属助手运行时世代。
            request_id: 已完成或被取消的 Foreground 请求编号。
        """

        for activity_id in self._task_result_delivery.closing_activities_for_request(runtime_epoch, request_id):
            await self._retry_task_result_materials(runtime_epoch, activity_id)
        pending_deliveries = self._task_result_delivery.acknowledge_request(
            runtime_epoch,
            request_id,
        )
        try:
            for pending_delivery in pending_deliveries:
                await self._deliver_task_result_terminal(pending_delivery)
        finally:
            # 一轮可能领取多个任务；前项失败或协程取消时，后项必须能够重新领取。
            # 已完成项已移除，正在关闭的项仍保持关闭，只有未交付项恢复可重试资格。
            for pending_delivery in pending_deliveries:
                self._task_result_delivery.abandon_terminal_delivery(pending_delivery)

    def interrupt_task_result_deliveries(self, runtime_epoch: int) -> list[dict[str, object]]:
        """中断指定运行时世代尚未完成的 TaskResult 交付并释放材料。

        Args:
            runtime_epoch: 已失效的助手运行时世代。

        Returns:
            释放对应材料租约产生的瞬时事件。
        """

        return self._task_result_delivery.interrupt_runtime_epoch(runtime_epoch)

    async def _handle_runtime_unavailable(self, event: RuntimeEvent) -> None:
        """把助手运行时意外失效收口为活动中断、提醒和材料释放。

        每一步都放在 ``finally`` 中：即使活动投影或 Notice 收口异常，仍必须继续释放
        TaskResult 的材料租约，避免已失效世代留下不可见的资源占用。
        """

        if event.runtime_epoch is None:
            logger.warning("无法归因运行时世代的 assistant.runtime_unavailable 已忽略")
            return
        try:
            if self._activity_interruption_handler is not None:
                await self._activity_interruption_handler(
                    event.runtime_epoch,
                    "runtime_interrupted",
                )
        finally:
            try:
                if self._activity_notice_interruption_handler is not None:
                    # 生产端口会先让 Coordinator 废止旧弱提醒；意外中断再为每项真实活动
                    # 建立强终态候选，并条件取消已在生成中的旧 System Notice 请求。
                    self._activity_notice_interruption_handler(event.runtime_epoch, "notify")
            finally:
                for release_event in self.interrupt_task_result_deliveries(event.runtime_epoch):
                    await self._publish_materials_ephemeral(release_event)
                if self._background_agent_interaction_interruption_handler is not None:
                    for interaction_event in (
                        self._background_agent_interaction_interruption_handler(
                            event.runtime_epoch
                        )
                    ):
                        await self._publish_activity_ephemeral(interaction_event)

    async def _handle_cognitive_event(self, event: RuntimeEvent) -> None:
        """按“更新 Registry → 广播快照 → 写 Notice”处理 Cognitive 生命周期事件。

        Registry 返回 duplicate 或 absorbed 时不能再次广播或提醒，否则迟到、重放事件会
        让用户看到重复进度或重复终态。
        """
        notice_key = _cognitive_notice_key(event)
        apply_result = self._apply_activity(event)
        if apply_result.disposition != "changed":
            if notice_key is not None and notice_key in self._pending_cognitive_notices:
                self._deliver_pending_cognitive_notice(notice_key)
            return
        recovery_taken = False
        if self._self_reflection_service is not None:
            if event.type == "cognitive_task.started":
                self._self_reflection_service.observe_cognitive_started(event)
            elif event.type == "cognitive_task.failed":
                receipt = self._self_reflection_service.observe_cognitive_failed(event)
                recovery_taken = receipt.suppress_source_notice
            elif event.type in {
                "cognitive_task.completed",
                "cognitive_task.cancelled",
                "cognitive_task.interrupted",
                "cognitive_task.rejected",
            }:
                self._self_reflection_service.observe_cognitive_terminal(event)
        if recovery_taken:
            # 先收口原失败 Notice，再广播公开 Activity。这样广播失败时，重放事件即使
            # 被 Registry 吸收，也不会把普通失败提醒遗留在已接管的 Recovery 旁边。
            notice_event = replace(
                event,
                payload={**event.payload, "suppress_notice": True},
            )
        else:
            notice_event = event
        if notice_key is not None:
            self._pending_cognitive_notices[notice_key] = notice_event
        if recovery_taken and notice_key is not None:
            self._deliver_pending_cognitive_notice(notice_key)
        # 先让 Self-Reflection 释放来源快照，再广播公开 Activity。广播失败时事件会被
        # Drainer 隔离，不能因此把 Cognitive 材料租约永久留在主进程。
        try:
            await self._broadcast_activity(apply_result.projection)
        except Exception:
            if not recovery_taken and notice_key is not None:
                # Activity 快照广播失败不会抹掉已经成立的 Cognitive Notice。先把
                # Notice 交给可重放账本，再让上层按原事件身份重试公开快照。
                self._deliver_pending_cognitive_notice(notice_key)
            raise
        if recovery_taken:
            return
        if notice_key is not None:
            self._deliver_pending_cognitive_notice(notice_key)

    async def _handle_background_agent_started(self, event: RuntimeEvent) -> None:
        """更新后台任务中心，并为可交付任务建立公开结果卡身份。

        Args:
            event: `background_agent.started` 原始事件。
        """

        should_publish, ready_terminal = self._task_result_delivery.register_started(event)
        apply_result = self._apply_activity(event)
        if apply_result.disposition == "absorbed":
            for release_event in self._task_result_delivery.discard_activity(event):
                await self._publish_materials_ephemeral(release_event)
            return
        if apply_result.disposition == "changed":
            if should_publish and event.payload.get("deliverable") is True:
                public_started = public_background_agent_started(event)
                await self._publish_activity(public_started.to_control_event())
            await self._broadcast_activity(apply_result.projection)
            self._apply_activity_notice(event)
        if ready_terminal is not None:
            await self._deliver_task_result_terminal(ready_terminal)

    async def _handle_background_agent_terminal(self, event: RuntimeEvent) -> None:
        """先关闭后台任务中心，再按交付规则处理结果卡、提醒和材料。

        Args:
            event: 后台智能体成功、失败或取消终态。
        """

        if event.runtime_epoch is not None:
            await self._retry_task_result_materials(event.runtime_epoch, _clean_text(event.payload.get("activity_id")))
        before_start_terminal = (
            event.type in {"background_agent.failed", "background_agent.cancelled"}
            and event.payload.get("execution_phase") == "before_start"
        )
        # 启动前失败没有 started 可供 Activity Registry 先建立 active 槽位；
        # 先登记 TaskResult，才能让 acceptance + acknowledge 仍然交付失败结果卡。
        ready_terminal = (
            self._task_result_delivery.track_terminal(event)
            if before_start_terminal
            else None
        )
        apply_result = self._apply_activity(event)
        if apply_result.disposition == "absorbed":
            if ready_terminal is not None:
                # Notice 写入失败后的重放事件会被 Activity Registry 幂等吸收，
                # 但 TaskResult 刚重领的交付仍必须继续，不能被 duplicate 路径丢弃。
                await self._deliver_task_result_terminal(ready_terminal)
                return
            for release_event in self._task_result_delivery.discard_activity(event):
                await self._publish_materials_ephemeral(release_event)
            return
        if apply_result.disposition == "changed":
            await self._broadcast_activity(apply_result.projection)
        if ready_terminal is None:
            ready_terminal = self._task_result_delivery.track_terminal(event)
        if ready_terminal is not None:
            await self._deliver_task_result_terminal(ready_terminal)

    async def _deliver_task_result_terminal(self, delivery: TaskResultTerminalDelivery) -> None:
        """按“结果卡 → Notice → 材料释放”交付已具备资格的 TaskResult 终态。

        结果卡先于 Notice，确保前台说“任务完成”时用户已经能打开实际成果。
        Notice 成功后才关闭状态并释放材料；暂时写入失败会放弃 claim，交给 Drainer 重试。
        """

        if not self._task_result_delivery.is_delivery_current(delivery):
            # Runtime replacement 可能在上一次 await 期间关闭旧世代；领取凭据失效后，
            # 不能再让迟到协程写结果卡或建立终态 Notice。
            return
        broadcast_error: Exception | None = None
        if self._task_result_delivery.should_broadcast_result_card(delivery):
            assert delivery.public_event is not None
            # 先提交可重连快照。实时连接即使发送失败，用户重连后仍能看到成果卡。
            self._task_result_delivery.commit_result_card(delivery.public_event)
            try:
                await self._publish_activity(delivery.public_event.to_control_event())
            except Exception as exception:
                # Notice 只读取权威终态，不应被控制连接故障一起吞掉；异常稍后仍交给 Drainer 诊断。
                broadcast_error = exception
            else:
                self._task_result_delivery.mark_result_card_broadcasted(delivery)
        if not self._task_result_delivery.is_delivery_current(delivery):
            return
        try:
            self._apply_activity_notice(delivery.source_event)
        except Exception as exception:
            # Notice 是强事实的持久化入口。它没写入时不能退役 TaskResult，
            # 也不能释放用户仍可能通过结果卡访问的材料。
            self._task_result_delivery.abandon_terminal_delivery(delivery)
            raise ActivityTerminalSettlementRetryableError(
                f"TaskResult Notice 写入失败: {delivery.source_event.type}"
            ) from exception
        if not self._task_result_delivery.is_delivery_current(delivery):
            return
        try:
            release_events = self._task_result_delivery.complete_terminal_delivery(delivery)
        except Exception as exception:
            # Notice 已成功，重试只能继续释放材料，不能再开交付资格或重复写入通知。
            raise ActivityTerminalSettlementRetryableError("TaskResult 材料释放失败") from exception
        for release_event in release_events:
            await self._publish_materials_ephemeral(release_event)
        if broadcast_error is not None:
            raise broadcast_error

    async def _retry_task_result_materials(self, runtime_epoch: int, activity_id: str) -> None:
        """在来源事件重放时继续材料收口，并把失败交回队列的退避重试通道。

        Args:
            runtime_epoch: 当前事件所属助手世代。
            activity_id: 仅重试当前事件或发起轮次所拥有的任务。
        """
        try:
            release_events = self._task_result_delivery.retry_material_release(runtime_epoch, activity_id)
        except Exception as exception:
            raise ActivityTerminalSettlementRetryableError("TaskResult 材料释放重试失败") from exception
        for release_event in release_events:
            await self._publish_materials_ephemeral(release_event)

    def _apply_activity(self, event: RuntimeEvent) -> ActivityApplyResult:
        """把来源事件同步应用到 Activity Registry，并返回明确的幂等结果。

        没有运行时世代的事件无法安全归属到 Registry，按 absorbed 处理；测试未注入
        Registry 端口时返回最小 changed 结果，以保持处理器可独立构造。
        """

        if self._activity_apply_handler is None:
            return ActivityApplyResult("changed", ActivityProjection(revision=0))
        if event.runtime_epoch is None:
            return ActivityApplyResult("absorbed")
        return self._activity_apply_handler(event.type, event.payload, event.runtime_epoch)

    def _apply_activity_notice(self, event: RuntimeEvent) -> None:
        """把可归属世代的活动事实交给当前媒体会话处理。

        此方法在生产环境不直接调用 Coordinator：注入的是媒体会话包装器。
        ``permission_granted`` 由媒体会话直接消费；其它事件再由包装器调用请求上下文
        准备器完成来源事件映射，并根据 Coordinator 的 supersession 结果取消过期请求。
        本类不复制媒体确认、映射、去重、批次或请求取消规则。
        """

        if self._activity_notice_handler is None or event.runtime_epoch is None:
            return
        # 走 LiveKitMediaSession._handle_activity_event；这里的职责到“成功交出活动事实”为止。
        self._activity_notice_handler(event.type, event.payload, event.runtime_epoch)

    def _deliver_pending_cognitive_notice(
        self,
        notice_key: tuple[int, str, str],
    ) -> bool:
        """提交一条可重放 Cognitive Notice，并只在端口成功后删除记录。

        Args:
            notice_key: 运行时世代、Activity 编号和事件类型组成的稳定身份。
        """

        event = self._pending_cognitive_notices.get(notice_key)
        if event is None:
            return True
        try:
            self._apply_activity_notice(event)
        except Exception:
            # 普通事件异常会被 Drainer 隔离，不会自动回放。把同一事件身份安排到
            # 独立退避队列，既不重复写入成功 Notice，也不依赖来源事件重发。
            self._schedule_cognitive_notice_retry(notice_key)
            raise
        self._pending_cognitive_notices.pop(notice_key, None)
        self._cognitive_notice_retry_counts.pop(notice_key, None)
        self._scheduled_cognitive_notice_retries.discard(notice_key)
        return True

    def _schedule_cognitive_notice_retry(
        self,
        notice_key: tuple[int, str, str],
    ) -> None:
        """为尚未交付的 Cognitive Notice 安排独立退避重试。

        Args:
            notice_key: 运行时世代、Activity 编号和事件类型组成的稳定身份。
        """

        if notice_key in self._scheduled_cognitive_notice_retries:
            return
        self._scheduled_cognitive_notice_retries.add(notice_key)
        retry_count = self._cognitive_notice_retry_counts.get(notice_key, 0) + 1
        self._cognitive_notice_retry_counts[notice_key] = retry_count
        delay_seconds = min(0.05 * (2 ** min(retry_count - 1, 16)), 5.0)

        def retry() -> None:
            self._scheduled_cognitive_notice_retries.discard(notice_key)
            try:
                self._deliver_pending_cognitive_notice(notice_key)
            except Exception:
                # 本次失败已经重新安排下一次退避；不能让 loop callback 抛出未处理异常。
                logger.warning(
                    "Cognitive Notice 重试失败，将继续退避 event_key=%s retry_count=%s",
                    notice_key,
                    self._cognitive_notice_retry_counts.get(notice_key, retry_count),
                )

        asyncio.get_running_loop().call_later(delay_seconds, retry)

    async def _broadcast_activity(self, projection: ActivityProjection | None) -> None:
        """只在 Registry 确认公共事实变化后广播权威活动快照。"""

        if projection is None or self._activity_broadcast_handler is None:
            return
        await self._activity_broadcast_handler(projection)


def _cognitive_notice_key(event: RuntimeEvent) -> tuple[int, str, str] | None:
    """构造 Cognitive Notice 重放使用的稳定事件身份。

    Args:
        event: 已通过运行时世代门禁的 Cognitive 生命周期事件。

    Returns:
        完整身份；缺少世代或 Activity 编号时返回空。
    """

    runtime_epoch = event.runtime_epoch
    activity_id = event.payload.get("activity_id")
    if (
        type(runtime_epoch) is not int
        or runtime_epoch <= 0
        or not isinstance(activity_id, str)
        or not activity_id.strip()
    ):
        return None
    return runtime_epoch, activity_id.strip(), event.type
