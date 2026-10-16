from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import replace
from threading import Event, Lock
from typing import Any, Protocol
from uuid import uuid4

from companion.activities.foreground_snapshot import ForegroundActivitySnapshot
from companion.character_cards.models import CharacterCardDefinition
from companion.llm.foreground_models import (
    TTS_DELIVERY_IMMEDIATE,
    ForegroundEvent,
    ForegroundExecutionContext,
    ForegroundModelStreamChunk,
    ForegroundRequestMode,
    ForegroundToolCall,
)
from companion.llm.call_trace import ForegroundRequestTrace, LlmCallTrace
from companion.llm.output_policies import LlmProtocolLeakError
from companion.llm.request_intent import LlmInvocationIntent
from companion.llm.tool_protocol import build_assistant_tool_call_message
from companion.llm.response_commit import ForegroundResponseCommit
from companion.materials.foreground_text import load_foreground_text_batch
from companion.materials.foreground_images import (
    FrozenVisualInput,
    load_foreground_visual_input,
    merge_frozen_visual_inputs,
)
from companion.materials.foreground_view import render_material_history_note
from companion.core.local_clock import format_local_datetime_text, local_wall_datetime
from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.catalog.waiting_policy import ToolWaitingPolicyResolver
from companion.foreground_tools.inline_execution import InlineExecutionContext
from companion.foreground_tools.orchestration import (
    ExecutedToolCall,
    ForegroundToolCallOrchestrator,
    ForegroundToolExecution,
    ToolFollowupFact,
    ToolReplyRequirement,
    InlineToolResult,
    InlineToolCompleted,
)
from companion.foreground_tools.orchestration.transactions import (
    BackgroundActivityReservation,
    CommitAction,
)
from companion.foreground_tools.activity_control.broker import (
    ActivityCancellationBroker,
)
from companion.foreground_tools.activity_control.execution import (
    ActivityCancellationReservation,
    ActivityCancellationToolExecutor,
)
from companion.foreground_tools.activity_control.policy import (
    CancellableActivityContext,
    select_cancellable_activity_contexts,
)
from companion.foreground_tools.activity_control.schemas import (
    is_cancel_activity_tool,
)
from companion.foreground_tools.timed_reminders.broker import (
    TimedReminderCommandBroker,
)
from companion.foreground_tools.timed_reminders.execution import (
    TimedReminderCommandReservation,
    TimedReminderToolExecutor,
)
from companion.foreground_tools.timed_reminders.schemas import (
    is_timed_reminder_tool,
)
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.llm.foreground_request_policy import foreground_request_policy
from companion.foreground_tools.cognitive.contracts import CognitiveExecutionPlan
from companion.observability.logging_config import get_backend_logger
from companion.observability.structured_logging import log_event
from companion.prompt.builder import (
    PromptBuildInput,
    PromptBuilder,
)
from companion.prompt.tool_followup import (
    BACKGROUND_WORK_ALREADY_RUNNING_MESSAGE,
    build_tool_followup_messages,
    render_tool_followup_fallback,
)
from companion.prompt.tool_followup_policy import (
    VisibleTransitionKind,
    select_tool_followup_plan,
)
from companion.prompt.system_state import freeze_attached_strong_notices
from companion.prompt.activity_state import (
    compile_activity_context,
    progress_reports_from_payload,
)
from companion.realtime.assistant_output_contracts import (
    ActivityProgressReference,
    AssistantHistoryPolicy,
)
from companion.realtime.assistant_events import (
    RECENT_MATERIAL_OBSERVATION_FIELD,
    RecentMaterialObservation,
)
from companion.realtime.sentence_queue import StreamingSentenceBuffer
from companion.realtime.text_tokenizer import count_text_tokens
from companion.session.context import (
    SessionContext,
    SessionMessage,
)
from companion.foreground_tools.cognitive.schemas import is_cognitive_tool
from companion.foreground_tools.cognitive.tools import CognitiveToolContext, CognitiveToolExecutor
from companion.foreground_tools.background_agent.schemas import is_start_background_work_tool
from companion.foreground_tools.background_agent.interaction_response import (
    is_respond_to_work_interaction_tool,
)
from companion.foreground_tools.background_agent.execution import (
    BackgroundAgentToolExecutor,
)
from companion.foreground_tools.background_agent.materials import (
    materials_from_foreground_context,
)
from companion.foreground_tools.activity_correction.policy import (
    CorrectableActivityContext,
    select_correctable_activity_contexts,
)
from companion.foreground_tools.activity_correction.execution import (
    ActivityCorrectionToolExecutor,
)
from companion.foreground_tools.activity_correction.schemas import (
    CORRECT_BACKGROUND_WORK_TOOL,
    CORRECT_COGNITIVE_ACTIVITY_TOOL,
    is_activity_correction_tool,
)
from companion.foreground_tools.activity_correction.service import ActivityCorrectionService
from companion.foreground_self_reflection.contracts import (
    SelfReflectionRecoveryContext,
)
from companion.foreground_self_reflection.prompt import (
    build_recovery_tool_followup_prompt,
    recovery_tool_followup_fallback,
    RecoveryToolCapability,
    render_recovery_instruction,
)
from companion.foreground_self_reflection.models import SelfReflectionOutcomeFact
from companion.foreground_self_reflection.tool_policy import (
    SelfReflectionToolDefinition,
)
from companion.foreground_tools.background_agent.submission_broker import (
    WorkInteractionSubmissionBroker,
)
from companion.foreground_tools.task_memory.broker import TaskMemoryLookupBroker
from companion.foreground_tools.task_memory.schemas import is_recall_task_memory_tool
from companion.foreground_tools.recent_image.schemas import is_load_recent_image_for_foreground_tool
from companion.foreground_tools.recent_image.execution import execute_load_recent_image
from companion.background_agent.activity_resources import (
    BACKGROUND_AGENT_UNCONFIGURED_MESSAGE,
)
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.runtime_memory.models import RuntimeMemorySnapshot
from companion.runtime_memory.compaction import RuntimeMemoryCompactionWorker


foreground_logger = get_backend_logger(__name__)

# 工具二轮只需近期对话来消解指代；固定上限避免重新引入完整协议历史。
_TOOL_FOLLOWUP_VISIBLE_DIALOGUE_LIMIT = 8


class ForegroundClientProtocol(Protocol):
    """定义前台模型客户端需要实现的最小能力。"""

    def warmup(self) -> None:
        """执行一次幂等的最小连接预热。"""

    def stream_message(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        intent: LlmInvocationIntent,
        audit_source: str,
        audit_operation: str,
        trace: LlmCallTrace | None = None,
        cancellation_event: Event | None = None,
    ) -> Iterable[ForegroundModelStreamChunk]:
        """按到达顺序返回模型流式片段。

        Args:
            messages: 已组装完成的对话消息列表。
            tools: 本轮允许暴露给模型的工具声明。
            intent: 本轮工具、思考与输出格式等语义调用意图。
            audit_source: 触发本次调用的真实请求来源。
            audit_operation: 当前是首轮还是工具后二轮。

        Returns:
            可迭代的前台模型流式片段序列。
        """

class CognitiveActivityExecutorProtocol(Protocol):
    """定义 foreground 提交后台认知任务需要的最小接口。"""

    def prepare(
        self,
        *,
        interaction_session_id: str,
        plan: CognitiveExecutionPlan,
        source_user_text: str,
    ) -> tuple[bool, list[dict[str, object]]]:
        """登记一个等待 generation 接受的后台认知计划。

        Args:
            interaction_session_id: 任务所属交互会话编号。
            plan: 同一轮全部认知目标合并后的执行计划。
            source_user_text: 触发任务的真实用户正文。

        Returns:
            是否接受任务及立即产生的生命周期事件。
        """

    def start(self, activity_id: str) -> bool:
        """启动已经被当前 generation 接受的认知任务。"""

    def cancel_before_start(self, activity_id: str) -> bool:
        """回滚尚未被当前 generation 接受的认知任务。"""

    def snapshot(self, interaction_session_id: str) -> object:
        """读取当前会话的认知任务状态。

        Args:
            interaction_session_id: 需要查询的交互会话编号。

        Returns:
            面向认知工具执行层的任务状态快照。
        """

    def activity_snapshots(
        self,
        interaction_session_id: str,
    ) -> tuple[ForegroundActivitySnapshot, ...]:
        """读取 foreground 当前可使用的活动事实集合。

        Args:
            interaction_session_id: 需要查询的交互会话编号。

        Returns:
            当前活动的只读快照集合；没有活动时返回空元组。
        """

    def acknowledge_progress_notice(self, activity_id: str, sequence: int) -> bool:
        """确认 foreground 已表达指定序号的冻结进度。

        Args:
            activity_id: 已完成表达的活动编号。
            sequence: 已表达进度覆盖到的冻结序号。

        Returns:
            Activity 执行器接受本次确认时返回真。
        """

    def drain_events(self, limit: int = 100) -> list[dict[str, object]]:
        """按预算取出后台认知任务事件。

        Args:
            limit: 本次最多取出的事件数量。

        Returns:
            按产生顺序排列的内部认知任务事件。
        """


class BackgroundAgentActivityExecutorProtocol(Protocol):
    """定义 Foreground 派单后台智能体需要的最小接口。"""

    def prepare(self, **kwargs: object) -> tuple[bool, list[dict[str, object]]]:
        """登记后台智能体任务。"""

    def start(self, activity_id: str) -> bool:
        """启动已提交的后台智能体任务。"""

    def cancel_before_start(self, activity_id: str) -> bool:
        """回滚尚未启动的后台智能体任务。"""

    def background_available(self) -> bool:
        """返回当前提交边界是否可以接受新后台任务。

        Returns:
            后台配置、Runner 和容量都允许 prepare 时返回真。
        """

    def activity_snapshots(
        self,
        interaction_session_id: str,
    ) -> tuple[ForegroundActivitySnapshot, ...]:
        """读取普通与恢复通道的活动快照集合。"""

    def drain_events(self, limit: int = 100) -> list[dict[str, object]]:
        """读取后台智能体事件。"""


class SelfReflectionRecoveryToolExecutorProtocol(Protocol):
    """定义可见 Recovery 调用唯一安全工具时使用的事务端口。"""

    def execute_tool(
        self,
        *,
        context: SelfReflectionRecoveryContext,
        tool_call: ForegroundToolCall,
        interaction_session_id: str,
        foreground_context: dict[str, object],
        environment_snapshot: FrozenEnvironmentSnapshot | None,
        cancellation_event: Event | None,
    ) -> SelfReflectionOutcomeFact:
        """执行一次已通过静态白名单的工具，并返回 canonical 恢复事实。"""


