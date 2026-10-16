from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from queue import Queue
from threading import Lock

from companion.llm.foreground_models import ForegroundEvent
from companion.observability.logging_config import get_backend_logger
from companion.records.debug import InteractionDebugRecorderProtocol, NoopInteractionDebugRecorder
from companion.realtime.assistant_events import AssistantRequest, RuntimeEvent
from companion.realtime.assistant_output_contracts import (
    AssistantOutputSettlementOutcome,
    progress_references_payload,
)
from companion.realtime.assistant_generation import (
    AssistantRequestWorkItem,
    GenerationContext,
    GenerationLane,
    TtsGenerationFinished,
)
from companion.realtime.runtime_identity import RuntimeIdentityFactory


logger = get_backend_logger(__name__)


class GenerationSupervisor:
    """统一管理助手 generation 的创建、取消和事件有效性。

    Args:
        request_queue: 主输出域 LLM lane 消费的工作队列。
        auxiliary_request_queue: 辅助输出域 LLM lane 消费的工作队列。
        tts_queue: TTS lane 消费的工作队列。
        event_queue: 输出给 assistant runtime bridge 的事件队列。
        now: 单调时钟函数。
        event_notifier: 有新事件时唤醒外层循环的可选回调。
        debug_recorder: 记录 generation 事件轨迹的调试边界。
    """

    def __init__(
        self,
        *,
        request_queue: Queue[object],
        auxiliary_request_queue: Queue[object],
        tts_queue: Queue[object],
        event_queue: Queue[RuntimeEvent],
        now: Callable[[], float],
        event_notifier: Callable[[], None] | None = None,
        debug_recorder: InteractionDebugRecorderProtocol | None = None,
        runtime_identity_factory: RuntimeIdentityFactory | None = None,
    ) -> None:
        self.request_queue = request_queue
        self.auxiliary_request_queue = auxiliary_request_queue
        self.tts_queue = tts_queue
        self.event_queue = event_queue
        self.now = now
        self.event_notifier = event_notifier
        self.debug_recorder = debug_recorder or NoopInteractionDebugRecorder()
        self.runtime_identity_factory = runtime_identity_factory or RuntimeIdentityFactory()
        self._lock = Lock()
        self._current_by_lane: dict[GenerationLane, GenerationContext | None] = {
            "primary": None,
            "auxiliary": None,
        }
        # current 只表示每条 lane 的最新 generation；live 还保留已取消但尚未排空的旧辅助回应。
        self._live_contexts: dict[str, GenerationContext] = {}
        self._reply_admission_by_episode: dict[str, bool] = {}
        self._pending_recovery_fallbacks: dict[str, str] = {}
        # 控制命令可能先于排队请求到达；短期取消记录阻止旧请求随后重新开始生成。
        self._cancelled_request_ids: OrderedDict[str, str] = OrderedDict()

    def start_generation(self, request: AssistantRequest) -> str:
        """在请求所属输出域启动 generation，并只取消同域旧 generation。

        Args:
            request: 最新用户请求事实。

        Returns:
            新 generation 标识。
        """

        source_session_id = request.source_session_id.strip()
        if not source_session_id:
            raise ValueError("source_session_id 必须是非空字符串")

        lane: GenerationLane = (
            "auxiliary"
            if request.source == "self_reflection_interruption_reply"
            else "primary"
        )
        reflection_context = getattr(request, "self_reflection_context", None)
        episode_id = getattr(reflection_context, "episode_id", "")
        self_reflection_id = getattr(reflection_context, "self_reflection_id", "")
        with self._lock:
            generation_id = self.runtime_identity_factory.new("generation")
            cancellation_reason = self._cancelled_request_ids.pop(request.request_id, None)
            if cancellation_reason == "followup_toggle" and request.source == "self_reflection_recovery":
                cancellation_reason = None
            pending_fallback_reason = (
                self._pending_recovery_fallbacks.pop(self_reflection_id, "")
                if request.source == "self_reflection_recovery"
                else ""
            )
            context = GenerationContext(
                request=request,
                source_session_id=source_session_id,
                generation_id=generation_id,
                lane=lane,
            )
            if pending_fallback_reason:
                context.forced_fallback_requested = True
                context.forced_fallback_reason = pending_fallback_reason
            self._live_contexts[generation_id] = context
            if request.source == "self_reflection_recovery":
                # 每次 Recovery 只拥有一个 episode；新 Recovery 会让旧准入事实失效。
                self._reply_admission_by_episode = {episode_id: True}
            admission_denied = (
                lane == "auxiliary"
                and not self._reply_admission_by_episode.get(episode_id, False)
            )
            old_context = (
                None if admission_denied or cancellation_reason else self._current_by_lane[lane]
            )
            if not admission_denied and not cancellation_reason:
                self._current_by_lane[lane] = context

        # 新 context 已经成为唯一有效代际，旧流关闭即使回调 supervisor 也不会与状态锁互相等待。
        if old_context is not None:
            self._cancel_context(
                old_context,
                reason=(
                    request.generation_preemption_reason
                    or "new_request"
                ),
            )

        self.debug_recorder.record_generation_started(context)
        if pending_fallback_reason:
            context.model_cancellation_event.cancel(pending_fallback_reason)
        logger.info(
            "开始为请求生成回复 request_id=%s source_turn_id=%s generation_id=%s text_length=%s",
            request.request_id,
            request.source_turn_id,
            generation_id,
            len(request.user_text),
        )
        self._put_internal_event(
            "assistant.generation_started",
            {
                "request_id": request.request_id,
                "source": request.source,
                "source_turn_id": request.source_turn_id,
                "generation_id": generation_id,
                "generation_lane": lane,
            },
        )
        target_queue = (
            self.auxiliary_request_queue
            if lane == "auxiliary"
            else self.request_queue
        )
        if admission_denied:
            # 竞态中迟到的辅助请求仍需要完整 settlement，不能悄悄消失或重新打开准入。
            self._cancel_context(
                context,
                reason="self_reflection_reply_admission_closed",
            )
        elif cancellation_reason:
            # 仍交给 lane 完成可见输出 settlement，但取消令牌阻止模型和 TTS 执行。
            self._cancel_context(context, reason=cancellation_reason)
        target_queue.put(AssistantRequestWorkItem(context=context))
        return generation_id

    def mark_request_superseded(self, request: AssistantRequest) -> None:
        """记录同一轮 drain 中被更新请求覆盖的旧请求。

        Args:
            request: 尚未启动就被更新请求覆盖的旧请求。
        """

        logger.info(
            "助手请求被同轮最新请求覆盖 request_id=%s source_turn_id=%s text_length=%s",
            request.request_id,
            request.source_turn_id,
            len(request.user_text),
        )
        self._put_internal_event(
            "assistant.generation_superseded",
            {
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
            },
        )
        self._put_internal_event(
            "assistant.output_settled",
            {
                "settlement_id": f"output-settlement:{request.request_id}",
                "request_id": request.request_id,
                "generation_id": None,
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
                "outcome": "superseded",
            },
        )

    def cancel_current(
        self, *, reason: str, generation_id: str | None = None, request_id: str | None = None
    ) -> None:
        """取消当前有效 generation。

        Args:
            reason: 取消 generation 的业务原因。
            generation_id: 仅在当前代际匹配时取消；为空时取消当前代际。
            request_id: 仅取消该请求；即使请求尚未开始，也保存有界取消记录。

        Returns:
            None: 匹配的生成收到取消，其他请求继续运行。
        """

        with self._lock:
            if request_id is not None:
                self._cancelled_request_ids[request_id] = reason
                self._cancelled_request_ids.move_to_end(request_id)
                while len(self._cancelled_request_ids) > 256:
                    self._cancelled_request_ids.popitem(last=False)
            if generation_id is None and request_id is None:
                context = self._current_by_lane["primary"]
            else:
                context = next(
                    (
                        candidate
                        for candidate in self._current_by_lane.values()
                        if candidate is not None
                        and (generation_id is None or candidate.generation_id == generation_id)
                        and (request_id is None or candidate.request.request_id == request_id)
                    ),
                    None,
                )
            if context is None or context.cancellation_event.is_set():
                return
            # 快捷键表达的是立刻闭嘴；Recovery 按 ADR 0054 继续完成推理和工具，播放由主进程过滤。
            if reason == "followup_toggle" and context.request.source == "self_reflection_recovery":
                return
            self._current_by_lane[context.lane] = None

        self._cancel_context(context, reason=reason)

    def request_recovery_fallback(
        self,
        *,
        self_reflection_id: str,
        reason: str,
    ) -> bool:
        """停止 Recovery 的模型决策，但保留主输出域生成 fallback。

        Args:
            self_reflection_id: 需要兜底的 Self-Reflection 身份。
            reason: 触发兜底的稳定原因。

        Returns:
            请求已经记录或成功作用于当前 Recovery 时返回真；Recovery 已经结算时返回假。
        """

        normalized_id = self_reflection_id.strip()
        normalized_reason = reason.strip()
        if not normalized_id or not normalized_reason:
            return False
        with self._lock:
            context = self._current_by_lane["primary"]
            reflection_context = (
                getattr(context.request, "self_reflection_context", None)
                if context is not None
                else None
            )
            if (
                context is None
                or context.request.source != "self_reflection_recovery"
                or reflection_context is None
                or reflection_context.self_reflection_id != normalized_id
            ):
                self._pending_recovery_fallbacks[normalized_id] = normalized_reason
                return True
            if context.completion_event.is_set() or context.response_committed:
                return False
            context.forced_fallback_requested = True
            context.forced_fallback_reason = normalized_reason
        # 该信号只关闭模型流；主 cancellation_event 保持开放，保证 fallback 能正常
        # 进入文本、TTS 和 VisibleOutputSettlement。
        context.model_cancellation_event.cancel(normalized_reason)
        return True

    def cancel_all(self, *, reason: str) -> None:
        """取消两个输出域仍在运行的 generation。

        Args:
            reason: 关闭或热替换触发的统一取消原因。
        """

        with self._lock:
            contexts = [
                context
                for context in self._live_contexts.values()
                if not context.cancellation_event.is_set()
            ]
            self._current_by_lane = {
                "primary": None,
                "auxiliary": None,
            }

        for context in contexts:
            self._cancel_context(context, reason=reason)

    def cancel_superseded_system_notice(self, *, request_id: str, reason: str) -> bool:
        """按请求编号取消仍在运行的独立 System Notice。

        Args:
            request_id: 需要严格匹配的独立 System Notice 请求编号。
            reason: 触发事实失效取消的业务原因。

        Returns:
            当前请求编号和来源都匹配且成功取消时返回真。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return False
        with self._lock:
            context = self._current_by_lane["primary"]
            if context is None or context.cancellation_event.is_set():
                return False
            if (
                context.request.request_id != normalized_request_id
                or context.request.source != "system_notice"
                or context.response_committed
            ):
                return False
            self._current_by_lane["primary"] = None

        self._cancel_context(context, reason=reason)
        return True

    def cancel_timed_reminder_notice(self, *, request_id: str, reason: str) -> bool:
        """按请求身份取消一条 Reminder Notice，即使正文已经形成 Final。

        Args:
            request_id: 需要严格匹配的 Reminder Notice 请求编号。
            reason: 用户打断或关闭提醒产生的取消原因。

        Returns:
            当前主输出正属于该 Reminder Notice 且已触发取消时返回真。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return False
        with self._lock:
            context = self._current_by_lane["primary"]
            if context is None or context.cancellation_event.is_set():
                return False
            if (
                context.request.request_id != normalized_request_id
                or context.request.source != "timed_reminder_notice"
            ):
                return False
            self._current_by_lane["primary"] = None

        self._cancel_context(context, reason=reason)
        return True

    def current_context(
        self,
        lane: GenerationLane = "primary",
    ) -> GenerationContext | None:
        """返回当前有效 generation 上下文。

        Args:
            lane: 需要读取的输出域。

        Returns:
            当前 generation 上下文；尚未启动请求时返回空。
        """

        with self._lock:
            return self._current_by_lane[lane]

    def is_current_context(self, context: GenerationContext) -> bool:
        """判断上下文是否仍是当前 generation。

        Args:
            context: 需要校验代际身份的上下文。

        Returns:
            上下文仍是当前代际时返回真。
        """

        with self._lock:
            return self._current_by_lane[context.lane] is context

    def is_context_active(self, context: GenerationContext) -> bool:
        """判断上下文是否仍允许产出事件或触发模型调用。

        Args:
            context: 需要校验运行状态的 generation 上下文。

        Returns:
            上下文属于当前代际且未被取消时返回真。
        """

        return not context.cancellation_event.is_set() and self.is_current_context(context)

    def is_model_stream_active(self, context: GenerationContext) -> bool:
        """判断模型流是否仍可提交新分片。"""

        return (
            not context.cancellation_event.is_set()
            and not context.model_cancellation_event.is_set()
            and self.is_current_context(context)
        )

    def recovery_fallback_requested(self, context: GenerationContext) -> bool:
        """判断当前 Recovery 是否已经收到一次确定性 fallback 请求。"""

        with self._lock:
            return (
                context.forced_fallback_requested
                and self._current_by_lane["primary"] is context
                and not context.response_committed
            )

    def _enrich_assistant_payload(
        self,
        context: GenerationContext,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """把当前请求的完整身份补到助手事件载荷中。

        Args:
            context: 事件所属 generation 上下文。
            payload: 业务层提供的原始载荷。

        Returns:
            带有请求、generation、历史来源和 Self-Reflection 身份的载荷副本。
        """

        normalized_payload = dict(payload)
        normalized_payload.setdefault("session_id", context.source_session_id)
        normalized_payload.setdefault("generation_id", context.generation_id)
        normalized_payload.setdefault("request_id", context.request.request_id)
        normalized_payload.setdefault("source", context.request.source)
        normalized_payload.setdefault(
            "history_policy",
            context.request.history_policy,
        )
        normalized_payload.setdefault(
            "activity_progress_references",
            progress_references_payload(
                context.request.activity_progress_references
            ),
        )
        _attach_request_derivation_metadata(
            normalized_payload,
            context.request.derived_from_activity_ids,
        )

        self_reflection_context = getattr(
            context.request,
            "self_reflection_context",
            None,
        )
        if self_reflection_context is not None:
            normalized_payload.setdefault(
                "self_reflection_id",
                self_reflection_context.self_reflection_id,
            )
            normalized_payload.setdefault(
                "episode_id",
                self_reflection_context.episode_id,
            )
            normalized_payload.setdefault(
                "runtime_epoch",
                self_reflection_context.runtime_epoch,
            )
            trigger_input_identity = getattr(
                self_reflection_context,
                "trigger_input_identity",
                None,
            )
            if trigger_input_identity is not None:
                normalized_payload.setdefault(
                    "trigger_input_identity",
                    trigger_input_identity,
                )
        return normalized_payload

    def put_event_if_current(self, context: GenerationContext, event_type: str, payload: dict) -> bool:
        """为当前 generation 的运行时事件补齐归属字段并投递。

        Args:
            context: 事件所属的 generation 上下文。
            event_type: 待投递的运行时事件类型。
            payload: 来源层提供的事件载荷。

        Returns:
            上下文仍有效且事件成功入队时返回真。
        """

        if event_type == "assistant.text_final":
            return self.put_foreground_event_if_current(
                context,
                ForegroundEvent(type=event_type, payload=payload),
            )

        if not self.is_context_active(context):
            self.log_stale_event(context, event_type)
            return False

        normalized_payload = dict(payload)
        if event_type.startswith("assistant."):
            normalized_payload = self._enrich_assistant_payload(
                context,
                normalized_payload,
            )
            if event_type in {
                "assistant.audio_chunk",
                "assistant.audio_finished",
                "assistant.tts_segment_failed",
            }:
                delivery_id = getattr(context, "delivery_id", None)
                if isinstance(delivery_id, str) and delivery_id:
                    normalized_payload.setdefault("delivery_id", delivery_id)
        if event_type == "llm.foreground.tool_call_completed":
            normalized_payload.setdefault("request_id", context.request.request_id)
        if event_type == "assistant.text_final" and context.request.turn_material_context_id is not None:
            normalized_payload.setdefault(
                "turn_material_context_id",
                context.request.turn_material_context_id,
            )
        self._put_event(context, event_type, normalized_payload)
        return True

    def put_foreground_event_if_current(self, context: GenerationContext, foreground_event: ForegroundEvent) -> bool:
        """仅当上下文仍有效时投递前台模型产生的事件。

        Args:
            context: 事件所属的 generation 上下文。
            foreground_event: foreground 模型产生的事件。

        Returns:
            上下文仍有效且事件成功入队时返回真。
        """

        # 某些 foreground 事件会顺带携带一个“提交后才允许生效”的副作用，
        # 比如真正启动一个后台任务；supervisor 就是这类副作用的最终把关口。
        commit_action = foreground_event.commit_action
        with self._lock:
            # final 事件只允许唯一提交一次；同一轮如果已经交过 final，后来的重复 final 必须丢弃。
            is_duplicate_final = (
                foreground_event.type == "assistant.text_final"
                and getattr(context, "response_committed", False)
            )
            # 只要当前 generation 已取消、已经被新代际顶掉，或者 final 重复提交，
            # 这条事件就算“没被当前上下文接收”，后面的副作用也不能放行。
            if (
                context.cancellation_event.is_set()
                or self._current_by_lane[context.lane] is not context
                or is_duplicate_final
            ):
                accepted = False
            else:
                # 事务先进入 accepted，保留事件入队失败时释放 prepared 资源的补偿能力。
                action_accepted = commit_action is None or commit_action.accept()
                if not action_accepted:
                    accepted = False
                else:
                    # 只有通过代际校验和预提交的事件才会对外投递；queue.put 成功后才越过提交点。
                    payload = self._normalize_event_payload(
                        context,
                        foreground_event.type,
                        foreground_event.payload,
                    )
                    try:
                        self._put_event(context, foreground_event.type, payload)
                    except BaseException:
                        if commit_action is not None:
                            commit_action.rollback()
                        raise
                    if commit_action is not None and not commit_action.commit_enqueued():
                        raise RuntimeError("前台事件已入队但事务未能提交")
                    if foreground_event.type == "assistant.text_final":
                        context.response_committed = True
                    elif foreground_event.type == "assistant.text_segment_final":
                        context.committed_visible_part_count = len(context.visible_parts)
                    accepted = True

        if not accepted:
            # 外层没有接住这条事件时，要把尚未生效的副作用一并回滚，避免留下 prepared 脏占位。
            if commit_action is not None:
                commit_action.rollback()
            self.log_stale_event(context, foreground_event.type)
        elif commit_action is not None:
            logger.info(
                "本轮回复已确认，提交事务进入 accepted 状态 request_id=%s generation_id=%s transaction_id=%s "
                "transaction_state=%s event_type=%s",
                context.request.request_id,
                context.generation_id,
                commit_action.transaction_id,
                commit_action.state,
                foreground_event.type,
            )
        return accepted

    def put_interrupted_final(
        self,
        context: GenerationContext,
        foreground_event: ForegroundEvent,
    ) -> bool:
        """原子提交已经实际展示的中断片段和 final 事件。

        Args:
            context: 被取消但已经释放过可见文本的 generation。
            foreground_event: 带中断历史提交动作的最终事件。

        Returns:
            本次调用首次提交中断 final 时返回真。
        """

        commit_action = foreground_event.commit_action
        with self._lock:
            if (
                not context.cancellation_event.is_set()
                or getattr(context, "response_committed", False)
            ):
                accepted = False
            else:
                action_accepted = commit_action is None or commit_action.accept()
                if not action_accepted:
                    accepted = False
                else:
                    payload = self._normalize_event_payload(
                        context,
                        foreground_event.type,
                        foreground_event.payload,
                    )
                    try:
                        self._put_event(context, foreground_event.type, payload)
                    except BaseException:
                        if commit_action is not None:
                            commit_action.rollback()
                        raise
                    if commit_action is not None and not commit_action.commit_enqueued():
                        raise RuntimeError("中断 final 已入队但事务未能提交")
                    context.response_committed = True
                    accepted = True

        if not accepted and commit_action is not None:
            commit_action.rollback()
        elif accepted and commit_action is not None:
            logger.info(
                "中断回复已确认，提交事务进入 accepted 状态 request_id=%s generation_id=%s transaction_id=%s "
                "transaction_state=%s",
                context.request.request_id,
                context.generation_id,
                commit_action.transaction_id,
                commit_action.state,
            )
        return accepted

    def _normalize_event_payload(
        self,
        context: GenerationContext,
        event_type: str,
        payload: dict,
    ) -> dict:
        """补齐绑定当前 generation 的标准事件字段。

        Args:
            context: 事件所属 generation。
            event_type: 标准事件类型。
            payload: 业务层原始载荷。

        Returns:
            可直接投递给运行时队列的载荷副本。
        """

        normalized_payload = dict(payload)
        if event_type.startswith("assistant."):
            normalized_payload = self._enrich_assistant_payload(
                context,
                normalized_payload,
            )
        if event_type == "llm.foreground.tool_call_completed":
            normalized_payload.setdefault("request_id", context.request.request_id)
        if event_type == "assistant.text_final" and context.request.turn_material_context_id is not None:
            normalized_payload.setdefault(
                "turn_material_context_id",
                context.request.turn_material_context_id,
            )
        return normalized_payload

    def put_visible_text_event_if_current(
        self,
        context: GenerationContext,
        foreground_event: ForegroundEvent,
        *,
        text: str,
    ) -> bool:
        """原子登记可见文本并投递对应前台事件。

        Args:
            context: 文本所属的 generation 上下文。
            foreground_event: 即将交给控制面的文本增量事件。
            text: 已完成清洗且真实可见的文本。

        Returns:
            上下文仍有效且文本事件成功入队时返回真。
        """

        if not text:
            return False
        first_recovery_chunk = (
            context.request.source == "self_reflection_recovery"
            and not context.visible_parts
        )
        if first_recovery_chunk:
            return self._put_first_recovery_text_after_auxiliary_drain(
                context,
                foreground_event,
                text=text,
            )
        with self._lock:
            if (
                context.cancellation_event.is_set()
                or self._current_by_lane[context.lane] is not context
            ):
                return False
            context.visible_parts.append(text)
            payload = self._visible_text_payload(context, foreground_event)
            self._put_event(context, foreground_event.type, payload)
            return True

    def settle_output(
        self,
        context: GenerationContext,
        *,
        outcome: AssistantOutputSettlementOutcome,
    ) -> bool:
        """为一个 generation 发出一次且仅一次可见输出结算。

        Args:
            context: 已经停止产生 LLM 正文的 generation 上下文。
            outcome: 本次输出停止的稳定原因分类。

        Returns:
            本次调用首次发出结算事件时返回真。
        """

        with self._lock:
            if context.output_settled:
                return False
            context.output_settled = True
            payload = {
                "settlement_id": (
                    f"output-settlement:{context.request.request_id}:"
                    f"{context.generation_id}"
                ),
                "request_id": context.request.request_id,
                "generation_id": context.generation_id,
                "source": context.request.source,
                "source_turn_id": context.request.source_turn_id,
                "turn_material_context_id": (
                    context.request.turn_material_context_id
                ),
                "history_policy": context.request.history_policy,
                "self_reflection_id": (
                    getattr(getattr(context.request, "self_reflection_context", None), "self_reflection_id", None)
                    if getattr(context.request, "self_reflection_context", None) is not None
                    else None
                ),
                "episode_id": (
                    getattr(getattr(context.request, "self_reflection_context", None), "episode_id", None)
                    if getattr(context.request, "self_reflection_context", None) is not None
                    else None
                ),
                "runtime_epoch": (
                    getattr(getattr(context.request, "self_reflection_context", None), "runtime_epoch", None)
                    if getattr(context.request, "self_reflection_context", None) is not None
                    else None
                ),
                "trigger_input_identity": (
                    getattr(getattr(context.request, "self_reflection_context", None), "trigger_input_identity", None)
                    if getattr(context.request, "self_reflection_context", None) is not None
                    else None
                ),
                "activity_progress_references": progress_references_payload(
                    context.request.activity_progress_references
                ),
                "derived_from_activity_ids": list(
                    context.request.derived_from_activity_ids
                ),
                "outcome": outcome,
            }
            self._put_event(context, "assistant.output_settled", payload)
            return True

    def mark_lane_finished(self, context: GenerationContext, *, lane: str) -> None:
        """标记 generation 的某条 lane 已结束，并在双 lane 排空后结算。

        Args:
            context: lane 所属 generation 上下文。
            lane: 已结束的运行 lane 名称。
        """

        with self._lock:
            if lane == "llm":
                context.llm_completed = True
            elif lane == "tts":
                context.tts_completed = True
            else:
                raise ValueError(f"unknown assistant lane: {lane}")

            generation_finished = (
                context.llm_completed
                and context.tts_completed
                and not context.completion_event.is_set()
            )

        if not generation_finished:
            return

        # 输出结算必须晚于文本和音频两条 lane。先把 settlement 放入事件队列，
        # 再唤醒等待首段 Recovery 正文的交接方，保证它看到的是完整收口事实。
        outcome = context.pending_output_outcome or (
            "interrupted"
            if context.cancellation_event.is_set()
            else "completed"
            if context.response_committed
            else "failed"
        )
        self.settle_output(context, outcome=outcome)
        with self._lock:
            context.completion_event.set()
            self._live_contexts.pop(context.generation_id, None)
            if self._current_by_lane[context.lane] is context:
                self._current_by_lane[context.lane] = None

    def _put_first_recovery_text_after_auxiliary_drain(
        self,
        context: GenerationContext,
        foreground_event: ForegroundEvent,
        *,
        text: str,
    ) -> bool:
        """排空辅助域后在线性化临界区公开首段 Recovery 正文。

        Args:
            context: 即将公开首段正文的 Recovery generation。
            foreground_event: 即将公开的首段正文事件。
            text: 已完成清洗且真实可见的首段正文。

        Returns:
            Recovery 仍有效且首段正文成功入队时返回真，否则返回假。
        """

        reflection_context = context.request.self_reflection_context
        episode_id = reflection_context.episode_id
        with self._lock:
            if (
                context.cancellation_event.is_set()
                or self._current_by_lane["primary"] is not context
            ):
                return False
            self._reply_admission_by_episode[episode_id] = False

        while True:
            with self._lock:
                if (
                    context.cancellation_event.is_set()
                    or self._current_by_lane["primary"] is not context
                ):
                    return False
                auxiliary_contexts = tuple(
                    candidate
                    for candidate in self._live_contexts.values()
                    if candidate.lane == "auxiliary"
                    and getattr(
                        candidate.request.self_reflection_context,
                        "episode_id",
                        None,
                    )
                    == episode_id
                    and not candidate.completion_event.is_set()
                )
                if not auxiliary_contexts:
                    # 这把锁同时保护辅助 generation 创建和首正文入队。稳定空集一旦成立，
                    # 新辅助请求只能在正文事件之后观察到关闭准入，不再存在交错窗口。
                    self._put_internal_event(
                        "foreground.self_reflection_reply_admission_closed",
                        {
                            "request_id": context.request.request_id,
                            "generation_id": context.generation_id,
                            "self_reflection_id": reflection_context.self_reflection_id,
                            "episode_id": episode_id,
                            "runtime_epoch": reflection_context.runtime_epoch,
                            "revision": 2,
                        },
                    )
                    self._put_event(
                        context,
                        "assistant.self_reflection_status_changed",
                        {
                            "request_id": context.request.request_id,
                            "session_id": context.source_session_id,
                            "generation_id": context.generation_id,
                            "self_reflection_id": reflection_context.self_reflection_id,
                            "episode_id": episode_id,
                            "runtime_epoch": reflection_context.runtime_epoch,
                            "revision": 2,
                            "active": False,
                        },
                    )
                    context.visible_parts.append(text)
                    self._put_event(
                        context,
                        foreground_event.type,
                        self._visible_text_payload(context, foreground_event),
                    )
                    return True
                current_auxiliary = self._current_by_lane["auxiliary"]
                if current_auxiliary in auxiliary_contexts:
                    self._current_by_lane["auxiliary"] = None

            for auxiliary_context in auxiliary_contexts:
                if not auxiliary_context.cancellation_event.is_set():
                    self._cancel_context(
                        auxiliary_context,
                        reason="self_reflection_body_started",
                    )
            for auxiliary_context in auxiliary_contexts:
                auxiliary_context.completion_event.wait()

    def _visible_text_payload(
        self,
        context: GenerationContext,
        foreground_event: ForegroundEvent,
    ) -> dict[str, object]:
        """补齐可见正文跨进程归档所需的完整请求身份。

        Args:
            context: 正文所属 generation 上下文。
            foreground_event: Foreground 处理器产生的原始正文事件。

        Returns:
            可以直接放入运行时事件队列的独立载荷。
        """

        payload = self._enrich_assistant_payload(
            context,
            foreground_event.payload,
        )
        return payload

    def log_stale_event(self, context: GenerationContext, event_type: str) -> None:
        """记录被 generation 边界丢弃的迟到事件。

        Args:
            context: 产生迟到事件的旧 generation 上下文。
            event_type: 被丢弃的运行时事件名称。
        """

        logger.info(
            "丢弃过期助手事件 event_type=%s generation_id=%s request_id=%s cancelled=%s is_current=%s",
            event_type,
            context.generation_id,
            context.request.request_id,
            context.cancellation_event.is_set(),
            self.is_current_context(context),
        )

    def _cancel_context(self, context: GenerationContext, *, reason: str) -> None:
        """在 supervisor 锁外取消指定 generation 并完成事件收口。

        Args:
            context: 需要取消的 generation 上下文。
            reason: 取消代际的业务原因。
        """

        cancel_with_reason = getattr(context.cancellation_event, "cancel", None)
        if callable(cancel_with_reason):
            cancelled = cancel_with_reason(reason) is not False
        else:
            if context.cancellation_event.is_set():
                return
            context.cancellation_event.set()
            cancelled = True
        if not cancelled:
            return

        self.debug_recorder.record_generation_cancelled(context, reason=reason)
        self.tts_queue.put(TtsGenerationFinished(context=context))
        logger.info(
            "助手generation被取消 reason=%s generation_id=%s request_id=%s",
            reason,
            context.generation_id,
            context.request.request_id,
        )
        self._put_internal_event(
            "assistant.generation_interrupted",
            {
                "generation_id": context.generation_id,
                "request_id": context.request.request_id,
                "source": context.request.source,
                "source_turn_id": context.request.source_turn_id,
                "reason": reason,
                "turn_material_context_id": context.request.turn_material_context_id,
                "history_policy": context.request.history_policy,
                "activity_progress_references": progress_references_payload(
                    context.request.activity_progress_references
                ),
                "derived_from_activity_ids": list(
                    context.request.derived_from_activity_ids
                ),
            },
        )

    def _put_internal_event(self, event_type: str, payload: dict) -> None:
        """投递 assistant runtime 内部事件。

        Args:
            event_type: 内部运行时事件名称。
            payload: 不绑定 generation 的事件载荷。
        """

        self.event_queue.put(
            RuntimeEvent(
                type=event_type,
                payload=payload,
                produced_at=self.now(),
                source="assistant_output_lanes",
            )
        )
        if self.event_notifier is not None:
            self.event_notifier()

    def _put_event(self, context: GenerationContext, event_type: str, payload: dict) -> None:
        """投递当前 generation 的标准运行时事件。

        Args:
            context: 提供请求与 generation 归属的上下文。
            event_type: 标准运行时事件名称。
            payload: 事件特有的业务载荷。
        """

        _ = context
        self.event_queue.put(
            RuntimeEvent(
                type=event_type,
                payload=payload,
                produced_at=self.now(),
                source="assistant_output_lanes",
            )
        )
        if self.event_notifier is not None:
            self.event_notifier()


def _attach_request_derivation_metadata(
    payload: dict[str, object],
    derived_from_activity_ids: tuple[str, ...],
) -> None:
    """把请求来源同时附着到输出事件和其中的 assistant 历史消息。

    Foreground 不需要知道替代账本规则，只需把主进程冻结的来源身份原样带回。
    用户消息和工具协议不依赖 Cognitive 成果，因此只给可见 assistant 消息加元数据。

    Args:
        payload: 即将进入助手运行时事件队列的载荷。
        derived_from_activity_ids: 请求冻结的 Cognitive Result Activity 编号。

    Returns:
        无；直接更新当前事件载荷。
    """

    normalized_activity_ids = list(derived_from_activity_ids)
    payload.setdefault("derived_from_activity_ids", normalized_activity_ids)
    if not normalized_activity_ids:
        return
    raw_memory_commit = payload.get("memory_commit")
    if not isinstance(raw_memory_commit, dict):
        return
    raw_messages = raw_memory_commit.get("messages")
    if not isinstance(raw_messages, (list, tuple)):
        return

    memory_commit = dict(raw_memory_commit)
    messages: list[object] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict) or raw_message.get("role") != "assistant":
            messages.append(raw_message)
            continue
        message = dict(raw_message)
        raw_metadata = message.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        metadata["derived_from_activity_ids"] = normalized_activity_ids.copy()
        message["metadata"] = metadata
        messages.append(message)
    memory_commit["messages"] = messages
    payload["memory_commit"] = memory_commit
