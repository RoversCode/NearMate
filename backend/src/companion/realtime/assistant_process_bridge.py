from __future__ import annotations

import threading
from collections.abc import Callable
from queue import Empty, Queue
from typing import Literal, Protocol
from uuid import uuid4

from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import (
    AssistantControlCommand,
    AssistantRequest,
    CancelCurrentGenerationCommand,
    SelfReflectionToolCommitDecisionCommand,
    RuntimeEvent,
)
from companion.realtime.assistant_output_contracts import progress_references_payload
from companion.realtime.assistant_process_handle import SwappableAssistantProcessClient
from companion.realtime.system_notice_playout import SystemNoticePlayoutTracker
from companion.realtime.runtime_identity import (
    AssistantRequestIdentityConflict,
    AssistantRequestIdentityRegistry,
)
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger
from companion.realtime.assistant_request_ownership import (
    AssistantRequestOwnership,
    AssistantRequestOwnershipLedger,
)


logger = get_backend_logger(__name__)


AssistantRuntimeBridgeState = Literal["starting", "running", "failed", "stopping"]


class AssistantOutputUnavailableError(RuntimeError):
    """表示活跃媒体会话的助手输出链路已经永久失效。"""


class AssistantAudioOutputLike(Protocol):
    """桥接线程依赖的助手音频输出边界。"""

    def enqueue_audio_event(self, event: RuntimeEvent) -> None:
        """提交一条助手音频事件。

        Args:
            event: `assistant.audio_chunk` 或 `assistant.audio_finished` 事件。

        Returns:
            None: 事件会进入专用音频输出调度器。
        """
        ...

    def clear_pending_audio(self, *, interrupt_before: float | None = None) -> tuple[str, ...]:
        """清空尚未播放的助手音频。

        Args:
            interrupt_before: 可选的手动打断时间，阻止旧唤醒短答迟到入队。

        Returns:
            此次清理覆盖的请求编号。
        """
        ...

    def clear_pending_audio_for_request(self, request_id: str) -> bool:
        """只清空指定助手请求的待播音频。

        Args:
            request_id: 需要停止发声的助手请求编号。

        Returns:
            请求被音频调度器接受时返回真。
        """
        ...