class ForegroundTurnProcessor:
    """负责前台对话流、工具调用和 route 粗提示的统一编排。"""

    def __init__(
        self,
        *,
        client: ForegroundClientProtocol,
        prompt_builder: PromptBuilder,
        session_context: SessionContext,
        foreground_tools: ForegroundToolService,
        tool_waiting_policy_resolver: ToolWaitingPolicyResolver | None = None,
        cognitive_activity_executor: CognitiveActivityExecutorProtocol | None = None,
        background_agent_activity_executor: BackgroundAgentActivityExecutorProtocol | None = None,
        work_interaction_submission_broker: WorkInteractionSubmissionBroker | None = None,
        task_memory_lookup_broker: TaskMemoryLookupBroker | None = None,
        activity_cancellation_broker: ActivityCancellationBroker | None = None,
        timed_reminder_command_broker: TimedReminderCommandBroker | None = None,
        runtime_memory_compaction_worker: RuntimeMemoryCompactionWorker | None = None,
        background_agent_tool_executor: BackgroundAgentToolExecutor | None = None,
        background_commit_barrier: BackgroundCommitDrainBarrier | None = None,
        self_reflection_tool_executor: (
            SelfReflectionRecoveryToolExecutorProtocol | None
        ) = None,
    ) -> None:
        """初始化前台对话编排器。

        Args:
            client: 前台模型客户端，负责输出文本流和工具调用。
            prompt_builder: 提示词构建器，负责拼装系统提示与会话历史。
            session_context: 会话上下文，用于记录用户与助手消息。
            foreground_tools: foreground 工具服务，负责提供工具声明并执行同步工具。
            tool_waiting_policy_resolver: 可选工具等待反馈策略解析器。
            cognitive_activity_executor: 可选认知 Activity 执行器，用于执行后台综合认知计划。
            background_agent_activity_executor: 可选后台智能体 Activity 执行器，用于处理明确派单任务。
            work_interaction_submission_broker: 自然语言工作回答等待权威回执的 Broker。
            task_memory_lookup_broker: 按 Activity 编号等待主进程详情的 Broker。
            activity_cancellation_broker: 等待主进程 Activity 取消回执的 Broker。
            timed_reminder_command_broker: 等待主进程提醒写命令回执的 Broker。
            runtime_memory_compaction_worker: 执行主进程压缩计划的 Summary Worker。
            background_agent_tool_executor: 共享后台工具提交执行器；传入时由普通 Foreground
                与自我反思共同使用同一个 canonical 提交账本。
            background_commit_barrier: Background 提交与执行器关闭共用的排水屏障。
            self_reflection_tool_executor: 可见 Recovery 的唯一工具事务执行器。
        """

        self._client = client
        self._prompt_builder = prompt_builder
        self._session_context = session_context
        self._foreground_tools = foreground_tools
        self._tool_waiting_policy_resolver = tool_waiting_policy_resolver
        self._cognitive_activity_executor = cognitive_activity_executor
        self._background_agent_activity_executor = background_agent_activity_executor
        self._work_interaction_submission_broker = (
            work_interaction_submission_broker
        )
        self._task_memory_lookup_broker = task_memory_lookup_broker
        self._activity_cancellation_broker = activity_cancellation_broker
        self._activity_cancellation_tool_executor = (
            ActivityCancellationToolExecutor(activity_cancellation_broker)
            if activity_cancellation_broker is not None
            else None
        )
        self._timed_reminder_command_broker = timed_reminder_command_broker
        self._timed_reminder_tool_executor = (
            TimedReminderToolExecutor(timed_reminder_command_broker)
            if timed_reminder_command_broker is not None
            else None
        )
        self._runtime_memory_compaction_worker = runtime_memory_compaction_worker
        self._self_reflection_tool_executor = self_reflection_tool_executor
        self._background_agent_tool_executor = background_agent_tool_executor or BackgroundAgentToolExecutor(
            background_agent_activity_executor,
            work_interaction_submission_broker,
            task_memory_lookup_broker,
            commit_barrier=background_commit_barrier,
        )
        self._activity_correction_tool_executor = (
            ActivityCorrectionToolExecutor(
                correction_service=ActivityCorrectionService(
                    cognitive_executor=cognitive_activity_executor,
                    background_executor=background_agent_activity_executor,
                    background_commit_barrier=background_commit_barrier,
                ),
            )
            if cognitive_activity_executor is not None
            or background_agent_activity_executor is not None
            else None
        )
        self._cognitive_tool_executor = (
            CognitiveToolExecutor(
                activity_executor=cognitive_activity_executor,
            )
            if cognitive_activity_executor is not None
            else None
        )
        # 编排前台工具调用：决定什么时候调工具、怎么把结果续写回对话。
        self._tool_call_orchestrator = ForegroundToolCallOrchestrator()
        # 流式输出中判断“是不是要开始工具调用”的最小 token 阈值。
        self._pre_tool_prefix_min_tokens = StreamingSentenceBuffer().min_segment_tokens
        self._token_counter = count_text_tokens
        # 终态排水与进度确认共用短锁，避免清账本和迟到提交交错后留下僵尸记录。
        self._activity_progress_commit_lock = Lock()

    def warmup_foreground(self) -> None:
        """按客户端能力触发前台模型最小预热。"""

        self._client.warmup()

    def shutdown(self, timeout: float = 1.0) -> None:
        """关闭 Foreground 编排器持有的共享资源。

        Args:
            timeout: 为运行时统一关闭接口保留的超时秒数。
        """

        self._tool_call_orchestrator.shutdown(timeout)

    def create_interrupted_response_event(
        self,
        display_text: str,
        *,
        model_text: str,
        request_id: str,
        history_policy: AssistantHistoryPolicy,
        progress_references: tuple[ActivityProgressReference, ...],
        interruption_reason: str = "",
    ) -> ForegroundEvent:
        """创建记录真实可见片段的中断最终事件。

        Args:
            display_text: 已附加中断标记的 Desktop 可见文本。
            model_text: 模型在取消前真正公开的正文，不包含 UI 尾注。
            request_id: 这段文字所属的原始助手请求编号。
            history_policy: 正文进入稳定对话还是 Progress 临时账本。
            progress_references: Progress 正文关联的 Activity 冻结进度。
            interruption_reason: 系统主动接管等稳定中断原因。

        Returns:
            只有被 supervisor 接受后才提交内部副作用的最终事件。
        """

        _ = progress_references
        response_payload: dict[str, object] = {
            "text": display_text,
            "settlement_text": model_text,
            "interrupted": True,
        }

        def commit_interrupted_response() -> None:
            """只为稳定对话生成不含 UI 尾注的主进程提交。"""

            if history_policy != "stable_dialogue":
                return
            if model_text.strip():
                self._session_context.record_assistant_reply(
                    content=model_text,
                    metadata={
                        "interrupted": True,
                        "incomplete": interruption_reason == "self_reflection_takeover",
                        "interruption_reason": interruption_reason,
                    },
                )
            if interruption_reason == "self_reflection_takeover":
                record_system_message = getattr(
                    self._session_context,
                    "record_system_message",
                    None,
                )
                if callable(record_system_message):
                    record_system_message(
                        content="这轮回答未完成，因系统主动接管而中断，不会自动重放。",
                        metadata={
                            "fact": "incomplete_assistant_response",
                            "interruption_reason": interruption_reason,
                        },
                    )
            if not model_text.strip() and interruption_reason != "self_reflection_takeover":
                return
            self._attach_memory_commit(
                response_payload,
                request_id=request_id,
            )

        response_commit = ForegroundResponseCommit.create(
            kind="interrupted",
            visible_text=display_text,
            on_accept=commit_interrupted_response,
        )
        return ForegroundEvent(
            type="assistant.text_final",
            payload=response_payload,
            commit_action=response_commit.action,
        )

    def create_runtime_fallback_event(
        self,
        text: str,
        *,
        request_id: str,
        history_policy: AssistantHistoryPolicy,
        mark_notice_delivery_failed: bool = True,
    ) -> ForegroundEvent:
        """创建运行时错误兜底最终事件。

        Args:
            text: 用户实际看到的兜底回复。
            request_id: 这条兜底所属的原始助手请求编号。
            history_policy: 正文归入稳定对话、Progress 还是未处理 episode。
            mark_notice_delivery_failed: 是否把这条可见正文标记为 Notice 未完成。
                自我反思使用冻结安全正文时应传假，因为用户已经收到完整事实。

        Returns:
            只有被 supervisor 接受后才写历史的最终事件。
        """

        response_payload: dict[str, object] = {"text": text}
        if mark_notice_delivery_failed:
            response_payload["notice_delivery_failed"] = True

        def commit_runtime_fallback() -> None:
            """写入运行时兜底回复并生成主进程提交。"""

            if history_policy != "stable_dialogue":
                return
            self._session_context.record_assistant_reply(
                content=text,
                metadata={"runtime_fallback": True},
            )
            self._attach_memory_commit(
                response_payload,
                request_id=request_id,
            )

        response_commit = ForegroundResponseCommit.create(
            kind="runtime_fallback",
            visible_text=text,
            on_accept=commit_runtime_fallback,
        )
        return ForegroundEvent(
            type="assistant.text_final",
            payload=response_payload,
            commit_action=response_commit.action,
        )

    def create_direct_output_event(
        self,
        text: str,
        *,
        request_id: str,
        history_policy: AssistantHistoryPolicy,
    ) -> ForegroundEvent:
        """创建不经过 Foreground LLM 的普通最终正文事件。

        Args:
            text: 主进程已经确定的完整可见正文。
            request_id: 这条正文所属的助手请求编号。
            history_policy: 正文归入稳定对话、Progress 或未处理片段的策略。

        Returns:
            只有被 supervisor 接受后才写入对应历史的最终事件。
        """

        response_payload: dict[str, object] = {"text": text}

        def commit_direct_output() -> None:
            """把确定性正文作为普通角色回复写入稳定历史。"""

            if history_policy != "stable_dialogue":
                return
            self._session_context.record_assistant_reply(content=text)
            self._attach_memory_commit(
                response_payload,
                request_id=request_id,
            )

        response_commit = ForegroundResponseCommit.create(
            kind="assistant",
            visible_text=text,
            on_accept=commit_direct_output,
        )
        return ForegroundEvent(
            type="assistant.text_final",
            payload=response_payload,
            commit_action=response_commit.action,
        )

    def _provider_protocol_failure_event(
        self,
        *,
        exception: LlmProtocolLeakError,
        request_trace: ForegroundRequestTrace | None,
    ) -> ForegroundEvent:
        """把厂商协议泄漏转换为不产生可见内容的提醒失败事件。

        Args:
            exception: Provider 协议层报告的通用泄漏异常。
            request_trace: 当前请求及 generation 的日志追踪身份。
        Returns:
            交给 Notice 协调层执行独立重试规则的空终态事件。
        """

        log_event(
            foreground_logger,
            logging.WARNING,
            "foreground_provider_protocol_leak",
            **_request_trace_fields(request_trace),
            provider_id=exception.provider_id,
            protocol=exception.protocol,
        )
        return ForegroundEvent(
            type="assistant.text_final",
            payload={
                "text": "",
                "notice_delivery_failed": True,
                "notice_failure_reason": "provider_protocol_leak",
            },
        )

    def stream_user_text(
        self,
        text: str,
        *,
        now: float,
        interaction_session_id: str,
        character_card: CharacterCardDefinition,
        user_call_name: str,
        user_self_intro: str,
        user_extra_notes: str,
        streamvox_model: str = "",
        foreground_context: dict[str, object] | None = None,
        environment_snapshot: FrozenEnvironmentSnapshot | None = None,
        frozen_visual_input: FrozenVisualInput | None = None,
        request_mode: ForegroundRequestMode = "user_turn",
        cancellation_event: Event | None = None,
        request_trace: ForegroundRequestTrace | None = None,
        runtime_memory_snapshot: RuntimeMemorySnapshot | None = None,
        self_reflection_recovery_context: SelfReflectionRecoveryContext | None = None,
    ) -> Iterable[ForegroundEvent]:
        """流式处理一条用户文本并持续产出前台事件。

        Args:
            text: 用户原始输入。
            now: 当前时间戳。
            interaction_session_id: 主进程产品交互会话标识。
            character_card: 本轮请求使用的角色卡快照。
            user_call_name: 本轮请求使用的用户称呼。
            user_self_intro: 本轮请求使用的用户自我介绍。
            user_extra_notes: 本轮请求使用的用户补充备注。
            streamvox_model: 本轮请求对应的 StreamVox 运行模型。
            foreground_context: 本轮前台可读取的产品态势面板。
            environment_snapshot: 与本轮请求严格绑定的环境截图快照。
            frozen_visual_input: 请求准备时冻结的图片；未提供时普通用户轮就近装载一次。
            request_mode: 当前请求是用户轮还是系统提醒。
            cancellation_event: 当前 generation 的协作式取消信号。
            request_trace: 当前请求及 generation 的日志追踪身份。
            runtime_memory_snapshot: 主进程在请求创建时冻结的权威历史快照。
            self_reflection_recovery_context: 可见 Recovery 的身份、截止时间和失败事实；
                普通请求必须为空。
        Returns:
            按流式顺序产出的前台事件序列。
        """

        request_policy = foreground_request_policy(request_mode)
        is_self_reflection_recovery = request_mode == "self_reflection_recovery"
        if is_self_reflection_recovery != (
            self_reflection_recovery_context is not None
        ):
            raise ValueError("Recovery 请求模式与专用上下文必须同时存在")
        active_request_id = (
            request_trace.request_id if request_trace is not None else ""
        )
        if is_self_reflection_recovery and request_trace is not None:
            # 反思开始时只告诉桌宠“正在思考”，不公开模型 reasoning，也不进入正文账本。
            yield ForegroundEvent(
                type="assistant.self_reflection_status_changed",
                payload={
                    "request_id": request_trace.request_id,
                    "session_id": interaction_session_id,
                    "generation_id": request_trace.generation_id,
                    "self_reflection_id": self_reflection_recovery_context.self_reflection_id,
                    "episode_id": self_reflection_recovery_context.episode_id,
                    "runtime_epoch": self_reflection_recovery_context.runtime_epoch,
                    "revision": 1,
                    "active": True,
                },
            )

        # 只有策略明确允许时才创建或恢复子进程会话视图，避免请求越过自己的历史边界。
        if request_policy.touches_session:
            if runtime_memory_snapshot is not None:
                self._session_context.load_runtime_snapshot(
                    runtime_memory_snapshot,
                    interaction_session_id=interaction_session_id,
                )
            else:
                self._session_context.ensure_session(interaction_session_id)

        # 先清理首尾空白，避免纯空格内容进入模型和会话历史。
        trimmed_text = text.strip()

        # 空白输入直接本地兜底，不占用模型配额，也不写入会话历史。
        if not trimmed_text:
            yield ForegroundEvent(
                type="assistant.text_final",
                payload={"text": "我刚才没有收到内容喔。"},
            )
            return

        # 每轮只捕获一次真实本地时间，避免零点附近历史日期和当前时间出现不一致。
        turn_datetime = local_wall_datetime()
        turn_current_time = format_local_datetime_text(turn_datetime)

        frozen_foreground_context = dict(foreground_context or {})
        current_user_message = None
        if request_policy.records_user_message:
            # 先写入用户消息，交给会话层完成消息编号和空闲态维护。
            current_user_message = self._session_context.record_user_message(
                trimmed_text,
                created_at=turn_datetime,
                metadata={"request_id": active_request_id},
                llm_context_note=render_material_history_note(
                    frozen_foreground_context.get("turn_material_summary")
                ),
            )

        # 工具执行可能持续较久，并与其它历史提交交错。二轮必须沿用首轮开始前的
        # 对话视图，不能在 after_commit 之后重新读取已经变化的会话窗口。
        tool_followup_visible_dialogue: tuple[SessionMessage, ...] = ()
        if request_policy.allows_tools:
            tool_followup_visible_dialogue = tuple(
                self._session_context.visible_dialogue_messages(
                    exclude_message_id=(
                        current_user_message.message_id
                        if current_user_message is not None
                        else None
                    ),
                    before_message_id=(
                        current_user_message.message_id
                        if current_user_message is not None
                        else None
                    ),
                    limit=_TOOL_FOLLOWUP_VISIBLE_DIALOGUE_LIMIT,
                )
            )

        # 完整小文本只在用户请求中按材料阈值就近注入，system notice 不复用上一轮正文。
        foreground_text_batch = load_foreground_text_batch(
            frozen_foreground_context.get("active_turn_material_context")
            if request_policy.accepts_materials
            else None
        )
        # 生产请求在准备阶段已经冻结图片；直接调用编排器时也只读取一次。
        # Recovery 缺少来源像素时只使用冻结截图，不能重读可能已经改变的本地图片。
        visual_input = FrozenVisualInput()
        if request_policy.accepts_materials:
            visual_input = frozen_visual_input or load_foreground_visual_input(
                None if is_self_reflection_recovery else frozen_foreground_context.get("active_turn_material_context"),
                environment_snapshot,
            )
            snapshot_metadata = frozen_foreground_context.get("environment_snapshot")
            if isinstance(snapshot_metadata, dict):
                frozen_foreground_context["environment_snapshot"] = {
                    **snapshot_metadata,
                    "screen_available": visual_input.screen_available,
                }

        # 只把完整文本正文交给本轮 Prompt，不把图片或文件路径写入聊天历史。
        current_text_materials = [
            material.to_prompt_payload() for material in foreground_text_batch.materials
        ]

        recovery_tool_definitions: tuple[SelfReflectionToolDefinition, ...] = ()
        recovery_instruction = ""
        if self_reflection_recovery_context is not None:
            recovery_tool_definitions = self._foreground_tools.self_reflection_tools()
            definition_names = [item.name for item in recovery_tool_definitions]
            if len(definition_names) != len(set(definition_names)):
                raise ValueError("Self-Reflection 工具目录包含重复名称")
            recovery_instruction = render_recovery_instruction(
                replace(
                    self_reflection_recovery_context.prompt_facts,
                    tools=tuple(
                        RecoveryToolCapability(item.capability_description)
                        for item in recovery_tool_definitions
                    ),
                )
            )

        execution_context = self._build_execution_context(
            frozen_foreground_context=frozen_foreground_context,
            interaction_session_id=interaction_session_id,
            request_mode=request_mode,
            foreground_text_available=bool(current_text_materials),
            recovery_tool_definitions=recovery_tool_definitions,
        )
        tools = list(execution_context.available_tools)
        available_tool_names = frozenset(
            str(tool.get("function", {}).get("name") or "")
            for tool in tools
            if isinstance(tool.get("function"), dict)
        )  # 防御代码

        resolved_foreground_context = execution_context.prompt_environment
        if (
            request_policy.requires_notice_batch
            and not _notice_batch(resolved_foreground_context)
        ):
            # 请求离开主进程队列后仍可能在子进程启动前过期；空终态可阻止模型对空批次编造播报。
            yield ForegroundEvent(
                type="assistant.text_final",
                payload={"text": "", "notice_delivery_failed": True},
            )
            return

        # 当前输入虽然已入会话，但在构建 prompt 时仍要避免重复注入。
        messages = self._build_prompt_messages(
            current_user_text=trimmed_text,
            current_user_message=current_user_message,
            character_card=character_card,
            user_call_name=user_call_name,
            user_self_intro=user_self_intro,
            user_extra_notes=user_extra_notes,
            streamvox_model=streamvox_model,
            current_time=turn_current_time,
            foreground_context=resolved_foreground_context,
            request_mode=request_mode,
            available_tool_names=available_tool_names,
            correctable_activities=execution_context.correctable_activities,
            cancellable_activities=execution_context.cancellable_activities,
            current_text_materials=current_text_materials,
            frozen_visual_input=visual_input,
            self_reflection_recovery_instruction=recovery_instruction,
        )

        visible_parts: list[str] = []
        visible_transition_kind = VisibleTransitionKind.NONE
        pre_tool_text_chunks: list[str] = []
        tool_calls: list[ForegroundToolCall] = []
        observation_window_open = not is_self_reflection_recovery
        tool_call_started = False

        # 首轮文本只在短窗口内暂存；system notice 由厂商协议层完整校验后才会到达这里。
        invocation_intent = LlmInvocationIntent(
            tool_mode="auto" if request_policy.allows_tools else "forbid",
            allow_parallel_tools=(
                request_policy.allows_tools and not is_self_reflection_recovery
            ),
            thinking_level=request_policy.thinking_level,
        )
        try:
            model_stream = iter(
                self._client.stream_message(
                    messages=messages,
                    tools=tools,
                    intent=invocation_intent,
                    audit_source=(
                        request_trace.request_source
                        if request_trace is not None
                        else request_mode
                    ),
                    audit_operation="initial",
                    trace=(
                        request_trace.llm_call("initial")
                        if request_trace is not None
                        else None
                    ),
                    cancellation_event=cancellation_event,
                )
            )
        except LlmProtocolLeakError as exception:
            yield self._provider_protocol_failure_event(
                exception=exception,
                request_trace=request_trace,
            )
            return
        while True:
            try:
                stream_chunk = next(model_stream)
            except StopIteration:
                break
            except LlmProtocolLeakError as exception:
                yield self._provider_protocol_failure_event(
                    exception=exception,
                    request_trace=request_trace,
                )
                return
            if stream_chunk.kind == "text_delta":
                # Recovery 本身就是用户可见 Foreground。第一段正式正文必须立刻公开，
                # 即使模型随后选择工具，也不能把已经说出口的话从真实历史中抹掉。
                if is_self_reflection_recovery:
                    for event in self._push_visible_text_chunk(
                        visible_parts=visible_parts,
                        chunk=stream_chunk.text,
                    ):
                        yield event
                    continue
                if observation_window_open and tool_call_started:
                    continue

                if not observation_window_open:
                    for event in self._push_visible_text_chunk(
                        visible_parts=visible_parts,
                        chunk=stream_chunk.text,
                    ):
                        yield event
                    continue

                pre_tool_text_chunks.append(stream_chunk.text)
                if self._has_pre_tool_prefix_min_tokens(pre_tool_text_chunks):
                    visible_transition_kind = VisibleTransitionKind.MODEL_PREFIX
                    for text_chunk in pre_tool_text_chunks:
                        for event in self._push_visible_text_chunk(
                            visible_parts=visible_parts,
                            chunk=text_chunk,
                        ):
                            yield event
                    pre_tool_text_chunks = []
                    observation_window_open = False
                continue

            if stream_chunk.kind == "tool_call_started":
                if not observation_window_open:
                    tool_call_started = True
                    continue

                # 工具早信号先于提交门槛到达时，前缀只属于模型内部工具过渡语，不能进入 UI/TTS/history。
                pre_tool_text_chunks = []
                tool_call_started = True
                continue

            # 工具调用先缓存，后续再按协议补入 assistant/tool 消息链。
            if stream_chunk.kind == "tool_calls":
                tool_calls.extend(stream_chunk.tool_calls)

        # 是否进入工具分支完全由模型显式输出决定，不做关键词猜测。
        if tool_calls:
            if self_reflection_recovery_context is not None:
                for event in self._handle_self_reflection_tool_call(
                    tool_calls=tool_calls,
                    tool_definitions=recovery_tool_definitions,
                    context=self_reflection_recovery_context,
                    character_card=character_card,
                    streamvox_model=streamvox_model,
                    interaction_session_id=interaction_session_id,
                    foreground_context=resolved_foreground_context,
                    environment_snapshot=environment_snapshot,
                    frozen_visual_input=visual_input,
                    visible_parts=visible_parts,
                    cancellation_event=cancellation_event,
                    request_trace=request_trace,
                ):
                    yield event
                return
            attempted_background_work = any(
                is_start_background_work_tool(tool_call.name)
                or is_activity_correction_tool(tool_call.name)
                for tool_call in tool_calls
            )
            if attempted_background_work:
                pre_tool_text_chunks = []
            else:
                if pre_tool_text_chunks:
                    visible_transition_kind = VisibleTransitionKind.MODEL_PREFIX
                for text_chunk in pre_tool_text_chunks:
                    for event in self._push_visible_text_chunk(
                        visible_parts=visible_parts,
                        chunk=text_chunk,
                    ):
                        yield event
                pre_tool_text_chunks = []
                observation_window_open = False
            log_event(
                foreground_logger,
                logging.INFO,
                "调用工具foreground_tools_selected",
                **_request_trace_fields(request_trace),
                tool_count=len(tool_calls),
                tool_calls=[
                    {
                        "name": call.name,
                        "tool_call_id": call.tool_call_id,
                        "arguments": call.arguments,
                    }
                    for call in tool_calls
                ],
            )
            for event in self._handle_tool_calls(
                tool_calls=tool_calls,
                character_card=character_card,
                streamvox_model=streamvox_model,
                current_user_text=trimmed_text,
                current_time=turn_current_time,
                current_user_message=current_user_message,
                interaction_session_id=interaction_session_id,
                execution_context=execution_context,
                environment_snapshot=environment_snapshot,
                frozen_visual_input=visual_input,
                visible_parts=visible_parts,
                visible_transition_kind=visible_transition_kind,
                emit_batch_waiting_phrase=(
                    observation_window_open
                    and tool_call_started
                ),
                cancellation_event=cancellation_event,
                request_trace=request_trace,
                tool_followup_visible_dialogue=tool_followup_visible_dialogue,
            ):
                yield event
            return

        for text_chunk in pre_tool_text_chunks:
            for event in self._push_visible_text_chunk(
                visible_parts=visible_parts,
                chunk=text_chunk,
            ):
                yield event

        for event in self._finish_visible_text_stream(
            visible_parts=visible_parts,
            foreground_context=resolved_foreground_context,
            request_mode=request_mode,
            request_id=active_request_id,
        ):
            yield event

    def _build_prompt_messages(
        self,
        *,
        current_user_text: str,
        current_user_message: SessionMessage | None,
        character_card: CharacterCardDefinition,
        user_call_name: str,
        user_self_intro: str,
        user_extra_notes: str,
        streamvox_model: str,
        current_time: str,
        foreground_context: dict[str, object],
        request_mode: ForegroundRequestMode,
        available_tool_names: frozenset[str],
        correctable_activities: tuple[CorrectableActivityContext, ...] = (),
        cancellable_activities: tuple[CancellableActivityContext, ...] = (),
        current_text_materials: list[dict[str, object]],
        frozen_visual_input: FrozenVisualInput = FrozenVisualInput(),
        self_reflection_recovery_instruction: str = "",
    ) -> list[dict[str, Any]]:
        """构建发送给前台模型的消息列表。

        Args:
            current_user_text: 当前轮的用户输入文本。
            current_user_message: 已写入会话的当前用户消息对象；系统提醒请求没有真实用户消息时为空。
            character_card: 本轮请求使用的角色卡快照。
            user_call_name: 本轮请求使用的用户称呼。
            user_self_intro: 本轮请求使用的用户自我介绍。
            user_extra_notes: 本轮请求使用的用户补充备注。
            streamvox_model: 当前 StreamVox 运行模型。
            current_time: 当前轮捕获的本地时间文本。
            foreground_context: 当前产品态势面板。
            request_mode: 当前请求是用户轮还是系统提醒。
            available_tool_names: 本轮请求中真实注册的工具名称。
            correctable_activities: Prompt、schema 和执行共用的冻结纠正现场。
            cancellable_activities: Prompt、schema 和执行共用的冻结取消现场。
            current_text_materials: 仅供当前 prompt 使用的整批完整小文本。
            frozen_visual_input: 本轮实际装载的视觉证据。
            self_reflection_recovery_instruction: 可见 Recovery 的专用系统说明。

        Returns:
            发给前台模型的消息列表。
        """

        recent_messages: list[SessionMessage] = []
        session_summary = ""
        session_summary_covered_from = None
        session_summary_covered_to = None
        history_view = foreground_request_policy(request_mode).history_view
        if history_view == "full_protocol":
            # 把“当前这轮用户输入”从 recent_messages 中剔除，避免它既被单独处理、又被当作历史消息重复注入。
            recent_messages = [
                message
                for message in self._session_context.recent_messages()
                if current_user_message is None
                or message.message_id != current_user_message.message_id
            ]
            self._session_context.validate_prompt_history(recent_messages)
            session_summary = self._session_context.rolling_summary
            session_summary_covered_from = self._session_context.summary_covered_from
            session_summary_covered_to = self._session_context.summary_covered_to
        elif history_view == "visible_dialogue":
            recent_messages = self._session_context.visible_dialogue_messages()

        return self._prompt_builder.build(
            PromptBuildInput(
                current_user_text=current_user_text,
                current_text_materials=current_text_materials,
                frozen_visual_input=frozen_visual_input,
                session_summary=session_summary,
                session_summary_covered_from=session_summary_covered_from,
                session_summary_covered_to=session_summary_covered_to,
                recent_messages=recent_messages,
                current_time=current_time,
                character_card=character_card,
                user_call_name=user_call_name,
                user_self_intro=user_self_intro,
                user_extra_notes=user_extra_notes,
                streamvox_model=streamvox_model,
                request_mode=request_mode,
                foreground_context=foreground_context,
                available_tool_names=available_tool_names,
                correctable_activities=correctable_activities,
                cancellable_activities=cancellable_activities,
                self_reflection_recovery_instruction=(
                    self_reflection_recovery_instruction
                ),
            )
        )

    def _build_execution_context(
        self,
        *,
        frozen_foreground_context: dict[str, object],
        interaction_session_id: str,
        request_mode: ForegroundRequestMode,
        foreground_text_available: bool,
        recovery_tool_definitions: tuple[SelfReflectionToolDefinition, ...] = (),
    ) -> ForegroundExecutionContext:
        """根据主进程传进来的信息，构造出 LLM 这次生成需要的执行上下文。

        这个上下文包含：当前有哪些 Activity 在进行、有没有材料、能不能用工具、
        以及最后拼好的 prompt 环境信息。

        Args:
            frozen_foreground_context: 主进程传入且不得原地修改的请求事实。
            interaction_session_id: 当前交互会话编号。
            request_mode: 当前请求是用户轮还是系统提醒。
            foreground_text_available: 当前轮是否加载到可直接注入 Prompt 的完整小文本。
            recovery_tool_definitions: Recovery 本轮冻结的静态安全工具集合。

        Returns:
            工具列表、Activity 快照和 Prompt 环境的执行期状态。
        """

        request_policy = foreground_request_policy(request_mode)
        if request_mode == "self_reflection_recovery":
            prompt_environment = dict(frozen_foreground_context)
            prompt_environment["has_material_inputs"] = (
                frozen_foreground_context.get("has_base_material_inputs") is True
                or frozen_foreground_context.get("has_material_inputs") is True
            )
            prompt_environment["foreground_text_available"] = (
                foreground_text_available
            )
            return ForegroundExecutionContext(
                available_tools=tuple(
                    definition.request_schema()
                    for definition in recovery_tool_definitions
                ),
                prompt_environment=prompt_environment,
            )
        if not request_policy.includes_activity_state:
            return ForegroundExecutionContext(
                available_tools=(),
                prompt_environment=dict(frozen_foreground_context),
            )

        # 第一步：读取当前会话下所有 Activity 的快照。
        # Cognitive 与后台智能体都可能跨越多个前台轮次，Prompt 必须现场读取最新状态。
        snapshots: list[ForegroundActivitySnapshot] = []
        for activity_executor in (
            self._cognitive_activity_executor,
            self._background_agent_activity_executor,
        ):
            snapshot_reader = getattr(activity_executor, "activity_snapshots", None)
            if not callable(snapshot_reader):
                continue
            # Activity 快照必须现场读取，避免使用主进程预留提醒时已经过期的正文。
            try:
                current_snapshots = snapshot_reader(interaction_session_id)
            except Exception:
                # 快照故障不能打断用户轮；后面的 Background 可用性会保持拒绝式
                # 默认值，避免看不见占用事实时误发起新的普通任务。
                foreground_logger.warning(
                    "Foreground Activity 快照读取失败 executor_type=%s",
                    type(activity_executor).__name__,
                    exc_info=True,
                )
                continue
            if not isinstance(current_snapshots, (list, tuple)):
                continue
            for snapshot in current_snapshots:
                if not isinstance(snapshot, ForegroundActivitySnapshot):
                    continue
                if snapshot.interaction_session_id != interaction_session_id:
                    # 跨会话保留有界目标摘要和数量，但删除控制身份、完整目标、
                    # Progress 与纠正资格，避免旧会话内容成为当前轮可执行指令。
                    snapshot = replace(
                        snapshot,
                        activity_id="",
                        interaction_session_id=interaction_session_id,
                        goal=(
                            snapshot.goal_summary
                            or (
                                "当前已有后台工作正在处理。"
                                if snapshot.source == "background_agent"
                                else "当前已有异步任务正在处理。"
                            )
                        ),
                        progress_reasoning_text="",
                        progress_sequence=0,
                        work_plan_summary=None,
                        previous_reports=(),
                        cognitive_tool_types=(),
                    )
                snapshots.append(snapshot)

        # 第二步：判断本轮有没有正式材料；这里只给 Prompt 提供事实，不再做旧成果资格裁决。
        has_material_inputs = (
            frozen_foreground_context.get("has_base_material_inputs") is True
        )

        # 第三步：基于主进程传入的事实，构造 prompt 用的环境信息。
        prompt_environment = dict(frozen_foreground_context)
        prompt_environment["has_material_inputs"] = has_material_inputs
        prompt_environment["foreground_text_available"] = foreground_text_available
        background_snapshot_count = sum(
            1 for snapshot in snapshots if snapshot.source == "background_agent"
        )
        prompt_environment["background_work_availability"] = {
            "ordinary_can_accept": False,
            "ordinary_busy": background_snapshot_count > 0,
            "recovery_busy": False,
            "active_count": background_snapshot_count,
            "queue_supported": False,
        }
        availability_reader = getattr(
            self._background_agent_activity_executor,
            "background_availability_snapshot",
            None,
        )
        if callable(availability_reader):
            try:
                availability = availability_reader()
                to_payload = getattr(availability, "to_payload", None)
                if callable(to_payload):
                    prompt_environment["background_work_availability"] = to_payload()
            except Exception:
                foreground_logger.warning(
                    "Foreground Background 可用性读取失败",
                    exc_info=True,
                )

        # 第四步：收集所有需要查历史进度的 Activity 身份。
        # 包括当前运行中的 Activity，以及已经从 notice 终态里能反查到的 Activity。
        activity_keys = {
            (snapshot.source, snapshot.activity_id)
            for snapshot in snapshots
            if snapshot.activity_id
        }
        for notice in _terminal_notices(prompt_environment):  # 提取terminal notices
            source = notice.get("source")
            activity_id = notice.get("activity_id")
            if (
                isinstance(source, str)
                and source.strip()
                and isinstance(activity_id, str)
                and activity_id.strip()
            ):
                activity_keys.add((source.strip(), activity_id.strip()))
        # 查这些 Activity 之前已经播报过的进度，避免重复说或漏说。
        previous_reports_by_activity = progress_reports_from_payload(
            prompt_environment.pop("activity_progress_reports", None),
            activity_keys=activity_keys,
        )

        # 第五步：先把实时 Activity 拼进 Prompt 环境，再从这同一份结果冻结纠正资格。
        # schema、Prompt 和后续执行都只能使用这里得到的不可变列表，避免三处各读一次状态。
        compiled_prompt_environment = compile_activity_context(
            prompt_environment,
            snapshots,
            previous_reports_by_activity=previous_reports_by_activity,
        )
        correctable_activities = select_correctable_activity_contexts(
            compiled_prompt_environment.get("active_activities"),
            interaction_session_id=interaction_session_id,
        )
        cancellable_activities = select_cancellable_activity_contexts(
            compiled_prompt_environment.get("active_activities"),
            interaction_session_id=interaction_session_id,
        )
        timed_reminders = tuple(
            dict(reminder)
            for reminder in compiled_prompt_environment.get("timed_reminders", [])
            if isinstance(reminder, dict)
        )
        timed_reminder_runtime_id_value = compiled_prompt_environment.get(
            "timed_reminder_runtime_id"
        )
        timed_reminder_runtime_id = (
            timed_reminder_runtime_id_value.strip()
            if isinstance(timed_reminder_runtime_id_value, str)
            else ""
        )

        # 第六步：决定 LLM 这次能不能使用工具。
        # System Notice 只负责播报已有事实，不能调用工具发起新动作。
        available_tools = (
            ()
            if not request_policy.allows_tools
            else tuple(
                self._foreground_tools.tool_schemas(
                    has_pending_work_interaction=bool(
                        compiled_prompt_environment.get("pending_work_interactions")
                    ),
                    correctable_activities=correctable_activities,
                    cancellable_activities=cancellable_activities,
                    timed_reminders=timed_reminders,
                )
            )
        )
        return ForegroundExecutionContext(
            available_tools=available_tools,
            prompt_environment=compiled_prompt_environment,
            correctable_activities=correctable_activities,
            cancellable_activities=cancellable_activities,
            timed_reminders=timed_reminders,
            timed_reminder_runtime_id=timed_reminder_runtime_id,
        )

    def drain_events(self, limit: int = 100) -> list[ForegroundEvent]:
        """按预算读取 foreground 附属异步组件产生的事件。

        Args:
            limit: 本次最多返回的事件数量。

        Returns:
            已转换为 foreground 协议且未超过预算的事件。
        """

        if limit <= 0:
            return []
        with self._activity_progress_commit_lock:
            events: list[ForegroundEvent] = []
            if self._activity_cancellation_broker is not None:
                events.extend(
                    self._activity_cancellation_broker.drain_events(limit)
                )
            if self._timed_reminder_command_broker is not None:
                remaining = limit - len(events)
                if remaining > 0:
                    events.extend(
                        self._timed_reminder_command_broker.drain_events(remaining)
                    )
            if self._work_interaction_submission_broker is not None:
                remaining = limit - len(events)
                if remaining > 0:
                    events.extend(
                        self._work_interaction_submission_broker.drain_events(
                            remaining
                        )
                    )
            if self._task_memory_lookup_broker is not None:
                remaining = limit - len(events)
                if remaining > 0:
                    events.extend(
                        self._task_memory_lookup_broker.drain_events(remaining)
                    )
            if self._runtime_memory_compaction_worker is not None:
                remaining = limit - len(events)
                if remaining > 0:
                    events.extend(
                        self._runtime_memory_compaction_worker.drain_events(remaining)
                    )
            for activity_executor in (
                self._cognitive_activity_executor,
                self._background_agent_activity_executor,
            ):
                remaining = limit - len(events)
                if remaining <= 0:
                    break
                drain_events = getattr(activity_executor, "drain_events", None)
                if not callable(drain_events):
                    continue
                raw_events = drain_events(remaining)
                for raw_event in raw_events:
                    if not isinstance(raw_event, dict):
                        continue
                    event_type = str(raw_event.get("type") or "")
                    payload = raw_event.get("payload")
                    if not event_type:
                        continue
                    normalized_payload = dict(payload) if isinstance(payload, dict) else {}
                    events.append(
                        ForegroundEvent(
                            type=event_type,
                            payload=normalized_payload,
                        )
                    )
        return events

    def _handle_self_reflection_tool_call(
        self,
        *,
        tool_calls: list[ForegroundToolCall],
        tool_definitions: tuple[SelfReflectionToolDefinition, ...],
        context: SelfReflectionRecoveryContext,
        character_card: CharacterCardDefinition,
        streamvox_model: str,
        interaction_session_id: str,
        foreground_context: dict[str, object],
        environment_snapshot: FrozenEnvironmentSnapshot | None,
        frozen_visual_input: FrozenVisualInput,
        visible_parts: list[str],
        cancellation_event: Event | None,
        request_trace: ForegroundRequestTrace | None,
    ) -> Iterable[ForegroundEvent]:
        """执行 Recovery 唯一工具，并用关闭 Thinking 的二轮表达真实结果。

        Args:
            tool_calls: 首轮模型返回的全部工具调用。
            tool_definitions: 本轮已经冻结的静态安全工具定义。
            context: Recovery 身份、截止时间和旧方法事实。
            character_card: 当前角色卡快照。
            streamvox_model: 当前语音模型标识。
            interaction_session_id: 当前产品交互会话编号。
            foreground_context: 只含来源轮材料与现场的冻结上下文。
            environment_snapshot: 来源请求冻结的现场截图。
            frozen_visual_input: 来源请求冻结的视觉证据。
            visible_parts: 首轮已经公开的真实正文。
            cancellation_event: 当前 generation 的协作式取消信号。
            request_trace: 当前请求和 generation 的追踪身份。

        Returns:
            工具事实、无 Thinking 二轮正文和最终结算事件。
        """

        definition_by_name = {
            definition.name: definition for definition in tool_definitions
        }
        tool_call = tool_calls[0] if len(tool_calls) == 1 else None
        executor = self._self_reflection_tool_executor
        if (
            tool_call is None
            or tool_call.name not in definition_by_name
            or executor is None
        ):
            reason = (
                "multiple_tool_calls_forbidden"
                if len(tool_calls) > 1
                else "unknown_self_reflection_tool"
                if tool_call is not None
                else "self_reflection_tool_executor_unavailable"
            )
            outcome = SelfReflectionOutcomeFact.failed(
                self_reflection_id=context.self_reflection_id,
                source_activity_id=context.source_activity_id,
                reason=reason,
            )
        else:
            try:
                outcome = executor.execute_tool(
                    context=context,
                    tool_call=tool_call,
                    interaction_session_id=interaction_session_id,
                    foreground_context=dict(foreground_context),
                    environment_snapshot=environment_snapshot,
                    cancellation_event=cancellation_event,
                )
            except Exception:
                outcome = SelfReflectionOutcomeFact.failed(
                    self_reflection_id=context.self_reflection_id,
                    source_activity_id=context.source_activity_id,
                    reason="self_reflection_tool_execution_failed",
                )

        yield ForegroundEvent(
            type="foreground.self_reflection_outcome",
            payload=outcome.to_payload(),
        )
        if tool_call is None or outcome.tool_fact is None:
            for event in self._text_chunk_events(
                text_chunk=context.fallback_text,
                visible_parts=visible_parts,
            ):
                yield event
            for event in self._finish_visible_text_stream(
                visible_parts=visible_parts,
                foreground_context=foreground_context,
                request_mode="self_reflection_recovery",
                request_id=(
                    request_trace.request_id if request_trace is not None else ""
                ),
            ):
                yield event
            return

        public_prefix = "".join(visible_parts).strip()
        active_request_id = (
            request_trace.request_id if request_trace is not None else ""
        )
        if public_prefix:
            yield self._public_text_segment_event(
                public_prefix,
                request_id=active_request_id,
            )
            visible_parts.clear()

        followup = build_recovery_tool_followup_prompt(
            character_card=character_card,
            streamvox_model=streamvox_model,
            facts=context.prompt_facts,
            tool_call=tool_call,
            outcome=outcome,
            visible_prefix=public_prefix,
            frozen_visual_input=frozen_visual_input,
        )
        repeated_tool_calls: list[ForegroundToolCall] = []
        protocol_anomaly = False
        try:
            for stream_chunk in self._client.stream_message(
                messages=followup.messages,
                tools=[],
                intent=LlmInvocationIntent.text(thinking_level="off"),
                audit_source=(
                    request_trace.request_source
                    if request_trace is not None
                    else "self_reflection_recovery"
                ),
                audit_operation="tool_followup",
                trace=(
                    request_trace.llm_call("tool_followup")
                    if request_trace is not None
                    else None
                ),
                cancellation_event=cancellation_event,
            ):
                if stream_chunk.kind == "text_delta":
                    for event in self._push_visible_text_chunk(
                        visible_parts=visible_parts,
                        chunk=stream_chunk.text,
                    ):
                        yield event
                elif stream_chunk.kind == "tool_calls":
                    repeated_tool_calls.extend(stream_chunk.tool_calls)
        except LlmProtocolLeakError:
            protocol_anomaly = True

        for repeated_tool_call in repeated_tool_calls:
            yield ForegroundEvent(
                type="llm.foreground.tool_call_rejected",
                payload={
                    "tool_call_id": repeated_tool_call.tool_call_id,
                    "name": repeated_tool_call.name,
                    "arguments": repeated_tool_call.arguments,
                    "reason": "tool_round_limit",
                },
            )
        if cancellation_event is not None and cancellation_event.is_set():
            return
        for event in self._finish_tool_result_text_stream(
            visible_parts=visible_parts,
            empty_reply_fallback=recovery_tool_followup_fallback(
                context.prompt_facts,
                outcome,
            ),
            new_reply_start_index=0,
            force_fallback=bool(repeated_tool_calls or protocol_anomaly),
            foreground_context=foreground_context,
            assistant_tool_call_message=followup.assistant_tool_call_message,
            tool_result_messages=[followup.tool_result_message],
            request_id=active_request_id,
            protocol_transaction_id=(
                f"self-reflection-{context.self_reflection_id}"
            ),
        ):
            yield event

    def _handle_tool_calls(
        self,
        *,
        tool_calls: list[ForegroundToolCall],
        character_card: CharacterCardDefinition,
        streamvox_model: str,
        current_user_text: str,
        current_time: str,
        current_user_message: SessionMessage | None,
        interaction_session_id: str,
        execution_context: ForegroundExecutionContext,
        environment_snapshot: FrozenEnvironmentSnapshot | None,
        frozen_visual_input: FrozenVisualInput = FrozenVisualInput(),
        visible_parts: list[str] | None = None,
        visible_transition_kind: VisibleTransitionKind = VisibleTransitionKind.NONE,
        emit_batch_waiting_phrase: bool = True,
        cancellation_event: Event | None = None,
        request_trace: ForegroundRequestTrace | None = None,
        tool_followup_visible_dialogue: tuple[SessionMessage, ...],
    ) -> Iterable[ForegroundEvent]:
        """编排一轮工具调用，并在需要时续写面向用户的 Foreground 回复。

        后台工具先 prepare 并把启动资格交给事件提交边界；Inline 工具并发执行后
        再收集结果。所有工具结果最终按模型原始调用顺序回写到协议历史。

        Args:
            tool_calls: 模型返回的工具调用列表。
            character_card: 本轮请求固定的角色卡快照。
            streamvox_model: 当前 StreamVox 运行模型。
            current_user_text: 当前轮真实用户正文。
            current_time: 当前轮开始时冻结的本地时间，包含明确的 UTC 偏移。
            current_user_message: 当前轮用户消息。
            interaction_session_id: 当前产品交互会话编号。
            execution_context: 当前轮工具、材料资格、Activity 与 Prompt 环境。
            environment_snapshot: 当前请求冻结的环境图片与元数据。
            frozen_visual_input: 首轮和工具执行共用的图像证据。
            visible_parts: 已经向用户释放的本轮可见文本。
            visible_transition_kind: 首轮已经公开的等待句或模型前缀类型。
            emit_batch_waiting_phrase: 是否需要在执行工具前播批次级固定等待句。
            cancellation_event: 当前 generation 的协作式取消信号。
            request_trace: 当前请求及 generation 的日志追踪身份。
            tool_followup_visible_dialogue: initial 前冻结的受限可见对话。
        Returns:
            工具执行过程及最终回复对应的前台事件序列。
        """

        if cancellation_event is not None and cancellation_event.is_set():
            return

        # 首轮消息只服务初次理解和工具选择；这里只构造最终持久化所需的原生工具协议。
        assistant_tool_call_message = build_assistant_tool_call_message(tool_calls)
        protocol_transaction_id = f"tool-{uuid4().hex}"

        visible_parts = visible_parts if visible_parts is not None else []
        transition_kind = visible_transition_kind
        foreground_context = execution_context.prompt_environment
        protocol_tool_result_messages: list[dict[str, Any]] = []
        tool_completed_events: list[ForegroundEvent] = []
        contains_background_work_call = any(
            is_start_background_work_tool(tool_call.name)
            or is_activity_correction_tool(tool_call.name)
            for tool_call in tool_calls
        )

        # 普通工具保留既有的可取消等待窗口。后台工作必须先通过 prepare 的单槽位
        # 校验，避免等待句先于真实接单资格展示给用户。
        if emit_batch_waiting_phrase and not contains_background_work_call:
            waiting_events = self._batch_waiting_phrase_events(
                tool_calls=tool_calls,
                visible_parts=visible_parts,
                request_trace=request_trace,
            )
            if waiting_events and transition_kind == VisibleTransitionKind.NONE:
                transition_kind = VisibleTransitionKind.WAITING_ACK
            for event in waiting_events:
                yield event

        if cancellation_event is not None and cancellation_event.is_set():
            return

        active_request_id = (
            request_trace.request_id if request_trace is not None else ""
        )

        tool_orchestrator = self._tool_call_orchestrator

        # Inline 工具的超时由工具目录提供；缺失时由编排器使用默认策略。
        foreground_tools = self._foreground_tools
        inline_timeout_resolver = getattr(foreground_tools, "execution_timeout_seconds", None)
        mounted_image_claim_lock = Lock()
        mounted_image_data_urls: set[str] = set()

        # 分流后，Cognitive 整批 prepare，后台智能体建立唯一主任务，Inline 并发执行。
        preparation = tool_orchestrator.prepare(
            tool_calls=tool_calls,
            is_cognitive_tool=is_cognitive_tool,
            is_task_result_tool=lambda name: (
                is_start_background_work_tool(name)
                or is_activity_correction_tool(name)
            ),
            execute_cognitive_calls=lambda calls: self._execute_cognitive_tool_calls(
                tool_calls=calls,
                interaction_session_id=interaction_session_id,
                current_user_text=current_user_text,
                current_user_message=current_user_message,
                foreground_context=foreground_context,
                frozen_visual_input=frozen_visual_input,
                cancellation_event=cancellation_event,
            ),
            execute_task_result_call=lambda tool_call: (
                self._execute_activity_correction_call(
                    tool_call=tool_call,
                    interaction_session_id=interaction_session_id,
                    current_user_text=current_user_text,
                    foreground_context=foreground_context,
                    correctable_activities=execution_context.correctable_activities,
                    frozen_visual_input=frozen_visual_input,
                    environment_snapshot=environment_snapshot,
                    cancellation_event=cancellation_event,
                )
                if is_activity_correction_tool(tool_call.name)
                else self._background_agent_tool_executor.execute_start_background_work(
                    execution_lane="ordinary",
                    tool_call=tool_call,
                    interaction_session_id=interaction_session_id,
                    foreground_context=foreground_context,
                    environment_snapshot=environment_snapshot,
                    cancellation_event=cancellation_event,
                    protocol_transaction_id=protocol_transaction_id,
                )
            ),
            is_deferred_action_tool=lambda name: (
                is_respond_to_work_interaction_tool(name)
                or is_cancel_activity_tool(name)
                or is_timed_reminder_tool(name)
            ),
            execute_deferred_action_call=lambda tool_call: (
                self._execute_timed_reminder_call(
                    tool_call=tool_call,
                    timed_reminders=execution_context.timed_reminders,
                    timed_reminder_runtime_id=execution_context.timed_reminder_runtime_id,
                )
                if is_timed_reminder_tool(tool_call.name)
                else self._execute_activity_cancellation_call(
                    tool_call=tool_call,
                    cancellable_activities=(
                        execution_context.cancellable_activities
                    ),
                )
                if is_cancel_activity_tool(tool_call.name)
                else self._background_agent_tool_executor.execute_work_interaction(
                    tool_call=tool_call,
                    current_user_text=current_user_text,
                    foreground_context=foreground_context,
                    cancellation_event=cancellation_event,
                )
            ),
            execute_inline_call=lambda tool_call, inline_execution_context: self._execute_inline_tool_with_trace(
                foreground_tools=foreground_tools,
                tool_call=tool_call,
                request_trace=request_trace,
                cancellation_event=cancellation_event,
                inline_execution_context=inline_execution_context,
                foreground_context=foreground_context,
                frozen_visual_input=frozen_visual_input,
                mounted_image_claim_lock=mounted_image_claim_lock,
                mounted_image_data_urls=mounted_image_data_urls,
            ),
            inline_timeout_resolver=(
                inline_timeout_resolver
                if callable(inline_timeout_resolver)
                else None
            ),
            cancellation_event=cancellation_event,
        )

        # 取消发生在提交前时，停止 Inline future，并释放所有尚未启动的后台占位。
        if preparation.cancelled_before_commit or (
            cancellation_event is not None and cancellation_event.is_set()
        ):
            preparation.cancel_inline()
            self._rollback_uncommitted_tool_calls(preparation.prepared_calls)
            return

        background_work_rejected = any(
            _is_background_activity_tool(executed_call.call.name)
            and executed_call.state not in {"accepted", "merged"}
            for executed_call in preparation.prepared_calls
        )
        # 等待句必须建立在 prepare 已经接受任务这一事实之上。槽位拒绝时直接进入
        # 二轮说明，不能先向用户制造“正在处理”的错觉。
        if (
            emit_batch_waiting_phrase
            and contains_background_work_call
            and not background_work_rejected
        ):
            waiting_events = self._batch_waiting_phrase_events(
                tool_calls=tool_calls,
                visible_parts=visible_parts,
                request_trace=request_trace,
            )
            if waiting_events and transition_kind == VisibleTransitionKind.NONE:
                transition_kind = VisibleTransitionKind.WAITING_ACK
            for event in waiting_events:
                yield event

        # reservations 统一持有待提交的 Activity 动作，生成器提前结束时可逐一回滚。
        # 同一来源的同一 Activity 只允许绑定一次事务，避免重复启动或重复取消。
        reservations: list[
            BackgroundActivityReservation
            | ActivityCancellationReservation
            | TimedReminderCommandReservation
        ] = []
        activity_action_keys: set[tuple[str, str]] = set()

        # prepare 阶段的后台与延迟调用已先发送完成事件，这里用同一 Python 对象身份避免重复处理。
        prepared_call_object_ids = {
            id(executed_call.call)
            for executed_call in preparation.prepared_calls
        }
        for executed_call in preparation.prepared_calls:
            # 只有 accepted 调用需要事务；merged 后台调用复用同一 Activity，不重复启动。
            reservation = (
                self._timed_reminder_reservation(
                    executed_call=executed_call,
                    existing_keys=activity_action_keys,
                )
                if is_timed_reminder_tool(executed_call.call.name)
                else self._activity_cancellation_reservation(
                    executed_call=executed_call,
                    existing_keys=activity_action_keys,
                )
                if is_cancel_activity_tool(executed_call.call.name)
                else self._background_activity_reservation(
                    executed_call=executed_call,
                    existing_keys=activity_action_keys,
                )
            )
            if reservation is not None:
                # reservation 一创建就进入统一回滚集合，后续投影失败也不能让已经
                # 占住的 Activity 槽位脱离 finally。
                reservations.append(reservation)

            # commit_action 仅随内部 ForegroundEvent 交给 Supervisor，不会发送给 Desktop。
            tool_completed_events.append(
                self._tool_call_completed_event(
                    executed_call=executed_call,
                    request_id=active_request_id,
                    interaction_session_id=interaction_session_id,
                    current_user_message=current_user_message,
                    foreground_context=foreground_context,
                    protocol_transaction_id=protocol_transaction_id,
                    commit_action=(reservation.action if reservation is not None else None),
                )
            )

        # 先 yield 带事务的 accepted 事件，使 Supervisor 能尽早提交或回滚后台占位。
        tool_completed_events.sort(key=lambda event: event.commit_action is None)
        try:
            for event in tool_completed_events:
                yield event
        finally:
            # 事件未被 Supervisor 接收、入队失败或生成器中断时，事务仍可释放 prepared
            # 占位；已提交的事务会忽略这次幂等回滚，不影响已启动的 Activity。
            for reservation in reservations:
                reservation.rollback()

        if cancellation_event is not None and cancellation_event.is_set():
            preparation.cancel_inline()
            return

        # 后台事件已越过提交边界；现在再等待 Inline future，避免拖慢后台 Activity 启动。
        execution = preparation.collect(cancellation_event=cancellation_event)
        if cancellation_event is not None and cancellation_event.is_set():
            preparation.cancel_inline()
            return

        mounted_visual_inputs = tuple(
            executed_call.outcome.mounted_visual_input
            for executed_call in execution.calls
            if isinstance(executed_call.outcome, InlineToolCompleted)
            and executed_call.outcome.mounted_visual_input.images
        )
        followup_visual_input = merge_frozen_visual_inputs(
            frozen_visual_input,
            *mounted_visual_inputs,
        )

        for executed_call in execution.calls:
            tool_call = executed_call.call
            result = executed_call.result_payload()
            stable_result = self._stable_tool_result(executed_call)

            # 协议历史保留完整结果；第二轮 LLM 只读取另行构建的白名单上下文。
            tool_result_message = {
                "role": "tool",
                "content": json.dumps(stable_result, ensure_ascii=False),
                "tool_call_id": tool_call.tool_call_id,
            }
            protocol_tool_result_messages.append(tool_result_message)

            # 后台调用已在 prepare 阶段发过事件，只有 Inline 结果需要在此补发。
            if id(tool_call) not in prepared_call_object_ids:
                yield self._tool_call_completed_event(
                    executed_call=executed_call,
                    request_id=active_request_id,
                    interaction_session_id=interaction_session_id,
                    current_user_message=current_user_message,
                    foreground_context=foreground_context,
                    protocol_transaction_id=protocol_transaction_id,
                )  # inline完成event

        tool_followup_facts = self._tool_followup_facts(
            execution=execution,
            reservations=reservations,
        )
        followup_plan = select_tool_followup_plan(
            execution.calls,
            visible_transition_kind=transition_kind,
            attached_notices=freeze_attached_strong_notices(foreground_context),
            tool_followup_facts=tool_followup_facts,
        )

        if not followup_plan.required:
            if cancellation_event is not None and cancellation_event.is_set():
                return
            for event in self._finish_tool_result_text_stream(
                visible_parts=visible_parts,
                empty_reply_fallback="我已经接下了，完成后会告诉你。",
                new_reply_start_index=None,
                foreground_context=foreground_context,
                assistant_tool_call_message=assistant_tool_call_message,
                tool_result_messages=protocol_tool_result_messages,
                request_id=active_request_id,
                protocol_transaction_id=protocol_transaction_id,
            ):
                yield event
            return

        public_prefix = "".join(visible_parts).strip()
        # 这个条件只决定前缀是否额外结算为一条独立 segment（ADR 0020 的占位/占用场景），
        # 与二轮 Prompt 无关：前缀只要真实公开过，就必须进入二轮上下文（ADR 0042 的组装顺序），
        # 否则二轮不知道首轮已经说过什么，会换种说法重复同一信息。
        should_commit_public_prefix = bool(
            public_prefix
            and any(
                _is_background_activity_tool(executed_call.call.name)
                and executed_call.state not in {"accepted", "merged"}
                for executed_call in execution.calls
            )
        )
        if should_commit_public_prefix:
            yield self._public_text_segment_event(
                public_prefix,
                request_id=active_request_id,
            )
            visible_parts.clear()

        followup_messages = build_tool_followup_messages(
            character_card,
            streamvox_model,
            current_user_text,
            execution.calls,
            current_time=current_time,
            plan=followup_plan,
            visible_dialogue=tool_followup_visible_dialogue,
            visible_assistant_prefix=public_prefix,
            frozen_visual_input=followup_visual_input,
        )
        background_work_already_running = any(
            _is_background_activity_tool(executed_call.call.name)
            and executed_call.state == "already_running"
            for executed_call in execution.calls
        )
        background_work_unconfigured = any(
            is_start_background_work_tool(executed_call.call.name)
            and executed_call.result_payload().get("reason")
            == "background_agent_unconfigured"
            for executed_call in execution.calls
        )
        protected_background_rejection_message = (
            BACKGROUND_WORK_ALREADY_RUNNING_MESSAGE
            if background_work_already_running
            else BACKGROUND_AGENT_UNCONFIGURED_MESSAGE
            if background_work_unconfigured
            else None
        )
        background_item_count = sum(
            executed_call.channel == "background"
            and executed_call.reply_requirement != ToolReplyRequirement.SILENT
            for executed_call in execution.calls
        )
        inline_item_count = sum(
            executed_call.channel == "inline"
            and executed_call.reply_requirement != ToolReplyRequirement.SILENT
            for executed_call in execution.calls
        )
        deferred_item_count = sum(
            executed_call.channel == "deferred"
            and executed_call.reply_requirement != ToolReplyRequirement.SILENT
            for executed_call in execution.calls
        )
        log_event(
            foreground_logger,
            logging.INFO,
            "foreground_tool_followup_context_built",
            **_request_trace_fields(request_trace),
            background_item_count=background_item_count,
            inline_item_count=inline_item_count,
            deferred_item_count=deferred_item_count,
            visible_dialogue_count=len(tool_followup_visible_dialogue),
        )

        # 工具结果进入隔离上下文后继续流式回复，让前台和 TTS 尽早收到可见文本。
        repeated_tool_calls: list[ForegroundToolCall] = []
        protocol_anomaly = False
        held_followup_text_chunks: list[str] = []
        new_reply_start_index = len(visible_parts)
        try:
            for stream_chunk in self._client.stream_message(
                messages=followup_messages,
                tools=[],
                intent=LlmInvocationIntent.text(),
                audit_source=(
                    request_trace.request_source
                    if request_trace is not None
                    else "user_turn"
                ),
                audit_operation="tool_followup",
                trace=(
                    request_trace.llm_call("tool_followup")
                    if request_trace is not None
                    else None
                ),
                cancellation_event=cancellation_event,
            ):
                if stream_chunk.kind == "text_delta":
                    if protected_background_rejection_message is not None:
                        held_followup_text_chunks.append(stream_chunk.text)
                        continue
                    for event in self._push_visible_text_chunk(
                        visible_parts=visible_parts,
                        chunk=stream_chunk.text,
                    ):
                        yield event
                    continue

                # 测试客户端或异常厂商仍可能返回结构化调用；只记录并走确定性兜底。
                if stream_chunk.kind == "tool_calls":
                    repeated_tool_calls.extend(stream_chunk.tool_calls)
        except LlmProtocolLeakError as exception:
            protocol_anomaly = True
            log_event(
                foreground_logger,
                logging.WARNING,
                "foreground_tool_followup_protocol_leak",
                **_request_trace_fields(request_trace),
                provider_id=exception.provider_id,
                protocol=exception.protocol,
            )

        # 占用拒绝的二轮正文要到协议完整结束后才释放。这样异常厂商在正文后又夹带
        # 工具调用或协议泄漏时，用户只会收到确定性拒绝，不会看到半截错误承诺。
        if (
            protected_background_rejection_message is not None
            and not repeated_tool_calls
            and not protocol_anomaly
        ):
            for text_chunk in held_followup_text_chunks:
                for event in self._push_visible_text_chunk(
                    visible_parts=visible_parts,
                    chunk=text_chunk,
                ):
                    yield event

        if repeated_tool_calls:
            for tool_call in repeated_tool_calls:
                # 当前协议只允许一轮工具事务，明确拒绝二次调用以保持历史链路完整。
                yield ForegroundEvent(
                    type="llm.foreground.tool_call_rejected",
                    payload={
                        "tool_call_id": tool_call.tool_call_id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                        "reason": "tool_round_limit",
                    },
                )

        if cancellation_event is not None and cancellation_event.is_set():
            return
        for event in self._finish_tool_result_text_stream(
            visible_parts=visible_parts,
            empty_reply_fallback=(
                protected_background_rejection_message
                if (
                    protected_background_rejection_message is not None
                    and not followup_plan.attached_notices
                )
                else render_tool_followup_fallback(
                    followup_plan,
                    execution.calls,
                )
            ),
            new_reply_start_index=new_reply_start_index,
            force_fallback=bool(repeated_tool_calls or protocol_anomaly),
            foreground_context=foreground_context,
            assistant_tool_call_message=assistant_tool_call_message,
            tool_result_messages=protocol_tool_result_messages,
            request_id=active_request_id,
            protocol_transaction_id=protocol_transaction_id,
        ):
            yield event

    def _execute_cognitive_tool_calls(
        self,
        *,
        tool_calls: tuple[ForegroundToolCall, ...],
        interaction_session_id: str,
        current_user_text: str,
        current_user_message: SessionMessage | None,
        foreground_context: dict[str, object],
        frozen_visual_input: FrozenVisualInput,
        cancellation_event: Event | None,
    ) -> tuple[dict[str, object], ...]:
        """把当前轮全部 Cognitive 调用交给工具层合并 prepare。

        这里返回的是每条原始调用对应的 accepted、merged 或 rejected 载荷，不会
        启动 Knowledge 请求；启动需等待工具完成事件成功提交。

        Args:
            tool_calls: foreground 模型按原始顺序产出的全部认知调用。
            interaction_session_id: 当前交互会话编号。
            current_user_text: 触发调用的真实用户正文。
            current_user_message: 已写入会话的当前用户消息。
            foreground_context: 当前轮材料、活动与环境态势。
            frozen_visual_input: 本轮冻结的图像和读取限制。
            cancellation_event: 当前 generation 的协作式取消信号。

        Returns:
            与原调用顺序一一对应的认知执行结果。
        """

        if self._cognitive_tool_executor is None:
            return tuple(
                {
                    "state": "rejected",
                    "kind": tool_call.name,
                    "reason": "cognitive_activity_executor_unavailable",
                    "safe_message": "认知能力当前不可用，查询没有启动。",
                }
                for tool_call in tool_calls
            )

        return self._cognitive_tool_executor.execute_many(
            tool_calls,
            context=CognitiveToolContext(
                interaction_session_id=interaction_session_id,
                source_user_message_id=(
                    current_user_message.message_id if current_user_message is not None else None
                ),
                source_user_text=current_user_text,
                foreground_context=foreground_context,
                images=frozen_visual_input.images,
                evidence_limitations=frozen_visual_input.limitations,
                image_materials=frozen_visual_input.image_materials,
                cancellation_event=cancellation_event,
                task_memory_lookup=(
                    self._lookup_task_memory
                    if self._task_memory_lookup_broker is not None
                    else None
                ),
            ),
        )

    def _execute_activity_correction_call(
        self,
        *,
        tool_call: ForegroundToolCall,
        interaction_session_id: str,
        current_user_text: str,
        foreground_context: dict[str, object],
        correctable_activities: tuple[CorrectableActivityContext, ...],
        environment_snapshot: FrozenEnvironmentSnapshot | None,
        frozen_visual_input: FrozenVisualInput,
        cancellation_event: Event | None,
    ) -> dict[str, object]:
        """把两类显式纠正工具送入统一纠正服务。

        Args:
            tool_call: Foreground 模型产生的纠正调用。
            interaction_session_id: 当前交互会话编号。
            current_user_text: 用户本轮真实纠正文。
            foreground_context: 当前轮产品态势。
            correctable_activities: Prompt、schema 与执行共用的冻结纠正允许列表。
            environment_snapshot: 当前轮冻结现场。
            frozen_visual_input: 首轮和纠正任务共用的图像。
            cancellation_event: 当前 generation 的取消信号。

        Returns:
            新 Activity 的准备结果，或纠正能力不可用的安全拒绝。
        """

        executor = self._activity_correction_tool_executor
        if executor is None:
            return {
                "state": "rejected",
                "reason": "activity_correction_unavailable",
                "safe_message": "Activity 纠正当前不可用。",
            }
        if tool_call.name == CORRECT_COGNITIVE_ACTIVITY_TOOL:
            return executor.execute_cognitive_correction(
                tool_call=tool_call,
                interaction_session_id=interaction_session_id,
                current_user_text=current_user_text,
                foreground_context=foreground_context,
                correctable_activities=correctable_activities,
                frozen_visual_input=frozen_visual_input,
                cancellation_event=cancellation_event,
            )
        return executor.execute_background_correction(
            tool_call=tool_call,
            interaction_session_id=interaction_session_id,
            foreground_context=foreground_context,
            correctable_activities=correctable_activities,
            environment_snapshot=environment_snapshot,
            cancellation_event=cancellation_event,
        )

    def _execute_activity_cancellation_call(
        self,
        *,
        tool_call: ForegroundToolCall,
        cancellable_activities: tuple[CancellableActivityContext, ...],
    ) -> dict[str, object]:
        """准备一笔只在 Foreground 提交后生效的 Activity 取消。

        Args:
            tool_call: Foreground 模型产生的精确取消调用。
            cancellable_activities: Prompt、schema 与执行共用的冻结允许列表。

        Returns:
            deferred 通道可识别的准备结果，或取消控制面不可用的安全拒绝。
        """

        executor = self._activity_cancellation_tool_executor
        if executor is None:
            return {
                "state": "rejected",
                "reason": "activity_cancellation_unavailable",
                "safe_message": "当前无法提交取消请求，任务没有被取消。",
            }
        return executor.prepare(
            tool_call=tool_call,
            cancellable_activities=cancellable_activities,
        )

    def _execute_timed_reminder_call(
        self,
        *,
        tool_call: ForegroundToolCall,
        timed_reminders: tuple[dict[str, object], ...],
        timed_reminder_runtime_id: str,
    ) -> dict[str, object]:
        """准备一笔只在 Foreground 提交后生效的提醒写命令。

        Args:
            tool_call: Foreground 模型产生的创建或管理调用。
            timed_reminders: Prompt、schema 与执行共用的冻结提醒清单。
            timed_reminder_runtime_id: 该冻结清单所属的 Backend 运行编号。

        Returns:
            deferred 通道可识别的准备结果，或控制面不可用的安全拒绝。
        """

        executor = self._timed_reminder_tool_executor
        if executor is None:
            return {
                "state": "rejected",
                "reason": "timed_reminder_command_unavailable",
                "safe_message": "当前无法提交提醒操作，提醒没有改变。",
            }
        return executor.prepare(
            tool_call=tool_call,
            timed_reminders=timed_reminders,
            timed_reminder_runtime_id=timed_reminder_runtime_id,
        )

    def _lookup_task_memory(
        self,
        activity_id: str,
        cancellation_event: Event | None,
    ) -> dict[str, object]:
        """通过子进程 Broker 读取一条父任务详情。

        Args:
            activity_id: 聊天历史中明确引用的 Activity 编号。
            cancellation_event: 当前 generation 的取消信号。

        Returns:
            主进程权威任务详情或稳定失败事实。
        """

        if self._task_memory_lookup_broker is None:
            return {
                "state": "not_available",
                "activity_id": activity_id,
                "reason": "task_memory_broker_unavailable",
                "safe_message": "旧任务详情当前无法读取。",
            }
        return self._task_memory_lookup_broker.lookup_and_wait(
            activity_id=activity_id,
            purpose="task_derivation",
            cancellation_event=cancellation_event,
        )

    def _rollback_uncommitted_tool_calls(
        self,
        calls: tuple[ExecutedToolCall, ...],
    ) -> None:
        """在 generation 提前取消时释放本轮尚未提交的资源占用。

        Args:
            calls: 当前轮 prepare 阶段已归档的工具结果。
        """

        for executed_call in calls:
            tool_name = executed_call.call.name
            if executed_call.state != "accepted":
                continue
            if is_cancel_activity_tool(tool_name):
                cancellation_id = executed_call.resource_id
                if (
                    cancellation_id
                    and self._activity_cancellation_tool_executor is not None
                ):
                    self._activity_cancellation_tool_executor.rollback_prepared(
                        cancellation_id
                    )
                continue
            if is_timed_reminder_tool(tool_name):
                command_id = executed_call.resource_id
                if command_id and self._timed_reminder_tool_executor is not None:
                    self._timed_reminder_tool_executor.rollback_prepared(command_id)
                continue
            activity_id = executed_call.activity_id
            if (
                is_activity_correction_tool(tool_name)
                and isinstance(activity_id, str)
                and activity_id.strip()
                and self._activity_correction_tool_executor is not None
            ):
                self._activity_correction_tool_executor.rollback_prepared(
                    activity_id.strip()
                )
                continue
            if (
                is_start_background_work_tool(tool_name)
                and isinstance(activity_id, str)
                and activity_id.strip()
            ):
                self._background_agent_tool_executor.rollback_prepared_background_work(
                    activity_id.strip()
                )
                continue
            activity_executor = (
                self._cognitive_activity_executor
                if is_cognitive_tool(tool_name)
                else self._background_agent_activity_executor
                if is_start_background_work_tool(tool_name)
                else None
            )
            cancel_before_start = getattr(activity_executor, "cancel_before_start", None)
            if isinstance(activity_id, str) and activity_id.strip():
                if callable(cancel_before_start):
                    cancel_before_start(activity_id.strip())

    def _tool_call_completed_event(
        self,
        *,
        executed_call: ExecutedToolCall,
        request_id: str,
        interaction_session_id: str,
        current_user_message: SessionMessage | None,
        foreground_context: dict[str, object],
        protocol_transaction_id: str,
        commit_action: CommitAction | None = None,
    ) -> ForegroundEvent:
        """构造工具完成事件，并可附着仅限 Backend 内部消费的提交事务。

        payload 会进入运行时事件队列；commit_action 不会序列化到 Desktop 或模型协议，
        只由 GenerationSupervisor 在确认事件入队时决定提交或回滚。

        Args:
            executed_call: 原始调用及其唯一执行结果。
            request_id: 当前助手请求编号。
            interaction_session_id: 当前交互会话编号。
            current_user_message: 触发本轮工具调用的用户消息。
            foreground_context: 当前产品态势面板。
            protocol_transaction_id: 当前完整工具事务的稳定编号。
            commit_action: 事件提交时使用的内部事务；不会写入 payload。

        Returns:
            可交给 generation supervisor 的内部前台事件。
        """

        tool_call = executed_call.call
        raw_result = executed_call.result_payload()
        result = self._stable_tool_result(executed_call)
        payload: dict[str, object] = {
            "interaction_session_id": interaction_session_id,
            "source_user_message_id": (
                current_user_message.message_id if current_user_message is not None else None
            ),
            "tool_call_id": tool_call.tool_call_id,
            "protocol_transaction_id": protocol_transaction_id,
            "request_id": request_id,
            "name": tool_call.name,
            "activity_id": result.get("activity_id"),
            "state": result.get("state"),
            "result": result,
        }
        raw_recent_material_observation = raw_result.get(
            RECENT_MATERIAL_OBSERVATION_FIELD
        )
        if raw_recent_material_observation is not None:
            # 路径只借内部工具完成事件跨一次进程边界。模型结果和 canonical result
            # 已由 _stable_tool_result 删除该字段，主进程 Router 消费后还会再次剥离。
            payload[RECENT_MATERIAL_OBSERVATION_FIELD] = (
                RecentMaterialObservation.from_payload(
                    raw_recent_material_observation
                ).to_payload()
            )
        if is_start_background_work_tool(tool_call.name) or tool_call.name == CORRECT_BACKGROUND_WORK_TOOL:
            deliverable = result.get("deliverable")
            if isinstance(deliverable, bool):
                payload["deliverable"] = deliverable
            based_on_activity_id = result.get("based_on_activity_id")
            if (
                tool_call.name == CORRECT_BACKGROUND_WORK_TOOL
                and isinstance(based_on_activity_id, str)
                and based_on_activity_id.strip()
            ):
                payload["based_on_activity_id"] = based_on_activity_id.strip()
        if is_respond_to_work_interaction_tool(tool_call.name):
            agent_interaction_request_id = result.get("request_id")
            if (
                isinstance(agent_interaction_request_id, str)
                and agent_interaction_request_id.strip()
            ):
                payload["agent_interaction_request_id"] = (
                    agent_interaction_request_id.strip()
                )
        should_bind_material_context = _is_background_activity_tool(tool_call.name) or is_cognitive_tool(tool_call.name) or (
            is_respond_to_work_interaction_tool(tool_call.name)
            and bool(
                materials_from_foreground_context(foreground_context)
            )
        )
        if should_bind_material_context and executed_call.state == "accepted":
            material_context = foreground_context.get("active_turn_material_context")
            if isinstance(material_context, dict):
                context_id = material_context.get("context_id")
                if isinstance(context_id, str) and context_id.strip():
                    payload["turn_material_context_id"] = context_id.strip()
        return ForegroundEvent(
            type="llm.foreground.tool_call_completed",
            payload=payload,
            commit_action=commit_action,
        )

    def _background_activity_reservation(
        self,
        *,
        executed_call: ExecutedToolCall,
        existing_keys: set[tuple[str, str]],
    ) -> BackgroundActivityReservation | None:
        """为尚未启动的后台 Activity 创建一次性提交与回滚事务。

        后台工具在 prepare 阶段只建立 Activity 占位，不应立刻启动。只有
        Foreground 成功接收并入队对应工具事件后，事务才会调用 start；如果
        事件没有被接收或中途取消，则调用 cancel_before_start 释放该占位。
        同一 Activity 的 merged 调用已经归入首个 accepted 调用，不应重复
        创建启动动作。

        Args:
            executed_call: 已归一化的单条工具调用及其准备结果。
            existing_keys: 本批次已创建事务的“来源 + Activity 编号”集合。

        Returns:
            可附着到工具完成事件的 Activity 事务；不需要启动动作时返回空。
        """

        activity_id = executed_call.activity_id
        # 只有首条 accepted 调用拥有创建和启动 Activity 的职责；merged 调用只复用它。
        if executed_call.state != "accepted" or activity_id is None:
            return None

        # 按工具类别选出对应的 Activity 执行器，两类执行器的启动和回滚接口相同。
        tool_name = executed_call.call.name
        if is_start_background_work_tool(tool_name) or tool_name == CORRECT_BACKGROUND_WORK_TOOL:
            source = "background_agent"
            activity_executor = self._background_agent_activity_executor
        elif is_cognitive_tool(tool_name) or tool_name == CORRECT_COGNITIVE_ACTIVITY_TOOL:
            source = "cognitive"
            activity_executor = self._cognitive_activity_executor
        else:
            return None

        normalized_activity_id = activity_id
        if is_activity_correction_tool(executed_call.call.name):
            correction_executor = self._activity_correction_tool_executor
            if correction_executor is None:
                return None
            correction_reservation = correction_executor.take_reservation(
                normalized_activity_id
            )
            if correction_reservation is None:
                raise RuntimeError(
                    "纠正工具已接受，但缺少对应提交事务 "
                    f"activity_id={normalized_activity_id}"
                )
            key = (correction_reservation.source, normalized_activity_id)
            if key in existing_keys:
                correction_reservation.rollback()
                return None
            existing_keys.add(key)
            return correction_reservation

        if is_start_background_work_tool(tool_name):
            prepared_background_work = (
                self._background_agent_tool_executor.take_prepared_background_work(
                    normalized_activity_id
                )
            )
            if prepared_background_work is None:
                raise RuntimeError(
                    "后台工具已接受，但缺少 canonical 提交事务 "
                    f"activity_id={normalized_activity_id}"
                )
            key = (source, normalized_activity_id)
            if key in existing_keys:
                prepared_background_work.rollback_unclaimed()
                return None
            existing_keys.add(key)
            return prepared_background_work.claim_reservation()

        key = (source, normalized_activity_id)
        # 同一批工具调用可能共享同一个 Activity，只能登记一次提交事务。
        if key in existing_keys:
            return None

        # 执行器可在降级运行或测试替身中缺失；缺少任一端点就不建立无法收口的事务。
        start_activity = getattr(activity_executor, "start", None)
        cancel_before_start = getattr(activity_executor, "cancel_before_start", None)
        if not callable(start_activity) or not callable(cancel_before_start):
            return None
        existing_keys.add(key)

        result_payload = executed_call.result_payload()
        goal_value = result_payload.get("brief") or result_payload.get("goal")
        goal = goal_value.strip() if isinstance(goal_value, str) else "这项任务"

        def start_reserved_activity() -> ToolFollowupFact:
            """在工具事件成功入队后启动已占位的 Activity。

            执行器拒绝启动说明 prepare 阶段建立的占位已经失效；这里返回明确失败
            事实，避免二轮读取 prepare 的 accepted 并误报为已启动。
            """

            try:
                started = start_activity(normalized_activity_id)
            except Exception:
                logger.exception(
                    "后台 Activity 提交后启动异常 source=%s activity_id=%s",
                    source,
                    normalized_activity_id,
                )
                return ToolFollowupFact(
                    kind="activity_start_failed",
                    activity_id=normalized_activity_id,
                    goal=goal,
                    safe_message="这项任务没有启动。",
                )
            if started is not True:
                return ToolFollowupFact(
                    kind="activity_start_failed",
                    activity_id=normalized_activity_id,
                    goal=goal,
                    safe_message="这项任务没有启动。",
                )
            return ToolFollowupFact(
                kind="activity_started",
                activity_id=normalized_activity_id,
                goal=goal,
            )

        return BackgroundActivityReservation.create(
            source=source,
            activity_id=normalized_activity_id,
            start_after_commit=start_reserved_activity,
            on_rollback=lambda: cancel_before_start(normalized_activity_id),
        )

    def _activity_cancellation_reservation(
        self,
        *,
        executed_call: ExecutedToolCall,
        existing_keys: set[tuple[str, str]],
    ) -> ActivityCancellationReservation | None:
        """领取一笔已准备的 Activity 取消提交事务。

        Args:
            executed_call: 已归一化的 `cancel_activity` 调用及准备结果。
            existing_keys: 本批次已经绑定事务的资源身份集合。

        Returns:
            首次 accepted 调用对应的取消事务；拒绝、失败或重复目标返回空。
        """

        if executed_call.state != "accepted":
            return None
        cancellation_id = executed_call.resource_id
        activity_id = executed_call.activity_id
        executor = self._activity_cancellation_tool_executor
        if not cancellation_id or not activity_id or executor is None:
            return None
        reservation = executor.take_reservation(cancellation_id)
        if reservation is None:
            raise RuntimeError(
                "Activity 取消已接受，但缺少对应提交事务 "
                f"cancellation_id={cancellation_id}"
            )
        key = (reservation.source, reservation.activity_id)
        if key in existing_keys:
            reservation.rollback()
            return None
        existing_keys.add(key)
        return reservation

    def _timed_reminder_reservation(
        self,
        *,
        executed_call: ExecutedToolCall,
        existing_keys: set[tuple[str, str]],
    ) -> TimedReminderCommandReservation | None:
        """领取一笔已准备的定时提醒提交事务。

        Args:
            executed_call: 已归一化的创建或管理调用及准备结果。
            existing_keys: 本批次已经绑定事务的资源身份集合。

        Returns:
            首次 accepted 调用对应的提醒事务；拒绝或重复命令返回空。
        """

        if executed_call.state != "accepted":
            return None
        command_id = executed_call.resource_id
        executor = self._timed_reminder_tool_executor
        if not command_id or executor is None:
            return None
        reservation = executor.take_reservation(command_id)
        if reservation is None:
            raise RuntimeError(
                "定时提醒工具已接受，但缺少对应提交事务 "
                f"command_id={command_id}"
            )
        key = (reservation.source, reservation.reminder_id or reservation.command_id)
        if key in existing_keys:
            reservation.rollback()
            return None
        existing_keys.add(key)
        return reservation

    def _tool_followup_facts(
        self,
        *,
        execution: ForegroundToolExecution,
        reservations: list[
            BackgroundActivityReservation
            | ActivityCancellationReservation
            | TimedReminderCommandReservation
        ],
    ) -> tuple[ToolFollowupFact, ...]:
        """读取 after_commit 最终事实，并为 merged 调用补齐可表达事实。

        Args:
            execution: 已收集的 Foreground 工具执行结果。
            reservations: 本轮已绑定提交事务的后台 Activity 预留。

        Returns:
            按原始工具调用顺序排列且按 Activity 去重的最终事实。
        """

        fact_by_activity_id = {
            reservation.activity_id: reservation.action.after_commit_fact
            for reservation in reservations
            if not isinstance(reservation, TimedReminderCommandReservation)
            and reservation.action.after_commit_fact is not None
        }
        fact_by_command_id = {
            reservation.command_id: reservation.action.after_commit_fact
            for reservation in reservations
            if isinstance(reservation, TimedReminderCommandReservation)
            and reservation.action.after_commit_fact is not None
        }
        facts: list[ToolFollowupFact] = []
        seen_activity_ids: set[str] = set()
        for executed_call in execution.calls:
            if is_timed_reminder_tool(executed_call.call.name):
                command_id = executed_call.resource_id or ""
                fact = fact_by_command_id.get(command_id)
                if fact is None and executed_call.state == "accepted":
                    fact = ToolFollowupFact(
                        kind="timed_reminder_command_failed",
                        command_id=command_id,
                        safe_message="这次提醒操作的结果无法确认。",
                    )
                if fact is not None:
                    facts.append(fact)
                continue
            is_cancellation = is_cancel_activity_tool(executed_call.call.name)
            if executed_call.channel != "background" and not is_cancellation:
                continue
            activity_id = executed_call.activity_id
            if not activity_id or activity_id in seen_activity_ids:
                continue
            seen_activity_ids.add(activity_id)
            fact = fact_by_activity_id.get(activity_id)
            result_payload = executed_call.result_payload()
            goal_value = result_payload.get("brief") or result_payload.get("goal")
            goal = goal_value.strip() if isinstance(goal_value, str) else "这项任务"
            if fact is None and executed_call.state == "merged":
                fact = ToolFollowupFact(
                    kind="activity_started",
                    activity_id=activity_id,
                    goal=goal,
                    already_running=True,
                )
            if fact is None and executed_call.state == "accepted":
                fact = (
                    ToolFollowupFact(
                        kind="activity_cancellation_failed",
                        activity_id=activity_id,
                        goal=goal,
                        safe_message="这项任务没有确认进入停止流程。",
                    )
                    if is_cancellation
                    else ToolFollowupFact(
                        kind="activity_start_failed",
                        activity_id=activity_id,
                        goal=goal,
                        safe_message="这项任务的启动结果无法确认。",
                    )
                )
            if fact is not None:
                facts.append(fact)
        return tuple(facts)

    def _has_pre_tool_prefix_min_tokens(self, pre_tool_text_chunks: list[str]) -> bool:
        """判断工具确认前文本是否允许提前提交。

        Args:
            pre_tool_text_chunks: 尚未确认工具模式前缓存的模型文本分片。

        Returns:
            达到 TTS 流式句缓冲最小 token 门槛时返回 True。
        """

        text = "".join(pre_tool_text_chunks).strip()
        if not text:
            return False

        return self._token_counter(text) >= self._pre_tool_prefix_min_tokens

    def _batch_waiting_phrase_events(
        self,
        *,
        tool_calls: list[ForegroundToolCall],
        visible_parts: list[str],
        request_trace: ForegroundRequestTrace | None = None,
    ) -> list[ForegroundEvent]:
        """为整批工具调用生成至多一句固定等待反馈。

        Args:
            tool_calls: 本轮待执行的完整工具调用列表。
            visible_parts: 当前轮最终可见文本累积区。
            request_trace: 当前请求及 generation 的日志追踪身份。

        Returns:
            需要先于工具执行释放并立即送入 TTS 的唯一等待句事件。
        """

        # 先拿到“这批工具要不要先说一句等候话术”的策略器，以及查询工具元数据所需的目录服务。
        waiting_policy_resolver = self._tool_waiting_policy_resolver
        foreground_tools = self._foreground_tools
        # 这几种情况都不需要播固定等待句：
        # 没有策略器、拿不到工具元数据，或者前面已经有可见文本了，此时再插一句会显得重复又突兀。
        if (
            waiting_policy_resolver is None
            or foreground_tools is None
            or "".join(visible_parts).strip()
        ):
            return []

        # 等待句不是按单个工具硬编码，而是按整批工具一起决策：
        # 会综合工具名、是否后台任务、工具元数据和工具族策略，决定这一轮该不该说、该说哪一句。
        decision = waiting_policy_resolver.resolve_batch(
            tool_names=[tool_call.name for tool_call in tool_calls],
            is_background_tool=lambda name: (
                is_cognitive_tool(name)
                or is_start_background_work_tool(name)
                or is_activity_correction_tool(name)
                or is_respond_to_work_interaction_tool(name)
                or is_cancel_activity_tool(name)
                or is_timed_reminder_tool(name)
            ),
            metadata_resolver=foreground_tools.tool_metadata,
            family_metadata_resolver=foreground_tools.tool_family_metadata,
        )
        if decision.text is None:
            return []

        log_event(
            foreground_logger,
            logging.INFO,
            "foreground_tool_waiting_phrase_emitted",
            **_request_trace_fields(request_trace),
            mode=decision.mode,
            tool_count=len(tool_calls),
            tool_names=[tool_call.name for tool_call in tool_calls],
        )
        # 选中的等待句直接当作当前轮第一段可见文本发出去，让前端和 TTS 都能立刻收到。
        return self._text_chunk_events(
            text_chunk=decision.text,
            visible_parts=visible_parts,
            tts_delivery=TTS_DELIVERY_IMMEDIATE,
        )

    def _execute_inline_tool_with_trace(
        self,
        *,
        foreground_tools: ForegroundToolService,
        tool_call: ForegroundToolCall,
        request_trace: ForegroundRequestTrace | None,
        cancellation_event: Event | None,
        inline_execution_context: InlineExecutionContext,
        foreground_context: dict[str, object],
        frozen_visual_input: FrozenVisualInput,
        mounted_image_claim_lock: Lock | None = None,
        mounted_image_data_urls: set[str] | None = None,
    ) -> dict[str, Any] | InlineToolResult:
        """执行一个 Inline 工具并记录不含原始参数的生命周期事件。

        Args:
            foreground_tools: 当前前台工具注册表。
            tool_call: 模型产生的工具调用。
            request_trace: 当前请求及 generation 的日志追踪身份。
            cancellation_event: 当前 generation 的协作式取消信号。
            inline_execution_context: 从 Inline 提交时开始计时的副作用门禁。

        Returns:
            工具执行器返回的协议载荷。
        """

        trace_fields = _request_trace_fields(request_trace)
        log_event(
            foreground_logger,
            logging.INFO,
            "inline_tool_started",
            **trace_fields,
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.name,
        )
        if is_recall_task_memory_tool(tool_call.name):
            activity_id = tool_call.arguments.get("activity_id")
            normalized_activity_id = (
                activity_id.strip() if isinstance(activity_id, str) else ""
            )
            if self._task_memory_lookup_broker is None:
                result = {
                    "state": "failed",
                    "activity_id": normalized_activity_id,
                    "reason": "task_memory_broker_unavailable",
                    "safe_message": "任务详情当前无法读取。",
                }
            else:
                result = self._task_memory_lookup_broker.lookup_and_wait(
                    activity_id=normalized_activity_id,
                    purpose="foreground_recall",
                    cancellation_event=cancellation_event,
                )
        elif is_load_recent_image_for_foreground_tool(tool_call.name):
            already_mounted_paths = frozenset(
                image_material.path
                for image_material in frozen_visual_input.image_materials
            )
            payload, mounted_visual_input = execute_load_recent_image(
                tool_call.arguments,
                foreground_context=foreground_context,
                inline_execution_context=inline_execution_context,
                already_mounted_paths=already_mounted_paths,
                already_mounted_data_urls=frozenset(
                    image.data_url for image in frozen_visual_input.images
                ),
                claim_mounted_image=(
                    lambda data_url: _claim_mounted_image(
                        data_url,
                        lock=mounted_image_claim_lock,
                        claimed_data_urls=mounted_image_data_urls,
                    )
                    if mounted_image_claim_lock is not None
                    and mounted_image_data_urls is not None
                    else True
                ),
            )
            result = InlineToolResult(
                payload=payload,
                mounted_visual_input=mounted_visual_input,
            )
        else:
            result = foreground_tools.execute(
                tool_call.name,
                tool_call.arguments,
                inline_execution_context=inline_execution_context,
            )
        log_payload = result.payload if isinstance(result, InlineToolResult) else result
        log_event(
            foreground_logger,
            logging.INFO,
            "inline_tool_completed",
            **trace_fields,
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.name,
            ok=log_payload.get("ok"),
            state=log_payload.get("state"),
            reason=log_payload.get("reason"),
        )
        return result

    def _stable_tool_result(
        self,
        executed_call: ExecutedToolCall,
    ) -> dict[str, object]:
        """生成可以长期写入历史和内部事件的最小工具事实。

        Args:
            executed_call: 本轮已经完成的工具调用。

        Returns:
            普通工具的原结果副本，或任务召回的有限状态事实。
        """

        result = executed_call.result_payload()
        result.pop(RECENT_MATERIAL_OBSERVATION_FIELD, None)
        if not is_recall_task_memory_tool(executed_call.call.name):
            return result
        return {
            "state": result.get("state"),
            "activity_id": result.get("activity_id"),
            "terminal_state": result.get("terminal_state"),
            "detail_loaded": result.get("state") == "completed",
            "original_character_count": result.get("original_character_count"),
            "returned_character_count": result.get("returned_character_count"),
            "truncated": result.get("truncated", False),
            "reason": result.get("reason"),
            "safe_message": result.get("safe_message"),
        }

    def _push_visible_text_chunk(
        self,
        *,
        visible_parts: list[str],
        chunk: str,
    ) -> list[ForegroundEvent]:
        """处理单个文本分片并生成可见文本事件。

        Args:
            visible_parts: 已确认可见的文本分片列表。
            chunk: 本次收到的模型文本分片。

        Returns:
            本次分片对应的前台事件列表。
        """

        if not chunk:
            return []
        return self._text_chunk_events(
            text_chunk=chunk,
            visible_parts=visible_parts,
        )

    def _finish_visible_text_stream(
        self,
        *,
        visible_parts: list[str],
        foreground_context: dict[str, object] | None = None,
        request_mode: ForegroundRequestMode = "user_turn",
        request_id: str,
    ) -> list[ForegroundEvent]:
        """结束普通文本流，并补齐最终事件与会话记录。

        Args:
            visible_parts: 已确认可见的文本分片列表。
            foreground_context: 当前产品态势面板。
            request_mode: 当前请求是用户轮还是系统提醒。
            request_id: 当前回复所属的助手请求编号；纯解析测试可以传空字符串。
        Returns:
            收尾阶段产生的前台事件列表。
        """

        visible_text = "".join(visible_parts)
        foreground_context = foreground_context or {}
        progress_references: list[ActivityProgressReference] = []
        if visible_text.strip():
            progress_references = (
                _progress_notice_references(foreground_context)
                if request_mode == "system_notice"
                else []
            )
        has_terminal_notice = bool(_terminal_notices(foreground_context))
        request_policy = foreground_request_policy(request_mode)
        should_record_stable_reply = request_policy.records_assistant_reply and bool(
            visible_text.strip()
        ) and (
            not progress_references or has_terminal_notice
        )
        response_payload: dict[str, object] = {"text": visible_text}

        def commit_visible_response() -> None:
            """在 final 被接受后写入稳定回复并生成主进程提交。"""

            if not visible_text.strip():
                return
            if should_record_stable_reply:
                self._session_context.record_assistant_reply(
                    content=visible_text,
                )
                self._attach_memory_commit(
                    response_payload,
                    request_id=request_id,
                )

        # 最终文本事件固定放在最后，方便实时层直接收束当前轮输出。
        response_commit = ForegroundResponseCommit.create(
            kind="assistant",
            visible_text=visible_text,
            on_accept=commit_visible_response,
        )
        return [ForegroundEvent(
            type="assistant.text_final",
            payload=response_payload,
            commit_action=response_commit.action,
        )]

    def acknowledge_activity_progress(
        self,
        *,
        references: tuple[ActivityProgressReference, ...],
    ) -> int:
        """在主进程确认广播成功后清理执行器冻结进度。

        Args:
            references: 主进程已经登记到 Progress 临时账本的 Activity 引用。

        Returns:
            本次被对应执行器接受的引用数量。
        """

        with self._activity_progress_commit_lock:
            activity_executor_by_source = {
                "cognitive": self._cognitive_activity_executor,
                "background_agent": self._background_agent_activity_executor,
            }
            acknowledged_count = 0
            for reference in references:
                acknowledge = getattr(
                    activity_executor_by_source.get(reference.source),
                    "acknowledge_progress_notice",
                    None,
                )
                if callable(acknowledge) and acknowledge(
                    reference.activity_id,
                    reference.notice_sequence,
                ) is True:
                    acknowledged_count += 1
            return acknowledged_count

    def _finish_tool_result_text_stream(
        self,
        *,
        visible_parts: list[str],
        empty_reply_fallback: str,
        new_reply_start_index: int | None,
        force_fallback: bool = False,
        foreground_context: dict[str, object],
        assistant_tool_call_message: dict[str, Any],
        tool_result_messages: list[dict[str, Any]],
        request_id: str,
        protocol_transaction_id: str,
    ) -> list[ForegroundEvent]:
        """结束工具调用后的文本流。

        Args:
            visible_parts: 已确认可见的文本分片列表。
            empty_reply_fallback: 第二轮没有正文时使用的确定性非空文案。
            new_reply_start_index: 第二轮新增文字在 visible_parts 中的起始位置；跳过第二轮时为空。
            force_fallback: 二轮协议异常时，即使已经流出半句也必须追加完整兜底。
            foreground_context: 当前产品态势面板。
            assistant_tool_call_message: 持久历史使用的空 content 工具声明消息。
            tool_result_messages: 与声明中 call id 一一对应的工具结果消息。
            request_id: 当前工具事务所属的助手请求编号。
            protocol_transaction_id: 工具批次进入编排时分配的稳定事务编号。

        Returns:
            工具回复收尾阶段产生的前台事件列表。
        """

        events: list[ForegroundEvent] = []
        visible_text = "".join(visible_parts)
        required_reply_text = (
            visible_text
            if new_reply_start_index is None
            else "".join(visible_parts[new_reply_start_index:])
        )
        # 前缀和等待句不能冒充第二轮结果；需要续写时必须检查本轮真正新增的正文。
        if force_fallback or not required_reply_text.strip():
            events.extend(
                self._text_chunk_events(
                    text_chunk=empty_reply_fallback,
                    visible_parts=visible_parts,
                )
            )
            visible_text = "".join(visible_parts)

        response_payload: dict[str, object] = {"text": visible_text}
        def commit_tool_response() -> None:
            """在 final 被接受后原子写入工具事务并生成主进程提交。"""

            self._session_context.record_tool_turn(
                assistant_tool_call_message,
                tool_result_messages,
                visible_text,
                protocol_transaction_id=protocol_transaction_id,
            )
            self._attach_memory_commit(
                response_payload,
                request_id=request_id,
            )

        response_commit = ForegroundResponseCommit.create(
            kind="tool_transaction",
            visible_text=visible_text,
            on_accept=commit_tool_response,
        )
        events.append(
            ForegroundEvent(
                type="assistant.text_final",
                payload=response_payload,
                commit_action=response_commit.action,
            )
        )
        return events

    def _public_text_segment_event(
        self,
        text: str,
        *,
        request_id: str,
    ) -> ForegroundEvent:
        """把已真实公开的工具前缀结算为一条独立角色消息。

        Args:
            text: 已经通过文本事件进入界面和播音队列的非空文字。
            request_id: 公开前缀所属的助手请求编号。

        Returns:
            不结束当前 generation 的消息分段事件。
        """

        response_payload: dict[str, object] = {"text": text}

        def commit_segment() -> None:
            """写入公开分段并生成同一 generation 的增量提交。"""

            self._session_context.record_assistant_reply(
                content=text,
                metadata={"continued_in_same_generation": True},
            )
            self._attach_memory_commit(
                response_payload,
                request_id=request_id,
            )

        response_commit = ForegroundResponseCommit.create(
            kind="assistant_segment",
            visible_text=text,
            on_accept=commit_segment,
        )
        return ForegroundEvent(
            type="assistant.text_segment_final",
            payload=response_payload,
            commit_action=response_commit.action,
        )

    def _attach_memory_commit(
        self,
        payload: dict[str, object],
        *,
        request_id: str,
    ) -> None:
        """把子进程本轮历史增量附到最终事件，交给主进程提交。

        Args:
            payload: 尚未进入 RuntimeEvent 队列的内部事件载荷。
            request_id: 产生本次历史增量的助手请求编号。

        Returns:
            无。缺少生产请求身份时保持原载荷不变。
        """

        request_id = request_id.strip()
        if not request_id:
            return
        payload["memory_commit"] = self._session_context.build_memory_commit(
            request_id=request_id
        ).to_payload()


    def _text_chunk_events(
        self,
        *,
        text_chunk: str,
        visible_parts: list[str],
        tts_delivery: str | None = None,
    ) -> list[ForegroundEvent]:
        """把一次清洗文本转换为前台文本事件。

        Args:
            text_chunk: 标签清洗后的统一文本分片。
            visible_parts: 已确认的统一文本分片列表。
            tts_delivery: 可选 TTS 交付语义；等待句可用 immediate 绕过普通分句缓冲。

        Returns:
            需要交给 runner 的前台文本事件列表。
        """

        if not text_chunk:
            return []

        # Provider 会把词间空格、换行等作为独立 delta 返回。它们属于完整正文，
        # 但单独公开会被公共协议正确拒绝，所以先保留，等下一段可见文字一起发送。
        if not text_chunk.strip():
            visible_parts.append(text_chunk)
            return []

        unpublished_whitespace_start = len(visible_parts)
        while (
            unpublished_whitespace_start > 0
            and not visible_parts[unpublished_whitespace_start - 1].strip()
        ):
            unpublished_whitespace_start -= 1
        public_text_chunk = (
            "".join(visible_parts[unpublished_whitespace_start:]) + text_chunk
        )
        visible_parts.append(text_chunk)

        payload: dict[str, Any] = {"text": public_text_chunk}
        if tts_delivery is not None:
            payload["tts_delivery"] = tts_delivery

        return [ForegroundEvent(type="assistant.text_chunk", payload=payload)]

