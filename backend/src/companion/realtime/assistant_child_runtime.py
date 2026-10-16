from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from time import monotonic

from companion.observability.logging_config import get_backend_logger
from companion.foreground_tools.background_agent.submission_broker import (
    WorkInteractionSubmissionBroker,
)
from companion.foreground_tools.task_memory.broker import TaskMemoryLookupBroker
from companion.foreground_tools.activity_control.broker import (
    ActivityCancellationBroker,
)
from companion.foreground_tools.timed_reminders.broker import (
    TimedReminderCommandBroker,
)
from companion.foreground_self_reflection.tool_commit_broker import (
    SelfReflectionToolCommitBroker,
)
from companion.runtime_memory.compaction import RuntimeMemoryCompactionWorker
from companion.realtime.assistant_events import (
    AcknowledgeActivityProgressCommand,
    ActivityCancellationResultCommand,
    AssistantControlCommand,
    AssistantRequest,
    CancelActivityCommand,
    CancelCurrentGenerationCommand,
    RequestSelfReflectionFallbackCommand,
    CancelSupersededSystemNoticeCommand,
    CancelTimedReminderNoticeCommand,
    BackgroundAgentInteractionAnswerCommand,
    BackgroundAgentInteractionSubmissionRejectedCommand,
    RuntimeEvent,
    TaskMemoryLookupResultCommand,
    TimedReminderCommandResultCommand,
    RuntimeMemoryCompactionPlanCommand,
    SelfReflectionToolCommitDecisionCommand,
)
from companion.realtime.assistant_output_lanes import AssistantOutputLanes
from companion.foreground_self_reflection.coordinator import ForegroundSelfReflectionCoordinator


logger = get_backend_logger(__name__)


