from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.asyncio_exception_policy import install_backend_asyncio_exception_policy
from companion.background_agent.usage_service import BackgroundAgentUsageService
from companion.core.broadcast import BroadcastHub
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.observability.logging_config import get_backend_logger
from companion.realtime.runtime_event_service import RuntimeEventService
from companion.timed_reminders.delivery_coordinator import (
    TimedReminderDeliveryCoordinator,
)
from companion.timed_reminders.scheduler import TimedReminderScheduler
from companion.core.tts_setup_service import TtsSetupService
from companion.core.application.status_broadcaster import ApplicationStatusBroadcaster


logger = get_backend_logger(__name__)


class ApplicationLifecycle:
    """管理控制服务启动预热与运行时资源关闭顺序。

    Args:
        hub: 控制 WebSocket 连接和启动状态持有者。
        realtime_input_runtime_owner: 实时输入运行时所有者。
        assistant_process_supervisor: 助手子进程所有者。
        livekit_session_service: 可选 LiveKit 会话服务。
        background_agent_usage_service: 负责后台工作用量仓储启停和队列排空的服务。
        runtime_event_service: Backend 进程唯一的运行时事件消费者。
        tts_setup: 应用容器拥有的语音配置任务服务，退出时关闭工作进程。
        application_status: 可选的初始化与 Bootstrap 状态广播器。
    """

    def __init__(
        self,
        *,
        hub: BroadcastHub,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
        assistant_process_supervisor: AssistantProcessSupervisor,
        livekit_session_service: object | None,
        background_agent_usage_service: BackgroundAgentUsageService,
        runtime_event_service: RuntimeEventService,
        timed_reminder_delivery: TimedReminderDeliveryCoordinator,
        timed_reminder_scheduler: TimedReminderScheduler,
        tts_setup: TtsSetupService | None = None,
        application_status: ApplicationStatusBroadcaster | None = None,
    ) -> None:
        self.hub = hub
        self.realtime_input_runtime_owner = realtime_input_runtime_owner
        self.assistant_process_supervisor = assistant_process_supervisor
        self.livekit_session_service = livekit_session_service
        self.background_agent_usage_service = background_agent_usage_service
        self.runtime_event_service = runtime_event_service
        self.timed_reminder_delivery = timed_reminder_delivery
        self.timed_reminder_scheduler = timed_reminder_scheduler
        self.tts_setup = tts_setup
        self.application_status = application_status
        self._restore_asyncio_exception_policy: Callable[[], None] | None = None

    @asynccontextmanager
    async def __call__(self, app: object) -> AsyncIterator[None]:
        """作为 FastAPI lifespan 执行启动和关闭流程。"""

        _ = app
        try:
            self._restore_asyncio_exception_policy = install_backend_asyncio_exception_policy()
            # startup 可能在用量 worker 启动后失败；清理保护必须覆盖启动本身，
            # 否则 FastAPI 尚未进入 lifespan 时就会遗留线程和执行器。
            await self.startup()
            yield
        finally:
            try:
                await self.shutdown()
            finally:
                # 即使关闭阶段某个资源收口失败，也不能把本次生命周期安装的
                # 异常处理器遗留在事件循环里，避免污染后续测试或重启。
                if self._restore_asyncio_exception_policy is not None:
                    self._restore_asyncio_exception_policy()
                    self._restore_asyncio_exception_policy = None

    async def startup(self) -> None:
        """启动助手运行时并尽力预热 LiveKit Backend 媒体会话。"""

        # 用量旁路必须先就绪，随后启动的助手终态才能立即进入非阻塞持久化队列。
        await self.background_agent_usage_service.startup()
        await self.runtime_event_service.start()
        self.assistant_process_supervisor.start_if_initialized()
        wait_until_ready = getattr(self.assistant_process_supervisor, "wait_until_ready", None)
        if callable(wait_until_ready):
            try:
                await asyncio.to_thread(wait_until_ready)
            except Exception:
                # 控制面仍需保持可诊断；运行时失败由 StreamVox/业务接口返回 503，
                # 不能让整个 Backend 因一个输出子进程启动失败而失去初始化和设置页面。
                logger.exception("助手运行时启动未完成，控制面将保持可用")
        if self.application_status is not None:
            await self.application_status.publish_changed(force=True)
        if not self.hub.bootstrap.status_payload()["is_initialized"]:
            return
        self.timed_reminder_delivery.start()
        self.timed_reminder_scheduler.start()

        # Backend 媒体会话使用独立 token 加入固定 LiveKit room。
        warm_livekit_media_session = getattr(
            self.livekit_session_service,
            "ensure_backend_media_session_started",
            None,
        )
        if not callable(warm_livekit_media_session):
            logger.warning(
                "LiveKit backend media session warmup is unavailable; "
                "the session API must start it."
            )
            return

        # 控制面启动优先于媒体预热，预热失败只记录告警。
        with suppress(Exception):
            await warm_livekit_media_session()
            return
        logger.warning(
            "LiveKit backend media session warmup failed during startup; "
            "desktop audio may stay disconnected until session is recreated."
        )

    async def shutdown(self) -> None:
        """按依赖方向逆序释放控制连接和运行时资源。"""

        # 先停止绝对时刻领取，关闭期间不能再产生新的 Foreground takeover。
        await self.timed_reminder_scheduler.shutdown()
        # 助手与媒体输出仍可接收请求级取消时，先释放 Reminder reservation 并结算
        # 已经入队的响铃尝试。反过来先停子进程会让取消命令失去接收方。
        await self.timed_reminder_delivery.release_active_requests()

        try:
            await self.hub.close_control_connections()
        except Exception as exception:
            logger.warning("Control connection shutdown failed: %s", exception)

        await self.runtime_event_service.drain_once()

        try:
            self.assistant_process_supervisor.shutdown()
        except Exception as exception:
            logger.warning("Assistant process shutdown failed: %s", exception)

        await self.runtime_event_service.drain_once()

        shutdown_livekit = getattr(self.livekit_session_service, "shutdown", None)
        if callable(shutdown_livekit):
            try:
                await shutdown_livekit()
            except Exception as exception:
                logger.warning("LiveKit session service shutdown failed: %s", exception)

        await self.runtime_event_service.stop()
        await self.timed_reminder_delivery.shutdown()

        try:
            self.realtime_input_runtime_owner.shutdown()
        except Exception as exception:
            logger.warning("Realtime input runtime shutdown failed: %s", exception)
        # 助手和输入运行时都停止后不再产生新终态，此时可以安全排空用量队列。
        await self.background_agent_usage_service.shutdown()
        if self.tts_setup is not None:
            self.tts_setup.shutdown()