def _is_background_activity_tool(name: str) -> bool:
    """判断工具是否会创建或纠正一项后台 Activity。

    Args:
        name: Foreground 模型请求调用的工具名。

    Returns:
        工具会创建或纠正 Background Work 时返回真。
    """

    return is_start_background_work_tool(name) or name == CORRECT_BACKGROUND_WORK_TOOL


def _notice_batch(foreground_context: dict[str, object]) -> list[dict[str, object]]:
    """读取本轮内部提醒批次。

    Args:
        foreground_context: 当前产品态势面板。

    Returns:
        过滤掉非字典项后的提醒列表。
    """

    raw_batch = foreground_context.get("notice_batch")
    if not isinstance(raw_batch, list):
        return []
    return [notice for notice in raw_batch if isinstance(notice, dict)]


def _terminal_notices(foreground_context: dict[str, object]) -> list[dict[str, object]]:
    """提取本轮全部强终态提醒。

    Args:
        foreground_context: 当前产品态势面板。

    Returns:
        类型为终态的提醒列表。
    """

    terminal_intents = {
        "cognitive_result",
        "work_result",
        "terminal_status",
    }
    return [
        notice
        for notice in _notice_batch(foreground_context)
        if notice.get("intent") in terminal_intents
    ]


def _terminal_activity_keys(foreground_context: dict[str, object]) -> set[tuple[str, str]]:
    """提取本轮终态提醒对应的来源和活动编号。

    Args:
        foreground_context: 当前产品态势面板。

    Returns:
        去除无效字段后的来源与活动编号集合。
    """

    activity_keys: set[tuple[str, str]] = set()
    for notice in _terminal_notices(foreground_context):
        activity_id = notice.get("activity_id")
        source = notice.get("source")
        if (
            isinstance(activity_id, str)
            and activity_id.strip()
            and isinstance(source, str)
            and source.strip()
        ):
            activity_keys.add((source.strip(), activity_id.strip()))
    return activity_keys