class AssistantChildRuntime:
    """拥有助手子进程内的输出通道与关闭生命周期。

    Args:
        runner: 已经装配真实 LLM/TTS 依赖的助手输出通道。
        shutdown_callbacks: 输出 worker 退出后需要关闭的附属运行资源。
        cancel_activity_handlers: Activity 来源到双参数真实取消端口的映射。
    """

    def __init__(
        self,
        *,
        runner: AssistantOutputLanes,
        shutdown_callbacks: Sequence[Callable[[float], None]] = (),
        cancel_activity_handlers: Mapping[str, Callable[[str, str], bool]] | None = None,
        answer_background_interaction: Callable[[BackgroundAgentInteractionAnswerCommand], bool]
        | None = None,
        work_interaction_submission_broker: WorkInteractionSubmissionBroker | None = None,
        task_memory_lookup_broker: TaskMemoryLookupBroker | None = None,
        activity_cancellation_broker: ActivityCancellationBroker | None = None,
        timed_reminder_command_broker: TimedReminderCommandBroker | None = None,
        runtime_memory_compaction_worker: RuntimeMemoryCompactionWorker | None = None,
        self_reflection_coordinator: ForegroundSelfReflectionCoordinator | None = None,
        self_reflection_tool_commit_broker: SelfReflectionToolCommitBroker | None = None,
    ) -> None:
        """初始化助手子进程运行资源。

        Args:
            runner: 已经装配真实 LLM/TTS 依赖的输出通道。
            shutdown_callbacks: worker 退出后需要显式关闭的附属运行时。
            cancel_activity_handlers: 内部活动来源到 Activity 编号和取消原因处理函数的映射。
            answer_background_interaction: 把已裁决回答交给后台执行器的函数。
            work_interaction_submission_broker: 当前子进程内等待权威回执的 Broker。
            task_memory_lookup_broker: 当前子进程内等待主进程任务详情的 Broker。
            activity_cancellation_broker: 当前子进程内等待主进程取消回执的 Broker。
            timed_reminder_command_broker: 当前子进程内等待主进程提醒写回执的 Broker。
            runtime_memory_compaction_worker: 只执行 Summary 调用的压缩 Worker。
            self_reflection_coordinator: 执行可见 Recovery 已选择工具事务的 Coordinator。
            self_reflection_tool_commit_broker: 等待主进程工具提交回执的 Broker。
        """

        self.runner = runner
        self._shutdown_callbacks = tuple(shutdown_callbacks)
        self._cancel_activity_handlers = dict(cancel_activity_handlers or {})
        self._answer_background_interaction = answer_background_interaction
        self._work_interaction_submission_broker = (
            work_interaction_submission_broker
        )
        self._task_memory_lookup_broker = task_memory_lookup_broker
        self._activity_cancellation_broker = activity_cancellation_broker
        self._timed_reminder_command_broker = timed_reminder_command_broker
        self._runtime_memory_compaction_worker = runtime_memory_compaction_worker
        self._self_reflection_coordinator = self_reflection_coordinator
        self._self_reflection_tool_commit_broker = (
            self_reflection_tool_commit_broker
        )

    @property
    def tts_provider(self) -> object:
        """返回子进程内唯一 TTS provider，供管理命令使用。

        Returns:
            当前 runner 持有的语音合成 provider。
        """

        return self.runner.tts_provider

    def start(self) -> None:
        """启动常驻 LLM/TTS worker。"""

        self.runner.start()

    def shutdown(self, timeout: float = 1.0) -> None:
        """关闭常驻 worker 和附属调度运行时。

        Args:
            timeout: 每个关闭边界允许使用的最长等待秒数。
        """

        normalized_timeout = max(timeout, 0.0)
        deadline = monotonic() + normalized_timeout
        if self._self_reflection_tool_commit_broker is not None:
            self._self_reflection_tool_commit_broker.shutdown(
                reason="runtime_shutdown"
            )
        if self._self_reflection_coordinator is not None:
            # Broker 先给未决请求终局，Coordinator 才能让未提交 reservation 回滚；
            # 已获批 Background 提交则在普通输出和执行器关闭前越过 after-commit。
            self._self_reflection_coordinator.shutdown(
                timeout=max(0.0, deadline - monotonic())
            )
            runner_timeout = max(0.0, deadline - monotonic())
        else:
            # 没有 Self-Reflection 附属资源时保留原合同，主 Runner 先取得完整预算。
            runner_timeout = normalized_timeout
        self.runner.shutdown(timeout=runner_timeout)
        for callback in self._shutdown_callbacks:
            try:
                callback(max(0.0, deadline - monotonic()))
            except Exception:
                logger.exception("助手附属运行时关闭失败")

    def drain_events(self) -> list[RuntimeEvent]:
        """读取 runner 已产出的 runtime 事件。

        Returns:
            当前已经产生且尚未转发的运行时事件。
        """

        return self.runner.drain_events()

    def forward_event_to_main(
        self,
        event: RuntimeEvent,
        *,
        send_event: Callable[[RuntimeEvent], None],
    ) -> bool:
        """把本地事件写入跨进程队列，并在线性化点交接提交所有权。

        Args:
            event: 子进程本地 FIFO 已经产出的运行时事件。
            send_event: 写入主进程跨进程事件队列的同步端口。

        Returns:
            事件已经跨进程写入时返回真；失效的 Self-Reflection 提交请求
            或暂时写入失败时返回假。
        """

        if event.type == "foreground.self_reflection_tool_commit_requested":
            if self._self_reflection_tool_commit_broker is None:
                return False
            return self._self_reflection_tool_commit_broker.forward_request_to_main(
                event,
                send_event=send_event,
            )
        send_event(event)
        return True

    def start_request(self, request: AssistantRequest) -> str:
        """启动最新助手请求。

        Args:
            request: 已通过跨进程边界的助手请求。

        Returns:
            为本次请求分配的 generation 编号。
        """

        return self.runner.start_request(request)

    def mark_request_superseded(self, request: AssistantRequest) -> None:
        """记录尚未启动就被同轮最新请求覆盖的旧请求。

        Args:
            request: 被同批次更新请求覆盖的旧助手请求。
        """

        self.runner.mark_request_superseded(request)

    def warmup_foreground(self) -> None:
        """触发前台模型最小预热。"""

        self.runner.warmup_foreground()

    def handle_control_command(self, command: AssistantControlCommand) -> None:
        """处理助手运行时控制命令。

        Args:
            command: 需要应用到当前 generation 的控制命令。
        """

        if isinstance(command, CancelCurrentGenerationCommand):
            self.runner.cancel_current_generation(
                reason=command.reason,
                generation_id=command.generation_id,
                request_id=command.request_id,
            )
            return
        if isinstance(command, RequestSelfReflectionFallbackCommand):
            self.runner.request_recovery_fallback(
                self_reflection_id=command.self_reflection_id,
                reason=command.reason,
            )
            return
        if isinstance(command, CancelSupersededSystemNoticeCommand):
            self.runner.cancel_superseded_system_notice(
                request_id=command.request_id,
                reason=command.reason,
            )
            return
        if isinstance(command, CancelTimedReminderNoticeCommand):
            self.runner.cancel_timed_reminder_notice(
                request_id=command.request_id,
                reason=command.reason,
            )
            return
        if isinstance(command, BackgroundAgentInteractionAnswerCommand):
            if self._answer_background_interaction is None:
                return
            if (
                command.submission_id is not None
                and self._work_interaction_submission_broker is not None
            ):
                self._work_interaction_submission_broker.apply_answer_command(
                    command,
                    submit_answer=self._answer_background_interaction,
                )
                return
            self._answer_background_interaction(command)
            return
        if isinstance(
            command,
            BackgroundAgentInteractionSubmissionRejectedCommand,
        ):
            if self._work_interaction_submission_broker is not None:
                self._work_interaction_submission_broker.reject_submission(
                    command
                )
            return
        if isinstance(command, TaskMemoryLookupResultCommand):
            if self._task_memory_lookup_broker is not None:
                self._task_memory_lookup_broker.apply_result(command)
            return
        if isinstance(command, ActivityCancellationResultCommand):
            if self._activity_cancellation_broker is not None:
                self._activity_cancellation_broker.apply_result(command)
            return
        if isinstance(command, TimedReminderCommandResultCommand):
            if self._timed_reminder_command_broker is not None:
                self._timed_reminder_command_broker.apply_result(command)
            return
        if isinstance(command, RuntimeMemoryCompactionPlanCommand):
            if self._runtime_memory_compaction_worker is not None:
                self._runtime_memory_compaction_worker.submit(command.plan)
            return
        if isinstance(command, AcknowledgeActivityProgressCommand):
            self.runner.acknowledge_activity_progress(
                references=command.references,
            )
            return
        if isinstance(command, SelfReflectionToolCommitDecisionCommand):
            if self._self_reflection_tool_commit_broker is not None:
                self._self_reflection_tool_commit_broker.apply_decision(command)
            return
        if not isinstance(command, CancelActivityCommand):
            return
        handler = self._cancel_activity_handlers.get(command.source)
        if handler is None:
            logger.error("未注册 Activity 取消来源 source=%s", command.source)
            return
        handler(command.activity_id, command.reason)