class AssistantProcessBridge:
    """在独立线程中桥接输入侧队列、助手运行时和下行音频调度器。"""

    def __init__(
        self,
        *,
        assistant_runtime: SwappableAssistantProcessClient,
        assistant_request_queue: Queue[AssistantRequest],
        assistant_control_queue: Queue[AssistantControlCommand],
        event_queue: Queue[RuntimeEvent],
        audio_output_scheduler: AssistantAudioOutputLike,
        system_notice_playout_tracker: SystemNoticePlayoutTracker | None = None,
        request_committed_handler: Callable[[AssistantRequest], None] | None = None,
        request_identity_registry: AssistantRequestIdentityRegistry | None = None,
        request_ownership_ledger: AssistantRequestOwnershipLedger | None = None,
        output_health_ledger: AssistantOutputHealthLedger | None = None,
        bridge_wait_timeout_sec: float = 1.0,
    ) -> None:
        """初始化桥接线程依赖和生命周期状态。

        Args:
            assistant_runtime: 助手后半段运行时客户端。
            assistant_request_queue: 输入侧提交给助手运行时的生成请求队列。
            assistant_control_queue: 输入侧提交给助手运行时的控制命令队列。
            event_queue: 非音频运行时事件出口，后续由控制面 Router 广播。
            audio_output_scheduler: 音频事件出口，用于发布助手下行语音。
            system_notice_playout_tracker: 权限 System Notice 的短期播放身份账本。
            request_committed_handler: 请求进入 Bridge 队列后、唤醒消费线程前的同步提交回调。
            request_identity_registry: Backend 进程级请求身份永久登记表。
            bridge_wait_timeout_sec: bridge 空闲等待唤醒的诊断兜底秒数，避免子进程异常退出后永久沉睡。
        """

        # assistant_runtime 是唯一助手后半段运行时边界，bridge 只调用其同步队列 API。
        self.assistant_runtime = assistant_runtime
        # assistant_request_queue 承载 final ASR 产生的助手请求。
        self.assistant_request_queue = assistant_request_queue
        # assistant_control_queue 承载插话取消等高优先级控制命令。
        self.assistant_control_queue = assistant_control_queue
        # event_queue 承载非音频控制事件，后续仍由主循环 Router 广播。
        self.event_queue = event_queue
        # audio_output_scheduler 是音频事件的唯一出口，bridge 不直接调用 LiveKit capture_frame。
        self.audio_output_scheduler = audio_output_scheduler
        self.system_notice_playout_tracker = system_notice_playout_tracker
        # 请求提交回调用于在 Bridge 消费前确认 Notice lease 与 system request 身份。
        self.request_committed_handler = request_committed_handler
        self.request_identity_registry = request_identity_registry
        self.request_ownership_ledger = (
            request_ownership_ledger or AssistantRequestOwnershipLedger()
        )
        self.output_health_ledger = output_health_ledger or AssistantOutputHealthLedger()
        self.bridge_id = f"assistant-bridge-{uuid4()}"
        # Bridge 一创建就取得健康账本所有权。媒体会话重建后，旧 Bridge 的迟到退出
        # 只能完成自己的请求补偿，不能再覆盖新 Bridge 的可用状态。
        self.output_health_ledger.register_owner(self.bridge_id)
        # 正常路径由事件唤醒；长超时只用于周期性发现子进程异常退出等无通知故障。
        self.bridge_wait_timeout_sec = bridge_wait_timeout_sec
        self._stop_event = threading.Event()
        self._started_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._handoff_mode = False
        self._accepting_submissions = False
        self._health_state: AssistantRuntimeBridgeState = "stopping"  # 当前bridge的状态
        self.last_error: BaseException | None = None

        # 大白话属性说明：
        # _stop_event: 通知 Bridge 线程“该下班了”。主进程调用 stop() 时置位，
        #              线程循环检测到后退出 run，完成优雅收尾。
        # _started_event: Bridge 线程启动成功后给自己“打卡”的信号。
        #                 start() 会阻塞等待它，确认线程真的跑起来才返回。
        # _thread: 实际跑 Bridge 循环的后台线程本体。构造时还没创建，所以是 None。
        # _lock: 保护 Bridge 生命周期与提交入口，跨会话请求状态由进程级 Ledger 单独加锁。
        #        防止主线程和 Bridge 线程同时读写状态导致判断错乱。
        # _health_state: Bridge 当前的生命周期状态。
        #   - stopping: 初始状态，表示“还没启动，或正在停止”。
        #   - starting: start() 已经创建线程，等待它跑起来。
        #   - running: 线程成功启动，可以接收请求。
        #   - failed: 启动失败或运行中异常，不能再复用该实例。
        # last_error: 最近一次让 Bridge 失败的异常。失败时抛给外层看原因用。

    @property
    def health_state(self) -> AssistantRuntimeBridgeState:
        """返回 Bridge 当前健康状态。

        Returns:
            starting、running、failed 或 stopping 中的一项。
        """

        with self._lock:
            return self._health_state

    @property
    def is_running(self) -> bool:
        """返回桥接线程是否仍在运行。

        Returns:
            bool: 线程存在且存活时返回 True。
        """

        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def is_output_available(self) -> bool:
        """判断 Bridge 是否仍能原子接收新的请求或控制命令。

        Returns:
            Bridge 线程存活且健康状态为 running 时返回真。
        """

        with self._lock:
            return (
                self._health_state == "running"
                and not self._handoff_mode
                and self._thread is not None
                and self._thread.is_alive()
            )

    def start(self) -> None:
        """启动助手运行时桥接线程。

        Returns:
            None: 后台线程开始接管请求转发、控制命令转发和运行时事件分流。
        """

        with self._lock:
            thread_is_alive = self._thread is not None and self._thread.is_alive()
            if self._health_state == "running" and thread_is_alive:
                return
            if self._health_state == "running":
                self.last_error = RuntimeError(
                    "assistant runtime bridge thread exited while marked running"
                )
                self._health_state = "failed"
                raise AssistantOutputUnavailableError(
                    "Bridge 线程已退出"
                ) from self.last_error
            if self._health_state == "failed":
                raise AssistantOutputUnavailableError("失败的 Bridge 实例不能重新启动")
            if self._health_state == "stopping" and thread_is_alive:
                raise AssistantOutputUnavailableError("旧 Bridge 线程仍在停止")
            if self._health_state != "starting":
                self._stop_event.clear()
                self._started_event.clear()
                self.last_error = None
                self._handoff_mode = False
                self._accepting_submissions = False
                # room 重连只会重启 Bridge 线程；尚未收到双终态的请求事实必须跨重启保留，
                # 否则后续运行时替换或输出故障将失去精确补偿所需的原始身份。
                if self.system_notice_playout_tracker is not None:
                    self.system_notice_playout_tracker.clear()
                self._health_state = "starting"
                self._thread = threading.Thread(
                    target=self._run_thread,
                    name="companion-assistant-runtime-bridge",
                    daemon=True,
                )
                self._thread.start()

                # 媒体会话重建后只接管旧 Bridge 尚未提交的请求；已绑定旧世代的请求绝不重跑。
                for record in self.request_ownership_ledger.adopt_queued_requests(
                    self.bridge_id
                ):
                    self.assistant_request_queue.put(record.request)

        # start 返回时提交入口必须已经稳定为 running 或 failed，避免 Audio Worker 抢跑。
        started = self._started_event.wait(timeout=2.0)
        with self._lock:
            state = self._health_state
            if started and state == "running":
                self._accepting_submissions = True
                self._emit_output_status_changed(available=True)
                return
            if state == "starting":
                startup_error = TimeoutError(
                    "assistant runtime bridge startup timed out"
                )
                self.last_error = startup_error
                self._health_state = "failed"
                self._stop_event.set()
                self._started_event.set()
            else:
                startup_error = self.last_error

        if not started:
            try:
                self.assistant_runtime.notify_bridge_work_available()
            except Exception:
                logger.warning("Bridge 启动超时后唤醒退出失败", exc_info=True)
            raise AssistantOutputUnavailableError(
                "助手输出 Bridge 启动超时"
            ) from startup_error
        raise AssistantOutputUnavailableError(
            f"助手输出 Bridge 未能启动，当前状态为 {state}"
        ) from startup_error

    def stop(self, *, timeout: float | None = 2.0) -> None:
        """停止助手运行时桥接线程。

        Args:
            timeout: 等待线程退出的最大秒数。

        Returns:
            None: 线程退出后清理引用；超时时保留引用供后续诊断或再次停止。
        """

        with self._lock:
            # 直接 stop 可能发生在没有先走媒体会话 handoff 的异常路径，进程级快照仍要立即收口。
            # 已经是身份冲突等更具体故障时保留原 code，不能被普通停止覆盖。
            if self.output_health_ledger.snapshot().available:
                self.output_health_ledger.mark_unavailable(
                    "output_unavailable",
                    owner_id=self.bridge_id,
                )
            thread = self._thread
            if thread is None:
                if self._health_state != "failed":
                    self._health_state = "stopping"
                return

            self._stop_event.set()
            self._accepting_submissions = False
            if self._health_state != "failed":
                self._health_state = "stopping"

        try:
            self.assistant_runtime.notify_bridge_work_available()
        except Exception:
            logger.warning("停止 Bridge 时唤醒运行时失败", exc_info=True)

        thread.join(timeout=timeout)

        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def handoff(self, *, timeout: float = 1.0) -> None:
        """在媒体会话重建前交接排队请求，并补偿超时的旧世代请求。

        Args:
            timeout: 等待旧运行时双终态的确认窗口秒数。
        """

        with self._lock:
            self._handoff_mode = True
            self._accepting_submissions = False
            thread = self._thread
            if thread is None:
                failed_requests = self.request_ownership_ledger.take_for_failure(
                    bridge_id=self.bridge_id,
                    include_queued=False,
                )
                self._emit_failed_request_records(
                    failed_requests,
                    reason="runtime_unavailable",
                    fallback_runtime_epoch=None,
                )
                self.request_ownership_ledger.release_queued_requests(self.bridge_id)
                return
            if self._health_state != "failed":
                self._health_state = "stopping"

        self._emit_output_status_changed(
            available=False,
            code="output_unavailable",
        )
        try:
            self.assistant_runtime.notify_bridge_work_available()
        except Exception:
            logger.warning("媒体会话交接时唤醒旧运行时失败", exc_info=True)

        completed = self.request_ownership_ledger.wait_for_submitted_completion(
            bridge_id=self.bridge_id,
            timeout=timeout,
        )
        if not completed:
            failed_requests = self.request_ownership_ledger.take_for_failure(
                bridge_id=self.bridge_id,
                include_queued=False,
            )
            self._emit_failed_request_records(
                failed_requests,
                reason="runtime_unavailable",
                fallback_runtime_epoch=None,
            )

        self.request_ownership_ledger.release_queued_requests(self.bridge_id)
        with self._lock:
            self._stop_event.set()
        try:
            self.assistant_runtime.notify_bridge_work_available()
        except Exception:
            logger.warning("媒体会话交接后停止旧运行时失败", exc_info=True)
        thread.join(timeout=2.0)
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def submit_request(self, request: AssistantRequest) -> bool:
        """在健康检查与队列提交的同一原子边界内接收请求。

        Args:
            request: 需要交给助手子进程的请求。

        Returns:
            请求已经进入 Bridge 队列时返回真；输出链路不可用时返回假。
        """

        failed_requests: dict[str, AssistantRequestOwnership] | None = None
        with self._lock:
            # 在加锁边界内做健康检查：输出链路不可用或正在停止时直接拒绝新请求。
            if not self._can_accept_submissions_locked():
                return False
            try:
                if self.request_identity_registry is not None:
                    self.request_identity_registry.claim(request)
                self.request_ownership_ledger.claim(
                    request,
                    bridge_id=self.bridge_id,
                )
            except AssistantRequestIdentityConflict as exception:
                # claim 和入队必须是同一个锁边界；重复身份不能覆盖旧请求或进入队列。
                self.last_error = exception
                self._health_state = "failed"
                self._stop_event.set()
                failed_requests = self._take_failed_requests_locked(include_queued=True)
                failed_requests.setdefault(
                    request.request_id,
                    AssistantRequestOwnership(
                        request=request,
                        owner_bridge_id=self.bridge_id,
                    ),
                )
                while True:
                    try:
                        self.assistant_control_queue.get_nowait()
                    except Empty:
                        break
            if failed_requests is None:
                # 请求越过队列提交边界，此后即使回调失败也视为已入队，不能再倒回。
                self.assistant_request_queue.put(request)
            # request_committed_handler 负责请求入队后的主进程侧 bookkeeping：
            # 例如 system notice 需要登记输出身份，以及通知提醒协调器请求已入队。
            if failed_requests is None and self.request_committed_handler is not None:
                try:
                    self.request_committed_handler(request)
                except Exception:
                    # handler 是旁路观测，它的异常不能伪装成入队失败，
                    # 否则队列里已有请求却无法被回滚。
                    logger.warning(
                        "Bridge 请求提交回调失败 request_id=%s",
                        request.request_id,
                        exc_info=True,
                    )

        if failed_requests is not None:
            self._emit_failed_request_records(
                failed_requests,
                reason="request_identity_conflict",
                fallback_runtime_epoch=None,
            )
            self._emit_output_status_changed(
                available=False,
                code="request_identity_conflict",
            )
        self._notify_work_available(request_id=request.request_id, work_kind="request")
        if failed_requests is not None:
            return False
        return True

    def submit_control(self, command: AssistantControlCommand) -> bool:
        """在健康检查与队列提交的同一原子边界内接收控制命令。

        Args:
            command: 需要交给助手子进程的控制命令。

        Returns:
            命令已经进入 Bridge 队列时返回真；输出链路不可用时返回假。
        """

        with self._lock:
            if not self._can_accept_submissions_locked():
                return False
            self.assistant_control_queue.put(command)

        self._notify_work_available(
            request_id=getattr(command, "request_id", None),
            work_kind="control",
        )
        return True

    def fail_for_identity_conflict(
        self,
        exception: AssistantRequestIdentityConflict,
    ) -> None:
        """因出口发现身份冲突而永久关闭当前 Bridge。

        Args:
            exception: Router 或 Journal 报告的脱敏身份冲突。

        Returns:
            None: 当前 Bridge 已接管和排队请求会统一进入失败补偿。
        """

        with self._lock:
            if self._health_state == "failed":
                return
            self.last_error = exception
            self._health_state = "failed"
            self._stop_event.set()
            failed_requests = self._take_failed_requests_locked(include_queued=True)
            while True:
                try:
                    self.assistant_control_queue.get_nowait()
                except Empty:
                    break
        self._emit_failed_request_records(
            failed_requests,
            reason="request_identity_conflict",
            fallback_runtime_epoch=None,
        )
        self._emit_output_status_changed(
            available=False,
            code="request_identity_conflict",
        )
        self._notify_work_available(
            request_id=exception.request_id,
            work_kind="identity_conflict",
        )

    def interrupt_current_output(
        self,
        *,
        issued_at: float,
        output_identity: tuple[str, str | None] | None = None,
    ) -> tuple[str, ...]:
        """在同一提交边界截住已有回复，并按请求停止生成与播放。

        Args:
            issued_at: 用户打断意图的单调时间。
            output_identity: 产品状态中仍在处理或播放的请求与生成编号。

        Returns:
            所有被打断的请求编号，供 Desktop 拦截迟到的播放通知。

        Raises:
            AssistantOutputUnavailableError: Bridge 已失效或正在交接，不能接收取消命令。
        """

        with self._lock:
            if not self._can_accept_submissions_locked():
                raise AssistantOutputUnavailableError("当前助手输出链路无法接受取消")
            identities = {
                record.request.request_id: record.generation_id
                for record in self.request_ownership_ledger.snapshot()
                if record.owner_bridge_id == self.bridge_id
            }
            if output_identity is not None:
                identities.setdefault(*output_identity)
            # 生成的双终态可能已经释放 Ledger，音轨仍在播放；必须把真实音频身份合并进回执。
            for request_id in self.audio_output_scheduler.clear_pending_audio(interrupt_before=issued_at):
                identities.setdefault(request_id, None)
            for request_id, generation_id in identities.items():
                self.audio_output_scheduler.clear_pending_audio_for_request(request_id)
                self.assistant_control_queue.put(CancelCurrentGenerationCommand(
                    generation_id=generation_id,
                    request_id=request_id,
                    reason="followup_toggle",
                    issued_at=issued_at,
                ))
        self._notify_work_available(request_id=None, work_kind="control")
        return tuple(identities)

    def remove_pending_requests(
        self,
        predicate: Callable[[AssistantRequest], bool],
    ) -> list[AssistantRequest]:
        """原子移除尚未被 Bridge 接管且满足条件的请求。

        Args:
            predicate: 判断请求是否应从待提交队列移除的函数。

        Returns:
            已从待提交队列移除的请求列表；队列不支持条件移除时返回空列表。
        """

        with self._lock:
            remove_matching = getattr(
                self.assistant_request_queue, "remove_matching", None
            )
            if not callable(remove_matching):
                return []
            removed = list(remove_matching(predicate))
            # submit_request 已经在入队前登记所有权。请求从 Bridge 本地队列撤销后
            # 不会再产生子进程终态，必须在同一锁边界释放短期所有权账本。
            for request in removed:
                self.request_ownership_ledger.remove(
                    request.request_id,
                    bridge_id=self.bridge_id,
                )
            return removed

    def _can_accept_submissions_locked(self) -> bool:
        """在持有 Bridge 锁时判断是否允许新提交。"""

        return (
            self._health_state == "running"
            and not self._handoff_mode
            and self._thread is not None
            and self._thread.is_alive()
        )

    def _notify_work_available(
        self,
        *,
        request_id: str | None,
        work_kind: str,
    ) -> None:
        """唤醒 Bridge，并保留已提交事实不受唤醒异常影响。

        Args:
            request_id: 可选请求编号，用于本地诊断。
            work_kind: request 或 control，用于本地诊断。
        """

        try:
            self.assistant_runtime.notify_bridge_work_available()
        except Exception:
            logger.warning(
                "Bridge 工作已提交但唤醒失败 work_kind=%s request_id=%s",
                work_kind,
                request_id,
                exc_info=True,
            )

    def process_once(self) -> bool:
        """同步推进一次助手运行时桥接。

        Returns:
            bool: 本轮处理到任意控制命令、请求或运行时事件时返回 True。

        Raises:
            BaseException: 底层 runtime 或音频调度器异常会向上传递给线程入口或测试调用方。
        """

        consumed_control = self.bridge_assistant_control_commands_once()
        consumed_request = self.bridge_assistant_requests_once()
        consumed_event = self.drain_assistant_runtime_once()
        return consumed_control or consumed_request or consumed_event

    def _run_thread(self) -> None:
        """Bridge 后台线程的入口。

        一直循环处理控制命令、助手请求和运行时事件，直到收到停止信号或发生异常。
        如果运行时热替换或 Bridge 失败，会先把没处理完的请求补偿出去再退出。

        Returns:
            None: 线程循环结束。
        """

        with self._lock:
            if self._stop_event.is_set() or self._health_state == "failed":
                self._started_event.set()
                return
            self._health_state = "running"
            self._started_event.set()  # 确认启动

        while not self._stop_event.is_set():
            try:
                self._compensate_replaced_runtime_requests()
                # 先清除上一轮唤醒信号，再 drain 当前所有可见工作；期间新到达的工作会重新置位事件。
                self.assistant_runtime.clear_bridge_work_available()
                if self.process_once():
                    continue
                self._wait_for_next_bridge_work()
            except BaseException as exception:
                if isinstance(exception, AssistantRequestIdentityConflict):
                    # 身份字段一旦被改写，继续消费同一运行时的后续事件只会扩大污染范围；
                    # 这里必须走一次性失败收口，并使用专门的产品错误码，不能伪装成普通输出故障。
                    logger.error(
                        "助手运行时事件身份冲突，Bridge 将永久关闭 request_id=%s field=%s",
                        exception.request_id,
                        exception.field,
                    )
                    self.fail_for_identity_conflict(exception)
                    return
                if _is_replaced_runtime_error(self.assistant_runtime, exception):
                    replaced_epoch = _runtime_epoch_from_exception(exception)
                    logger.info(
                        "旧助手运行时操作随热替换结束，bridge 将继续服务新世代 old_epoch=%s",
                        replaced_epoch,
                    )
                    # 已经提交给旧 runtime 的请求不可能由新实例续跑；待提交队列仍保留给新世代消费。
                    self._emit_failed_request_events(
                        reason="runtime_unavailable",
                        runtime_epoch=replaced_epoch,
                        include_queued=False,
                        owned_runtime_epoch=replaced_epoch,
                    )
                    continue
                self.last_error = exception
                if self._stop_event.is_set():
                    return
                runtime_epoch = _runtime_epoch_from_exception(exception)
                # 先原子关闭提交入口并取走账本，再执行日志和事件 I/O，避免诊断耗时扩大死亡队列窗口。
                failed_requests = self._mark_failed_and_take_requests()
                logger.exception(
                    "Assistant runtime bridge thread failed", exc_info=exception
                )
                self._emit_failed_request_records(
                    failed_requests,
                    reason="output_unavailable",
                    fallback_runtime_epoch=runtime_epoch,
                )
                self.event_queue.put(
                    RuntimeEvent(
                        type="assistant.runtime_unavailable",
                        source="assistant_process",
                        payload={"reason": "bridge_failed"},
                        runtime_epoch=runtime_epoch,
                    )
                )
                self._emit_output_status_changed(
                    available=False,
                    code="output_unavailable",
                    runtime_epoch=runtime_epoch,
                )
                return

    def _wait_for_next_bridge_work(self) -> None:
        """等待新的助手请求或控制命令。

        Returns:
            None: 有新输入、子进程回流事件或停止信号时返回。
        """

        self.assistant_runtime.wait_for_bridge_work(
            timeout=self.bridge_wait_timeout_sec
        )

    def _emit_output_status_changed(
        self,
        *,
        available: bool,
        code: Literal["output_unavailable", "request_identity_conflict"] | None = None,
        runtime_epoch: int | None = None,
    ) -> None:
        """投递不含内部异常详情的助手输出健康状态。

        Args:
            available: 当前 Bridge 是否允许继续提交助手请求。
            code: 不可用时的稳定产品错误码；恢复时必须为空。
            runtime_epoch: 发生故障的可选助手运行时世代。

        Returns:
            None: 状态事件只进入主进程事件队列。

        Raises:
            ValueError: 可用状态与错误码组合不合法时抛出。
        """

        if available and code is not None:
            raise ValueError("助手输出恢复事件不能携带错误码")
        if not available and code is None:
            raise ValueError("助手输出不可用事件必须携带错误码")
        health_snapshot = (
            self.output_health_ledger.mark_available(owner_id=self.bridge_id)
            if available
            else self.output_health_ledger.mark_unavailable(
                code,
                owner_id=self.bridge_id,
            )
        )
        if health_snapshot is None:
            return
        if health_snapshot.available != available or health_snapshot.code != code:
            # Ledger 已确认当前健康状态属于更新的 Bridge。旧 Bridge 的迟到故障既不能
            # 改写快照，也不能伪装成一条新的公共状态事件。
            return
        self.event_queue.put(
            RuntimeEvent(
                type="assistant.output_status_changed",
                source="assistant_process",
                payload=health_snapshot.to_event()["payload"],
                runtime_epoch=runtime_epoch,
            )
        )

    def bridge_assistant_control_commands_once(self) -> bool:
        """转发当前积压的助手控制命令。

        Returns:
            bool: 本轮转发过任意控制命令时返回 True。
        """

        consumed = False
        while True:
            with self._lock:
                if self._handoff_mode:
                    return consumed
                try:
                    command = self.assistant_control_queue.get_nowait()
                except Empty:
                    return consumed

            # 插话取消必须先停止旧语音，再通知助手 runtime，减少用户听到旧回复尾音的窗口。
            if command.kind == "cancel_current_generation":
                if command.request_id is None:
                    self.audio_output_scheduler.clear_pending_audio()
                else:
                    self.audio_output_scheduler.clear_pending_audio_for_request(command.request_id)
            elif command.kind == "cancel_timed_reminder_notice":
                self.audio_output_scheduler.clear_pending_audio_for_request(
                    command.request_id
                )

            # 工具批准命令可能在 Bridge 队列中等待一小段时间。转发前再次比较
            # 目标世代，避免热替换把旧事实送进新的助手子进程。
            if isinstance(command, SelfReflectionToolCommitDecisionCommand):
                current_epoch = self.assistant_runtime.current_epoch
                command_epoch = command.runtime_epoch
                if command_epoch != current_epoch:
                    logger.info(
                        "吸收旧助手世代 Recovery 命令 command_epoch=%s current_epoch=%s",
                        command_epoch,
                        current_epoch,
                    )
                    consumed = True
                    continue
                self.assistant_runtime.submit_control_command_for_epoch(
                    command,
                    runtime_epoch=command_epoch,
                )
            else:
                self.assistant_runtime.submit_control_command(command)
            consumed = True

    def bridge_assistant_requests_once(self) -> bool:
        """转发当前积压的助手生成请求。

        Returns:
            bool: 本轮转发过任意助手请求时返回 True。
        """

        consumed = False
        while True:
            with self._lock:
                if self._handoff_mode:
                    return consumed
                try:
                    request = self.assistant_request_queue.get_nowait()
                except Empty:
                    return consumed

                # 出队是请求所有权转移边界；之后任一步骤失败都必须能按原身份精确补偿。
                try:
                    self.request_ownership_ledger.mark_submitted(
                        request.request_id,
                        bridge_id=self.bridge_id,
                    )
                except AssistantRequestIdentityConflict as exception:
                    if exception.field != "request_id" or exception.actual != "missing":
                        raise
                    self.request_ownership_ledger.claim(
                        request,
                        bridge_id=self.bridge_id,
                    )
                    self.request_ownership_ledger.mark_submitted(
                        request.request_id,
                        bridge_id=self.bridge_id,
                    )
            logger.info(
                "有新request请求进来，清空旧音频 request_id=%s source_turn_id=%s text_length=%s",
                request.request_id,
                request.source_turn_id,
                len(request.user_text),
            )
            # 入场与 Recovery 按可见输出 settlement 串行提交。Recovery 开始时不能
            # 清掉仍在播放的入场音频，否则用户会只听到半句话。
            if request.source not in {
                "self_reflection_entry",
                "self_reflection_recovery",
            }:
                self.audio_output_scheduler.clear_pending_audio()
            try:
                runtime_epoch = self.assistant_runtime.submit_bridge_request(request)
                if self.request_identity_registry is not None:
                    self.request_identity_registry.bind_runtime_epoch(
                        request.request_id,
                        runtime_epoch,
                    )
                self.request_ownership_ledger.bind_runtime_epoch(
                    request.request_id,
                    runtime_epoch,
                    bridge_id=self.bridge_id,
                )
            except AssistantRequestIdentityConflict:
                # runtime 已经可能接收了请求，不能把它当成普通提交失败移出账本；
                # 让线程入口统一关闭 Bridge，并补偿全部已接管和排队请求。
                raise
            except Exception as exception:
                # 请求已经离开主队列但未进入 runtime，必须补齐交付终态和输出结算两个事实。
                failed_request = self.request_ownership_ledger.remove(
                    request.request_id,
                    bridge_id=self.bridge_id,
                ) or AssistantRequestOwnership(
                    request=request,
                    owner_bridge_id=self.bridge_id,
                )
                logger.exception(
                    "助手请求提交 runtime 失败 request_id=%s",
                    request.request_id,
                )
                self._emit_failed_request_records(
                    {request.request_id: failed_request},
                    reason="runtime_submit_failed",
                    fallback_runtime_epoch=_runtime_epoch_from_exception(exception),
                )
                consumed = True
                continue
            consumed = True

    def drain_assistant_runtime_once(self) -> bool:
        """把 assistant runtime 产生的事件分类发出去。

        从运行时把已经生成好的事件一轮轮取出来，然后按事件类型决定交给谁：
        - 文本、状态变化等控制事件 → 放进 event_queue，交给控制面处理
        - 音频事件 → 交给音频输出调度器播放
        - 终态事件 → 处理完后从 Bridge 账本里移除该请求

        Returns:
            bool: 本轮处理过任意事件时返回 True。
        """

        consumed = False
        for event in self.assistant_runtime.drain_events():
            consumed = True
            request_id = event.payload.get("request_id")
            if isinstance(request_id, str):
                generation_id = event.payload.get("generation_id")
                if (
                    _requires_request_identity_validation(event)
                    and self.request_identity_registry is not None
                ):
                    self.request_identity_registry.validate_event(
                        request_id=request_id,
                        runtime_epoch=event.runtime_epoch,
                        generation_id=(
                            generation_id
                            if isinstance(generation_id, str)
                            else None
                        ),
                    )
            if event.type == "assistant.generation_started":
                logger.info("助手开始生成回复 request_id=%s", request_id)
                continue
            if event.type in {
                "assistant.generation_interrupted",
                "assistant.generation_superseded",
            }:
                logger.info("助手生成被中断或替代 request_id=%s", request_id)
                if (
                    event.type == "assistant.generation_interrupted"
                    and (
                        event.payload.get("reason") == "activity_notice_superseded"
                        or event.payload.get("source")
                        == "self_reflection_interruption_reply"
                    )
                ):
                    # 子进程已经按请求身份确认旧 Notice 或辅助回应失效；这里只清理
                    # 同一请求的语音，不能误伤不可打断的 Recovery 正文。
                    if isinstance(request_id, str):
                        self.audio_output_scheduler.clear_pending_audio_for_request(
                            request_id
                        )
                # 继续往外转发
                self.event_queue.put(
                    RuntimeEvent(
                        type=event.type,
                        payload=event.payload,
                        produced_at=event.produced_at,
                        source="assistant_runtime",
                        runtime_epoch=event.runtime_epoch,
                    )
                )
                if isinstance(request_id, str):
                    self._record_event_handoff(event)
                    if event.type != "assistant.audio_finished":
                        self._finish_system_notice_playout(request_id)
                continue
            if event.type in {
                "assistant.audio_chunk",
                "assistant.audio_finished",
                "wake_response.audio_chunk",  # 唤醒回复音频
                "wake_response.audio_finished",
            }:
                # 音频数据直接交给音频调度器，不经过控制面。
                self.audio_output_scheduler.enqueue_audio_event(event)
                if isinstance(request_id, str):
                    self._record_event_handoff(event)
                    if event.type != "assistant.audio_finished":
                        self._finish_system_notice_playout(request_id)
                continue
            # 其余控制事件统一进入主进程队列，由下游按事件类型处理。
            self.event_queue.put(
                RuntimeEvent(
                    type=event.type,
                    payload=event.payload,
                    produced_at=event.produced_at,
                    source="assistant_runtime",
                    runtime_epoch=event.runtime_epoch,
                )
            )
            if isinstance(request_id, str):
                self._record_event_handoff(event)
                if event.type != "assistant.audio_finished":
                    self._finish_system_notice_playout(request_id)
        return consumed

    def _record_event_handoff(self, event: RuntimeEvent) -> None:
        """记录一条请求级事件已经成功交给主进程或音频调度器。

        Args:
            event: 已经完成下游交接的子进程运行时事件。

        Returns:
            无。只有交付终态与输出结算都成立时才删除 Bridge 请求账本。
        """

        request_id = event.payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return
        self.request_ownership_ledger.observe_event(event)

    def _finish_system_notice_playout(self, request_id: str) -> None:
        """释放已经不会继续产生语音的 System Notice 播放映射。

        Args:
            request_id: 已经完成、中断或失败的助手请求编号。
        """

        if self.system_notice_playout_tracker is None:
            return
        self.system_notice_playout_tracker.finish_request(request_id)

    def _compensate_replaced_runtime_requests(self) -> None:
        """给旧世代里还没完成的请求发中断通知。

        当助手运行时被热替换后，当前世代会变，旧世代里已经提交但还没收口的请求就失效了。
        本方法把这些旧请求找出来，逐个补发一次中断事件，并保留它们原来的身份信息。

        Returns:
            None: 每个旧世代请求都会补发一次带原身份的中断事件。
        """

        current_epoch = self.assistant_runtime.current_epoch
        stale_epochs = sorted(
            {
                owned.runtime_epoch
                for owned in self.request_ownership_ledger.snapshot()
                if owned.owner_bridge_id == self.bridge_id
                and owned.runtime_epoch is not None
                and owned.runtime_epoch != current_epoch
            }
        )
        for stale_epoch in stale_epochs:
            logger.info(
                "助手运行时已热替换，收口旧世代 Bridge 请求 old_epoch=%s current_epoch=%s",
                stale_epoch,
                current_epoch,
            )
            self._emit_failed_request_events(
                reason="runtime_unavailable",
                runtime_epoch=stale_epoch,
                include_queued=False,
                owned_runtime_epoch=stale_epoch,
            )

    def _emit_failed_request_events(
        self,
        *,
        reason: str,
        runtime_epoch: int | None,
        include_queued: bool = True,
        owned_runtime_epoch: int | None = None,
    ) -> None:
        """给 Bridge 已经没法继续处理的请求发一个“我中断了”的通知。

        这些请求可能还没交给 runtime，或者已经交给 runtime 但 runtime 挂了/被替换了。
        本方法会把它们从 Bridge 账本里拿出来，并按缺失事实补发取消与输出结算事件。

        Args:
            reason: 为什么这些请求不能继续，例如 "runtime_unavailable"。
            runtime_epoch: 出问题的运行时世代；如果不知道就填 None。
            include_queued: 是否把还在请求队列里、没交给 runtime 的请求也算进去一起补偿。
            owned_runtime_epoch: 只补偿指定世代的已接管请求；为空时全部补偿。
        """

        with self._lock:
            failed_requests = self._take_failed_requests_locked(
                include_queued=include_queued,
                owned_runtime_epoch=owned_runtime_epoch,
            )
        self._emit_failed_request_records(
            failed_requests,
            reason=reason,
            fallback_runtime_epoch=runtime_epoch,
        )

    def _mark_failed_and_take_requests(self) -> dict[str, AssistantRequestOwnership]:
        """把 Bridge 标记为失败，同时拿走所有还没处理完的请求。

        一旦 Bridge 失败，就不能再接收新请求了。本方法会：
        1. 把健康状态改成 failed，关闭提交入口。
        2. 把已接管和队列里的请求全部拿出来，准备发补偿事件。
        3. 清空控制命令队列，避免命令堆积。

        Returns:
            所有需要补偿的请求。
        """

        with self._lock:
            self._health_state = "failed"
            failed_requests = self._take_failed_requests_locked(include_queued=True)
            while True:
                try:
                    self.assistant_control_queue.get_nowait()
                except Empty:
                    break
            return failed_requests

    def _take_failed_requests_locked(
        self,
        *,
        include_queued: bool,
        owned_runtime_epoch: int | None = None,
    ) -> dict[str, AssistantRequestOwnership]:
        """在加锁状态下，把需要补偿的请求从 Bridge 账本里拿出来。

        Args:
            include_queued: 是否把还在请求队列里、没交给 runtime 的请求也一起拿出来。
            owned_runtime_epoch: 只拿指定世代的已接管请求；为空时全部拿走。
        """

        failed_requests = self.request_ownership_ledger.take_for_failure(
            bridge_id=self.bridge_id,
            include_queued=include_queued,
            runtime_epoch=owned_runtime_epoch,
        )
        if not include_queued:
            return failed_requests

        while True:
            try:
                queued_request = self.assistant_request_queue.get_nowait()
            except Empty:
                return failed_requests
            failed_requests.setdefault(
                queued_request.request_id,
                AssistantRequestOwnership(
                    request=queued_request,
                    owner_bridge_id=self.bridge_id,
                ),
            )

    def _emit_failed_request_records(
        self,
        failed_requests: dict[str, AssistantRequestOwnership],
        *,
        reason: str,
        fallback_runtime_epoch: int | None,
    ) -> None:
        """把已经从账本里拿出来的请求，变成一个个中断事件发出去。

        每个请求只补发尚未成功交给下游的交付终态和输出结算。请求自己的 runtime_epoch
        缺失时使用 fallback_runtime_epoch，确保旧世代补偿仍能按原身份收口。

        Args:
            failed_requests: 已经从账本里移出来的失败请求。
            reason: 失败原因，例如 "runtime_unavailable"。
            fallback_runtime_epoch: 请求自己没有记录世代时，作为替代的世代号。
        """

        for failed_request in failed_requests.values():
            request = failed_request.request
            self._finish_system_notice_playout(request.request_id)
            request_runtime_epoch = (
                failed_request.runtime_epoch
                if failed_request.runtime_epoch is not None
                else fallback_runtime_epoch
            )
            payload: dict[str, object] = {
                "request_id": request.request_id,
                "source": request.source,
                "source_turn_id": request.source_turn_id,
                "turn_material_context_id": request.turn_material_context_id,
                "history_policy": request.history_policy,
                "activity_progress_references": progress_references_payload(
                    request.activity_progress_references
                ),
                "derived_from_activity_ids": list(
                    request.derived_from_activity_ids
                ),
                "reason": reason,
            }
            if failed_request.visible_response_completed:
                payload["visible_response_completed"] = True
            if failed_request.generation_id is not None:
                payload["generation_id"] = failed_request.generation_id
            if not failed_request.delivery_terminal_received:
                self.event_queue.put(
                    RuntimeEvent(
                        type="assistant.generation_interrupted",
                        source="assistant_process",
                        payload=payload,
                        runtime_epoch=request_runtime_epoch,
                    )
                )
            if failed_request.output_settled_received:
                continue
            self.event_queue.put(
                RuntimeEvent(
                    type="assistant.output_settled",
                    source="assistant_process",
                    payload={
                        **_request_output_contract_payload(failed_request),
                        "settlement_id": (
                            f"output-settlement:{request.request_id}:bridge"
                        ),
                        "outcome": (
                            "failed"
                            if reason == "runtime_submit_failed"
                            else "output_unavailable"
                        ),
                    },
                    runtime_epoch=request_runtime_epoch,
                )
            )


