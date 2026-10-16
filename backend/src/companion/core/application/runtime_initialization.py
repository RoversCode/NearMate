"""串行装配运行时、公开初始化进度，并收回未完成的清理资源。"""

import asyncio
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from functools import partial
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from companion.observability.logging_config import get_backend_logger
from companion.core.application.runtime_initialization_state import RuntimeInitializationSnapshot, PHASE_MESSAGES, RuntimePhase

logger = get_backend_logger(__name__)


class SupersededInitialization(RuntimeError):
    """旧初始化已被后来的配置或退出取代，不能再发布候选或状态。"""


class RuntimeInitialization:
    """复用配置变更锁，防止旧运行时清理关闭新初始化的资源。

    Args:
        container: 当前 Backend 的资源所有者集合。
    """

    def __init__(self, container):
        """绑定应用资源和唯一初始化状态，不在构造时加载模型。

        Args:
            container: 当前 Backend 的资源所有者集合。
        Returns:
            无返回值；后续通过受管任务装配资源。
        """
        self.container = container
        self.bootstrap = container.hub.bootstrap
        self.stopped = False
        self.pending_cleanup: dict[str, Callable] = {}
        self.cleanup_tasks: dict[str, asyncio.Task] = {}
        self.transition_lock = asyncio.Lock()
        self.monitor_wakeup = asyncio.Event()
        self.snapshot = RuntimeInitializationSnapshot.initial()
        self.bootstrap.runtime_status_provider = self.status_payload
        self._initialization_generation = 0
        self._requested_resume: tuple[int, str, bool] | None = None
        self._resume_task: asyncio.Task | None = None
        self._closing = False
        self._retirement_required = False
        container.runtime_reconfiguration.runtime_initialization = self

    def status_payload(self) -> dict[str, object]:
        """读取当前初始化快照，刷新页面不会另起一次恢复。

        Returns:
            HTTP 和控制事件共用的安全快照。
        """
        return self.snapshot.to_payload()

    @property
    def initialization_generation(self) -> int:
        """返回配置准备开始时应冻结的操作世代。

        Returns:
            当前操作世代，仅用于阻止在途旧配置覆盖新初始化操作。
        """
        return self._initialization_generation

    async def _publish_snapshot(self) -> None:
        """阶段广播失败只记诊断，已进入的初始化阶段仍保持有效。"""
        publisher = getattr(self.container.hub, "publisher", None)
        if publisher is None:
            return
        try:
            await publisher("application").publish_ephemeral(
                {"type": "runtime.initialization_changed", "payload": self.status_payload()}
            )
        except Exception:
            logger.warning("运行时阶段广播失败")

    async def _publish_application_status(self, *, force: bool = False) -> None:
        """状态投递失败只记诊断，不能改变已提交的准入与终态。"""
        broadcaster = getattr(self.container, "application_status", None)
        if broadcaster is None:
            return
        try:
            await broadcaster.publish_changed(force=force)
        except Exception:
            logger.warning("运行时应用状态广播失败")

    async def _broadcast_current_input_identity(self) -> None:
        """解除业务事件静音后补发输入身份，现有连接也能识别新世代。"""
        broadcast = getattr(self.container.realtime_input_runtime_owner,
                            "broadcast_current_runtime_identity", None)
        if broadcast is None:
            return
        try:
            await broadcast()
        except Exception:
            # 身份投递只负责同步消费者，不能把已经可运行的资源改报为失败。
            logger.warning("恢复后的输入运行时身份广播失败")

    def invalidate_initialization(self) -> None:
        """使旧候选失效，不能让迟到加载发布到新的初始化世代。

        Returns:
            无；正在运行的同步加载会自然结束并回滚，不能取消线程后丢失资源所有权。
        """
        self._initialization_generation += 1
        self._requested_resume = None

    async def start_resume(self, *, replace_current: bool = False) -> dict[str, object]:
        """登记唯一的后台初始化任务，重复点击重试不会重复加载模型。

        Args:
            replace_current: 是否先关闭当前运行时及其领域资源。
        Returns:
            当前初始化快照；已在初始化时返回同一个操作编号。
        """
        if self._closing:
            raise SupersededInitialization("Backend 正在退出")
        if self.snapshot.state == "initializing":
            return self.status_payload()
        self._initialization_generation += 1
        operation_id = str(uuid4())
        self._retirement_required = self._retirement_required or replace_current
        self._requested_resume = (self._initialization_generation, operation_id, self._retirement_required)
        # 先关闭准入，再交给事件循环加载，HTTP 回执不会打开半套资源。
        self.bootstrap.runtime_blocked = True
        self.container.hub.runtime_events_enabled = False
        self.snapshot = self.snapshot.changed(state="initializing", phase="cleanup", operation_id=operation_id)
        if self._resume_task is None or self._resume_task.done():
            self._resume_task = asyncio.create_task(self._run_requested_resume(), name="nearmate-runtime-initialization")
        self.monitor_wakeup.set()
        return self.status_payload()

    async def _run_requested_resume(self) -> None:
        """同一个受管任务串行消费最新恢复，旧操作不能覆盖新操作状态。"""
        while self._requested_resume is not None and not self._closing:
            generation, operation_id, replace_current = self._requested_resume

            report_phase = partial(self.report_initialization_phase,
                                   (generation, operation_id))

            try:
                await report_phase("cleanup")
                await self.resume(replace_current=replace_current, report_phase=report_phase)
                if generation != self._initialization_generation or self._closing:
                    continue
            except SupersededInitialization:
                # 只丢弃旧操作的公开结果；线程与候选资源仍由原事务完成回滚。
                if generation != self._initialization_generation:
                    continue
                self.snapshot = self.snapshot.changed(state="idle", phase="idle", operation_id=operation_id)
            except Exception:
                if generation != self._initialization_generation or self._closing:
                    continue
                cleanup_failed = bool(self.pending_cleanup)
                self.bootstrap.runtime_blocked = True
                self.container.hub.runtime_events_enabled = False
                self.snapshot = self.snapshot.changed(state="failed", phase="failed",
                    operation_id=operation_id, retryable=True,
                    message="旧运行时尚未关闭，请重试初始化。" if cleanup_failed else None)
                logger.error("运行时初始化失败 phase=%s", self.snapshot.phase)
            if generation == self._initialization_generation:
                self._requested_resume = None
                await self._publish_snapshot()
                await self._publish_application_status(force=True)

    async def begin_configuration_initialization(self, *, include_realtime_input: bool,
        expected_generation: int | None = None) -> tuple[int, str]:
        """首次语音确认或配置替换开始加载时接入同一遮罩状态。

        Args:
            include_realtime_input: 本次事务是否确实加载输入模型。
            expected_generation: 配置准备开始时冻结的世代，防止旧请求覆盖新初始化。
        Returns:
            操作世代与 UUID；后续阶段与结算须带回这份身份。
        """
        if self._closing or (expected_generation is not None and expected_generation != self._initialization_generation):
            raise SupersededInitialization("配置初始化已被新操作取代")
        if self.pending_cleanup or self.cleanup_tasks:
            from companion.core.application.errors import StateConflict
            raise StateConflict("旧运行时清理尚未完成，请先重试初始化")
        self.invalidate_initialization()
        operation_id = str(uuid4())
        token = (self._initialization_generation, operation_id)
        self.bootstrap.runtime_blocked = True
        self.container.hub.runtime_events_enabled = False
        self.snapshot = self.snapshot.changed(state="initializing",
            phase="input_models" if include_realtime_input else "assistant", operation_id=operation_id)
        await self._publish_snapshot()
        return token

    async def report_initialization_phase(self, token: tuple[int, str],
                                          phase: RuntimePhase) -> None:
        """候选发布前核对操作世代，再公开实际阶段。

        Args:
            token: 开始加载时冻结的操作世代与 UUID。
            phase: 已经实际进入的初始化阶段。
        Returns:
            无；过期操作抛出受控异常，由事务回滚资源。
        """
        generation, operation_id = token
        if generation != self._initialization_generation or self._closing:
            raise SupersededInitialization("初始化已被新操作取代")
        if phase == "idle":
            return
        if self.snapshot.phase != phase or self.snapshot.message != PHASE_MESSAGES[phase]:
            self.snapshot = self.snapshot.changed(state="initializing", phase=phase,
                                                   operation_id=operation_id)
        await self._publish_snapshot()
        if generation != self._initialization_generation or self._closing:
            raise SupersededInitialization("初始化已被新操作取代")

    async def finish_configuration_initialization(self, token: tuple[int, str],
                                                  *, success: bool) -> None:
        """配置事务提交或回滚后结算阶段，旧事务不能解除新初始化阻断。

        Args:
            token: 构造候选前冻结的操作身份。
            success: 事务是否已经提交并完成 readiness。
        Returns:
            无；只结算仍属于当前操作的状态。
        """
        generation, operation_id = token
        if generation != self._initialization_generation or self._closing:
            return
        if success:
            await self.report_initialization_phase(token, "media")
        snapshot = self.snapshot.changed(state="ready" if success else "failed",
            phase="ready" if success else "failed", operation_id=operation_id, retryable=not success)
        # 快照与业务门禁在同一事件循环提交段改变，慢状态连接不能制造提前派发窗口。
        self.snapshot = snapshot
        if success:
            self.bootstrap.runtime_blocked = False
            self.container.hub.runtime_events_enabled = True
            await self._broadcast_current_input_identity()
        await self._publish_snapshot()

    async def close_media_and_input(self) -> None:
        """先停媒体消费者，再释放输入模型，防止推理线程访问已释放资源。

        Returns:
            无返回值；关闭失败由调用方保留重试。
        """
        if self.container.livekit_session_service is not None:
            await self.container.livekit_session_service.shutdown()
        await run_in_threadpool(self.container.realtime_input_runtime_owner.shutdown)

    async def close_runtime_control_connections(self) -> None:
        """替换运行时只关闭业务连接，状态连接继续接收初始化进度。

        Returns:
            无返回值；最多等待一秒交回受管重试。
        """

        close = getattr(self.container.hub, "close_runtime_control_connections", None)
        if close is None:
            close = self.container.hub.close_control_connections
        await asyncio.wait_for(close(), timeout=1)

    async def close_resource(self, close: Callable) -> None:
        """在独立任务中执行资源自己的关闭端口。

        Args:
            close: 资源拥有者的同步或异步关闭方法。
        Returns:
            无返回值；同步和异步关闭共享同一完成边界。
        """
        result = close()
        if isawaitable(result):
            await result

    async def reconcile(self) -> None:
        """收口仍未完成的资源；单项失败保留重试，其他资源继续关闭。

        Returns:
            无返回值；未完成项目保留在清理账本。
        """
        async with self.transition_lock:
            await self._reconcile()

    async def _reconcile(self, *, retire_current: bool = False) -> None:
        """在配置锁内收口旧资源，失败项保留给下一次重试。"""
        container = self.container
        async with container.hub.configuration_mutation_lock:
            if not retire_current and not self.pending_cleanup:
                return
            if not self.stopped:
                # 先冻结运行事件，避免旧世代业务事实越过资源替换边界。
                container.hub.runtime_events_enabled = False
                await self._publish_application_status(force=True)
                self.stopped = True
                self.bootstrap.runtime_blocked = True
                reminder_service = getattr(container, "timed_reminder_service", None)
                clear_reminders = (
                    getattr(reminder_service, "clear_for_runtime_transition", None)
                    if reminder_service is not None
                    else None
                )
                self.pending_cleanup = {
                    "reminder_scheduler": container.timed_reminder_scheduler.shutdown,
                    "reminder_delivery": container.timed_reminder_delivery.shutdown,
                    "control_connections": self.close_runtime_control_connections,
                    "assistant": lambda: run_in_threadpool(container.assistant_process_supervisor.shutdown),
                    "media_and_input": self.close_media_and_input,
                    "pending_turns": container.turn_admission.clear_pending_for_runtime_transition,
                    **container.runtime_reconfiguration.runtime_cleanup_steps(),
                }
                if clear_reminders is not None:
                    self.pending_cleanup["timed_reminders"] = clear_reminders
                voice_setup = getattr(container, "tts_setup", None)
                if voice_setup is not None:
                    self.pending_cleanup["voice_setup"] = voice_setup.cancel_for_runtime_transition
            for name, close in tuple(self.pending_cleanup.items()):
                if name not in self.cleanup_tasks:
                    self.cleanup_tasks[name] = asyncio.create_task(self.close_resource(close))
            if not self.cleanup_tasks:
                return
            # 各资源独立关闭；线程任务未结束时保留引用，重试不能重复启动同一关闭。
            await asyncio.wait(self.cleanup_tasks.values(), timeout=0.05)
            for name, task in tuple(self.cleanup_tasks.items()):
                if not task.done():
                    continue
                del self.cleanup_tasks[name]
                try:
                    task.result()
                except Exception:
                    # 错误消息可能来自第三方资源；仅记录组件名，避免夹带密钥或用户正文。
                    logger.error("运行时资源清理失败，等待重试 component=%s", name)
                else:
                    del self.pending_cleanup[name]

    async def resume(self, *, replace_current: bool = False,
                     report_phase: Callable[[RuntimePhase], Awaitable[None]] | None = None) -> bool:
        """先完成旧资源清理，再让配置协调器在同一变更锁下重建运行时。

        Args:
            replace_current: 当前资源将被替换，必须先关闭旧运行时。
            report_phase: 在真实阶段边界检查操作世代并公开安全提示的端口。

        Returns:
            已配置运行时成功恢复时为真；尚须首次配置时为假。
        """
        async with self.transition_lock:
            if replace_current:
                # 清理失败时唤醒受管重试，不重复启动仍在执行的关闭线程。
                self.monitor_wakeup.set()
                if not self.stopped:
                    await self._reconcile(retire_current=True)
            if self.pending_cleanup:
                await self._reconcile()
            if self.pending_cleanup:
                raise RuntimeError("正在关闭旧运行时，请稍后重试。")
            self._retirement_required = False
            coordinator = self.container.runtime_reconfiguration
            if report_phase is None:
                restored = await coordinator.restore_runtime()
            else:
                restored = await coordinator.restore_runtime(report_phase=report_phase)
                await report_phase("media" if restored is not False else "idle")
            # 新运行时确认成功后才解除输入准入，避免恢复失败时出现“可接纳但无运行时”的窗口。
            resume_after_transition = getattr(
                self.container.turn_admission,
                "resume_after_runtime_transition",
                None,
            )
            if resume_after_transition is not None:
                resume_after_transition()
            # 先准备公开终态，再无 await 地一起提交准入；广播成败不拥有业务门禁。
            self.snapshot = self.snapshot.changed(state="ready" if restored is not False else "idle",
                phase="ready" if restored is not False else "idle", operation_id=self.snapshot.operation_id)
            self.stopped = False
            self.bootstrap.runtime_blocked = False
            self.container.hub.runtime_events_enabled = True
            if self.bootstrap.status_payload()["is_initialized"]:
                self.container.timed_reminder_delivery.start()
                self.container.timed_reminder_scheduler.start()
            await self._broadcast_current_input_identity()
            await self._publish_snapshot()
            await self._publish_application_status(force=True)
            await self._reconcile()
            # 若仍有清理项，唤醒资源重试任务。
            self.monitor_wakeup.set()
            return restored is not False

    async def drain_cleanup(self) -> None:
        """原生命周期关闭前等待已启动清理，线程中的关闭不能靠取消协程停止。

        Returns:
            无返回值；所有已启动资源任务交回后才能退出。
        """
        self._closing = True
        self.bootstrap.runtime_blocked = True
        self.container.hub.runtime_events_enabled = False
        self.invalidate_initialization()
        if self._resume_task is not None:
            await self._resume_task
        async with self.transition_lock:
            if self.cleanup_tasks:
                await asyncio.gather(*self.cleanup_tasks.values(), return_exceptions=True)
                self.cleanup_tasks.clear()

    async def monitor(self) -> None:
        """只在有清理工作时重试资源关闭，不再检查外部产品资格。

        Returns:
            生命周期取消监控任务时结束。
        """
        while True:
            if self.pending_cleanup:
                try:
                    await self.reconcile()
                except Exception:
                    logger.error("运行时清理重试失败")
            try:
                await asyncio.wait_for(self.monitor_wakeup.wait(), timeout=5 if self.pending_cleanup else None)
            except asyncio.TimeoutError:
                pass
            self.monitor_wakeup.clear()


