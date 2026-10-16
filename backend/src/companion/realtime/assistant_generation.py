from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Literal, TypeAlias
from uuid import uuid4

from companion.audio.leading_silence_gate import LeadingSilenceGate
from companion.realtime.assistant_events import AssistantRequest
from companion.realtime.assistant_output_contracts import AssistantOutputSettlementOutcome
from companion.realtime.cancellation import CancellationToken


# REQUEST_QUEUE_STOP 是 LLM worker 的关闭哨兵，避免用 None 混淆合法工作项。
REQUEST_QUEUE_STOP = object()
# TTS_QUEUE_STOP 是 TTS worker 的关闭哨兵，退出时用于唤醒可能阻塞在队列上的常驻线程。
TTS_QUEUE_STOP = object()


GenerationLane: TypeAlias = Literal["primary", "auxiliary"]


@dataclass
class GenerationContext:
    """单次助手回复 generation 的运行时事实。"""

    # request 保留完整用户输入事实，LLM worker 只从上下文读取，不再额外传递散落参数。
    request: AssistantRequest
    # 来源会话会在公共事件投影边界映射为 session_id，供控制面按输入来源过滤。
    source_session_id: str
    # generation_id 是取消、过期事件过滤和音频播放代际判断的唯一标识。
    generation_id: str
    # lane 决定新请求只能替换哪一类输出，用户插话不能越过这条取消边界。
    lane: GenerationLane = "primary"
    # delivery_id 只绑定本 generation 的语音交付，不把持续 LiveKit 音轨误当成全局播放状态。
    delivery_id: str = field(default_factory=lambda: str(uuid4()))
    # cancellation_event 是协作式取消信号，LLM/TTS 两条 worker 都必须在关键输出点检查。
    cancellation_event: CancellationToken = field(default_factory=CancellationToken)
    # model_cancellation_event 只停止模型决策；Self-Reflection deadline 兜底时不能
    # 用主取消域，否则已经准备好的 fallback 无法进入正文和 TTS settlement。
    model_cancellation_event: CancellationToken = field(default_factory=CancellationToken)
    # completion_event 在 LLM/TTS 都结束后设置，供 join_current 等待当前 generation 收束。
    completion_event: Event = field(default_factory=Event)
    # visible_parts 保留已经释放给前端的文本 chunk，便于调试或后续扩展失败恢复。
    visible_parts: list[str] = field(default_factory=list)
    # committed_visible_part_count 之前的分片已经通过 segment 事件成为独立消息，中断时不能重复提交。
    committed_visible_part_count: int = 0
    # TTS 首响门控按 generation 共享，确保只处理整轮语音最开头的长静音。
    tts_leading_gate: LeadingSilenceGate | None = None
    # llm_completed 表示当前 generation 的 LLM 消费已经结束。
    llm_completed: bool = False
    # tts_completed 表示当前 generation 的 TTS 队列已经收到结束信号并完成消费。
    tts_completed: bool = False
    # tts_gate_released 标记 TTS 首响门控已经释放过首个可播音频，避免重复打 latency marker。
    tts_gate_released: bool = False
    # response_committed 表示该 generation 的最终历史和 final 事件已经完成唯一提交。
    response_committed: bool = False
    # output_settled 表示该 generation 已经发出唯一主进程可见输出结算事件。
    output_settled: bool = False
    # LLM 先记录最终结果，等 TTS 真正排空后再对主进程发布唯一结算。
    pending_output_outcome: AssistantOutputSettlementOutcome | None = None
    # deadline 兜底请求在模型流返回后由输出 lane 公开一次确定性正文。
    forced_fallback_requested: bool = False
    forced_fallback_reason: str = ""


@dataclass(frozen=True)
class AssistantRequestWorkItem:
    """LLM worker 的请求工作项。"""

    # context 是该工作项的唯一业务负载，避免 request 和 generation_id 分离后出现错配。
    context: GenerationContext


@dataclass(frozen=True)
class TtsSegmentWorkItem:
    """TTS worker 的句段合成工作项。"""

    # context 用于检查取消和补充事件 payload。
    context: GenerationContext
    # text 是已经完成切句的可播文本，不应为空白。
    text: str
    # streamvox_model 是 LLM lane 规范化文本时冻结的完整模型名，用于拒绝切换后的过期句段。
    streamvox_model: str


@dataclass(frozen=True)
class TtsGenerationFinished:
    """TTS worker 的 generation 结束工作项。"""

    # context 是要完成的 generation，上下文内的取消状态决定是否允许发自然完成事件。
    context: GenerationContext
