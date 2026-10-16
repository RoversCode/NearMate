"""在专用线程和事件循环中驱动 Claude Agent SDK。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import aclosing
from concurrent.futures import Future, InvalidStateError
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    Message,
    ResultMessage,
    SystemMessage,
    UserMessage,
)
from claude_agent_sdk.types import (
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from companion.background_agent.contracts import (
    AgentCompletionPayload,
    AgentEvent,
    AgentEventType,
    AgentTask,
)
from companion.background_agent.cua_capability import CuaCapabilitySlot
from companion.background_agent.cua_integration import CUA_SKILL_ID, CuaIntegration
from companion.background_agent.agent_session import (
    AgentSession,
    AgentSessionManager,
    AgentSessionResumeIdentity,
    AgentSessionTurn,
)
from companion.background_agent.interaction_bridge import (
    AgentInteractionAnswer,
    AgentInteractionRequest,
    InteractionBridge,
)
from companion.background_agent.material_access import AgentSessionMaterialCapability
from companion.background_agent.model_config import AgentExecutionConfig
from companion.background_agent.options_factory import build_agent_options
from companion.background_agent.permission_gate import (
    ActivityPermissionGate,
    PermissionResolutionOutcome,
)
from companion.background_agent.persona import ensure_persona_file
from companion.background_agent.task_prompt import render_agent_task_prompt
from companion.background_agent.prompt_audit import BackgroundPromptAuditDispatcher
from companion.background_agent.usage_collector import AgentTaskUsageCollector
from companion.background_agent.workspace import WorkspaceManager
from companion.background_agent.work_plan_mirror import AgentWorkPlanMirror
from companion.observability.logging_config import get_backend_logger
from companion.observability.prompt_audit import PromptAuditRecorder


logger = get_backend_logger(__name__)


_AgentTerminalEventType = Literal["completed", "failed", "cancelled"]
_TerminalDiagnosticKind = Literal[
    "thinking",
    "assistant",
    "tool",
    "tool.result",
    "result",
]


def _print_terminal_diagnostic(text: str) -> None:
    """把开发诊断安全写入当前终端，不让字符编码故障中断任务。

    Windows 原生应用标题可能包含当前控制台代码页无法表示的字符。开发诊断只负责
    帮人观察执行过程，因此这类字符使用反斜杠转义展示，真实 SDK 消息保持原样继续
    交给 Agent 消费。

    Args:
        text: 已经拼好 Activity 前缀的一行开发诊断。

    Returns:
        无返回值；终端不可写时只记录调试日志，不改变 Activity 状态。
    """

    output_stream = sys.stdout
    if output_stream is None:
        return
    try:
        output_encoding = output_stream.encoding or "utf-8"
        printable_text = text.encode(
            output_encoding,
            errors="backslashreplace",
        ).decode(output_encoding)
        print(printable_text, file=output_stream, flush=True)
    except Exception:
        # 开发终端不是任务协议的一部分。即使终端已经关闭，也不能反向打断 Agent。
        logger.debug("后台 Agent 开发诊断无法写入当前终端", exc_info=True)


async def _await_cleanup_despite_outer_cancellation(
    cleanup: Awaitable[None],
) -> asyncio.CancelledError | None:
    """即使 Runner 被取消，也等待已经开始的清理步骤真实结束。

    `asyncio.to_thread()` 被取消时只会停止协程等待，底层线程仍会继续运行。这里用
    shield 保住实际清理任务，并记住外层取消事实，等清理完成后再交给调用方传播。

    Args:
        cleanup: 已经创建、必须真实结束后才能释放后续资源的异步清理步骤。

    Returns:
        等待期间收到的第一项外层取消；没有取消时返回空。

    Raises:
        BaseException: 清理步骤自身失败时原样抛出。
    """

    cleanup_task = asyncio.ensure_future(cleanup)
    outer_cancellation: asyncio.CancelledError | None = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exception:
            if cleanup_task.cancelled():
                raise
            outer_cancellation = outer_cancellation or exception
    cleanup_task.result()
    return outer_cancellation


class _SDKSessionIdentityError(RuntimeError):
    """表示 SDK 消息流没有证明任务绑定到了预期 Claude 会话。"""


class AgentTaskRunner:
    """管理 V1 单任务槽位和 ClaudeSDKClient 生命周期。

    Args:
        runtime_dir: Agent 私有配置和 Persona 文件目录。
        workspace_manager: 默认工作区管理器。
        event_callback: 把 Runner 事件送往 Activity 执行器的回调。
        slot_released_callback: 响应流和交互资源完整清理后的通知回调。
        thread_name: Runner 专属事件循环线程名。
        prompt_audit_recorder: 可选本地 Prompt 审查记录器；为空时后台任务不进入审计。
        cua_integration: 官方 Cua Driver 安装、doctor 与 stdio 配置入口。
        cua_capability: 两条 Background lane 共用的全局 CUA Capability Slot。
        terminal_diagnostics_enabled: 是否把 SDK 可读文字和 CLI stderr 打印到开发终端。
    """

    def __init__(
        self,
        *,
        runtime_dir: Path,
        workspace_manager: WorkspaceManager,
        event_callback: Callable[[AgentEvent], None],
        slot_released_callback: Callable[[str], None] | None = None,
        cua_grant_finalizer: Callable[[str], None] | None = None,
        thread_name: str = "background-agent-sdk-loop",
        prompt_audit_recorder: PromptAuditRecorder | None = None,
        cua_integration: CuaIntegration | None = None,
        cua_capability: CuaCapabilitySlot | None = None,
        terminal_diagnostics_enabled: bool = False,
    ) -> None:
        """初始化后台智能体 Runner。

        Args:
            runtime_dir: Agent 私有配置和 system prompt 文件目录。
            workspace_manager: 默认工作区管理器。
            event_callback: 领域事件回调。
            slot_released_callback: Runner 身份完整释放后的通知回调。
            thread_name: 诊断时用于区分不同 Runner 的线程名。
            prompt_audit_recorder: 可选本地 Prompt 审查记录器。
            cua_integration: 官方 Cua Driver 安装、doctor 与 stdio 配置入口。
            cua_capability: 两条 Background lane 共用的全局 CUA Capability Slot。
            terminal_diagnostics_enabled: 是否把 SDK 可读文字和 CLI stderr 打印到开发终端。

        Raises:
            ValueError: thread_name 为空。
        """

        normalized_thread_name = thread_name.strip()
        if not normalized_thread_name:
            raise ValueError("Runner thread_name 不能为空")

        self._runtime_dir = runtime_dir.resolve()
        self._workspace_manager = workspace_manager
        self._event_callback = event_callback
        self._slot_released_callback = slot_released_callback
        self._cua_grant_finalizer = cua_grant_finalizer
        self._cua_integration = cua_integration
        self._cua_capability = cua_capability
        self._terminal_diagnostics_enabled = terminal_diagnostics_enabled
        self._lock = threading.RLock()
        self._current_task: AgentTask | None = None
        self._current_bridge: InteractionBridge | None = None
        self._current_material_access: AgentSessionMaterialCapability | None = None
        self._current_client: ClaudeSDKClient | None = None
        self._current_client_connected = False
        self._current_session: AgentSession | None = None
        self._current_sdk_session_identity_confirmed = False
        # 通过运行时名称解析 Client，测试替身和未来的 transport 工厂才能沿用同一入口。
        self._session_manager = AgentSessionManager(
            client_factory=lambda options: ClaudeSDKClient(options),
            query_dispatcher=BackgroundPromptAuditDispatcher(prompt_audit_recorder),
        )
        self._activity_sessions: dict[str, str] = {}
        self._current_future: Future[None] | None = None
        self._current_interrupt_futures: set[Future[None]] = set()
        self._current_usage_collector: AgentTaskUsageCollector | None = None
        self._current_started_emitted = False
        self._current_terminal_emitted = False
        self._current_terminal_event: threading.Event | None = None
        self._current_cleanup_event: threading.Event | None = None
        self._shutdown_complete_event = threading.Event()
        self._shutdown_monitor_started = False
        self._cancel_requested_activity_id: str | None = None
        self._cancel_requested_reason: str | None = None
        self._current_turn_output_index = 0
        self._shutdown = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=normalized_thread_name,
            daemon=True,
        )
        self._thread.start()

    def start(self, task: AgentTask, execution_config: AgentExecutionConfig) -> bool:
        """占用单任务槽位并异步启动 SDK Agent。

        Args:
            task: 已冻结的后台任务。
            execution_config: 从可信用户设置冻结的本任务 SDK 配置。

        Returns:
            成功占用槽位并提交运行协程时返回真；已有任务或已关闭时返回假。
        """

        with self._lock:
            if self._shutdown or self._current_task is not None:
                return False
            self._current_task = task
            self._current_usage_collector = None
            self._current_started_emitted = False
            self._current_terminal_emitted = False
            self._current_terminal_event = threading.Event()
            self._current_cleanup_event = threading.Event()
            self._cancel_requested_activity_id = None
            self._cancel_requested_reason = None
            self._current_turn_output_index = 0
            self._current_sdk_session_identity_confirmed = False
            task_coroutine = self._run_task(task, execution_config)
            try:
                self._current_future = asyncio.run_coroutine_threadsafe(
                    task_coroutine,
                    self._loop,
                )
            except Exception:
                # 事件循环已经停止时，协程尚未获得执行机会；显式关闭协程并释放
                # Runner 身份，让 Executor 把已提交任务补偿为失败终态。
                task_coroutine.close()
                cleanup_event = self._current_cleanup_event
                self._current_task = None
                self._current_bridge = None
                self._current_material_access = None
                self._current_client = None
                self._current_client_connected = False
                self._current_sdk_session_identity_confirmed = False
                self._current_future = None
                self._current_usage_collector = None
                self._current_started_emitted = False
                self._current_terminal_emitted = False
                self._current_terminal_event = None
                self._current_cleanup_event = None
                self._cancel_requested_activity_id = None
                self._cancel_requested_reason = None
                if cleanup_event is not None:
                    cleanup_event.set()
                logger.exception(
                    "后台智能体协程提交失败 activity_id=%s",
                    task.activity_id,
                )
                return False
        return True

    def cancel(self, activity_id: str, *, reason: str = "agent_interrupted") -> bool:
        """请求当前任务通过 SDK interrupt 取消。

        Args:
            activity_id: 需要取消的 Activity 编号。
            reason: 写入取消终态的稳定原因码。

        Returns:
            当前任务身份匹配且已安排取消时返回真。
        """

        interrupt_release: Future[None] | None = None
        with self._lock:
            task = self._current_task
            if (
                self._shutdown
                or task is None
                or task.activity_id != activity_id.strip()
                or self._current_terminal_emitted
            ):
                return False
            previous_cancel_activity_id = self._cancel_requested_activity_id
            previous_cancel_reason = self._cancel_requested_reason
            cancellation_reason = self._freeze_cancellation_reason_locked(task, reason)
            client = self._current_client
            client_connected = self._current_client_connected
            session = self._current_session
            bridge = self._current_bridge
            if client is not None and client_connected:
                interrupt_release = Future()

                async def interrupt_after_interactions_close() -> None:
                    """等待待答卡片先收口，再把 interrupt 交给当前 SDK Turn。"""

                    await asyncio.wrap_future(interrupt_release)
                    if session is not None:
                        await session.interrupt(activity_id=task.activity_id)
                    else:
                        await client.interrupt()

                interrupt_coroutine = interrupt_after_interactions_close()
                try:
                    scheduled_interrupt = asyncio.run_coroutine_threadsafe(
                        interrupt_coroutine,
                        self._loop,
                    )
                    self._current_interrupt_futures.add(scheduled_interrupt)

                    def forget_interrupt(
                        completed_future: Future[None],
                    ) -> None:
                        """移除已经结束的 SDK interrupt 控制任务。

                        Args:
                            completed_future: 已成功、失败或取消的 interrupt Future。

                        Returns:
                            无返回值；集合只保留仍可能占用 Session 控制锁的任务。
                        """

                        with self._lock:
                            self._current_interrupt_futures.discard(completed_future)

                    scheduled_interrupt.add_done_callback(forget_interrupt)
                except Exception:
                    # 调度入口拒绝时协程从未获得执行机会。关闭它并回滚首次取消身份，
                    # 外层 Executor 才能把 A 恢复成真实的旧状态继续管理。
                    interrupt_coroutine.close()
                    self._cancel_requested_activity_id = previous_cancel_activity_id
                    self._cancel_requested_reason = previous_cancel_reason
                    raise
        # SDK interrupt 可能直接取消 can_use_tool 协程，InteractionBridge 的 finally
        # 随即会移除 Future。先关闭桥并发布 resolved，主进程账本和 UI 卡片才不会
        # 因为拿不到 request_id 而永久停在 waiting_user。
        try:
            if bridge is not None:
                self._close_interactions(
                    task,
                    bridge,
                    (
                        "用户纠正了后台任务目标，旧任务已停止。"
                        if cancellation_reason == "user_correction"
                        else "用户取消了后台任务。"
                    ),
                )
        except Exception:
            # interrupt 已经成功排队，取消事实不能再回滚。交互桥自身会在 finally
            # 恢复 Future，这里只隔离事件出口异常，避免外层误把 A 改回 running。
            logger.exception(
                "后台任务取消时收口待答交互失败 activity_id=%s",
                task.activity_id,
            )
        finally:
            if interrupt_release is not None:
                try:
                    interrupt_release.set_result(None)
                except InvalidStateError:
                    # shutdown 可能同时取消等待门的协程，并把并发 Future 标成已结束。
                    # interrupt 已被事件循环接纳后不能因此向外抛错或回滚取消身份。
                    logger.exception(
                        "后台任务取消门释放失败 activity_id=%s",
                        task.activity_id,
                    )
        # 普通 connect 之前只冻结取消意图；SDK client 真正连上 CLI 后由执行协程补一次 interrupt。
        return True

    def is_available(self) -> bool:
        """判断 Runner 是否已经完成上一项任务的全部 SDK 清理。

        Returns:
            未关闭且当前没有运行或清理中的任务时返回真。
        """

        with self._lock:
            return not self._shutdown and self._current_task is None

    def resume_identity_for_activity(
        self,
        activity_id: str,
    ) -> AgentSessionResumeIdentity | None:
        """冻结一项活跃后台任务当前可证明的 SDK 会话恢复身份。

        Args:
            activity_id: 用户准备纠正的父 Activity 编号。

        Returns:
            产品会话、SDK 会话和工作区均可确认时返回身份；否则返回空。
        """

        normalized_activity_id = activity_id.strip()
        with self._lock:
            task = self._current_task
            session = self._current_session
            registered_session_id = self._activity_sessions.get(
                normalized_activity_id
            )
            if (
                task is None
                or task.activity_id != normalized_activity_id
                or session is None
                or not self._current_sdk_session_identity_confirmed
                or registered_session_id != session.agent_session_id
            ):
                return None
        return session.resume_identity()

    def wait_until_available(self, timeout: float = 5.0) -> bool:
        """等待当前 query 终态和 Turn 清理完成，但保留 AgentSession。

        Args:
            timeout: 最长等待秒数。

        Returns:
            Runner 已释放任务槽且仍可接单时返回真。
        """

        with self._lock:
            cleanup_event = self._current_cleanup_event
            already_available = not self._shutdown and self._current_task is None
        if already_available:
            return True
        if cleanup_event is None:
            return False
        cleanup_event.wait(max(timeout, 0.0))
        return self.is_available()

    def answer_interaction(
        self,
        answer: AgentInteractionAnswer,
        *,
        before_resolve: Callable[[], None] | None = None,
    ) -> bool:
        """把主进程回答送回当前 SDK 回调。

        Args:
            answer: 主进程校验后的回答。
            before_resolve: 确认请求仍有效后、恢复 SDK Future 前执行的动作。

        Returns:
            当前存在对应交互桥时返回真。
        """

        with self._lock:
            bridge = self._current_bridge
            task = self._current_task
        if bridge is None or task is None:
            return False
        def freeze_cancel_before_resume() -> None:
            """在 SDK Future 恢复前冻结问题卡产生的取消事实。

            Returns:
                无。
            """

            if answer.outcome != "cancel":
                if before_resolve is not None:
                    before_resolve()
                return
            with self._lock:
                if self._current_task is task:
                    self._freeze_cancellation_reason_locked(
                        task,
                        "agent_interrupted",
                    )
            if before_resolve is not None:
                before_resolve()

        return bridge.answer_from_runtime_thread(
            answer,
            before_resolve=freeze_cancel_before_resume,
        )

    def answer_question(
        self,
        answer: AgentInteractionAnswer,
    ) -> bool:
        """先扩展当前会话材料访问，再恢复资料问题 Future。

        Args:
            answer: 已由 Foreground 明确选择的资料答案。
        Returns:
            当前任务和资料问题仍然有效并成功安排恢复时返回真。
        """

        with self._lock:
            session = self._current_session
            task = self._current_task
        if session is None or task is None or answer.outcome != "answer":
            return False
        return self.answer_interaction(
            answer,
            before_resolve=lambda: session.extend_materials(answer.materials),
        )

    def shutdown(self, timeout: float = 1.0) -> None:
        """关闭当前 Agent、解决挂起交互并停止专用事件循环。

        Args:
            timeout: 等待 Runner 线程退出的最长秒数。

        Returns:
            无。
        """

        normalized_timeout = max(timeout, 0.0)
        deadline = monotonic() + normalized_timeout
        with self._lock:
            first_shutdown_request = not self._shutdown
            if first_shutdown_request:
                self._shutdown = True
            task = self._current_task
            bridge = self._current_bridge
            interrupt_futures = tuple(self._current_interrupt_futures)
        if not first_shutdown_request:
            self.wait_until_shutdown(max(0.0, deadline - monotonic()))
            return
        if bridge is not None and task is not None:
            self._close_interactions(task, bridge, "助手运行时正在关闭。")
        if task is not None:
            with self._lock:
                self._freeze_cancellation_reason_locked(task, "agent_cancelled")
        with self._lock:
            future = self._current_future
            terminal_event = self._current_terminal_event
            cleanup_event = self._current_cleanup_event
        # 进程关闭比普通用户取消更强：已经排队但不返回的 interrupt 可能占着
        # Session 控制锁，必须先取消；随后直接关闭 transport，不能再等待 Provider
        # 主动结束响应流。
        for interrupt_future in interrupt_futures:
            interrupt_future.cancel()
        if future is not None and not future.done():
            future.cancel()
        try:
            disconnect_future = asyncio.run_coroutine_threadsafe(
                self._session_manager.disconnect_all(
                    timeout=max(0.0, deadline - monotonic())
                ),
                self._loop,
            )
            disconnect_future.result(max(0.0, deadline - monotonic()))
        except Exception:
            logger.exception("关闭 AgentSession 失败")
        # transport 断开后，响应流才能退出并让 Runner 形成带完整当前用量的取消终态。
        # 第三方 SDK 若仍不返回，预算到期时同步补齐去内容化终态。
        if terminal_event is not None:
            terminal_event.wait(max(0.0, deadline - monotonic()))
        if task is not None and (terminal_event is None or not terminal_event.is_set()):
            with self._lock:
                usage_collector = self._current_usage_collector
            self._emit_terminal_once(
                task,
                "cancelled",
                usage_collector=usage_collector,
                session_id=None,
                reason="agent_cancelled",
            )
        # 终态和任务身份释放是两个独立证明。transport 已经尽力断开后，再用剩余
        # 预算等待 Browser session、CUA Grant 和 Runner 槽位按 finally 顺序收口。
        if cleanup_event is not None:
            cleanup_event.wait(max(0.0, deadline - monotonic()))
        self._stop_loop_after_cleanup(cleanup_event)
        self.wait_until_shutdown(max(0.0, deadline - monotonic()))

    def wait_until_shutdown(self, timeout: float | None = None) -> bool:
        """等待 Runner 线程和它已经启动的资源清理真实结束。

        Args:
            timeout: 最长等待秒数；为空时一直等到专用线程退出。

        Returns:
            Runner 专用线程已经关闭时返回真。
        """

        normalized_timeout = None if timeout is None else max(timeout, 0.0)
        return self._shutdown_complete_event.wait(normalized_timeout)

    def _stop_loop_after_cleanup(
        self,
        cleanup_event: threading.Event | None,
    ) -> None:
        """在当前 Activity 的真实 finally 完成后停止 Runner 事件循环。

        Args:
            cleanup_event: 当前任务由 `_release_task()` 设置的清理完成信号。

        Returns:
            无返回值；有未完成任务时由一个守护线程延后停止事件循环。
        """

        if cleanup_event is None or cleanup_event.is_set():
            self._loop.call_soon_threadsafe(self._loop.stop)
            return
        with self._lock:
            if self._shutdown_monitor_started:
                return
            self._shutdown_monitor_started = True

        def stop_after_cleanup() -> None:
            """等待真实清理信号，再请求所属事件循环退出。"""

            cleanup_event.wait()
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                # 其它关闭路径已经结束事件循环时，这个迟到唤醒只需幂等吸收。
                return

        threading.Thread(
            target=stop_after_cleanup,
            name=f"{self._thread.name}-shutdown-monitor",
            daemon=True,
        ).start()

    def _run_loop(self) -> None:
        """在线程中运行 SDK 专用事件循环。

        Returns:
            无。
        """

        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            try:
                self._loop.close()
            finally:
                self._shutdown_complete_event.set()

    async def _run_task(
        self,
        task: AgentTask,
        execution_config: AgentExecutionConfig,
    ) -> None:
        """执行一项 SDK 任务并把消息收敛成 NearMate 事件。

        Args:
            task: 已经越过 Foreground 提交边界的任务。
            execution_config: 任务准备时冻结的可信用户配置。

        Returns:
            无；生命周期结果全部通过来源事件发出。
        """

        session_id: str | None = task.expected_sdk_session_id
        sdk_session_identity_confirmed = False
        terminal_emitted = False
        bridge: InteractionBridge | None = None
        client: ClaudeSDKClient | None = None
        session: AgentSession | None = None
        turn_capability: AgentSessionTurn | None = None
        work_plan_mirror = AgentWorkPlanMirror()
        usage_collector = AgentTaskUsageCollector()
        with self._lock:
            if self._current_task is task:
                self._current_usage_collector = usage_collector

        def emit_terminal(
            event_type: _AgentTerminalEventType,
            **payload: object,
        ) -> bool:
            """保证一项任务最多向产品账本写入一个终态。

            Args:
                event_type: completed、failed 或 cancelled。
                payload: 终态事件需要携带的字段。

            Returns:
                本次确实发出终态时返回真；已有终态时返回假。
            """

            nonlocal terminal_emitted
            emitted = self._emit_terminal_once(
                task,
                event_type,
                usage_collector=usage_collector,
                **payload,
            )
            terminal_emitted = terminal_emitted or emitted
            return emitted

        try:
            workspace_paths = self._workspace_manager.validate_prepared_task(
                task.activity_id,
                agent_config_root=Path(task.agent_config_dir),
            )
            if (
                workspace_paths.workspace_root != Path(task.workspace_root).resolve()
                or workspace_paths.output_root != Path(task.task_output_dir).resolve()
                or workspace_paths.agent_config_root
                != execution_config.model_config.config_dir.absolute()
            ):
                emit_terminal(
                    "failed",
                    error_code="configuration_invalid",
                    error_summary="后台工作区初始化失败。",
                )
                return
            if bool(task.agent_session_id) != bool(task.expected_sdk_session_id):
                raise _SDKSessionIdentityError(
                    "产品 AgentSession 与 SDK resume 身份必须同时存在"
                )
            if task.expected_sdk_session_id is None:
                # 普通任务明确要求 new_session。上一个终态任务已经没有纠正入口，继续
                # 保留它的 Client 只会让长期运行的 Backend 累积空闲 CLI transport。
                # 单任务槽保证此刻没有其它活跃 Turn，可以先统一断开再创建全新 Session。
                await self._session_manager.disconnect_all(timeout=1.0)
                with self._lock:
                    self._activity_sessions.clear()
            session = self._session_manager.get_or_create(
                workspace_root=task.workspace_root,
                agent_session_id=task.agent_session_id,
                policy=(
                    "resume_required"
                    if task.expected_sdk_session_id
                    else "new_session"
                ),
            )
            with self._lock:
                self._current_session = session
                self._activity_sessions[task.activity_id] = session.agent_session_id
            # acceptance 已经越过提交边界；先登记 started，后续任何初始化失败才能进入
            # 同一 Activity 的合法终态，而不是成为一个没有开始事实的孤儿失败。
            self._emit_started_once(task)
            bridge = InteractionBridge(
                activity_id=task.activity_id,
                request_notifier=lambda request: self._emit_interaction(task, request),
                answer_applied_notifier=lambda request, answer: (
                    self._publish_interaction_resolution(task, request, answer)
                ),
            )
            permission_gate = ActivityPermissionGate(
                interaction_bridge=bridge,
                resolution_notifier=lambda request, outcome, suppress_notice: (
                    self._publish_permission_resolution(
                        task,
                        request,
                        outcome,
                        suppress_notice=suppress_notice,
                    )
                ),
                denial_notifier=lambda: self._freeze_permission_denial(task),
            )
            turn_capability = session.bind_turn(
                activity_id=task.activity_id,
                bridge=bridge,
                permission_gate=permission_gate,
                material_catalog=task.material_catalog,
                current_scene_path=task.current_scene_path,
            )
            with self._lock:
                self._current_bridge = bridge
                self._current_material_access = turn_capability.material_access
            prompt_path = ensure_persona_file(self._runtime_dir)
            if CUA_SKILL_ID in task.skill_snapshot.enabled_skill_ids:
                if self._cua_integration is None or not task.cua_capability_granted:
                    raise RuntimeError("CUA Grant 与官方集成状态不一致")
            options = build_agent_options(
                task=task,
                model_config=execution_config.model_config,
                system_prompt_path=prompt_path,
                session=session,
                turn_capability=turn_capability,
                cua_integration=self._cua_integration,
                stderr=lambda line: (
                    _print_terminal_diagnostic(
                        f"[BackgroundAgent][{task.activity_id}][cli.stderr] "
                        f"{line.rstrip()}"
                    )
                    if self._terminal_diagnostics_enabled
                    else logger.debug("Claude Code stderr: %s", line.rstrip())
                ),
                max_turns=execution_config.max_turns,
            )
            client = await session.ensure_connected(
                options,
                policy=(
                    "resume_required"
                    if task.expected_sdk_session_id
                    else "new_session"
                ),
                turn_token=turn_capability.turn_token,
            )
            with self._lock:
                self._current_client = client
            with self._lock:
                self._current_client_connected = True
                cancelled_before_query = (
                    self._shutdown
                    or self._cancel_requested_activity_id == task.activity_id
                )
            if cancelled_before_query:
                await session.interrupt(activity_id=task.activity_id)
                emit_terminal(
                    "cancelled",
                    session_id=session_id,
                    reason=self._cancellation_reason(task),
                )
                return
            prompt_text = render_agent_task_prompt(task)
            await session.query(
                prompt_text,
                turn_token=turn_capability.turn_token,
            )
            # ResultMessage 到达后会提前退出消费；aclosing 保证旧 reader 的 finally
            # 在下一 Turn 绑定之前完成，不能依赖异步生成器的垃圾回收时机。
            async with aclosing(
                session.receive_response(turn_token=turn_capability.turn_token)
            ) as response_stream:
                async for message in response_stream:  # 获取回复
                    # SDK 对象携带大量身份和用量字段；开发者真正需要的是 Agent
                    # 说了什么、调用了什么，以及工具和终态返回了什么。
                    if self._terminal_diagnostics_enabled:
                        for diagnostic_kind, diagnostic_text in (
                            _readable_terminal_diagnostics(message)
                        ):
                            _print_terminal_diagnostic(
                                f"[BackgroundAgent][{task.activity_id}]"
                                f"[{diagnostic_kind}] {diagnostic_text}"
                            )
                    if isinstance(message, SystemMessage):
                        received_session_id = _session_id_from_system_message(message)
                        if message.subtype == "init":
                            if (
                                received_session_id is None
                                or (
                                    task.expected_sdk_session_id is not None
                                    and received_session_id
                                    != task.expected_sdk_session_id
                                )
                                or (
                                    sdk_session_identity_confirmed
                                    and received_session_id != session_id
                                )
                            ):
                                await session.interrupt(activity_id=task.activity_id)
                                raise _SDKSessionIdentityError(
                                    "SDK init 没有返回预期的 session_id"
                                )
                            session_id = received_session_id
                            session.sdk_session_id = received_session_id
                            sdk_session_identity_confirmed = True
                            with self._lock:
                                if self._current_task is task:
                                    self._current_sdk_session_identity_confirmed = True
                            # CLI 的 init 到达才说明 Agent loop 已真正建立；连接和查询准备
                            # 仍属于 preparing，不能提前把启动失败伪装成执行中失败。
                            self._emit(
                                task,
                                "stage_changed",
                                stage="processing",
                                updated_at=_now_ms(),
                            )
                        elif (
                            sdk_session_identity_confirmed
                            and received_session_id is not None
                            and received_session_id != session_id
                        ):
                            await session.interrupt(activity_id=task.activity_id)
                            raise _SDKSessionIdentityError(
                                "SDK SystemMessage 的 session_id 发生漂移"
                            )
                    elif isinstance(message, AssistantMessage):
                        if not sdk_session_identity_confirmed:
                            await session.interrupt(activity_id=task.activity_id)
                            raise _SDKSessionIdentityError(
                                "SDK init 前出现 AssistantMessage"
                            )
                        usage_collector.record_assistant_message(message)
                        self._consume_assistant_message(task, message, work_plan_mirror)
                    elif isinstance(message, UserMessage):
                        if not sdk_session_identity_confirmed:
                            await session.interrupt(activity_id=task.activity_id)
                            raise _SDKSessionIdentityError(
                                "SDK init 前出现 UserMessage"
                            )
                        self._consume_user_message(task, message, work_plan_mirror)
                    elif isinstance(message, ResultMessage):
                        if (
                            not sdk_session_identity_confirmed
                            or not message.session_id
                            or message.session_id != session_id
                        ):
                            await session.interrupt(activity_id=task.activity_id)
                            raise _SDKSessionIdentityError(
                                "SDK ResultMessage 的 session_id 与 init 不一致"
                            )
                        usage_collector.record_result_message(message)
                        if message.subtype == "success":
                            try:
                                completion_payload = AgentCompletionPayload.from_structured_output(
                                    message.structured_output
                                )
                            except ValueError:
                                emit_terminal(
                                    "failed",
                                    session_id=session_id,
                                    error_code="invalid_provider_response",
                                    error_summary="后台工作返回了无法读取的结果。",
                                )
                                break
                            self._emit(task, "stage_changed", stage="delivering_result", updated_at=_now_ms())
                            output_paths = self._workspace_manager.collect_outputs(
                                task.activity_id
                            )
                            emit_terminal(
                                "completed",
                                display_content=completion_payload.display_content,
                                delivery_brief=completion_payload.delivery_brief,
                                output_paths=list(output_paths),
                                deliverable=task.deliverable,
                                session_id=session_id,
                            )
                        elif message.subtype == "error_during_execution":
                            with self._lock:
                                cancelled = (
                                    self._shutdown
                                    or self._current_task is not task
                                    or self._cancel_requested_activity_id == task.activity_id
                                )
                            if cancelled:
                                emit_terminal(
                                    "cancelled",
                                    session_id=session_id,
                                    reason=self._cancellation_reason(task),
                                )
                            else:
                                emit_terminal(
                                    "failed",
                                    session_id=session_id,
                                    error_code="agent_execution_error",
                                    error_summary="后台工作在执行过程中停止了。",
                                )
                        else:
                            emit_terminal(
                                "failed",
                                session_id=session_id,
                                error_code=_error_code_for_result(message.subtype),
                                error_summary="后台工作没有完成这项任务。",
                            )
                        # ResultMessage 是 SDK 的任务终态；收到第一条后不再消费任何迟到消息。
                        break
            if not terminal_emitted:
                with self._lock:
                    cancelled = (
                        self._shutdown
                        or self._current_task is not task
                        or self._cancel_requested_activity_id == task.activity_id
                    )
                if cancelled:
                    emit_terminal(
                        "cancelled",
                        session_id=session_id,
                        reason=self._cancellation_reason(task),
                    )
                else:
                    emit_terminal(
                        "failed",
                        session_id=session_id,
                        error_code="agent_cli_crash",
                        error_summary="Claude Code 连接意外结束，后台任务没有返回终态。",
                    )
        except asyncio.CancelledError:
            with self._lock:
                cancellation_reason = (
                    self._cancel_requested_reason
                    if self._cancel_requested_activity_id == task.activity_id
                    and self._cancel_requested_reason
                    else "agent_cancelled"
                )
            emit_terminal("cancelled", session_id=session_id, reason=cancellation_reason)
        except _SDKSessionIdentityError:
            logger.exception(
                "Claude SDK 会话身份校验失败 activity_id=%s",
                task.activity_id,
            )
            if session is not None:
                try:
                    await session.disconnect()
                except Exception:
                    logger.exception(
                        "关闭会话身份异常的 SDK Client 失败 activity_id=%s",
                        task.activity_id,
                    )
            emit_terminal(
                "failed",
                session_id=session_id,
                error_code=(
                    "correction_session_resume_failed"
                    if task.expected_sdk_session_id
                    else "invalid_provider_response"
                ),
                error_summary=(
                    "后台纠正无法恢复原来的 Claude 会话。"
                    if task.expected_sdk_session_id
                    else "后台工作没有建立可验证的 Claude 会话。"
                ),
            )
        except Exception as exception:
            with self._lock:
                cancelled = (
                    self._shutdown
                    or self._current_task is not task
                    or self._cancel_requested_activity_id == task.activity_id
                )
                client_connected = self._current_client_connected
            if cancelled:
                emit_terminal(
                    "cancelled",
                    session_id=session_id,
                    reason=self._cancellation_reason(task),
                )
            elif task.expected_sdk_session_id and not sdk_session_identity_confirmed:
                logger.exception(
                    "后台纠正恢复 Claude SDK 会话失败 activity_id=%s",
                    task.activity_id,
                )
                if session is not None:
                    try:
                        await session.disconnect()
                    except Exception:
                        logger.exception(
                            "关闭恢复失败的 SDK Client 异常 activity_id=%s",
                            task.activity_id,
                        )
                emit_terminal(
                    "failed",
                    session_id=session_id,
                    error_code="correction_session_resume_failed",
                    error_summary="后台纠正无法恢复原来的 Claude 会话。",
                )
            elif not client_connected:
                logger.exception("后台智能体启动失败 activity_id=%s", task.activity_id)
                emit_terminal(
                    "failed",
                    session_id=session_id,
                    error_code="agent_cli_crash",
                    error_summary="Claude Code 无法启动，后台任务没有开始执行。",
                )
            elif _is_sdk_transport_overflow(exception):
                logger.exception(
                    "后台智能体返回数据超过 SDK 单消息缓冲上限 activity_id=%s",
                    task.activity_id,
                )
                emit_terminal(
                    "failed",
                    session_id=session_id,
                    error_code="agent_transport_overflow",
                    error_summary="后台工作返回的数据超过安全传输上限，任务未能完成。",
                )
            else:
                logger.exception("后台智能体执行失败 activity_id=%s", task.activity_id)
                emit_terminal(
                    "failed",
                    session_id=session_id,
                    error_code="agent_execution_error",
                    error_summary="后台工作执行时发生异常。",
                )
        finally:
            cleanup_cancellation: asyncio.CancelledError | None = None
            try:
                if session is not None and turn_capability is not None:
                    try:
                        session.retire_turn(turn_capability.turn_token)
                    except Exception:
                        # Turn 退休失败不能阻止后续 transport 和 Grant 收口，否则一项
                        # 已有业务终态的任务会永久占住全局桌面能力。
                        logger.exception(
                            "后台任务 Turn 退休失败 activity_id=%s",
                            task.activity_id,
                        )
                if (
                    self._cua_integration is not None
                    and CUA_SKILL_ID in task.skill_snapshot.enabled_skill_ids
                ):
                    try:
                        # Browser CLI 不受 SDK 的 MCP transport 生命周期管理，所以在
                        # 处理全局 Grant 前显式结束当前 Activity 的官方 session。即使任务
                        # 没有真正打开浏览器，官方 end_session 也保持幂等。
                        cleanup_cancellation = (
                            await _await_cleanup_despite_outer_cancellation(
                                asyncio.to_thread(
                                    self._cua_integration.end_browser_session,
                                    task.activity_id,
                                )
                            )
                            or cleanup_cancellation
                        )
                    except asyncio.CancelledError as exception:
                        # shutdown 可能在清理途中取消 Runner。先记住取消事实，继续按顺序
                        # 尝试 transport 和 Grant 收口，最后再把取消交还事件循环。
                        cleanup_cancellation = exception
                    except Exception:
                        # 清理失败只进入诊断，不能把已经形成的业务终态改写成失败。
                        logger.exception(
                            "后台任务 Browser CLI session 清理失败 activity_id=%s",
                            task.activity_id,
                        )
                if session is not None:
                    try:
                        # 每项 Activity 都要断开自己冻结的 SDK options 和 MCP transport；
                        # 产品级 Session 与 SDK session_id 保留下来，纠正任务再显式恢复。
                        cleanup_cancellation = (
                            await _await_cleanup_despite_outer_cancellation(
                                session.disconnect_turn_transport()
                            )
                            or cleanup_cancellation
                        )
                    except asyncio.CancelledError as exception:
                        cleanup_cancellation = cleanup_cancellation or exception
                    except Exception:
                        # transport 清理失败不能长期占住全局 Grant。Session 已标记为损坏，
                        # 后续纠正会按既有恢复失败合同终止。
                        logger.exception(
                            "后台任务 SDK/MCP transport 清理失败 activity_id=%s",
                            task.activity_id,
                        )
            finally:
                try:
                    if task.cua_capability_granted:
                        try:
                            if self._cua_grant_finalizer is not None:
                                self._cua_grant_finalizer(task.activity_id)
                            elif self._cua_capability is not None:
                                self._cua_capability.release(task.activity_id)
                        except Exception:
                            # Grant 清理失败不能覆盖 Agent 已经形成的真实业务终态；精确
                            # Activity ID 也保证旧 Turn 不会释放下一项任务的所有权。
                            logger.exception(
                                "后台任务 CUA Grant 释放失败 activity_id=%s",
                                task.activity_id,
                            )
                finally:
                    try:
                        if bridge is not None:
                            self._close_interactions(task, bridge, "后台任务已经结束。")
                    finally:
                        self._release_task(task)
            if cleanup_cancellation is not None:
                raise cleanup_cancellation

    def _consume_assistant_message(
        self,
        task: AgentTask,
        message: AssistantMessage,
        work_plan_mirror: AgentWorkPlanMirror,
    ) -> None:
        """读取可见阶段和工具调用弱进度，不把内部思考写入结果。

        Args:
            task: 当前后台任务。
            message: SDK 返回的一条 Assistant 消息。
            work_plan_mirror: 当前任务独占的 Claude Task 镜像器。
        Returns:
            无；最终成果只从 SDK 结构化终态读取。
        """

        text_blocks = [
            block.text.strip()
            for block in message.content
            if isinstance(block, TextBlock) and block.text.strip()
        ]
        if text_blocks:
            # 只把 SDK 明确标记为 TextBlock 的内容交给 Activity；不读取 thinking、工具调用或工具结果。
            with self._lock:
                self._current_turn_output_index += 1
                turn_index = self._current_turn_output_index
            self._emit(
                task,
                "turn_output",
                turn_index=turn_index,
                content_text="\n".join(text_blocks),
                created_at=_now_ms(),
            )
        # ToolUse 只在能形成已确认 Work Plan 时进入产品领域；普通工具调用是 SDK 执行细节。
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                work_plan_mirror.record_tool_use(block)

    def _consume_user_message(
        self,
        task: AgentTask,
        message: UserMessage,
        work_plan_mirror: AgentWorkPlanMirror,
    ) -> None:
        """确认 Claude Task 工具结果，并发布新的全量计划快照。

        Args:
            task: 当前后台任务。
            message: SDK 返回、可能包含 ToolResultBlock 的 User 消息。
            work_plan_mirror: 当前任务独占的 Claude Task 镜像器。

        Returns:
            无；计划实际变化时通过领域事件发布。
        """

        if not isinstance(message.content, list):
            return
        for block in message.content:
            if not isinstance(block, ToolResultBlock):
                continue
            work_plan = work_plan_mirror.record_tool_result(
                block,
                tool_use_result=message.tool_use_result,
            )
            if work_plan is None:
                continue
            self._emit(
                task,
                "work_plan_changed",
                work_plan=work_plan.to_payload(),
                updated_at=_now_ms(),
            )

    def _emit_interaction(self, task: AgentTask, request: AgentInteractionRequest) -> None:
        """把 SDK 等待请求转换成产品交互事件。

        Args:
            task: 请求所属后台任务。
            request: SDK 回调创建的待答请求。

        Returns:
            无。
        """

        self._emit(
            task,
            "interaction_requested",
            request_id=request.request_id,
            kind=request.kind,
            tool_name=request.tool_name,
            input_data=request.input_data,
            action_summary=request.action_summary,
            grant_scope=request.grant_scope,
            questions=request.questions,
        )

    def _publish_interaction_resolution(
        self,
        task: AgentTask,
        request: AgentInteractionRequest,
        answer: AgentInteractionAnswer,
    ) -> None:
        """在 SDK 回调真正应用回答后发布交互收口事实。

        Args:
            task: 请求所属后台任务。
            request: 被解决的交互请求。
            answer: 已由主进程校验的用户回答。
        """

        outcome = {
            "allow": "allowed",
            "deny": "denied",
            "answer": "answered",
            "cancel": "cancelled",
        }[answer.outcome]
        self._emit(
            task,
            "interaction_resolved",
            request_id=request.request_id,
            outcome=outcome,
        )

    def _publish_permission_resolution(
        self,
        task: AgentTask,
        request: AgentInteractionRequest,
        outcome: PermissionResolutionOutcome,
        *,
        suppress_notice: bool,
    ) -> None:
        """按 Gate 已生效的状态发布权限收口和 Activity 授权事实。

        Args:
            task: 权限请求所属后台任务。
            request: 唯一 Activity 授权卡请求。
            outcome: Gate 已应用的允许或拒绝结果。
            suppress_notice: Foreground 已经即时确认自然语言授权时为真。
        """

        self._emit(
            task,
            "interaction_resolved",
            request_id=request.request_id,
            outcome=outcome,
        )
        if outcome == "allowed":
            self._emit(
                task,
                "permission_granted",
                request_id=request.request_id,
                grant_scope="activity",
                suppress_notice=suppress_notice,
            )

    def _freeze_permission_denial(self, task: AgentTask) -> None:
        """在权限回调恢复前冻结拒绝导致的 Activity 取消原因。

        Args:
            task: 当前等待权限决定的后台任务。
        """

        with self._lock:
            if self._current_task is not task:
                return
            self._freeze_cancellation_reason_locked(task, "permission_denied")

    def _freeze_cancellation_reason_locked(
        self,
        task: AgentTask,
        reason: str,
    ) -> str:
        """在持锁状态下记录当前 Turn 的首次取消原因。

        Args:
            task: 当前正在运行、即将收到取消请求的后台任务。
            reason: 本次取消请求希望写入的稳定原因码。

        Returns:
            当前 Turn 已冻结的权威取消原因。重复取消只返回首次原因，不会覆盖。
        """

        if (
            self._cancel_requested_activity_id != task.activity_id
            or not self._cancel_requested_reason
        ):
            self._cancel_requested_activity_id = task.activity_id
            self._cancel_requested_reason = reason.strip() or "agent_interrupted"
        return self._cancel_requested_reason

    def _cancellation_reason(self, task: AgentTask) -> str:
        """读取当前任务已经冻结的稳定取消原因。

        Args:
            task: 需要读取取消事实的后台任务。

        Returns:
            权限拒绝或普通运行时中断对应的稳定原因。
        """

        with self._lock:
            if self._current_task is task and self._cancel_requested_reason:
                return self._cancel_requested_reason
        return "agent_interrupted"

    def _close_interactions(
        self,
        task: AgentTask,
        bridge: InteractionBridge,
        reason: str,
    ) -> None:
        """关闭交互桥并为每张仍挂起的卡片发出取消事实。

        Args:
            task: 请求所属后台任务。
            bridge: 当前任务使用的 SDK 交互桥。
            reason: 交给 SDK 的取消说明。

        Returns:
            无。
        """

        def publish_resolved(request_ids: tuple[str, ...]) -> None:
            """在 SDK Future 恢复前收口全部主进程待答请求。

            Args:
                request_ids: 本次桥关闭冻结的全部挂起请求编号。

            Returns:
                无返回值。
            """

            for request_id in request_ids:
                self._emit(
                    task,
                    "interaction_resolved",
                    request_id=request_id,
                    outcome="cancelled",
                )

        bridge.close(reason, before_resolve=publish_resolved)

    def _emit(
        self,
        task: AgentTask,
        event_type: AgentEventType,
        **payload: object,
    ) -> None:
        """向 Activity 执行器发送一条领域事件。

        Args:
            task: 事件所属后台任务。
            event_type: 不带来源前缀的事件类型。
            payload: 事件业务字段。

        Returns:
            无。
        """

        with self._lock:
            agent_session_id = self._activity_sessions.get(task.activity_id)
            current_session = self._current_session
            sdk_session_id = (
                current_session.sdk_session_id
                if current_session is not None
                and current_session.agent_session_id == agent_session_id
                and self._current_sdk_session_identity_confirmed
                else None
            )
        event_payload = dict(payload)
        if agent_session_id:
            event_payload["agent_session_id"] = agent_session_id
        if sdk_session_id:
            event_payload["sdk_session_id"] = sdk_session_id
        self._event_callback(
            AgentEvent(
                event_type=event_type,
                activity_id=task.activity_id,
                interaction_session_id=task.interaction_session_id,
                payload=event_payload,
            )
        )

    def _emit_started_once(self, task: AgentTask) -> bool:
        """为当前任务发出唯一的 started 事实。

        Args:
            task: 需要登记开始的后台工作任务。

        Returns:
            本次确实发出 started 时返回真；任务已经失效或已发出时返回假。
        """

        with self._lock:
            if self._current_task is not task or self._current_started_emitted:
                return False
            self._current_started_emitted = True
        self._emit(task, "started")
        return True

    def _emit_terminal_once(
        self,
        task: AgentTask,
        event_type: _AgentTerminalEventType,
        *,
        usage_collector: AgentTaskUsageCollector | None,
        **payload: object,
    ) -> bool:
        """为任务发出唯一终态，并把当时可确认的用量一起封存。

        Args:
            task: 终态所属的后台工作任务。
            event_type: completed、failed 或 cancelled。
            usage_collector: SDK 消息用量收集器；没有初始化成功时为空。
            payload: 终态事件的其余业务字段。

        Returns:
            本次确实发出终态时返回真；任务已失效或已有终态时返回假。
        """

        self._emit_started_once(task)
        with self._lock:
            if self._current_task is not task or self._current_terminal_emitted:
                return False
            if (
                event_type != "cancelled"
                and self._cancel_requested_activity_id == task.activity_id
            ):
                # cancel 与 SDK Result 可能同时到达。谁先取得这把锁谁就拥有终态：
                # 已接纳取消后，迟到的 success/error 都只能成为同一个取消事实。
                event_type = "cancelled"
                cancellation_payload: dict[str, object] = {
                    "reason": self._cancel_requested_reason or "agent_interrupted"
                }
                session_id = payload.get("session_id")
                if isinstance(session_id, str) and session_id:
                    cancellation_payload["session_id"] = session_id
                payload = cancellation_payload
            self._current_terminal_emitted = True
            terminal_event = self._current_terminal_event
        # 终态是唯一需要写入用量的边界；即使 SDK 没有返回任何消息，也要留下
        # 一条不完整的零用量记录，方便用量页准确统计任务数和完整性。
        snapshot = (
            usage_collector.snapshot()
            if usage_collector is not None
            else AgentTaskUsageCollector().snapshot()
        )
        if event_type == "cancelled" and isinstance(payload.get("reason"), str):
            payload["cancellation_reason"] = payload["reason"]
        payload["internal_usage"] = snapshot.to_payload()
        try:
            self._emit(task, event_type, **payload)
        finally:
            if terminal_event is not None:
                terminal_event.set()
        return True

    def _release_task(self, task: AgentTask) -> None:
        """只释放当前任务身份，避免迟到终态清空新任务。

        Args:
            task: 已完成 SDK 清理的任务对象。

        Returns:
            无。
        """

        released = False
        with self._lock:
            if self._current_task is task:
                self._current_task = None
                self._current_bridge = None
                self._current_material_access = None
                self._current_client = None
                self._current_client_connected = False
                self._current_sdk_session_identity_confirmed = False
                self._current_session = None
                self._current_future = None
                self._current_usage_collector = None
                self._current_started_emitted = False
                self._current_terminal_emitted = False
                cleanup_event = self._current_cleanup_event
                self._cancel_requested_activity_id = None
                self._cancel_requested_reason = None
                if cleanup_event is not None:
                    cleanup_event.set()
                released = True
        if not released or self._slot_released_callback is None:
            return
        try:
            self._slot_released_callback(task.activity_id)
        except Exception:
            # Runner 身份已经权威释放。调度唤醒失败不能让清理协程倒写任务终态；
            # Executor 会记录诊断，热替换仍能安全关闭全部 waiting 资源。
            logger.exception(
                "后台 Runner 槽位释放通知失败 activity_id=%s",
                task.activity_id,
            )


def _session_id_from_system_message(message: SystemMessage) -> str | None:
    """从 SDK System 消息中读取会话编号。

    Args:
        message: SDK 返回的 System 消息。

    Returns:
        非空会话编号；消息没有合法编号时返回空。
    """

    value = message.data.get("session_id") if isinstance(message.data, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _error_code_for_result(subtype: str) -> str:
    """把 SDK 终态映射为稳定 NearMate 错误码。

    Args:
        subtype: SDK ResultMessage 的错误子类型。

    Returns:
        NearMate 公共故障报告使用的稳定错误码。
    """

    return {
        "error_max_turns": "agent_max_turns",
        "error_max_structured_output_retries": "agent_execution_error",
    }.get(subtype, "agent_execution_error")


def _readable_terminal_diagnostics(
    message: Message,
) -> list[tuple[_TerminalDiagnosticKind, str]]:
    """从 SDK 消息中提取开发者能直接阅读的执行文字。

    Args:
        message: Claude Agent SDK 返回的一条消息。

    Returns:
        按原内容块顺序排列的终端类别与文字；纯元数据消息返回空列表。
    """

    if isinstance(message, AssistantMessage):
        diagnostics: list[tuple[_TerminalDiagnosticKind, str]] = []
        for block in message.content:
            if isinstance(block, ThinkingBlock):
                thinking_text = block.thinking.strip()
                if thinking_text:
                    diagnostics.append(("thinking", thinking_text))
                continue
            if isinstance(block, TextBlock):
                assistant_text = block.text.strip()
                if assistant_text:
                    diagnostics.append(("assistant", assistant_text))
                continue
            if isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                tool_text = f"{block.name}\n{_terminal_json(block.input)}"
                diagnostics.append(("tool", tool_text))
                continue
            if isinstance(block, (ToolResultBlock, ServerToolResultBlock)):
                result_text = _tool_result_text(block.content)
                if result_text:
                    diagnostics.append(("tool.result", result_text))
        if message.error:
            diagnostics.append(("assistant", f"SDK 错误：{message.error}"))
        return diagnostics

    if isinstance(message, UserMessage) and isinstance(message.content, list):
        diagnostics = []
        for block in message.content:
            if not isinstance(block, (ToolResultBlock, ServerToolResultBlock)):
                continue
            result_text = _tool_result_text(block.content)
            if result_text:
                diagnostics.append(("tool.result", result_text))
        return diagnostics

    if isinstance(message, ResultMessage):
        return [("result", _result_message_text(message))]

    # SystemMessage 主要承载 init、thinking_tokens、会话身份和用量元数据，
    # 这些字段正是此前淹没可读文字的来源，因此全部保持静默。
    return []


def _tool_result_text(content: object) -> str | None:
    """读取工具结果内容块里的文字，跳过图片和二进制载荷。

    Args:
        content: 普通工具或 Server Tool 返回的 SDK 内容。

    Returns:
        合并后的非空文字；没有文字内容时返回空值。
    """

    if isinstance(content, str):
        normalized_text = content.strip()
        return normalized_text or None

    if isinstance(content, list):
        text_parts = [
            text
            for item in content
            if (text := _tool_result_text(item)) is not None
        ]
        return "\n".join(text_parts) or None

    if not isinstance(content, dict) or content.get("type") == "image":
        return None

    # SDK 文本内容块固定使用 text；部分 Server Tool 会再套一层 content，
    # error/message 则保留工具给出的具体失败说明。
    for field_name in ("text", "content", "error", "message"):
        if field_name not in content:
            continue
        result_text = _tool_result_text(content[field_name])
        if result_text:
            return result_text
    return None


def _result_message_text(message: ResultMessage) -> str:
    """把 SDK 终态压缩成最终结果或具体失败原因。

    Args:
        message: Claude Agent SDK 的任务终态消息。

    Returns:
        不含 Session、UUID 和用量字段的可读终态文字。
    """

    if message.is_error or message.subtype != "success":
        failure_parts = [
            text.strip()
            for text in (
                *(message.errors or []),
                message.terminal_reason,
                message.result,
            )
            if isinstance(text, str) and text.strip()
        ]
        if not failure_parts:
            failure_parts.append(message.subtype)
        return f"任务执行失败：{'；'.join(dict.fromkeys(failure_parts))}"

    if isinstance(message.result, str) and message.result.strip():
        return message.result.strip()
    if isinstance(message.structured_output, str) and message.structured_output.strip():
        return message.structured_output.strip()
    if message.structured_output is not None:
        return _terminal_json(message.structured_output)
    return "任务执行成功。"


def _terminal_json(value: object) -> str:
    """把工具参数或结构化结果转换为稳定、可读的 JSON。

    Args:
        value: SDK 提供的工具参数或结构化终态。

    Returns:
        保留中文且不使用 Python 数据类外壳的 JSON 文字。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=lambda unsupported: f"<{type(unsupported).__name__}>",
    )


def _is_sdk_transport_overflow(exception: BaseException) -> bool:
    """识别 SDK reader 的单条 JSON 消息超限，而不吞掉其它执行异常。

    Args:
        exception: Agent SDK 或其 CLI transport 抛出的原始异常。

    Returns:
        异常文本明确指向消息缓冲上限时返回真。
    """

    text = str(exception).casefold()
    return (
        "json message exceeded maximum buffer size" in text
        or "buffer size" in text and "exceeds limit" in text
    )


def _now_ms() -> float:
    """返回 Unix 毫秒时间，供来源事件排序和日志使用。

    Returns:
        当前 Unix 毫秒时间。
    """

    import time

    return time.time() * 1000.0
