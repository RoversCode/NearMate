from __future__ import annotations

import base64
import time
from collections.abc import Callable, Iterable
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Protocol

from companion.audio.providers.base import TtsProvider
from companion.audio.leading_silence_gate import LeadingSilenceGate
from companion.audio.streamvox_models import (
    StreamVoxModelProfile,
    resolve_streamvox_model_profile,
)
from companion.character_cards.models import CharacterCardDefinition
from companion.llm.call_trace import ForegroundRequestTrace
from companion.llm.foreground_models import (
    TTS_DELIVERY_IMMEDIATE,
    ForegroundEvent,
    ForegroundRequestMode,
)
from companion.observability.logging_config import get_backend_logger
from companion.observability.realtime_latency import RealtimeLatencyRecorder
from companion.records.debug import InteractionDebugRecorderProtocol, NoopInteractionDebugRecorder
from companion.realtime.assistant_events import AssistantRequest, RuntimeEvent
from companion.realtime.assistant_output_contracts import (
    ActivityProgressReference,
    AssistantHistoryPolicy,
    AssistantOutputSettlementOutcome,
)
from companion.realtime.assistant_generation import (
    REQUEST_QUEUE_STOP,
    TTS_QUEUE_STOP,
    AssistantRequestWorkItem,
    GenerationContext,
    TtsGenerationFinished,
    TtsSegmentWorkItem,
)
from companion.realtime.cancellation import CombinedCancellationSignal
from companion.realtime.generation_supervisor import GenerationSupervisor
from companion.realtime.sentence_queue import StreamingSentenceBuffer
from companion.realtime.stream_text_semantics import project_speakable_text
from companion.realtime.text_tokenizer import count_text_tokens
from companion.prompt.streamvox_capabilities import StreamVoxTagSanitizer, create_streamvox_tag_sanitizer
from companion.text.normalization import normalize_tts_text
from companion.text.http_urls import replace_http_urls_for_speech


logger = get_backend_logger(__name__)

USER_INTERRUPTED_FINAL_SUFFIX = "【user打断了此次的回复】"
NOTICE_SUPERSEDED_FINAL_SUFFIX = "【状态已更新，此回复已中断】"


AssistantRequestQueueItem = AssistantRequestWorkItem | object
TtsQueueItem = TtsSegmentWorkItem | TtsGenerationFinished | object


class ForegroundStreamingAgent(Protocol):
    """约束助手前台流式生成接口。"""

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
        request_mode: ForegroundRequestMode = "user_turn",
        cancellation_event: Event | None = None,
        request_trace: ForegroundRequestTrace | None = None,
    ) -> Iterable[ForegroundEvent]:
        """流式处理用户文本并按顺序产出前台事件。

        Args:
            text: 当前请求的输入文本。
            now: 主进程捕获的请求时间。
            interaction_session_id: 当前交互会话编号。
            character_card: 当前角色卡提示词配置。
            user_call_name: 角色称呼用户的名称。
            user_self_intro: 用户自我介绍。
            user_extra_notes: 用户补充说明。
            streamvox_model: 当前语音模型名称。
            foreground_context: 当前材料、环境与活动态势。
            request_mode: 当前请求是用户轮还是系统提醒。
            cancellation_event: 当前 generation 的协作式取消信号。
            request_trace: 当前请求及 generation 的日志追踪身份。

        Returns:
            按生成顺序产出的前台事件迭代器。
        """
        ...

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
        """创建带明确归档策略的中断最终事件。

        Args:
            display_text: Desktop 需要显示的模型正文和中断尾注。
            model_text: 模型在取消前真正公开的正文。
            request_id: 原始助手请求编号。
            history_policy: 正文应进入稳定对话、Progress 临时账本，
                还是未处理插话片段账本。
            progress_references: Progress 正文关联的 Activity 冻结进度。
            interruption_reason: 系统主动接管等稳定中断原因。

        Returns:
            可交给 GenerationSupervisor 提交的最终事件。
        """

        ...

    def create_runtime_fallback_event(
        self,
        text: str,
        *,
        request_id: str,
        history_policy: AssistantHistoryPolicy,
        mark_notice_delivery_failed: bool = True,
    ) -> ForegroundEvent:
        """创建带历史提交动作的运行时兜底最终事件。

        Args:
            text: 用户实际看到的确定性安全正文。
            request_id: 这条正文所属的助手请求编号。
            history_policy: 正文最终归入的稳定历史、Progress 或 episode 策略。
            mark_notice_delivery_failed: 是否把正文标记成 Notice 未完成。

        Returns:
            可交给输出监督器提交的最终事件。
        """

        ...

    def create_direct_output_event(
        self,
        text: str,
        *,
        request_id: str,
        history_policy: AssistantHistoryPolicy,
    ) -> ForegroundEvent:
        """创建无需模型参与、但按普通正文提交的最终事件。

        Args:
            text: 主进程已经确定的完整可见正文。
            request_id: 这条正文所属的助手请求编号。
            history_policy: 正文最终归入的稳定历史策略。

        Returns:
            可交给输出监督器提交的最终事件。
        """

        ...

    def warmup_foreground(self) -> None:
        """按需预热前台模型连接。"""
        ...

    def acknowledge_activity_progress(
        self,
        *,
        references: tuple[ActivityProgressReference, ...],
    ) -> int:
        """清理主进程已经确认公开的 Activity 冻结进度。

        Args:
            references: 已成功广播并写入主进程账本的 Progress 引用。

        Returns:
            被对应 Activity 执行器接受的引用数量。
        """

        ...