def install_runtime_initialization(app, container) -> None:
    """安装初始化状态接口和生命周期，重资源只由唯一受管任务装配。

    Args:
        app: 注册接口与生命周期的 FastAPI 应用。
        container: 持有 Backend 服务和资源所有者的应用容器。
    Returns:
        无返回值；初始化所有者保存到 app.state 供诊断使用。
    """
    from contextlib import asynccontextmanager, suppress
    from fastapi import Request
    from fastapi.responses import JSONResponse

    runtime = RuntimeInitialization(container)
    app.state.runtime_initialization = runtime
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        """先启动控制服务，再异步装配运行时；退出先收回初始化任务。"""
        async with original_lifespan(application):
            await runtime.start_resume()
            monitor = asyncio.create_task(runtime.monitor(), name="nearmate-runtime-cleanup")
            try:
                yield
            finally:
                try:
                    # 关闭准入必须发生在第一个 await 前，防止监控取消期间迟到加载重新开放业务。
                    await runtime.drain_cleanup()
                finally:
                    monitor.cancel()
                    with suppress(asyncio.CancelledError):
                        await monitor
    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def gate(request: Request, call_next):
        """资源尚未就绪时保留配置和诊断入口，业务请求保持阻断。"""
        path = request.url.path
        allowed = (path.startswith(("/api/runtime/", "/api/bootstrap/", "/api/llm/", "/api/settings", "/console"))
                   or path in {"/healthz", "/api/runtime-logs"} or request.method == "OPTIONS")
        registered = any(getattr(route, "path_regex", None) is not None and route.path_regex.fullmatch(path)
                         for route in app.router.routes)
        if registered and path.startswith("/api/") and not allowed and runtime.bootstrap.runtime_blocked:
            return JSONResponse({"detail": runtime.snapshot.message,
                "code": "runtime_initializing" if runtime.snapshot.state == "initializing" else "runtime_unavailable",
                "initialization": runtime.status_payload()}, status_code=503)
        return await call_next(request)

    @app.get("/api/runtime/initialization")
    async def initialization_status():
        """读取唯一初始化快照，轮询和重连不会重复加载资源。

        Returns:
            与 runtime.initialization_changed 相同的七字段快照。
        """
        return runtime.status_payload()

    @app.post("/api/runtime/initialization/retry")
    async def retry_initialization():
        """重试本机运行时初始化，进行中的操作保持原编号。

        Returns:
            当前或新登记的初始化操作快照。
        """
        return await runtime.start_resume()