def _requires_request_identity_validation(event: RuntimeEvent) -> bool:
    """判断子进程事件是否必须绑定已登记的来源助手请求。

    Args:
        event: 刚从稳定助手运行时排出的事件。

    Returns:
        助手输出事件携带请求编号时返回真。
    """

    is_assistant_event = event.type.startswith("assistant.")
    request_id = event.payload.get("request_id")
    return (
        is_assistant_event
        and isinstance(request_id, str)
        and bool(request_id.strip())
    )


def _runtime_epoch_from_exception(exception: BaseException) -> int | None:
    """读取稳定运行时代理附着在异常上的世代。

    Args:
        exception: bridge 捕获的底层运行时异常。

    Returns:
        可归因的运行时世代；普通测试替身异常返回空。
    """

    runtime_epoch = getattr(exception, "runtime_epoch", None)
    return runtime_epoch if isinstance(runtime_epoch, int) else None


def _request_output_contract_payload(
    owned_request: AssistantRequestOwnership,
) -> dict[str, object]:
    """复制 Bridge 补偿输出结算需要的完整请求契约。

    Args:
        owned_request: 仍由 Bridge 持有的请求状态。

    Returns:
        可直接写入 `assistant.output_settled` 的身份字段。
    """

    request = owned_request.request
    payload: dict[str, object] = {
        "request_id": request.request_id,
        "generation_id": owned_request.generation_id,
        "source": request.source,
        "source_turn_id": request.source_turn_id,
        "turn_material_context_id": request.turn_material_context_id,
        "history_policy": request.history_policy,
        "activity_progress_references": progress_references_payload(
            request.activity_progress_references
        ),
        "derived_from_activity_ids": list(request.derived_from_activity_ids),
    }
    reflection_context = request.self_reflection_context
    if reflection_context is not None:
        payload["self_reflection_id"] = reflection_context.self_reflection_id
        payload["episode_id"] = reflection_context.episode_id
        payload["runtime_epoch"] = reflection_context.runtime_epoch
        trigger_input_identity = getattr(
            reflection_context,
            "trigger_input_identity",
            None,
        )
        if isinstance(trigger_input_identity, str) and trigger_input_identity:
            payload["trigger_input_identity"] = trigger_input_identity
    return payload


def _is_replaced_runtime_error(
    assistant_runtime: SwappableAssistantProcessClient,
    exception: BaseException,
) -> bool:
    """判断异常是否只来自已经被热替换的旧运行时。

    Args:
        assistant_runtime: bridge 使用的稳定运行时句柄。
        exception: 当前循环捕获的运行时异常。

    Returns:
        异常世代已不再是当前世代时返回真。
    """

    failed_epoch = _runtime_epoch_from_exception(exception)
    current_epoch = getattr(assistant_runtime, "current_epoch", None)
    return (
        failed_epoch is not None
        and isinstance(current_epoch, int)
        and failed_epoch != current_epoch
    )