def _progress_notice_references(
    foreground_context: dict[str, object],
) -> list[ActivityProgressReference]:
    """提取本轮实际可表达的 progress 活动与冻结序号。

    Args:
        foreground_context: 当前产品态势面板。

    Returns:
        去除无效字段后的活动进度关联列表。
    """

    references: list[ActivityProgressReference] = []
    for notice in _notice_batch(foreground_context):
        if notice.get("intent") != "progress_report":
            continue
        activity_id = notice.get("activity_id")
        source = notice.get("source")
        sequence = notice.get("sequence")
        if (
            not isinstance(activity_id, str)
            or not activity_id.strip()
            or not isinstance(source, str)
            or not source.strip()
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= 0
        ):
            continue
        references.append(
            ActivityProgressReference(
                activity_id=activity_id.strip(),
                source=source.strip(),
                notice_sequence=sequence,
            )
        )
    return references


def _request_trace_fields(
    request_trace: ForegroundRequestTrace | None,
) -> dict[str, str]:
    """返回 Foreground 编排事件共用的轻量追踪字段。

    Args:
        request_trace: 当前请求及 generation 的日志追踪身份。

    Returns:
        可展开到结构化日志中的身份字段；没有追踪身份时返回空字典。
    """

    if request_trace is None:
        return {}
    return {
        "request_id": request_trace.request_id,
        "generation_id": request_trace.generation_id,
    }


def _claim_mounted_image(
    data_url: str,
    *,
    lock: Lock,
    claimed_data_urls: set[str],
) -> bool:
    """原子领取本请求唯一的近期图片挂载名额。

    Args:
        data_url: 已完成安全编码的图片内容。
        lock: 当前请求级互斥锁。
        claimed_data_urls: 当前请求已经领取的图片内容集合。

    Returns:
        首次领取该图片内容时返回真，重复或并行挂载返回假。
    """

    with lock:
        if claimed_data_urls:
            return False
        claimed_data_urls.add(data_url)
        return True