class AssistantOutputLanes:
    """管理助手输出侧的常驻 LLM 与 TTS worker。"""

    def __init__(
        self,
        *,
        agent: ForegroundStreamingAgent,
        tts_provider: TtsProvider,
        now: Callable[[], float],
        event_queue: Queue[RuntimeEvent] | None = None,
        event_notifier: Callable[[], None] | None = None,
        debug_recorder: InteractionDebugRecorderProtocol | None = None,
        latency_recorder: RealtimeLatencyRecorder | None = None,
    ) -> None:
        """初始化 runner 依赖与内部队列。

        Args:
            agent: 负责产出前台文本事件的流式代理。
            tts_provider: 负责把句段转换为音频块的 TTS 提供方。
            now: 事件时间戳来源。
            event_notifier: 产出新事件后用于唤醒外层调度循环的可选回调。
            debug_recorder: 完整交互调试记录器；未启用时使用 no-op。
            latency_recorder: 输出侧关键阶段使用的可选延迟记录器。
        """

        # runner 只消费统一事件流，不直接依赖具体模型客户端或提示词实现。
        self.agent = agent
        # TTS 通过统一 provider 边界注入，方便测试替身与真实实现切换。
        self.tts_provider = tts_provider
        # 所有运行时事件共用同一时间源，便于测试断言。
        self.now = now
        # 子进程模式下可借此唤醒外层循环及时转发事件；本地测试可留空。
        self.event_notifier = event_notifier
        # debug_recorder 是助手输出侧的本地诊断落盘边界，默认 no-op 保持热路径扁平。
        self.debug_recorder = debug_recorder or NoopInteractionDebugRecorder()
        # latency_recorder 是可选本地耗时记录器，用于输出侧关键 marker 打点。
        self.latency_recorder = latency_recorder
        # 对外事件统一先进入内部缓冲，再由上层批量拉取。
        self.event_queue = event_queue if event_queue is not None else Queue()
        # 主输出域串行消费普通请求与不可打断的 Self-Reflection Recovery。
        self._request_queue: Queue[AssistantRequestQueueItem] = Queue()
        # 辅助输出域独立消费打扰回应，高 Thinking 不能堵住确认用户已被听见的短回复。
        self._auxiliary_request_queue: Queue[AssistantRequestQueueItem] = Queue()
        # TTS worker 串行消费句段与收尾信号，避免多个 generation 交错播音。
        self._tts_queue: Queue[TtsQueueItem] = Queue()
        # 启停过程单独加锁，避免并发 start/shutdown 产生多组线程。
        self._lifecycle_lock = Lock()
        # 两条 worker 共享同一个关闭信号。
        self._stop_event = Event()
        # GenerationSupervisor
        self._supervisor = self._create_supervisor()
        # 三条常驻线程的生命周期都由 runner 托管。
        self._llm_worker_thread: Thread | None = None
        self._auxiliary_llm_worker_thread: Thread | None = None
        self._tts_worker_thread: Thread | None = None

    def _create_supervisor(self) -> GenerationSupervisor:
        """创建当前生命周期使用的 generation 状态机。

        Returns:
            绑定当前队列与事件通知器的 generation 监督器。
        """

        return GenerationSupervisor(
            request_queue=self._request_queue,
            auxiliary_request_queue=self._auxiliary_request_queue,
            tts_queue=self._tts_queue,
            event_queue=self.event_queue,
            now=self.now,
            event_notifier=self.event_notifier,
            debug_recorder=self.debug_recorder,
        )

    def start(self) -> None:
        """启动主 LLM、辅助 LLM 与共享 TTS worker。"""

        with self._lifecycle_lock:
            # 先统一读取线程状态，避免同一轮判断里多次查询造成语义分散。
            llm_worker_alive = self._is_thread_alive(self._llm_worker_thread)
            auxiliary_llm_worker_alive = self._is_thread_alive(
                self._auxiliary_llm_worker_thread
            )
            tts_worker_alive = self._is_thread_alive(self._tts_worker_thread)

            # 三条线程都还活着时，说明当前生命周期已经启动完成。
            if llm_worker_alive and auxiliary_llm_worker_alive and tts_worker_alive:
                return

            # 上一轮线程已经完全退出时，重建队列以清掉旧生命周期残留的停止哨兵。
            if (
                not llm_worker_alive
                and not auxiliary_llm_worker_alive
                and not tts_worker_alive
            ):
                self._request_queue = Queue[AssistantRequestQueueItem]()
                self._auxiliary_request_queue = Queue[AssistantRequestQueueItem]()
                self._tts_queue = Queue()
                self._supervisor = self._create_supervisor()

            # 新生命周期开始前清除 stop 标记，允许同一个 runner 被重新启用。
            self._stop_event.clear()
            if not tts_worker_alive:
                self._tts_worker_thread = Thread(
                    target=self._run_tts_worker,
                    name="assistant-tts-worker",
                    daemon=True,
                )
                self._tts_worker_thread.start()
            if not llm_worker_alive:
                # LLM 线程最后启动即可，不影响两个队列的消费语义。
                self._llm_worker_thread = Thread(
                    target=self._run_llm_worker,
                    name="assistant-llm-worker",
                    daemon=True,
                )
                self._llm_worker_thread.start()
            if not auxiliary_llm_worker_alive:
                self._auxiliary_llm_worker_thread = Thread(
                    target=self._run_auxiliary_llm_worker,
                    name="assistant-auxiliary-llm-worker",
                    daemon=True,
                )
                self._auxiliary_llm_worker_thread.start()

    def start_request(self, request: AssistantRequest) -> str:
        """启动一次新的助手生成。

        Args:
            request: 本轮生成请求。

        Returns:
            新 generation 的标识。
        """

        return self._supervisor.start_generation(request)

    def request_recovery_fallback(
        self,
        *,
        self_reflection_id: str,
        reason: str,
    ) -> bool:
        """停止 Recovery 的模型流，并让主输出域公开确定性兜底正文。"""

        return self._supervisor.request_recovery_fallback(
            self_reflection_id=self_reflection_id,
            reason=reason,
        )

    def cancel_current_generation(
        self, reason: str, generation_id: str | None = None, request_id: str | None = None
    ) -> None:
        """取消当前活跃 generation。

        Args:
            reason: 取消原因。
            generation_id: 可选的目标 generation 标识。
            request_id: 可选的目标请求编号，防止旧取消命令打断新回复。

        Returns:
            None: 将取消交给生成生命周期管理器。
        """

        self._supervisor.cancel_current(
            reason=reason, generation_id=generation_id, request_id=request_id
        )

    def cancel_superseded_system_notice(self, *, request_id: str, reason: str) -> bool:
        """取消编号匹配且来源正确的独立 System Notice。

        Args:
            request_id: 需要严格匹配的独立 System Notice 请求编号。
            reason: 触发事实失效取消的业务原因。

        Returns:
            请求编号和来源都匹配且成功取消时返回真。
        """

        return self._supervisor.cancel_superseded_system_notice(
            request_id=request_id,
            reason=reason,
        )

    def cancel_timed_reminder_notice(self, *, request_id: str, reason: str) -> bool:
        """按请求身份取消 Reminder Notice 的正文生成和 TTS 生产。"""

        return self._supervisor.cancel_timed_reminder_notice(
            request_id=request_id,
            reason=reason,
        )

    def warmup_foreground(self) -> None:
        """触发前台模型最小预热。"""

        self.agent.warmup_foreground()

    def acknowledge_activity_progress(
        self,
        *,
        references: tuple[ActivityProgressReference, ...],
    ) -> int:
        """把主进程确认过的 Progress 序号交给 Activity 执行器。

        Args:
            references: 已成功广播并登记到主进程临时账本的 Progress 引用。

        Returns:
            Foreground 关联执行器实际接受的引用数量。
        """

        return self.agent.acknowledge_activity_progress(references=references)

    def mark_request_superseded(self, request: AssistantRequest) -> None:
        """记录尚未启动就被同轮最新请求覆盖的旧请求。

        Args:
            request: 被同批次更新请求覆盖的旧助手请求。
        """

        self._supervisor.mark_request_superseded(request)

    def drain_events(self, limit: int = 100) -> list[RuntimeEvent]:
        """非阻塞读取已产出的运行时事件。

        Args:
            limit: 本次最多取出的事件数量。

        Returns:
            按产生顺序排列的运行时事件。
        """

        if limit <= 0:
            return []

        # 本轮读取结果先收成快照，避免调用方消费期间再受后续入队影响。
        events: list[RuntimeEvent] = []
        for _ in range(limit):
            try:
                events.append(self.event_queue.get_nowait())
            except Empty:
                break
        #  ForegroundTurnProcessor的drain events
        drain_agent_events = getattr(self.agent, "drain_events", None)
        if callable(drain_agent_events) and len(events) < limit:
            remaining = limit - len(events)
            for foreground_event in drain_agent_events(remaining):
                events.append(
                    RuntimeEvent(
                        type=foreground_event.type,
                        source="assistant_output_lanes",
                        payload=dict(foreground_event.payload),
                        produced_at=self.now(),
                    )
                )
        return events

    def join_current(self, timeout: float) -> None:
        """等待两个输出域当前 generation 收束完成。

        Args:
            timeout: 等待每条运行线程退出的最长秒数。
        """

        contexts = tuple(
            context
            for context in (
                self._supervisor.current_context(lane="primary"),
                self._supervisor.current_context(lane="auxiliary"),
            )
            if context is not None
        )
        deadline = time.monotonic() + max(0.0, timeout)
        for context in contexts:
            context.completion_event.wait(
                timeout=max(0.0, deadline - time.monotonic())
            )

    def shutdown(self, timeout: float = 1.0) -> None:
        """关闭常驻 LLM 与 TTS worker。

        Args:
            timeout: 两条 worker 共享的退出等待时长。
        """

        with self._lifecycle_lock:
            # 没有线程句柄时说明 runner 已经处于关闭态，重复调用只补 stop 标记。
            if (
                self._llm_worker_thread is None
                and self._auxiliary_llm_worker_thread is None
                and self._tts_worker_thread is None
            ):
                self._stop_event.set()
                return

            self._supervisor.cancel_all(reason="shutdown")
            self._stop_event.set()
            self._request_queue.put(REQUEST_QUEUE_STOP)
            self._auxiliary_request_queue.put(REQUEST_QUEUE_STOP)
            self._tts_queue.put(TTS_QUEUE_STOP)

            # 三条线程共享一个超时预算，避免逐个 join 把总耗时拉长。
            deadline = time.monotonic() + max(0.0, timeout)
            for thread in (
                self._llm_worker_thread,
                self._auxiliary_llm_worker_thread,
                self._tts_worker_thread,
            ):
                if thread is None:
                    continue
                remaining = max(0.0, deadline - time.monotonic())
                thread.join(timeout=remaining)

            if self._llm_worker_thread is not None and not self._llm_worker_thread.is_alive():
                self._llm_worker_thread = None
            if (
                self._auxiliary_llm_worker_thread is not None
                and not self._auxiliary_llm_worker_thread.is_alive()
            ):
                self._auxiliary_llm_worker_thread = None
            if self._tts_worker_thread is not None and not self._tts_worker_thread.is_alive():
                self._tts_worker_thread = None

    def _run_llm_worker(self) -> None:
        """LLM worker 主循环。"""

        while not self._stop_event.is_set():
            item = self._request_queue.get()
            if item is REQUEST_QUEUE_STOP:
                break
            if not isinstance(item, AssistantRequestWorkItem):
                continue
            self._run_llm_generation(item.context)

    def _run_auxiliary_llm_worker(self) -> None:
        """辅助 LLM worker 主循环。"""

        while not self._stop_event.is_set():
            item = self._auxiliary_request_queue.get()
            if item is REQUEST_QUEUE_STOP:
                break
            if not isinstance(item, AssistantRequestWorkItem):
                continue
            self._run_llm_generation(item.context)

    def _run_tts_worker(self) -> None:
        """TTS worker 主循环。"""

        while not self._stop_event.is_set():
            item = self._tts_queue.get()
            if item is TTS_QUEUE_STOP:
                break
            if isinstance(item, TtsGenerationFinished):
                self._finish_tts_generation(item.context)
                continue
            if isinstance(item, TtsSegmentWorkItem):
                self._run_tts_segment(item)

    def _run_llm_generation(self, context: GenerationContext) -> None:
        """消费单个 generation 的 LLM 文本流。

        Args:
            context: 当前 generation 上下文。

        """

        if not self._supervisor.is_context_active(context):
            self._supervisor.log_stale_event(context, "assistant.llm_generation_start")
            context.pending_output_outcome = _output_settlement_outcome(
                context,
                llm_failed=False,
            )
            self._supervisor.mark_lane_finished(context, lane="llm")
            return

        # 增量文本先聚合成完整句，尽量让 TTS 边生成边开口，而不是等整段结束。
        sentence_buffer = StreamingSentenceBuffer()
        # 失败态下不再把缓冲尾句继续送去播报。
        llm_failed = False
        # 请求时间事实直接透传给 agent，不能因为 0.0 等值被错误兜底。
        generation_now = context.request.requested_at
        first_text_chunk_seen = False
        streamvox_model = self._current_streamvox_model()
        streamvox_profile = resolve_streamvox_model_profile(streamvox_model)
        tag_sanitizer = create_streamvox_tag_sanitizer(streamvox_model)

        model_cancellation_event = CombinedCancellationSignal(
            context.cancellation_event,
            context.model_cancellation_event,
        )
        try:
            direct_output_text = (
                context.request.direct_output_text.strip()
                if isinstance(context.request.direct_output_text, str)
                else ""
            )
            if direct_output_text:
                # 权限卡短确认已经由主进程确定。这里只复用正文、TTS 和历史提交协议，
                # 不能再让 Foreground LLM 扩写成任务结果或长期授权承诺。
                direct_final_event = self.agent.create_direct_output_event(
                    direct_output_text,
                    request_id=context.request.request_id,
                    history_policy=context.request.history_policy,
                )
                if not self._publish_fallback_chunk(
                    context,
                    text=direct_output_text,
                    tag_sanitizer=tag_sanitizer,
                    streamvox_profile=streamvox_profile,
                ):
                    return
                if self._supervisor.put_foreground_event_if_current(
                    context,
                    direct_final_event,
                ):
                    if direct_final_event.commit_action is not None:
                        direct_final_event.commit_action.run_after_commit()
                    self.debug_recorder.record_llm_text_final(
                        context,
                        text=direct_output_text,
                    )
                return

            # 请求追踪同时绑定请求与生成身份，并为同一轮里的多次模型调用分配独立编号。
            for foreground_event in self.agent.stream_user_text(
                context.request.user_text,
                now=generation_now,
                interaction_session_id=context.request.interaction_session_id,
                character_card=context.request.character_card,
                user_call_name=context.request.user_call_name,
                user_self_intro=context.request.user_self_intro,
                user_extra_notes=context.request.user_extra_notes,
                streamvox_model=streamvox_model,
                foreground_context=context.request.foreground_context,
                environment_snapshot=context.request.environment_snapshot,
                frozen_visual_input=context.request.frozen_visual_input,
                request_mode=(
                    context.request.source
                    if context.request.source
                    in {"system_notice", "timed_reminder_notice"}
                    else context.request.source
                    if context.request.source
                    in {
                        "self_reflection_entry",
                        "self_reflection_recovery",
                        "self_reflection_interruption_reply",
                    }
                    else "user_turn"
                ),
                cancellation_event=model_cancellation_event,
                request_trace=ForegroundRequestTrace(
                    request_id=context.request.request_id,
                    generation_id=context.generation_id,
                    request_source=context.request.source,
                ),
                runtime_memory_snapshot=context.request.runtime_memory_snapshot,
                self_reflection_recovery_context=(
                    context.request.self_reflection_context
                    if context.request.source == "self_reflection_recovery"
                    else None
                ),
            ):
                if not self._supervisor.is_context_active(context):
                    return
                if not self._supervisor.is_model_stream_active(context):
                    break

                if foreground_event.type == "assistant.text_segment_final":
                    # 前缀已经成为一条完整角色消息。冲刷它的 TTS 尾句，但保持 generation
                    # 和 TTS worker 存活，让工具拒绝后的第二条回复继续沿用同一次交互。
                    self._flush_sentence_buffer(
                        context,
                        sentence_buffer,
                        tag_sanitizer,
                        streamvox_profile,
                    )
                    if self._supervisor.put_foreground_event_if_current(
                        context,
                        foreground_event,
                    ):
                        if foreground_event.commit_action is not None:
                            foreground_event.commit_action.run_after_commit()
                        sentence_buffer = StreamingSentenceBuffer()
                    continue

                if foreground_event.type == "assistant.text_final":
                    final_text = foreground_event.payload.get("text")
                    reflection_context = context.request.self_reflection_context
                    if (
                        context.request.source == "self_reflection_recovery"
                        and isinstance(final_text, str)
                        and final_text.strip()
                        and not context.visible_parts
                    ):
                        # 正常 Foreground 会先发 text_chunk，但适配器或测试替身可能
                        # 直接给出非空 final。它仍然必须经过同一个首正文交接边界，
                        # 否则辅助回应会在正式正文之后继续输出，而且这段正文不会进入 TTS。
                        direct_final_chunk = ForegroundEvent(
                            type="assistant.text_chunk",
                            payload={"text": final_text},
                        )
                        if not self._supervisor.put_visible_text_event_if_current(
                            context,
                            direct_final_chunk,
                            text=final_text,
                        ):
                            return
                        self.debug_recorder.record_llm_text_chunk(
                            context,
                            text=final_text,
                        )
                        self._queue_tts_sentence(
                            context,
                            final_text,
                            tag_sanitizer,
                            streamvox_profile,
                            source="direct_final",
                        )
                    if (
                        reflection_context is not None
                        and (
                            not isinstance(final_text, str)
                            or not final_text.strip()
                        )
                    ):
                        # Entry 与 Recovery 都有确定性安全文案。模型空输出不能把
                        # 入场状态或最终恢复事实静默吞掉。
                        foreground_event = self.agent.create_runtime_fallback_event(
                            reflection_context.fallback_text,
                            request_id=context.request.request_id,
                            history_policy=context.request.history_policy,
                            mark_notice_delivery_failed=False,
                        )
                        if not self._publish_fallback_chunk(
                            context,
                            text=reflection_context.fallback_text,
                            tag_sanitizer=tag_sanitizer,
                            streamvox_profile=streamvox_profile,
                        ):
                            return
                    # final 必须先进入统一事件队列，避免 TTS 空流收尾抢先清理 notice 状态。
                    self._flush_sentence_buffer(
                        context,
                        sentence_buffer,
                        tag_sanitizer,
                        streamvox_profile,
                    )
                    if self._supervisor.put_foreground_event_if_current(
                        context,
                        foreground_event,
                    ):
                        if foreground_event.commit_action is not None:
                            foreground_event.commit_action.run_after_commit()
                        final_text = foreground_event.payload.get("text")
                        if isinstance(final_text, str):
                            self.debug_recorder.record_llm_text_final(context, text=final_text)
                    return

                if foreground_event.type != "assistant.text_chunk":
                    if not self._supervisor.put_foreground_event_if_current(context, foreground_event):
                        return
                    if foreground_event.commit_action is not None:
                        foreground_event.commit_action.run_after_commit()
                    continue

                # 前台保留模型原始可见文本；TTS 只在完整句边界执行标签和语义正则化。
                text = foreground_event.payload.get("text")
                if not isinstance(text, str) or not text:
                    continue

                if not self._supervisor.put_visible_text_event_if_current(
                    context,
                    foreground_event,
                    text=text,
                ):
                    return

                self.debug_recorder.record_llm_text_chunk(context, text=text)

                if not first_text_chunk_seen:
                    first_text_chunk_seen = True
                    logger.info(
                        "LLM开始产出文本 generation_id=%s request_id=%s",
                        context.generation_id,
                        context.request.request_id,
                    )
                    # LLM 首 token 到达，记录 marker
                    if self.latency_recorder is not None and context.request.source_turn_id:
                        self.latency_recorder.record_marker(
                            trace_id=context.request.source_turn_id,
                            marker="llm_first_token",
                            at_wall=self.now(),
                        )

                # 策略等待句是已经收束好的完整提示语，必须即时入队，不能被普通流式分句门槛扣住。
                if foreground_event.payload.get("tts_delivery") == TTS_DELIVERY_IMMEDIATE:
                    self._queue_tts_sentence(
                        context,
                        text,
                        tag_sanitizer,
                        streamvox_profile,
                        source="immediate",
                    )
                    continue

                for sentence in sentence_buffer.push_text_delta(text):
                    if not self._supervisor.is_context_active(context):
                        return
                    if not self._supervisor.is_model_stream_active(context):
                        return
                    self._queue_tts_sentence(
                        context,
                        sentence,
                        tag_sanitizer,
                        streamvox_profile,
                        source="stream_sentence",
                    )
        except Exception as exc:
            logger.exception(
                "Foreground LLM lane 执行失败 request_id=%s generation_id=%s",
                context.request.request_id,
                context.generation_id,
            )
            llm_failed = True
            if self._supervisor.is_context_active(context):
                if self._is_llm_authentication_error(exc):
                    self._supervisor.put_event_if_current(
                        context,
                        "llm.foreground.authentication_failed",
                        {
                            "code": "llm_provider_credential_invalid",
                            "generation_id": context.generation_id,
                        },
                    )
                reflection_context = context.request.self_reflection_context
                fallback_text = (
                    reflection_context.fallback_text
                    if reflection_context is not None
                    else "抱歉，我刚才生成回复时遇到了问题，请稍后再试。"
                )
                fallback_event = self.agent.create_runtime_fallback_event(
                    f"{''.join(context.visible_parts)}{fallback_text}",
                    request_id=context.request.request_id,
                    history_policy=context.request.history_policy,
                    mark_notice_delivery_failed=reflection_context is None,
                )
                fallback_published = self._publish_fallback_chunk(
                    context,
                    text=fallback_text,
                    tag_sanitizer=tag_sanitizer,
                    streamvox_profile=streamvox_profile,
                )
                if fallback_published and self._supervisor.put_foreground_event_if_current(context, fallback_event):
                    if fallback_event.commit_action is not None:
                        fallback_event.commit_action.run_after_commit()
                self._supervisor.put_event_if_current(
                    context,
                    "assistant.state_changed",
                    {
                        "assistant_state": "failed",
                        "generation_id": context.generation_id,
                        "reason": "llm_error",
                    },
                )
                # 普通轮沿用原来的失败取消语义；Self-Reflection 的确定性 fallback
                # 必须继续走完 TTS，不能因为模型异常把已经公开的安全正文静音。
                if reflection_context is None:
                    context.cancellation_event.set("llm_error")
        finally:
            if self._supervisor.recovery_fallback_requested(context):
                self._publish_recovery_fallback(
                    context,
                    tag_sanitizer=tag_sanitizer,
                    streamvox_profile=streamvox_profile,
                )
            self._emit_interrupted_text_final(context)
            # 这里只冻结 LLM 的最终结果；共享 TTS lane 真正排空后才允许发布 settlement。
            context.pending_output_outcome = _output_settlement_outcome(
                context,
                llm_failed=llm_failed,
            )
            # 无论正常、取消还是异常，最终都要通知 TTS lane 可以准备收尾。
            if self._supervisor.is_context_active(context) and not llm_failed:
                self._flush_sentence_buffer(
                    context,
                    sentence_buffer,
                    tag_sanitizer,
                    streamvox_profile,
                )
            self._tts_queue.put(TtsGenerationFinished(context=context))
            self._supervisor.mark_lane_finished(context, lane="llm")

    def _run_tts_segment(self, item: TtsSegmentWorkItem) -> None:
        """处理单个 TTS 句段合成任务。

        Args:
            item: 已归属到 generation 的待合成句段。
        """

        context = item.context
        if not self._supervisor.is_context_active(context):
            self._supervisor.log_stale_event(context, "assistant.tts_segment_start")
            return
        current_model = self._current_streamvox_model()
        if resolve_streamvox_model_profile(current_model).model != item.streamvox_model:
            logger.warning(
                "TTS句段模型快照已过期，丢弃旧句段 generation_id=%s expected_model=%s current_model=%s",
                context.generation_id,
                item.streamvox_model,
                current_model,
            )
            return

        first_audio_chunk_seen = False
        self.debug_recorder.record_tts_segment(context, text=item.text)
        try:
            for chunk in self.tts_provider.stream_segment(item.text, context.generation_id):
                if (
                    context.cancellation_event.is_set()
                    or not self._supervisor.is_current_context(context)
                    or chunk.generation_id != context.generation_id
                ):
                    break
                if not first_audio_chunk_seen:
                    first_audio_chunk_seen = True
                    logger.info(
                        "TTS开始产出音频 generation_id=%s request_id=%s",
                        context.generation_id,
                        context.request.request_id,
                    )
                for pcm16 in self._gate_tts_audio_chunk(context, pcm16=chunk.pcm16, sample_rate=chunk.sample_rate):
                    if not self._emit_tts_audio_chunk(
                        context,
                        pcm16=pcm16,
                        sample_rate=chunk.sample_rate,
                        is_final=chunk.is_final,
                    ):
                        break
                else:
                    continue
                break
        except Exception as exc:
            logger.exception(
                "TTS 分段执行失败 request_id=%s generation_id=%s",
                context.request.request_id,
                context.generation_id,
            )
            if self._supervisor.is_context_active(context):
                self._supervisor.put_event_if_current(
                    context,
                    "assistant.tts_segment_failed",
                    {
                        "session_id": context.source_session_id,
                        "generation_id": context.generation_id,
                        "delivery_id": context.delivery_id,
                        "text": item.text,
                        "error": str(exc),
                    },
                )

    def _gate_tts_audio_chunk(
        self,
        context: GenerationContext,
        *,
        pcm16: bytes,
        sample_rate: int,
    ) -> list[bytes]:
        """对 generation 开头音频执行一次首响门控。

        Args:
            context: 当前 generation 上下文。
            pcm16: 本次 TTS 输出的 PCM16 音频。
            sample_rate: 本次 TTS 输出采样率。

        Returns:
            当前允许发送到播放队列的 PCM16 块列表。
        """

        if not pcm16:
            return [pcm16]

        if context.tts_leading_gate is None:
            context.tts_leading_gate = LeadingSilenceGate(sample_rate=sample_rate)

        results = context.tts_leading_gate.push(pcm16)

        # TTS 首响门控首次释放，记录 marker
        if results and not context.tts_gate_released:
            context.tts_gate_released = True
            if self.latency_recorder is not None and context.request.source_turn_id:
                self.latency_recorder.record_marker(
                    trace_id=context.request.source_turn_id,
                    marker="tts_gate_released",
                    at_wall=self.now(),
                )

        return results

    def _emit_tts_audio_chunk(
        self,
        context: GenerationContext,
        *,
        pcm16: bytes,
        sample_rate: int,
        is_final: bool,
    ) -> bool:
        """发送单个 TTS 音频事件并同步写入调试录音。

        Args:
            context: 音频所属 generation 上下文。
            pcm16: 单声道 PCM16 音频数据。
            sample_rate: 音频采样率。
            is_final: 是否为当前句段的最后一个音频块。

        Returns:
            音频仍属于当前有效 generation 并成功投递时返回真。
        """

        audio_payload = {
            "session_id": context.source_session_id,
            "generation_id": context.generation_id,
            "source_turn_id": context.request.source_turn_id,
            "pcm16_b64": base64.b64encode(pcm16).decode("ascii"),
            "sample_rate": sample_rate,
            "is_final": is_final,
        }
        if not self._supervisor.put_event_if_current(
            context,
            "assistant.audio_chunk",
            audio_payload,
        ):
            return False

        self.debug_recorder.record_tts_audio_chunk(
            context,
            pcm16=pcm16,
            sample_rate=sample_rate,
            is_final=is_final,
        )
        return True

    def _finish_tts_generation(self, context: GenerationContext) -> None:
        """完成单个 generation 的 TTS 收尾。

        Args:
            context: 需要结束 TTS lane 的 generation 上下文。
        """

        try:
            if self._supervisor.is_context_active(context):
                if context.tts_leading_gate is not None:
                    context.tts_leading_gate.finish()
                if self._supervisor.put_event_if_current(
                    context,
                    "assistant.audio_finished",
                    {
                        "session_id": context.source_session_id,
                        "generation_id": context.generation_id,
                        "request_id": context.request.request_id,
                        "source": context.request.source,
                    },
                ):
                    self.debug_recorder.record_generation_finished(context)
        finally:
            self._supervisor.mark_lane_finished(context, lane="tts")

    def _flush_sentence_buffer(
        self,
        context: GenerationContext,
        sentence_buffer: StreamingSentenceBuffer,
        tag_sanitizer: StreamVoxTagSanitizer,
        streamvox_profile: StreamVoxModelProfile,
    ) -> None:
        """把句子缓冲里剩余的尾句补投给 TTS worker。

        Args:
            context: 尾句所属 generation 上下文。
            sentence_buffer: 尚未形成完整句界的文本缓冲。
            tag_sanitizer: 当前语音模型对应的标签清洗器。
            streamvox_profile: generation 开始时冻结的模型画像。
        """

        if not self._supervisor.is_context_active(context):
            return

        # flush 会连同内部缓冲一起清空；短尾合并策略可能一次返回多个收尾句段。
        for sentence in sentence_buffer.flush():
            self._queue_tts_sentence(
                context,
                sentence,
                tag_sanitizer,
                streamvox_profile,
                source="flush",
            )

    def _queue_tts_sentence(
        self,
        context: GenerationContext,
        sentence: str,
        tag_sanitizer: StreamVoxTagSanitizer,
        streamvox_profile: StreamVoxModelProfile,
        *,
        source: str,
    ) -> None:
        """清洗 TTS 句段后投递给 TTS worker。

        Args:
            context: 句段所属 generation 上下文。
            sentence: 等待合成的原始文本句段。
            tag_sanitizer: 当前语音模型对应的标签清洗器。
            streamvox_profile: generation 开始时冻结的模型画像。
            source: 句段来源，用于运行时观测。
        """

        sanitized_sentence = tag_sanitizer.sanitize_sentence(sentence)
        speech_sentence = replace_http_urls_for_speech(sanitized_sentence)
        normalized_tts_sentence = normalize_tts_text(
            speech_sentence,
            force_remaining_digits=streamvox_profile.force_remaining_digits,
        )
        if not normalized_tts_sentence.strip():
            return
        speakable_sentence = project_speakable_text(sentence).strip()
        logger.info(
            "TTS句段入队 tts_source=%s generation_id=%s request_id=%s raw_tokens=%s speakable_tokens=%s "
            "sanitized_length=%s text=%r",
            source,
            context.generation_id,
            context.request.request_id,
            count_text_tokens(sentence),
            count_text_tokens(speakable_sentence) if speakable_sentence else 0,
            len(normalized_tts_sentence),
            normalized_tts_sentence,
        )
        self._tts_queue.put(
            TtsSegmentWorkItem(
                context=context,
                text=normalized_tts_sentence,
                streamvox_model=streamvox_profile.model,
            )
        )

    def _publish_fallback_chunk(
        self,
        context: GenerationContext,
        *,
        text: str,
        tag_sanitizer: StreamVoxTagSanitizer,
        streamvox_profile: StreamVoxModelProfile,
    ) -> bool:
        """把确定性 fallback 作为真实正文分片公开并立即送入 TTS。

        Args:
            context: fallback 所属 generation。
            text: 允许直接展示和播报的安全正文。
            tag_sanitizer: 当前语音模型的标签清洗器。
            streamvox_profile: generation 开始时冻结的语音模型画像。

        Returns:
            文本仍属于当前 generation 并成功进入公共事件队列时返回真。
        """

        fallback_event = ForegroundEvent(
            type="assistant.text_chunk",
            payload={"text": text},
        )
        if not self._supervisor.put_visible_text_event_if_current(
            context,
            fallback_event,
            text=text,
        ):
            return False
        self.debug_recorder.record_llm_text_chunk(context, text=text)
        self._queue_tts_sentence(
            context,
            text,
            tag_sanitizer,
            streamvox_profile,
            source="runtime_fallback",
        )
        return True

    def _publish_recovery_fallback(
        self,
        context: GenerationContext,
        *,
        tag_sanitizer: StreamVoxTagSanitizer,
        streamvox_profile: StreamVoxModelProfile,
    ) -> bool:
        """把 deadline 或运行时请求转换成一次正常 Recovery 正文结算。"""

        reflection_context = context.request.self_reflection_context
        if reflection_context is None or context.response_committed:
            return False
        fallback_text = reflection_context.fallback_text
        fallback_event = self.agent.create_runtime_fallback_event(
            f"{''.join(context.visible_parts)}{fallback_text}",
            request_id=context.request.request_id,
            history_policy=context.request.history_policy,
            mark_notice_delivery_failed=False,
        )
        if not self._publish_fallback_chunk(
            context,
            text=fallback_text,
            tag_sanitizer=tag_sanitizer,
            streamvox_profile=streamvox_profile,
        ):
            return False
        if not self._supervisor.put_foreground_event_if_current(
            context,
            fallback_event,
        ):
            return False
        if fallback_event.commit_action is not None:
            fallback_event.commit_action.run_after_commit()
        return True

    def _create_streamvox_tag_sanitizer(self) -> StreamVoxTagSanitizer:
        """创建当前 TTS provider 模型对应的 StreamVox 标签清洗器。

        Returns:
            使用当前模型标签集合的流式清洗器。
        """

        return create_streamvox_tag_sanitizer(self._current_streamvox_model())

    def _current_streamvox_model(self) -> str:
        """读取当前 TTS provider 的 StreamVox 模型快照。

        Returns:
            当前用于语音合成的模型名称。
        """

        provider_config = getattr(self.tts_provider, "config", None)
        model = getattr(provider_config, "model", "")
        if not isinstance(model, str):
            return ""
        return model

    def _emit_interrupted_text_final(self, context: GenerationContext) -> None:
        """在 generation 取消后补发中断终态，并按原因决定可见尾注。

        Args:
            context: 被用户打断的 generation 上下文。
        """

        if not context.cancellation_event.is_set():
            return

        visible_text = "".join(
            context.visible_parts[context.committed_visible_part_count:]
        ).strip()
        cancellation_reason = context.cancellation_event.reason
        if not visible_text and cancellation_reason != "self_reflection_takeover":
            return
        if cancellation_reason == "self_reflection_takeover":
            # 主动接管不是用户行为。即使正文尚未开始，也要提交一条不公开用户原文的
            # 封存事实；已有正文时则原样保留，不能伪造“用户打断”尾注。
            suffix = ""
        else:
            suffix = (
                NOTICE_SUPERSEDED_FINAL_SUFFIX
                if cancellation_reason == "activity_notice_superseded"
                else USER_INTERRUPTED_FINAL_SUFFIX
            )
        interrupted_text = f"{visible_text}{suffix}"
        interrupted_event = self.agent.create_interrupted_response_event(
            interrupted_text,
            model_text=visible_text,
            request_id=context.request.request_id,
            history_policy=context.request.history_policy,
            progress_references=context.request.activity_progress_references,
            interruption_reason=cancellation_reason,
        )
        if self._supervisor.put_interrupted_final(context, interrupted_event):
            if interrupted_event.commit_action is not None:
                interrupted_event.commit_action.run_after_commit()
            self.debug_recorder.record_llm_text_final(context, text=interrupted_text)

    def _is_llm_authentication_error(self, exc: Exception) -> bool:
        """判断 LLM 异常是否属于鉴权失败。

        Args:
            exc: LLM 调用抛出的异常。

        Returns:
            异常链包含鉴权失败时返回真。
        """

        # 先按类名兼容判断，避免测试替身或不同 SDK 包装层导致强依赖具体类型。
        exception_class_names = {exception_class.__name__ for exception_class in type(exc).__mro__}
        if "AuthenticationError" in exception_class_names:
            return True

        # 部分兼容实现不会抛专用异常，而是挂一个通用的 401 状态码。
        return getattr(exc, "status_code", None) == 401

    def _is_thread_alive(self, thread: Thread | None) -> bool:
        """判断线程句柄是否仍然存活。

        Args:
            thread: 可选的运行线程句柄。

        Returns:
            线程存在且仍在运行时返回真。
        """

        return thread is not None and thread.is_alive()


def _output_settlement_outcome(
    context: GenerationContext,
    *,
    llm_failed: bool,
) -> AssistantOutputSettlementOutcome:
    """根据 generation 的最终事实生成稳定结算分类。

    Args:
        context: 已经退出 LLM lane 的 generation 上下文。
        llm_failed: LLM worker 是否捕获了未恢复异常。

    Returns:
        可写入 `assistant.output_settled` 的稳定结果。
    """

    cancellation_reason = context.cancellation_event.reason
    if cancellation_reason == "activity_notice_superseded":
        return "superseded"
    if llm_failed or cancellation_reason == "llm_error":
        return "failed"
    if context.cancellation_event.is_set():
        return "interrupted"
    if context.response_committed:
        return "completed"
    return "failed"
