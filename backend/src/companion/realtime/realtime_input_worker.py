from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Literal

from companion.config.realtime_audio import RealtimeAudioYamlConfig
from companion.core.application.assistant_request_models import (
    AssistantRequestContextPreparerProtocol,
    PreparedAssistantRequestContext,
)
from companion.observability.logging_config import get_backend_logger
from companion.observability.realtime_latency import RealtimeLatencyRecorder
from companion.audio.pcm16_utils import latest_speaker_verification_pcm16
from companion.foreground_self_reflection.contracts import (
    AssistantRequestAdmissionLeaseProtocol,
    ForegroundAttentionAdmissionPort,
)
from companion.foreground_self_reflection.models import (
    DivertFinalInputCommand,
    InputDiversionResult,
    UnattendedMaterialReference,
)
from companion.foreground_self_reflection.policy import (
    input_diversion_blocks_normal_request,
    input_diversion_was_accepted,
)
from companion.records.debug import DebugAudioRecorderProtocol, NoopDebugAudioRecorder
from companion.realtime.asr_scheduler import (
    AsrErrorResult,
    AsrScheduler,
    EndpointEouResult,
    FinalAsrResult,
    PartialAsrResult,
)
from companion.realtime.assistant_events import (
    AssistantControlCommand,
    CancelCurrentGenerationCommand,
    WarmupForegroundCommand,
    AssistantRequest,
    RuntimeEvent,
)
from companion.realtime.barge_in_policy import (
    BargeInCandidate,
    SpeakerGateStatus,
    can_confirm_barge_in,
    initial_speaker_gate_status,
    speaker_gate_allows_barge_in,
)
from companion.realtime.transcript_filter import classify_final_transcript
from companion.realtime.runtime_identity import RuntimeIdentityFactory
from companion.audio.silence import is_silence_fast


logger = get_backend_logger(__name__)

# 低分需要连续出现两次才足以拒绝当前说话人，避免单次声纹抖动误伤整轮输入。
SPEAKER_LOW_REJECTION_STREAK = 2


@dataclass(frozen=True)
class AudioInputItem:
    """音频输入队列项。"""

    pcm16: bytes # 音频帧的原始 PCM16 字节
    received_at: float # 进入后端队列的单调时间，后续用于判断延迟和过期帧。
    input_frame_sequence_no: int  # LiveKit 媒体会话内全局递增的输入帧序号，用于观测乱序、丢帧或重复帧。
    input_track_generation: int  #  输入音轨代际编号，切换 track 后旧代际帧会被 worker 跳过。
    sample_rate: int  #  当前帧采样率，供音频模型和时长计算逻辑确认输入格式。
    num_channels: int  #  当前帧通道数，后续 worker 可据此决定是否需要拒绝或转换多声道音频。
    duration_ms: float  #  当前帧持续毫秒数，用于延迟统计和队列积压观测。


@dataclass(frozen=True)
class AudioControlCommand:
    """音频 worker 控制命令。

    注意：frozen dataclass 不会深冻结 payload 字典内容，调用方入队后不应继续修改载荷。"""

    # 控制命令名称，例如 track_switched，后续 worker 根据该值分派处理。
    kind: str
    # 命令参数字典，每个实例使用独立默认值；字典内容仍由调用方负责避免入队后修改。
    payload: dict[str, Any] = field(default_factory=dict)
    # 命令发出时间；默认 0.0 让测试和简单命令不必依赖真实时钟。
    issued_at: float = 0.0
    # 命令关联的输入音轨代际；None 表示该命令不绑定具体音轨代际。
    input_track_generation: int | None = None
    # 幂等命令标识；None 表示调用方暂未提供去重键。
    command_id: str | None = None


class TimedReminderInputTakeover:
    """协调媒体线程与输入 worker 的一次可撤销 Reminder takeover。

    调用方可以在 worker 尚未开始时取消命令；worker 一旦开始，就持有同一把锁直到
    输入状态清理完成。这样超时回滚不会留下迟到命令，避免 Reminder 没有提交却在
    稍后清掉用户语音。
    """

    def __init__(self) -> None:
        """初始化尚未开始的 takeover 命令。"""

        self._lock = threading.Lock()
        self._completed = threading.Event()
        self._state: Literal["pending", "running", "completed", "cancelled"] = (
            "pending"
        )
        self._interrupted = False
        self._failed = False

    def execute(self, takeover_input: Callable[[], bool]) -> bool:
        """若命令尚未取消，则原子执行输入清理。

        Args:
            takeover_input: 在输入 worker 线程判断并清理当前输入的函数。

        Returns:
            命令实际执行时返回真；已经被调用方取消时返回假。
        """

        with self._lock:
            if self._state != "pending":
                return False
            self._state = "running"
            try:
                self._interrupted = takeover_input()
            except Exception:
                self._failed = True
                logger.exception("Reminder takeover 清理输入失败")
            finally:
                self._state = "completed"
                self._completed.set()
            return True

    def wait(self, timeout: float) -> bool:
        """等待输入 owner 完成本次 takeover。

        Args:
            timeout: 最长等待秒数。

        Returns:
            命令已经完成或取消时返回真。
        """

        return self._completed.wait(timeout=timeout)

    def cancel_if_pending(self) -> bool:
        """只在 worker 尚未开始时取消命令。

        Returns:
            本次成功阻止输入清理时返回真；worker 已经开始或完成时返回假。
        """

        with self._lock:
            if self._state != "pending":
                return False
            self._state = "cancelled"
            self._completed.set()
            return True

    @property
    def interrupted(self) -> bool:
        """返回 takeover 是否真实丢弃了未完成用户语音。"""

        with self._lock:
            return (
                self._state == "completed"
                and not self._failed
                and self._interrupted
            )

    @property
    def completed_successfully(self) -> bool:
        """返回输入 owner 是否完整执行了 takeover 清理。"""

        with self._lock:
            return self._state == "completed" and not self._failed


@dataclass
class SmartTurnEndState:
    """Smart Turn 结束判定后的等待状态。

    Args:
        kind: 当前等待类型，区分结束观察期和等待用户续说。
        probability: Smart Turn 本次给出的结束概率。
        silence_ms: 进入等待状态后继续累计的静音时长。
        hint_required: 是否需要在低置信等待持续静音后提示 desktop。
        hint_visible: 是否已经向 desktop 发出低置信等待提示。
        timeout_multiplier: 绝对静音兜底倍率，文本 EOU 极低置信时用于延长等待。
        cached_final_text: endpoint EOU 阶段已经生成的 final ASR 文本；为空时仍需提交 final ASR。
        cached_final_submitted_at: endpoint EOU snapshot 的提交时间，复用 final 文本时写入调试元数据。
    """

    kind: Literal["pending_final_grace", "waiting_user_continue"]
    probability: float
    silence_ms: float = 0.0
    hint_required: bool = False
    hint_visible: bool = False
    timeout_multiplier: float = 1.0
    cached_final_text: str | None = None
    cached_final_submitted_at: float | None = None


@dataclass
class EndpointEouPendingState:
    """endpoint EOU 等待状态。

    Args:
        turn_id: 提交 endpoint EOU 时的用户 turn id。
        input_track_generation: 提交 endpoint EOU 时的输入音轨代际。
        byte_count: 提交 endpoint EOU 时的音频长度。
        submitted_at: endpoint EOU snapshot 的提交时间。
        smart_turn_probability: 触发 endpoint EOU 的 Smart Turn 概率。
        finalize_when_ready: 原始等待超时已到时，结果回来后是否立即收束。
    """

    turn_id: str | None
    input_track_generation: int
    byte_count: int
    submitted_at: float
    smart_turn_probability: float
    finalize_when_ready: bool = False


class RealtimeInputWorker:
    """实时音频 worker 的最小控制外壳。"""

    def __init__(
        self,
        *,
        realtime_input_runtime: Any,
        vad_provider: Any,
        smart_turn_provider: Any,
        audio_queue: Queue[AudioInputItem],
        control_queue: Queue[AudioControlCommand],
        event_queue: Queue[RuntimeEvent],
        assistant_request_submitter: Callable[[AssistantRequest], bool] | None,
        assistant_control_submitter: Callable[[AssistantControlCommand], bool] | None,
        now: Callable[[], float],
        latency_recorder: RealtimeLatencyRecorder | None = None,
        max_audio_lag_ms: float = 40.0,
        asr_scheduler: AsrScheduler | None = None,
        realtime_audio_config: RealtimeAudioYamlConfig,
        debug_audio_recorder: DebugAudioRecorderProtocol | None = None,
        assistant_output_enabled: bool = True,
        assistant_request_context_preparer: AssistantRequestContextPreparerProtocol | None = None,
        input_diversion_port: ForegroundAttentionAdmissionPort | None = None,
        current_runtime_epoch: Callable[[], int] | None = None,
        final_input_materials_provider: Callable[
            [str], tuple[UnattendedMaterialReference, ...]
        ]
        | None = None,
        final_input_materials_discard_handler: Callable[[str], object]
        | None = None,
        final_input_material_commit_handler: Callable[[str], object] | None = None,
        diverted_material_release_handler: Callable[[str, bool], object]
        | None = None,
        speaker_verification_provider: Any = None,
        voice_turn_started_handler: Callable[[str], object] | None = None,
        voice_turn_resolved_handler: Callable[..., object] | None = None,
        assistant_request_submission_failed_handler: Callable[[str], object]
        | None = None,
        runtime_identity_factory: RuntimeIdentityFactory | None = None,
    ) -> None:
        """初始化实时音频 worker。

        Args:
            realtime_input_runtime (Any): 输入侧运行时管理器，负责维护会话状态并提供 ASR 等共享能力。
            vad_provider (Any): 语音活动检测 provider，用于识别语音窗口、静音窗口和 turn 边界。
            smart_turn_provider (Any): Smart Turn 判定 provider，用于在静音达到阈值后决定是否结束当前 turn。
            audio_queue (Queue[AudioInputItem]): 待消费音频帧输入队列，LiveKit 或其它上游会把 PCM16 帧写入这里。
            control_queue (Queue[AudioControlCommand]): 高优先级控制命令队列，承载 track 切换、断连重置等输入侧控制事实。
            event_queue (Queue[RuntimeEvent]): 运行时产品事件输出队列，供外层 Router 广播到控制面。
            assistant_request_submitter: final transcript 生成后提交助手请求的 Bridge 端口。
            assistant_control_submitter: 提交预热、取消与插话命令的 Bridge 端口。
            now (Callable[[], float]): 当前单调时间函数，统一提供 worker 内部延迟、超时和节流判断。
            latency_recorder (RealtimeLatencyRecorder | None, optional): 可选本地耗时记录器；为空时不记录延迟 marker。默认为 None。
            max_audio_lag_ms (float, optional): 音频帧从入队到被 worker 消费允许的最大滞后；超过阈值的旧帧会被丢弃。默认为 40.0。
            asr_scheduler (AsrScheduler | None, optional): 可选后台 ASR 调度器；未提供时使用 realtime_input_runtime 的 ASR provider 创建默认 lane。默认为 None。
            realtime_audio_config (RealtimeAudioYamlConfig): 实时音频 YAML 配置，集中提供 VAD、Smart Turn 和说话人确认阈值。
            debug_audio_recorder (DebugAudioRecorderProtocol | None, optional): 可选调试音频录音器；未提供时使用 no-op 实现保持热路径稳定。默认为 None。
            assistant_output_enabled (bool, optional): 是否把 final ASR 投递给 LLM/TTS 输出侧；关闭时只验证输入链路。默认为 True。
            assistant_request_context_preparer: 冻结 Final ASR 请求事实并管理提醒事务的端口。
            input_diversion_port: Attention active 时接纳已公开 Final 输入的端口。
            current_runtime_epoch: 返回当前 Assistant Runtime 世代的函数。
            final_input_materials_provider: 在普通请求准备前读取安全材料引用的端口。
            final_input_materials_discard_handler: 普通 Final ASR 未改道时清理材料快照索引的端口。
            final_input_material_commit_handler: 普通 Final ASR 请求成功提交后的材料登记端口。
            diverted_material_release_handler: 按 Episode 是否成立登记并释放 staged 材料的端口。
            voice_turn_started_handler: VAD 确认开口后建立文本派发 hold 的同步端口。
            voice_turn_resolved_handler: Final ASR 有效或无效时提交语音优先级结果的同步端口；
                有效请求还会接收一个等待顺序屏障清空后才执行的提交回调。
            assistant_request_submission_failed_handler: Final ASR 请求未进入 Bridge 时释放预登记屏障的端口。
            runtime_identity_factory: Backend 主进程共享的运行期身份工厂。

        Returns:
            None: 构造函数只完成依赖注入和状态初始化，不直接消费音频或启动后台循环。"""

        # VAD 开口确认窗口必须是正数，否则无法稳定确认用户是否正式开口。
        vad_speech_start_window_count = realtime_audio_config.vad.speech_start_window_count
        if vad_speech_start_window_count <= 0:
            raise ValueError("vad.speech_start_window_count 必须为正数，才能确认用户开口")
        if assistant_output_enabled and assistant_request_context_preparer is None:
            raise ValueError("assistant_output_enabled=True 时必须提供 assistant_request_context_preparer")
        if assistant_output_enabled and (
            assistant_request_submitter is None or assistant_control_submitter is None
        ):
            raise ValueError("assistant_output_enabled=True 时必须提供 Bridge 提交端口")

        self.realtime_input_runtime = realtime_input_runtime
        self.vad_provider = vad_provider
        self.smart_turn_provider = smart_turn_provider
        self.realtime_audio_config = realtime_audio_config
        # 说话人确认 provider 可选，未启用为 None 时直接放行。
        self.speaker_verification_provider = speaker_verification_provider

        # asr
        self.asr_scheduler = asr_scheduler or AsrScheduler(
            asr_provider=realtime_input_runtime.asr_provider,
            text_eou_provider=getattr(realtime_input_runtime, "text_eou_provider", None),
        )
        # debug_audio_recorder 是调试音频落盘边界；默认 no-op，避免热路径散落启用开关判断。
        self.debug_audio_recorder = debug_audio_recorder or NoopDebugAudioRecorder()

        # 音频队列
        self.audio_queue = audio_queue
        # 控制队列
        self.control_queue = control_queue
        # 产品事件先进入队列，再由外层 Router 投影给 Desktop。
        self.event_queue = event_queue

        # final transcript 与控制命令只能通过 Bridge 的原子健康边界提交。
        self.assistant_request_submitter = assistant_request_submitter
        self.assistant_control_submitter = assistant_control_submitter
        # input-only 模式关闭助手输出侧，final ASR 只广播输入事实并释放 turn_state。
        self.assistant_output_enabled = assistant_output_enabled
        # 请求准备器在入队前冻结当前配置、材料、环境和提醒，并返回显式提交事务。
        self.assistant_request_context_preparer = assistant_request_context_preparer
        self.input_diversion_port = input_diversion_port
        self.current_runtime_epoch = current_runtime_epoch
        self.final_input_materials_provider = final_input_materials_provider
        self.final_input_materials_discard_handler = (
            final_input_materials_discard_handler
        )
        self.final_input_material_commit_handler = final_input_material_commit_handler
        self.diverted_material_release_handler = diverted_material_release_handler
        self.voice_turn_started_handler = voice_turn_started_handler
        self.voice_turn_resolved_handler = voice_turn_resolved_handler
        self.assistant_request_submission_failed_handler = (
            assistant_request_submission_failed_handler
        )
        self.runtime_identity_factory = runtime_identity_factory or RuntimeIdentityFactory()

        # 真实单调时钟。
        self.now = now

        # latency_recorder 是可选本地耗时记录器；为空时 worker 不记录延迟 marker。
        self.latency_recorder = latency_recorder

        # max_audio_lag_ms 定义音频帧从入队到消费允许的最大滞后，超过即丢弃。
        self.max_audio_lag_ms = max_audio_lag_ms

        # 表示当前 worker 接受的输入音轨代际
        self.active_input_track_generation = 0
        # 记录幂等跳过或未知控制命令，便于观测控制面质量。
        self.skipped_control_commands = 0
        # 记录因输入音轨代际不匹配被丢弃的音频帧数量。
        self.dropped_stale_track_generation_frames = 0

        # 记录因队列滞留过久被丢弃的音频帧数量。
        self.dropped_stale_audio_frames = 0

        # 单声道 16kHz PCM16 的 32ms 固定窗口大小。512 个采样点 * 2 字节 = 1024 字节。
        self.vad_window_bytes = 1024
        self.turn_silence_timeout_ms = realtime_audio_config.worker.turn_silence_timeout_ms

        # partial/final 的阈值在初始化时完成一次毫秒到 PCM16 字节数的换算，热路径里只做整数比较。
        self.partial_asr_min_new_bytes = self._milliseconds_to_pcm16_bytes(
            realtime_audio_config.worker.partial_asr_min_new_audio_ms
        )
        self.partial_asr_min_interval_ms = realtime_audio_config.worker.partial_asr_min_interval_ms

        # 连续多少个 VAD 语音窗口后才确认用户正式开口，降低单帧误判导致的 turn 抖动。
        self.vad_speech_start_window_count = vad_speech_start_window_count

        # 缓存尚未凑满固定 VAD 窗口的原始 PCM16 字节。
        self._vad_buffer = b""
        # 保存尚未达到开口确认阈值的候选语音，确认后一次性并入当前 turn，避免丢失首帧。
        self._speech_start_candidate_pcm16 = b""
        # 记录候选语音已连续命中的 VAD 窗口数，遇到静音会清零。
        self._speech_start_candidate_window_count = 0

        #  保存当前用户 turn 已确认窗口音频，供 partial/final ASR 使用。
        self._current_turn_pcm16 = b""
        #  标记当前缓存是否已经出现语音，避免纯静音触发 turn 结束。
        self._current_turn_has_speech = False

        # 有效人声累积
        self._current_turn_speech_pcm16_length = 0
        # 说话人确认只使用 VAD 判定为人声的音频，静音不进入白名单匹配。
        self._current_turn_speaker_pcm16 = b""

        # 说话人确认的 turn 级状态，随 _reset_turn_buffers 一起清零。
        # _sv_similarities 记录每次检测分数，供 final 多次结果均值判定。
        self._sv_similarities: list[float] = []
        self._sv_locked_pass = False
        self._sv_locked_reject = False
        # 只有连续两次低于 low_similarity，才锁定拒绝；中间出现非低分会清零。
        self._sv_consecutive_low_count = 0
        self._sv_check_count = 0
        self._sv_last_checked_speech_pcm16_length = 0
        self._speaker_gate_status: SpeakerGateStatus = initial_speaker_gate_status(
            speaker_verification_enabled=self.speaker_verification_provider is not None
        )
        # 单次/final 阈值、高分锁定放行、连续低分锁定拒绝、多次结果 final 取均值判定。
        self.speaker_verify_high = realtime_audio_config.speaker_verification.high_similarity
        self.speaker_verify_low = realtime_audio_config.speaker_verification.low_similarity
        self.speaker_verify_mean = realtime_audio_config.speaker_verification.mean_similarity

        # 记录助手播报期间开始的用户 turn，等待文本有效且说话人 gate 通过后再真正取消播报。
        self._barge_in_candidate: BargeInCandidate | None = None

        # 记录上次真正提交给 Scheduler 的累计字节数，只用于新增音频节流。
        self._last_partial_submitted_byte_count = 0
        # 记录上次已经展示给下游的累计字节数，只用于丢弃重复或迟到结果。
        self._last_partial_applied_byte_count = 0
        #  记录上次 partial ASR 的毫秒时间戳；None 表示当前 turn 尚未触发过 partial。
        self._last_partial_at_ms: float | None = None

        #  连续静音时长，用于触发 Smart Turn 判断。
        self._silence_ms = 0.0
        # Smart Turn 判定后的延迟收束状态，避免一次模型误判直接切断或永久卡住当前 turn。
        self._smart_turn_end_state: SmartTurnEndState | None = None
        self.smart_turn_final_grace_ms = realtime_audio_config.smart_turn.final_grace_ms
        self.smart_turn_ambiguous_probability = realtime_audio_config.smart_turn.ambiguous_probability
        self.smart_turn_hint_delay_ms = realtime_audio_config.smart_turn.hint_delay_ms
        self.smart_turn_ambiguous_silence_timeout_ms = (
            realtime_audio_config.smart_turn.ambiguous_silence_timeout_ms
        )
        self.smart_turn_absolute_silence_timeout_ms = (
            realtime_audio_config.smart_turn.absolute_silence_timeout_ms
        )
        self.text_eou_enabled = realtime_audio_config.text_eou.enabled
        self.text_eou_high_finish_probability = realtime_audio_config.text_eou.high_finish_probability
        self.text_eou_unlikely_probability = realtime_audio_config.text_eou.unlikely_probability
        self.text_eou_low_confidence_timeout_multiplier = (
            realtime_audio_config.text_eou.low_confidence_timeout_multiplier
        )
        # endpoint EOU 在 Smart Turn 未完成区间提前运行 final ASR；结果回流前必须保留版本信息。
        self._endpoint_eou_pending_state: EndpointEouPendingState | None = None

        # final ASR 与说话人确认都只在有效人声达到最短要求后才触发。
        self.min_final_asr_audio_length = self._milliseconds_to_pcm16_bytes(
            realtime_audio_config.worker.min_final_asr_audio_ms
        )

        # 记录已经提交 final ASR、等待结果回来的 turn；非 None 时新音频暂不进入当前 turn 缓冲。
        self._finalizing_turn_id: str | None = None
        # 记录 final ASR 提交时的音频字节数，用于迟到结果版本校验。
        self._finalizing_byte_count = 0

        # 暂存 final ASR 运行期间到来的下一轮用户音频，避免旧 turn 冻结后吞掉新语音开头。
        self._pending_next_turn_pcm16 = b""
        # 暂存区内被 VAD 判定为语音的 PCM 长度，提升为新 turn 后继续用于短误触过滤。
        self._pending_next_turn_speech_pcm16_length = 0
        # 暂存区内只给说话人确认使用的有效人声音频。
        self._pending_next_turn_speaker_pcm16 = b""
        # 标记暂存区是否已经出现真实语音；纯静音不会被提升为新 turn。
        self._pending_next_turn_has_speech = False
        # 记录下一轮用户语音第一次出现的时间，用于 final 完成后补发 turn started 事实。
        self._pending_next_turn_started_at: float | None = None
        # 记录暂存区中语音后的连续静音时长，提升后延续新 turn 的静音判断节奏。
        self._pending_next_turn_silence_ms = 0.0
        # work_available 聚合控制命令、音频帧和停止唤醒，避免 audio runtime 空队列忙轮询。
        self._work_available = threading.Event()

    def _milliseconds_to_pcm16_bytes(self, milliseconds: float) -> int:
        """把 16kHz 单声道 PCM16 的毫秒时长换算成字节数。"""

        # 当前实时输入固定使用 16kHz、单声道、每采样 16bit，因此 1 秒 = 32000 字节。
        return int(16000 * 2 * (milliseconds / 1000.0))

    async def process_once(self) -> None:
        """异步推进一次 worker 控制和音频处理。"""

        await self._wait_for_input_or_asr_progress()
        # 控制命令会改变 active_input_track_generation 或重置输入状态，必须先于音频帧消费，避免旧帧穿过新轨道边界。
        self._drain_control_queue()
        self._drain_partial_asr_results()
        self._drain_endpoint_eou_results()
        self._drain_final_asr_results()
        # ASR 错误同样来自后台 lane，必须在处理下一帧音频前转成内部错误事件，避免诊断事实滞后。
        self._drain_asr_error_results()
        # 控制面已经稳定后才处理下一帧音频，合格音频会进入准入过滤与 VAD 窗口调度。
        await self._process_next_audio_item()

    def wake(self) -> None:
        """唤醒可能正在等待输入的音频 worker。"""

        self._work_available.set()

    def notify_work_available(self) -> None:
        """通知音频 worker 已有新的控制命令或音频帧。"""

        self._work_available.set()

    def _queue_final_asr_material_cleanup_event(
        self,
        *,
        request_id: str,
        source: str,
        source_turn_id: str | None,
        turn_material_context_id: str | None = None,
    ) -> None:
        """为 final ASR 入队失败投递材料上下文清理事件。

        Args:
            request_id: 助手请求编号。
            source: 请求来源。
            source_turn_id: 可选输入轮次编号。
            turn_material_context_id: 需要定向清理的材料上下文编号。
        """

        normalized_context_id = (
            turn_material_context_id.strip()
            if isinstance(turn_material_context_id, str)
            else ""
        )
        payload: dict[str, object] = {
            "request_id": request_id,
            "source": source,
            "source_turn_id": source_turn_id,
        }
        if normalized_context_id:
            payload["turn_material_context_id"] = normalized_context_id

        # final ASR 没有 HTTP 异常边界；失败材料必须回到主分发线程统一结束并广播释放事件。
        self.event_queue.put(
            RuntimeEvent(
                type="assistant.request_enqueue_failed",
                payload=payload,
                produced_at=self.now(),
                source="realtime_input",
            )
        )

    async def _wait_for_input_or_asr_progress(self) -> None:
        """等待控制命令、音频帧或后台 ASR 结果出现。"""

        # 已有工作时直接推进，避免在已有完成结果或积压队列时额外休眠。
        if self.control_queue.qsize() > 0 or self.audio_queue.qsize() > 0:
            return
        completed_endpoint_eou_count = getattr(self.asr_scheduler, "completed_endpoint_eou_count", 0)
        if (
            self.asr_scheduler.completed_count > 0
            or self.asr_scheduler.completed_final_count > 0
            or completed_endpoint_eou_count > 0
        ):
            return

        # ASR 完成结果不是通过 Queue 投递，短超时等待可以兼顾低 CPU 与后台结果回流及时性。
        await asyncio.to_thread(self._work_available.wait, 0.02)
        self._work_available.clear()

    def _drain_control_queue(self) -> None:
        """清空当前积压的 worker 控制命令。"""

        # 使用非阻塞循环一次性清空 pending 控制面，业务意图是让控制命令在音频帧前拥有严格优先级。
        while True:
            try:
                command = self.control_queue.get_nowait()
            except Empty:
                # 队列耗尽是正常边界，代表本轮音频处理前没有更多控制命令需要抢占。
                return

            if command.kind == "track_switched":  # 输入轨道切换事件
                if command.input_track_generation is None or command.input_track_generation <= self.active_input_track_generation:
                    self.skipped_control_commands += 1
                    continue

                self.active_input_track_generation = command.input_track_generation
                self._reset_input_buffers(reason="track_switched")
                continue

            # 房间断开代表输入来源失效，只重置输入侧，避免残留 turn 音频进入下一次连接。
            if command.kind == "room_disconnected":
                self._reset_input_buffers(reason="room_disconnected")
                continue

            if command.kind == "timed_reminder_takeover":
                takeover = command.payload.get("takeover")
                if isinstance(takeover, TimedReminderInputTakeover):
                    takeover.execute(self._apply_timed_reminder_input_takeover)
                else:
                    self.skipped_control_commands += 1
                continue

            # 未知命令不应该让 worker 崩溃；计数后跳过，便于后续通过指标发现协议不一致。
            self.skipped_control_commands += 1

    def _apply_timed_reminder_input_takeover(self) -> bool:
        """在输入 owner 线程判断并清空所有未完成语音状态。

        Returns:
            清理前已经形成用户语音 Turn 时返回真。
        """

        interrupted = bool(
            self._current_turn_has_speech
            or self._finalizing_turn_id is not None
            or self.realtime_input_runtime.interaction_state.input_state.active
        )
        # reset 后如果继续消费同一批积压音频，它们会被误认成一条新 Turn，因此音频
        # 队列必须和 VAD、ASR、Smart Turn 及 Runtime 输入状态放在同一 owner 操作里清空。
        clear_audio_queue = getattr(self.audio_queue, "clear", None)
        if callable(clear_audio_queue):
            clear_audio_queue()
        self._reset_input_buffers(reason="timed_reminder_takeover")
        return interrupted

    def _reset_input_buffers(self, *, reason: str) -> None:
        """重置 worker 与 RealtimeInputRuntime 的输入侧状态。"""

        current_turn_id = (
            self.realtime_input_runtime.input_turn_session.current_turn_id
        )
        # 当前时刻
        reset_at = self.now()
        self._clear_smart_turn_end_state(reason=reason, now=reset_at)
        # input 重置事件
        reset_events = self.realtime_input_runtime.apply_input_reset(reason, now=reset_at)
        self._enqueue_runtime_events(reset_events, produced_at=reset_at)

        # 输入重置必须同时清空 worker 本地窗口和 turn 缓存，避免旧轨道尾帧跨边界进入新 turn。
        self._vad_buffer = b""
        self._reset_speech_start_candidate()
        self._reset_turn_buffers()
        self._reset_pending_next_turn_buffers()
        # 输入重置代表唤醒前缓存和当前调试片段都跨越了音频边界，必须同步清理。
        self.debug_audio_recorder.reset(reason)
        # ASR 调度器里尚未应用的 partial 结果属于旧输入上下文，重置时必须同步丢弃。
        self.asr_scheduler.reset()
        self.realtime_input_runtime.reset_wakeword_provider()
        # VAD 已经对外建立过 voice hold；任何没有形成有效 Final ASR 的 reset
        # 都必须显式收束它，否则待发文本会永远卡在队列中。
        if (
            self.voice_turn_resolved_handler is not None
            and isinstance(current_turn_id, str)
        ):
            self.voice_turn_resolved_handler(current_turn_id, None)

    async def _process_next_audio_item(self) -> None:
        """处理下一帧音频的准入过滤与 VAD 窗口调度。

            消费音频流
        """

        try:
            item = self.audio_queue.get_nowait()
        except Empty:
            # 没有音频帧是调度循环常态，直接返回让外层下一轮继续调度。
            return

        # 丢弃非当前track的数据
        if item.input_track_generation != self.active_input_track_generation:
            self.dropped_stale_track_generation_frames += 1
            return

        # 延迟计算
        audio_lag_ms = (self.now() - item.received_at) * 1000

        if audio_lag_ms > self.max_audio_lag_ms:
            logger.error(f"当前音频chunk消费延迟过大！！！！延迟{audio_lag_ms}毫秒，队列大小：{self.audio_queue.qsize()}")
            self.dropped_stale_audio_frames += 1
            return

        # now 捕获准入成功后的事实时间，唤醒事件和后续 VAD 处理共享同一轮音频消费的时间上下文。
        now = self.now()

        timeout_events = self.realtime_input_runtime.apply_timeouts(now=now)
        if timeout_events:
            self._enqueue_runtime_events(timeout_events, produced_at=now)

        requies_wake_word = self.realtime_input_runtime.interaction_state.requires_wake_word()
        if requies_wake_word:
            started_at = self.now()
            detected = self.realtime_input_runtime.detect_wakeword(item.pcm16)
            if not detected:
                return
            else:
                duration_ms = (self.now() - started_at) * 1000
                logger.info(f"唤醒成功，唤醒耗时：{duration_ms}，进行会话窗口更新。")
                wake_events = self.realtime_input_runtime.apply_wake_detected(now=now)
                self._enqueue_runtime_events(wake_events, produced_at=now)
                # 唤醒前遗留的不足窗口尾巴属于旧 dormant 上下文，必须在追加当前唤醒帧前清掉，避免污染 VAD 判断。
                self._vad_buffer = b""
                # VAD provider 可能持有跨窗口历史，唤醒后清理它可避免唤醒前静音或语音影响当前 turn 判断。
                reset_vad = getattr(self.vad_provider, "reset", None)
                if callable(reset_vad):
                    reset_vad()

        # 前置静音跳过
        if not self._current_turn_has_speech and is_silence_fast(item.pcm16):
            return

        self._vad_buffer += item.pcm16
        # 凑够vad一帧就进入判断
        while len(self._vad_buffer) >= self.vad_window_bytes:
            vad_window = self._vad_buffer[: self.vad_window_bytes]
            self._vad_buffer = self._vad_buffer[self.vad_window_bytes :]
            await self._process_vad_window(vad_window, duration_ms=32.0)

    async def _process_vad_window(self, pcm16: bytes, duration_ms: float) -> None:
        """处理一个固定 VAD 音频窗口。"""
        # 人声识别
        is_speech = self.vad_provider.is_speech_frame(pcm16)
        if is_speech:
            confirmed_pcm16 = self._observe_speech_window_for_start_confirmation(pcm16)
            if confirmed_pcm16 is None:
                return
            await self._handle_speech_window(confirmed_pcm16)
            return

        # 静音会打断尚未确认的开口候选，避免单次误判残留到下一段真实语音。
        self._reset_speech_start_candidate()
        # VAD 判定为静音时进入静音累计逻辑，只有当前 turn 已有语音才可能收束。
        await self._handle_silence_window(pcm16, duration_ms)

    def _observe_speech_window_for_start_confirmation(self, pcm16: bytes) -> bytes | None:
        """观察一个 VAD 语音窗口并判断是否已确认用户开口。 """

        # 已经处于普通用户 turn 时，后续语音不再需要开口防抖，否则会切碎连续讲话。
        if self._finalizing_turn_id is None and self._current_turn_has_speech:
            return pcm16

        # finalizing 期间若下一轮暂存区已确认有语音，后续语音也应直接并入暂存区。
        if self._finalizing_turn_id is not None and self._pending_next_turn_has_speech:
            return pcm16

        # 尚未确认开口时，先累计候选窗口；确认后再整体进入 turn，避免防抖吞掉开头音频。
        self._speech_start_candidate_pcm16 += pcm16
        self._speech_start_candidate_window_count += 1
        if self._speech_start_candidate_window_count < self.vad_speech_start_window_count:
            return None

        confirmed_pcm16 = self._speech_start_candidate_pcm16
        self._reset_speech_start_candidate()
        return confirmed_pcm16

    def _apply_user_turn_started(self, *, now: float, produced_at: float | None = None) -> None:
        """应用用户 turn 开始事实，并且只在新 turn 创建时打印一次识别到人声日志。"""

        event_produced_at = produced_at if produced_at is not None else now

        start_events = self.realtime_input_runtime.apply_user_turn_started(now=now)
        self._enqueue_runtime_events(start_events, produced_at=event_produced_at)
        if not start_events:
            return

        self._enqueue_foreground_warmup_command(now=now)

        # turn_id 在 RealtimeInputRuntime 应用 start 事实后已经可读，用于把日志与当前 turn 关联起来。
        logger.info(
            "识别到人声，开始新 turn。session_id=%s turn_id=%s input_track_generation=%s",
            self.realtime_input_runtime.current_audio_session_id,
            self.realtime_input_runtime.input_turn_session.current_turn_id,
            self.active_input_track_generation,
        )
        turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
        if self.voice_turn_started_handler is not None and isinstance(turn_id, str):
            self.voice_turn_started_handler(turn_id)

    def _enqueue_foreground_warmup_command(self, *, now: float) -> None:
        """在用户新 turn 开始时投递前台模型最小预热命令。"""

        if not self.assistant_output_enabled:
            return

        if self.assistant_control_submitter is None:
            return
        accepted = self.assistant_control_submitter(
            WarmupForegroundCommand(
                reason="user_turn_started",
                issued_at=now,
            )
        )
        if not accepted:
            logger.warning("助手输出侧不可用，已跳过前台模型预热")

    async def _handle_speech_window(self, pcm16: bytes) -> None:
        """处理 VAD 判定为语音的音频窗口。"""

        now = self.now()

        # final ASR 已经提交后，旧 turn 的音频快照必须冻结；新语音暂存为下一轮 turn 的开头。
        if self._finalizing_turn_id is not None:
            self._stage_pending_next_turn_speech_window(pcm16, now=now)
            return

        # 第一次检测到语音时才广播用户 turn 开始，业务上避免同一 turn 的连续语音重复创建前端状态。
        if not self._current_turn_has_speech:
            assistant_was_playing = self.realtime_input_runtime.interaction_state.assistant_output_state.playout_active
            self._apply_user_turn_started(now=now)
            if assistant_was_playing:
                self._start_barge_in_candidate()

        # 语音窗口代表用户继续表达，必须取消 Smart Turn 的延迟收束或等待续说状态。
        self._clear_smart_turn_end_state(reason="speech_resumed", now=now)
        self._clear_endpoint_eou_pending_state(reason="speech_resumed")
        self._current_turn_has_speech = True
        self._current_turn_speech_pcm16_length += len(pcm16)
        self._current_turn_speaker_pcm16 += pcm16
        # 静音累积重置
        self._silence_ms = 0.0
        # 当前 turn 音频持续累计
        self._current_turn_pcm16 += pcm16

        # 候选插话已经看到有效文本后，继续说话只负责补齐说话人证据；ASR 仍保持独立低延迟链路。
        self._maybe_confirm_barge_in_after_speaker_update(now=now)

        # partial ASR 同时受新增字节和最小时间间隔控制，降低实时字幕刷新成本。
        now_ms = now * 1000
        if not self._should_run_partial_asr(now_ms=now_ms):
            return

        # 满足节流后只提交 ASR snapshot；真实推理由 scheduler 后台串行执行，避免阻塞音频队列消费。
        self._submit_partial_asr_snapshot(submitted_at=now, submitted_at_ms=now_ms)

    async def _handle_silence_window(self, pcm16: bytes, duration_ms: float) -> None:
        """处理 VAD 判定为静音的音频窗口。"""

        # 当前 turn 尚未出现语音时，静音只是环境底噪，不应污染 turn 音频或触发结束判断。
        if not self._current_turn_has_speech:
            return

        # final ASR 已经提交后，旧 turn 不再追加尾音；有新语音历史时，静音归入下一轮暂存区。
        if self._finalizing_turn_id is not None:
            self._stage_pending_next_turn_silence_window(pcm16, duration_ms=duration_ms)
            return

        # Smart Turn 已经给出一次边界判断时，后续静音只推进等待状态，不再重复咨询模型。
        if self._smart_turn_end_state is not None:
            await self._handle_smart_turn_end_state_silence(pcm16, duration_ms=duration_ms)
            return

        # 有效 turn 内的尾部静音也属于 final ASR 上下文
        self._current_turn_pcm16 += pcm16

        # 首次静音时记录尾部静音起点 marker
        if self._silence_ms == 0.0 and self.latency_recorder is not None:
            _turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
            if _turn_id:
                self.latency_recorder.record_marker(
                    trace_id=_turn_id, marker="tail_silence_started", at_wall=self.now(),
                )

        self._silence_ms += duration_ms
        if self._silence_ms < self.turn_silence_timeout_ms:  # 200 ms
            return

        # 静音达到阈值，开始 Smart Turn 决策
        _turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
        if self.latency_recorder is not None and _turn_id:
            self.latency_recorder.record_marker(
                trace_id=_turn_id, marker="smart_turn_started", at_wall=self.now(),
            )

        start_ms = self.now()
        decision = self.smart_turn_provider.decide_turn_end(self._current_turn_pcm16)
        duration_ms = (self.now() - start_ms) * 1000
        logger.info(f"smart turn耗时：{duration_ms}")

        # Smart Turn 决策完成
        if self.latency_recorder is not None and _turn_id:
            self.latency_recorder.record_marker(
                trace_id=_turn_id, marker="smart_turn_finished", at_wall=self.now(),
            )
        if not decision.is_turn_finished:
            logger.info(
                f"current_turn_id: {self.realtime_input_runtime.input_turn_session.current_turn_id}， 置信度；{decision.probability}, smart turn认为你还没讲完"
            )
            self._handle_unfinished_smart_turn_decision(decision_probability=decision.probability)
            return
        logger.info(
            f"current_turn_id: {self.realtime_input_runtime.input_turn_session.current_turn_id}， 置信度；{decision.probability}，smart turn认为你讲完了"
        )

        self._enter_pending_final_grace(decision_probability=decision.probability)

    async def _handle_smart_turn_end_state_silence(self, pcm16: bytes, *, duration_ms: float) -> None:
        """处理 Smart Turn 等待状态中的静音窗口。

        Args:
            pcm16: 当前 VAD 静音窗口。
            duration_ms: 当前窗口持续毫秒数。

        Returns:
            None: 等待未到期时只累计上下文，到期后提交 final ASR。
        """

        state = self._smart_turn_end_state
        if state is None:
            return

        # 等待期内的静音仍属于当前 turn 尾部上下文；最长受兜底阈值限制，不会无限膨胀。
        self._current_turn_pcm16 += pcm16
        state.silence_ms += duration_ms

        if state.kind == "pending_final_grace":
            if state.silence_ms < self.smart_turn_final_grace_ms:
                return
            # final grace 期满，记录 marker
            if self.latency_recorder is not None:
                _turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
                if _turn_id:
                    self.latency_recorder.record_marker(
                        trace_id=_turn_id, marker="final_grace_elapsed", at_wall=self.now(),
                    )
            self._clear_smart_turn_end_state(reason="final_grace_elapsed", now=self.now())
            if state.cached_final_text is not None:
                self._apply_cached_endpoint_final_text(
                    text=state.cached_final_text,
                    submitted_at=state.cached_final_submitted_at,
                )
                return
            self._submit_final_asr_snapshot()
            return

        # desktop要不要提示正在听
        self._maybe_show_smart_turn_waiting_hint(state)

        if self._should_force_final_waiting_user(state):
            if self._endpoint_eou_pending_state is not None:
                self._endpoint_eou_pending_state.finalize_when_ready = True
                return
            self._clear_smart_turn_end_state(reason="waiting_timeout", now=self.now())
            self._submit_final_asr_snapshot()

    def _maybe_show_smart_turn_waiting_hint(self, state: SmartTurnEndState) -> None:
        """在低置信等待续说持续静音足够久后，发送一次 desktop 提示。"""

        if state.kind != "waiting_user_continue":
            return
        if not state.hint_required or state.hint_visible:
            return
        if state.silence_ms < self.smart_turn_hint_delay_ms:
            return

        state.hint_visible = True
        self._enqueue_smart_turn_waiting_user_started(probability=state.probability, produced_at=self.now())

    def _should_force_final_waiting_user(self, state: SmartTurnEndState) -> bool:
        """判断等待用户续说状态是否已经达到静音兜底条件。"""

        if state.kind != "waiting_user_continue":
            return False

        # 中等置信未结束通常只是模型犹豫，短静音兜底可以降低用户已说完后的卡顿。
        if (
            state.probability >= self.smart_turn_ambiguous_probability
            and state.silence_ms >= self.smart_turn_ambiguous_silence_timeout_ms
        ):
            return True

        # 所有等待续说状态都必须有绝对兜底，避免低置信误判导致 turn 永久悬挂。
        return state.silence_ms >= self.smart_turn_absolute_silence_timeout_ms * state.timeout_multiplier

    def _handle_unfinished_smart_turn_decision(self, *, decision_probability: float) -> None:
        """处理 Smart Turn 认为尚未结束的边界判断。"""

        # 文本 EOU 只辅助 Smart Turn 未完成区间；提交失败时立即回退原始等待逻辑。
        if self.text_eou_enabled and self._submit_endpoint_eou_snapshot(decision_probability=decision_probability):
            if not self._current_turn_has_speech:
                return
            self._enter_waiting_user_continue(decision_probability=decision_probability)
            return

        self._enter_waiting_user_continue(decision_probability=decision_probability)

    def _submit_endpoint_eou_snapshot(self, *, decision_probability: float) -> bool:
        """提交 endpoint EOU 快照。"""

        if self._current_turn_speech_pcm16_length < self.min_final_asr_audio_length:
            logger.info("这一轮 turn 有效人声太短，放弃当前输入 turn 并回到 idle")
            self._reset_input_buffers(reason="short_turn_ignored")
            return True

        now = self.now()
        current_turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
        if current_turn_id is None:
            raise RuntimeError("endpoint EOU 缺少当前 turn id，无法提交")

        # endpoint EOU 会运行 final ASR；只有明确非主人时才丢弃，等待/灰区由 final 阶段继续收束。
        speaker_status = self._update_speaker_gate_state(is_final=True)
        if speaker_status == "rejected":
            logger.warning(
                "说话人确认：endpoint EOU 被拒，丢弃整轮回 idle check=%d",
                self._sv_check_count,
            )
            self._reset_input_buffers(reason="speaker_rejected")
            return True
        self._try_confirm_barge_in(turn_id=current_turn_id, now=now)

        submit_endpoint_eou = getattr(self.asr_scheduler, "submit_endpoint_eou", None)
        if not callable(submit_endpoint_eou):
            return False

        byte_count = len(self._current_turn_pcm16)
        submitted = submit_endpoint_eou(
            pcm16=self._current_turn_pcm16,
            turn_id=current_turn_id,
            input_track_generation=self.active_input_track_generation,
            byte_count=byte_count,
            submitted_at=now,
        )
        if not submitted:
            return False

        self._endpoint_eou_pending_state = EndpointEouPendingState(
            turn_id=current_turn_id,
            input_track_generation=self.active_input_track_generation,
            byte_count=byte_count,
            submitted_at=now,
            smart_turn_probability=decision_probability,
        )
        return True

    def _enter_pending_final_grace(
        self,
        *,
        decision_probability: float,
        cached_final_text: str | None = None,
        cached_final_submitted_at: float | None = None,
    ) -> None:
        """进入 Smart Turn 已结束后的短观察期。"""

        self._silence_ms = 0.0
        self._smart_turn_end_state = SmartTurnEndState(
            kind="pending_final_grace",
            probability=decision_probability,
            cached_final_text=cached_final_text,
            cached_final_submitted_at=cached_final_submitted_at,
        )

    def _enter_waiting_user_continue(
        self,
        *,
        decision_probability: float,
        timeout_multiplier: float = 1.0,
    ) -> None:
        """进入 Smart Turn 未结束后的等待续说状态。"""

        self._silence_ms = 0.0
        hint_required = decision_probability < self.smart_turn_ambiguous_probability
        self._smart_turn_end_state = SmartTurnEndState(
            kind="waiting_user_continue",
            probability=decision_probability,
            hint_required=hint_required,
            timeout_multiplier=timeout_multiplier,
        )

    def _clear_smart_turn_end_state(self, *, reason: str, now: float) -> None:
        """清理 Smart Turn 等待状态，并在必要时关闭 desktop 提示。"""

        state = self._smart_turn_end_state
        if state is None:
            return

        self._smart_turn_end_state = None
        if state.hint_visible:
            self._enqueue_smart_turn_waiting_user_ended(
                probability=state.probability,
                reason=reason,
                produced_at=now,
            )

    def _clear_endpoint_eou_pending_state(self, *, reason: str) -> None:
        """清理 endpoint EOU 等待状态。"""

        if self._endpoint_eou_pending_state is None:
            return

        logger.info("清理 endpoint EOU 状态 reason=%s", reason)
        self._endpoint_eou_pending_state = None

    def _set_speaker_gate_status(self, status: SpeakerGateStatus) -> None:
        """同步当前 turn 的说话人 gate 状态。

        Args:
            status: 新的说话人 gate 状态。

        Returns:
            None: 该函数会同步更新当前候选插话状态。
        """

        self._speaker_gate_status = status
        if self._barge_in_candidate is not None:
            self._barge_in_candidate.speaker_status = status

    def _update_speaker_gate_state(self, *, is_final: bool) -> SpeakerGateStatus:
        """更新当前 turn 的说话人 gate 状态。

        Args:
            is_final: 是否处于 final/endpoint 这类收束阶段，灰区只有在该阶段才用均值决策。

        Returns:
            SpeakerGateStatus: 更新后的说话人 gate 状态。
        """

        if self.speaker_verification_provider is None:
            self._set_speaker_gate_status("disabled")
            return self._speaker_gate_status

        # 锁定短路让同一 turn 内的高置信结果保持稳定，避免重复推理造成状态抖动。
        if self._sv_locked_pass:
            self._set_speaker_gate_status("passed")
            return self._speaker_gate_status
        if self._sv_locked_reject:
            self._set_speaker_gate_status("rejected")
            return self._speaker_gate_status

        # 音频不足只表示证据不够；不能把“暂时无法验证”等价为“主人已通过”。
        if self._current_turn_speech_pcm16_length < self.min_final_asr_audio_length:
            self._set_speaker_gate_status("waiting_audio")
            return self._speaker_gate_status

        new_speaker_bytes = (
            self._current_turn_speech_pcm16_length
            - self._sv_last_checked_speech_pcm16_length
        )
        if not is_final and new_speaker_bytes < self.min_final_asr_audio_length:
            return self._speaker_gate_status

        start_ms = self.now()
        speaker_pcm16 = latest_speaker_verification_pcm16(self._current_turn_speaker_pcm16)
        result = self.speaker_verification_provider.verify(speaker_pcm16)
        # 说话人验证完成，记录 marker
        if self.latency_recorder is not None:
            _turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
            if _turn_id:
                self.latency_recorder.record_marker(
                    trace_id=_turn_id, marker="speaker_verification_finished", at_wall=self.now(),
                )
        logger.info(f"说话人确认耗时：{self.now()-start_ms}")
        self._sv_check_count += 1
        self._sv_last_checked_speech_pcm16_length = self._current_turn_speech_pcm16_length
        similarity = (
            result.best_similarity if result.best_similarity is not None else 0.0
        )
        self._sv_similarities.append(similarity)
        speech_seconds = self._current_turn_speech_pcm16_length / 32000.0
        logger.info(
            "说话人确认 sim=%.4f check=%d speech=%.2fs",
            similarity,
            self._sv_check_count,
            speech_seconds,
        )

        # 低分连续计数必须在收束判断前更新；一次非低分会清掉之前未连续的低分。
        if similarity < self.speaker_verify_low:
            self._sv_consecutive_low_count += 1
        else:
            self._sv_consecutive_low_count = 0

        count = len(self._sv_similarities)
        if is_final and count == 1:
            # 短语音只有一次检查时，final 直接使用单次阈值，不套用多次均值或低分 streak。
            if similarity >= self.speaker_verify_high:
                self._sv_locked_pass = True
                self._set_speaker_gate_status("passed")
                logger.info(
                    "说话人确认：单次 final 分数放行 sim=%.4f threshold=%.4f",
                    similarity,
                    self.speaker_verify_high,
                )
                return self._speaker_gate_status

            self._set_speaker_gate_status("rejected")
            logger.warning(
                "说话人确认：单次 final 分数拒绝 sim=%.4f threshold=%.4f",
                similarity,
                self.speaker_verify_high,
            )
            return self._speaker_gate_status

        # 高分锁定放行：确信白名单，后续不再检测。
        if similarity >= self.speaker_verify_high:
            self._sv_locked_pass = True
            self._set_speaker_gate_status("passed")
            logger.info(
                "说话人确认：高分锁定放行 sim=%.4f", similarity
            )
            return self._speaker_gate_status

        # 非 final 单次低分先保留当前 turn，只有连续两次低分才锁定拒绝。
        if self._sv_consecutive_low_count >= SPEAKER_LOW_REJECTION_STREAK:
            self._sv_locked_reject = True
            self._set_speaker_gate_status("rejected")
            logger.warning(
                "说话人确认：连续低分锁定拒绝 sim=%.4f streak=%d",
                similarity,
                self._sv_consecutive_low_count,
            )
            return self._speaker_gate_status

        if not is_final:
            # 非 final 阶段只让达到单次放行门槛的灰区结果参与插话；低分一次时继续等后续证据。
            gray_pass_similarity = self.speaker_verify_mean
            if similarity >= gray_pass_similarity:
                self._set_speaker_gate_status("passed")
                logger.info(
                    "说话人确认：灰区达到插话阈值放行 sim=%.4f threshold=%.4f",
                    similarity,
                    gray_pass_similarity,
                )
                return self._speaker_gate_status

            self._set_speaker_gate_status("uncertain")
            return self._speaker_gate_status

        observed_mean_similarity = sum(self._sv_similarities) / count
        if observed_mean_similarity >= self.speaker_verify_mean:
            self._set_speaker_gate_status("passed")
            logger.info(
                "说话人确认：final 灰区均值放行 mean=%.4f threshold=%.4f",
                observed_mean_similarity,
                self.speaker_verify_mean,
            )
            return self._speaker_gate_status
        self._set_speaker_gate_status("rejected")
        logger.warning(
            "说话人确认：final 灰区均值拒绝 mean=%.4f threshold=%.4f",
            observed_mean_similarity,
            self.speaker_verify_mean,
        )
        return self._speaker_gate_status

    def _submit_final_asr_snapshot(self) -> None:
        """提交当前 turn 的 final ASR 快照。"""
        if self._current_turn_speech_pcm16_length < self.min_final_asr_audio_length:
            logger.info("这一轮 turn 有效人声太短，放弃当前输入 turn 并回到 idle")
            self._reset_input_buffers(reason="short_turn_ignored")
            return
        # Smart Turn 确认结束后提交 final ASR snapshot，不能 await 慢模型阻塞后续音频消费。
        now = self.now()
        current_turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
        if current_turn_id is None:
            raise RuntimeError("final transcript 缺少当前 turn id，无法创建助手请求")

        # final 提交前更新说话人 gate；只有明确非主人时才丢弃整轮。
        speaker_status = self._update_speaker_gate_state(is_final=True)
        if speaker_status == "rejected":
            logger.warning(
                "说话人确认：final 被拒，丢弃整轮回 idle check=%d",
                self._sv_check_count,
            )
            self._reset_input_buffers(reason="speaker_rejected")
            return
        self._try_confirm_barge_in(turn_id=current_turn_id, now=now)

        # final_asr_input 必须在 submit_final 前记录，并使用完全相同的 bytes，便于主观确认 ASR 实际听到的内容。
        self.debug_audio_recorder.record_final_asr_input(
            current_turn_id,
            self._current_turn_pcm16,
            {
                "input_track_generation": self.active_input_track_generation,
                "byte_count": len(self._current_turn_pcm16),
                "speech_byte_count": self._current_turn_speech_pcm16_length,
                "stage": "final_asr_submit",
            },
        )
        submitted = self.asr_scheduler.submit_final(
            pcm16=self._current_turn_pcm16,
            turn_id=current_turn_id,
            input_track_generation=self.active_input_track_generation,
            byte_count=len(self._current_turn_pcm16),
            submitted_at=now,
        )
        if not submitted:
            logger.warning("final ASR 提交被拒绝，放弃当前输入 turn 并回到 idle")
            self._reset_input_buffers(reason="final_asr_submit_rejected")
            return

        # final ASR 提交成功，记录 marker
        if self.latency_recorder is not None:
            self.latency_recorder.record_marker(
                trace_id=current_turn_id, marker="final_asr_submitted", at_wall=self.now(),
            )

        # finalizing 状态记录提交版本，后续迟到 partial 或重复静音都不能再改写旧 turn。
        self._finalizing_turn_id = current_turn_id
        self._finalizing_byte_count = len(self._current_turn_pcm16)

    def _should_run_partial_asr(self, *, now_ms: float) -> bool:
        """判断当前累计音频是否满足 partial ASR 节流条件。"""

        # new_bytes 表示自上次 partial 后新增的上下文长度，用于避免过短音频频繁重复识别。
        new_bytes = len(self._current_turn_pcm16) - self._last_partial_submitted_byte_count
        if new_bytes < self.partial_asr_min_new_bytes:
            return False

        # 首次 partial 没有上一时间戳，业务上只要字节阈值达标即可产出首个实时字幕。
        if self._last_partial_at_ms is None:
            return True

        # elapsed_ms 表示距上次 partial 的时间间隔，用于避免第二次及以后高频调用 ASR。
        elapsed_ms = now_ms - self._last_partial_at_ms
        if elapsed_ms < self.partial_asr_min_interval_ms:
            return False

        return True

    def _drain_partial_asr_results(self) -> None:
        """应用后台 partial ASR 已完成结果。        """

        # 遍历当前asr的结果
        for result in self.asr_scheduler.drain_completed():
            # 判断 partial ASR 结果是否仍属于当前输入上下文。
            if not self._is_current_partial_result(result):
                continue

            # ASR 在静音或噪声窗口可能返回空文本；这类结果不能清理已有字幕，也不能确认插话。
            normalized_text = result.text.strip()
            if not normalized_text:
                logger.debug(
                    "丢弃空 partial ASR 结果 turn_id=%s byte_count=%s",
                    result.turn_id,
                    result.byte_count,
                )
                continue

            # now 捕获应用 partial 的事实时间；不同于提交时间，反映文本真正可见的时刻。
            now = self.now()
            self._confirm_barge_in_if_actionable_transcript(
                turn_id=result.turn_id,
                text=normalized_text,
                now=now,
            )
            partial_events = self.realtime_input_runtime.apply_partial_transcript(normalized_text)
            self._enqueue_runtime_events(partial_events, produced_at=now)
            # 只有公共事件成功进入运行时队列后才推进展示版本；提交版本不能在这里复用，否则慢 ASR 会被误判过期。
            self._last_partial_applied_byte_count = result.byte_count

    def _drain_endpoint_eou_results(self) -> None:
        """应用后台 endpoint EOU 已完成结果。"""

        drain_endpoint_eou = getattr(self.asr_scheduler, "drain_completed_endpoint_eou", None)
        if not callable(drain_endpoint_eou):
            return

        for result in drain_endpoint_eou():
            if not self._is_current_endpoint_eou_result(result):
                continue

            self._apply_endpoint_eou_result(result)

    def _drain_final_asr_results(self) -> None:
        """应用后台 final ASR 已完成结果。"""

        for result in self.asr_scheduler.drain_completed_final():
            if not self._is_current_final_result(result):
                continue

            self._apply_final_asr_result(result)

    def _drain_asr_error_results(self) -> None:
        """应用后台 ASR 已完成错误结果。"""

        # 部分轻量测试替身仍只实现旧 scheduler 表面；缺少错误 drain 时等价于当前没有错误事实可消费。
        drain_errors = getattr(self.asr_scheduler, "drain_errors", None)
        if not callable(drain_errors):
            return

        # 错误 drain 必须在音频处理前完成，保证后台 provider 失败能及时进入统一 runtime 错误通道。
        for result in drain_errors():
            payload = {
                        "component": "asr",
                        "kind": result.kind,
                        "turn_id": result.turn_id,
                        "input_track_generation": result.input_track_generation,
                        "byte_count": result.byte_count,
                        "submitted_at": result.submitted_at,
                        "message": result.message,
                    }
            logger.error(f"ASR发生错误，错误相关信息 {payload}")
            # produced_at 表示 worker 观察到错误事实并入队的时间，不覆盖 scheduler 提交时间。
            produced_at = self.now()
            self.event_queue.put(
                RuntimeEvent(
                    type="runtime.internal_error",
                    payload=payload,
                    produced_at=produced_at,
                    source="realtime_input",
                )
            )
            if result.kind == "endpoint_eou" and self._endpoint_eou_pending_state is not None:
                self._clear_endpoint_eou_pending_state(reason="endpoint_eou_error")
            if result.kind == "final" and self._is_current_final_error(result):
                self._reset_input_buffers(reason="final_asr_failed")

    def _is_current_final_error(self, result: AsrErrorResult) -> bool:
        """判断 Final ASR 错误是否仍属于当前正在收束的语音轮次。

        Args:
            result: ASR 调度线程返回的结构化错误事实。

        Returns:
            轮次、音轨代际和提交字节数都匹配时返回真。
        """

        if result.kind != "final":
            return False
        if result.input_track_generation != self.active_input_track_generation:
            return False
        if result.turn_id != self._finalizing_turn_id:
            return False
        return result.byte_count == self._finalizing_byte_count

    def _is_current_final_result(self, result: FinalAsrResult) -> bool:
        """判断 final ASR 结果是否仍属于当前收束中的 turn。"""

        # 没有 finalizing turn 说明当前结果已过期或 worker 已被 reset。
        if self._finalizing_turn_id is None:
            return False

        # track 切换后旧 final 结果不能应用到新输入代际。
        if result.input_track_generation != self.active_input_track_generation:
            return False

        # final 结果必须匹配提交时的 turn id，避免新 turn 误用旧识别文本。
        if result.turn_id != self._finalizing_turn_id:
            return False

        # 字节长度不一致说明该结果不是当前等待的 final snapshot。
        if result.byte_count != self._finalizing_byte_count:
            return False

        return True

    def _is_current_endpoint_eou_result(self, result: EndpointEouResult) -> bool:
        """判断 endpoint EOU 结果是否仍属于当前输入上下文。"""

        state = self._endpoint_eou_pending_state
        if state is None:
            return False
        if result.input_track_generation != self.active_input_track_generation:
            return False
        if result.input_track_generation != state.input_track_generation:
            return False
        if result.turn_id != state.turn_id:
            return False
        if result.byte_count != state.byte_count:
            return False
        # byte_count 只确认结果属于提交时的 endpoint snapshot；等待期追加静音不会让结果过期。
        return True

    def _apply_endpoint_eou_result(self, result: EndpointEouResult) -> None:
        """根据 endpoint EOU 结果更新 Smart Turn 等待状态。"""

        state = self._endpoint_eou_pending_state
        if state is None:
            return

        self._endpoint_eou_pending_state = None

        # 如果这个为True，说明这一环，smart turn那边的等待阈值都过了，这个结果可以直接使用当作final asr
        if state.finalize_when_ready:
            self._clear_smart_turn_end_state(reason="endpoint_eou_ready_after_timeout", now=self.now())
            self._apply_cached_endpoint_final_text(text=result.text, submitted_at=result.submitted_at)
            return

        if result.eou_probability >= self.text_eou_high_finish_probability:
            self._clear_smart_turn_end_state(reason="endpoint_eou_high_finish", now=self.now())
            self._enter_pending_final_grace(
                decision_probability=state.smart_turn_probability,
                cached_final_text=result.text,
                cached_final_submitted_at=result.submitted_at,
            )
            return

        current_state = self._smart_turn_end_state
        if current_state is None or current_state.kind != "waiting_user_continue":
            return

        # 确定用户没说完
        if (
            state.smart_turn_probability < self.smart_turn_ambiguous_probability
            and result.eou_probability < self.text_eou_unlikely_probability
        ):
            current_state.timeout_multiplier = self.text_eou_low_confidence_timeout_multiplier

    def _apply_final_asr_result(self, result: FinalAsrResult) -> None:
        """应用一个已确认有效的 final ASR 结果。 """

        self._clear_smart_turn_end_state(reason="final_asr_applied", now=self.now())
        source_turn_id = self._finalizing_turn_id
        if source_turn_id is None:
            raise RuntimeError("final transcript 缺少当前 turn id，无法创建助手请求")

        # final ASR 结果到达，记录 marker
        if self.latency_recorder is not None:
            self.latency_recorder.record_marker(
                trace_id=source_turn_id, marker="final_asr_completed", at_wall=self.now(),
            )

        self._apply_final_transcript_text(
            final_text=result.text,
            source_turn_id=source_turn_id,
            byte_count=result.byte_count,
            input_track_generation=result.input_track_generation,
            submitted_at=result.submitted_at,
        )

    def _apply_cached_endpoint_final_text(self, *, text: str, submitted_at: float | None) -> None:
        """复用 endpoint EOU 阶段已经生成的 final ASR 文本。"""

        source_turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
        if source_turn_id is None:
            raise RuntimeError("endpoint final transcript 缺少当前 turn id，无法创建助手请求")

        self._apply_final_transcript_text(
            final_text=text,
            source_turn_id=source_turn_id,
            byte_count=len(self._current_turn_pcm16),
            input_track_generation=self.active_input_track_generation,
            submitted_at=submitted_at,
        )

    def _apply_final_transcript_text(
        self,
        *,
        final_text: str,
        source_turn_id: str,
        byte_count: int,
        input_track_generation: int,
        submitted_at: float | None,
    ) -> None:
        """应用 final ASR 文本并完成当前 turn 收束。"""

        # now 表示 final 文本真正被应用到产品状态的时间。
        now = self.now()
        final_transcript_decision = classify_final_transcript(final_text)
        final_text_is_empty = final_transcript_decision.ignored_reason == "empty"
        runtime_identity_factory = getattr(
            self,
            "runtime_identity_factory",
            RuntimeIdentityFactory(),
        )
        assistant_request_id = (
            runtime_identity_factory.new("final-asr")
            if self.assistant_output_enabled and not final_text_is_empty
            else None
        )
        diversion_result = None
        if self.assistant_output_enabled and assistant_request_id is not None:
            # 所有非空 Final 都先走 Attention 线性化点。普通短句得到的 lease 会在下方
            # 立即回滚；Self-Reflection active 时则不会被短句过滤漏出 episode。
            diversion_result = self._divert_final_input(
                input_identity=source_turn_id,
                assistant_request_id=assistant_request_id,
                text=final_text,
            )
        diverted_to_episode = input_diversion_was_accepted(diversion_result)
        diversion_blocks_normal = input_diversion_blocks_normal_request(
            diversion_result
        )
        should_start_ordinary_assistant = (
            final_transcript_decision.should_request_assistant
            and not diverted_to_episode
            and not diversion_blocks_normal
        )
        if should_start_ordinary_assistant:
            self._confirm_barge_in_if_actionable_decision(
                turn_id=source_turn_id,
                should_request_assistant=True,
                now=now,
            )
        self.debug_audio_recorder.record_final_asr_result(
            source_turn_id,
            text=final_text,
            request_id=assistant_request_id,
            is_empty=final_text_is_empty,
            metadata={
                "byte_count": byte_count,
                "input_track_generation": input_track_generation,
                "submitted_at": submitted_at,
                "ignored_reason": final_transcript_decision.ignored_reason,
            },
        )

        if not self.assistant_output_enabled:
            final_events = self.realtime_input_runtime.apply_input_only_final_transcript(final_text, now=now)
            self._enqueue_runtime_events(final_events, produced_at=now)
            if self.voice_turn_resolved_handler is not None:
                self.voice_turn_resolved_handler(source_turn_id, None)
        # 只有通过质量过滤的最终文本才进入助手请求队列，避免无内容或过短口癖触发空回复。
        else:
            if diversion_blocks_normal:
                # Episode append 未提交时不能先公开一条看似已接纳的 Final，也不能把它
                # 降级成普通 Turn。这里只结束产品输入状态，原文仍保留在诊断记录中。
                final_events = (
                    self.realtime_input_runtime.close_uncommitted_final_transcript(
                        now=now,
                        reason="input_ownership_uncommitted",
                    )
                )
            else:
                final_events = self.realtime_input_runtime.apply_final_transcript(
                    final_text,
                    now=now,
                    assistant_request_id=(
                        assistant_request_id
                        if should_start_ordinary_assistant
                        else None
                    ),
                    should_request_assistant=should_start_ordinary_assistant,
                    history_disposition=(
                        diversion_result.history_disposition
                        if diversion_result is not None
                        else "ordinary_turn"
                    ),
                    episode_id=(
                        diversion_result.episode_id
                        if diversion_result is not None
                        else None
                    ),
                )
            # type, user.final_transcript
            self._enqueue_runtime_events(final_events, produced_at=now)
            if diverted_to_episode:
                if assistant_request_id is None:
                    raise RuntimeError("episode final transcript 缺少稳定请求 id")
                self._release_diverted_materials(
                    source_turn_id,
                    remember_recent_materials=True,
                )
                self._settle_diverted_assistant_handoff(
                    request_id=assistant_request_id,
                    now=now,
                )
                if self.voice_turn_resolved_handler is not None:
                    self.voice_turn_resolved_handler(source_turn_id, None)
            elif diversion_blocks_normal:
                if assistant_request_id is None:
                    raise RuntimeError("被 Attention 阻断的 Final 缺少稳定请求 id")
                self._release_diverted_materials(
                    source_turn_id,
                    remember_recent_materials=False,
                )
                self._settle_diverted_assistant_handoff(
                    request_id=assistant_request_id,
                    now=now,
                )
                if self.voice_turn_resolved_handler is not None:
                    self.voice_turn_resolved_handler(source_turn_id, None)
            elif should_start_ordinary_assistant:
                if assistant_request_id is None:
                    raise RuntimeError("可回复 final transcript 缺少助手请求 id")
                logger.info("当前 turn 最终 ASR 已进入普通请求 request_id=%s", assistant_request_id)
                attention_lease = (
                    diversion_result.assistant_request_lease
                    if diversion_result is not None
                    else None
                )
                source_session_id = self.realtime_input_runtime.current_audio_session_id
                interaction_session_id = (
                    self.realtime_input_runtime.interaction_state.interaction_session_id
                )
                if self.voice_turn_resolved_handler is None:
                    self._dispatch_final_asr_request(
                        request_id=assistant_request_id,
                        source_turn_id=source_turn_id,
                        final_text=final_text,
                        requested_at=now,
                        source_session_id=source_session_id,
                        interaction_session_id=interaction_session_id,
                        attention_lease=attention_lease,
                        raise_on_failure=True,
                    )
                else:
                    self.voice_turn_resolved_handler(
                        source_turn_id,
                        assistant_request_id,
                        lambda: self._dispatch_final_asr_request(
                            request_id=assistant_request_id,
                            source_turn_id=source_turn_id,
                            final_text=final_text,
                            requested_at=now,
                            source_session_id=source_session_id,
                            interaction_session_id=interaction_session_id,
                            attention_lease=attention_lease,
                        ),
                    )
            else:
                if (
                    diversion_result is not None
                    and diversion_result.assistant_request_lease is not None
                ):
                    diversion_result.assistant_request_lease.rollback()
                if self.voice_turn_resolved_handler is not None:
                    self.voice_turn_resolved_handler(source_turn_id, None)
                if final_text_is_empty:
                    logger.warning("当前turn，最终的ASR结果是空的，不对此做任何回应。")
                else:
                    logger.warning(
                        "当前turn，最终的ASR结果过短，不对此做任何回应 ignored_reason=%s text=%s",
                        final_transcript_decision.ignored_reason,
                        final_text,
                    )

        # 当前 turn 已经完整收束，清理 worker 本地缓存，下一次语音从新 turn 开始。
        self._reset_turn_buffers()
        # final ASR 等待期间到来的新语音不能丢，旧 turn 归档后立刻提升为下一轮输入开头。
        self._promote_pending_next_turn_if_needed(promoted_at=now)

    def _divert_final_input(
        self,
        *,
        input_identity: str,
        assistant_request_id: str,
        text: str,
    ) -> InputDiversionResult | None:
        """在普通 Final ASR 请求准备前尝试接纳当前输入。

        Args:
            input_identity: 语音 turn 的稳定身份。
            assistant_request_id: 普通请求若胜出时使用的 Bridge 请求编号。
            text: 已经发布到公共事件队列的最终正文。

        Returns:
            Attention 端口的结构化裁决；端口未接线时返回 ``None``。基础设施异常
            会返回明确失败原因，调用方据此阻止普通请求降级。
        """

        port = getattr(self, "input_diversion_port", None)
        if port is None:
            return None
        epoch_reader = getattr(self, "current_runtime_epoch", None)
        runtime_epoch = (
            epoch_reader()
            if epoch_reader is not None
            else getattr(self.realtime_input_runtime, "input_runtime_epoch", 0)
        )
        if type(runtime_epoch) is not int or runtime_epoch <= 0:
            logger.warning(
                "无法裁决 Final ASR 改道：Assistant Runtime 世代无效 turn_id=%s",
                input_identity,
            )
            return InputDiversionResult(
                status="not_diverted",
                reason="runtime_epoch_unavailable",
            )

        materials: tuple[UnattendedMaterialReference, ...] = ()
        provider = getattr(self, "final_input_materials_provider", None)
        if provider is not None:
            try:
                candidate_materials = provider(input_identity)
                materials = tuple(candidate_materials)
            except Exception:
                # 材料投影失败不应吞掉用户正文；没有安全引用时仍可保存文本片段。
                logger.warning(
                    "读取 Final ASR 改道材料引用失败 turn_id=%s",
                    input_identity,
                    exc_info=True,
                )

        try:
            result = port.divert_final_input(
                DivertFinalInputCommand(
                    runtime_epoch=runtime_epoch,
                    input_identity=input_identity,
                    assistant_request_id=assistant_request_id,
                    source="final_asr",
                    text=text,
                    materials=materials,
                )
            )
        except Exception:
            # Attention 端口决定输入是否能越过普通请求门禁。它异常时不能猜测
            # 当前是否仍有 active Attention，因此按 fail-closed 记录失败，禁止普通提交。
            logger.exception(
                "Final ASR 输入改道裁决失败 turn_id=%s",
                input_identity,
            )
            self._discard_projected_materials(input_identity)
            return InputDiversionResult(
                status="not_diverted",
                reason="diversion_failed",
            )
        if result.status not in {"accepted", "duplicate"}:
            self._discard_projected_materials(input_identity)
        return result

    def _self_reflection_attention_active(self) -> bool:
        """判断当前用户开口是否处于不可打断的 Self-Reflection 生命周期。

        Returns:
            Attention 明确活跃时返回真；端口异常时也返回真以避免误取消 Recovery。
        """

        port = getattr(self, "input_diversion_port", None)
        is_attention_active = getattr(port, "is_attention_active", None)
        if not callable(is_attention_active):
            return False
        epoch_reader = getattr(self, "current_runtime_epoch", None)
        runtime_epoch = (
            epoch_reader()
            if epoch_reader is not None
            else getattr(self.realtime_input_runtime, "input_runtime_epoch", 0)
        )
        if type(runtime_epoch) is not int or runtime_epoch <= 0:
            return True
        try:
            return bool(is_attention_active(runtime_epoch=runtime_epoch))
        except Exception:
            logger.exception("读取 Self-Reflection Attention 状态失败，保守禁止 barge-in")
            return True

    def _discard_projected_materials(self, input_identity: str) -> None:
        """清理未改道输入的临时材料索引，不释放普通请求真正使用的材料。

        Args:
            input_identity: 已确定继续走普通请求的 Final ASR 输入身份。
        """

        discard = getattr(self, "final_input_materials_discard_handler", None)
        if discard is None:
            return
        try:
            discard(input_identity)
        except Exception:
            logger.exception(
                "清理未改道 Final ASR 材料索引失败 turn_id=%s",
                input_identity,
            )

    def _release_diverted_materials(
        self,
        source_turn_id: str,
        *,
        remember_recent_materials: bool,
    ) -> None:
        """按输入是否成立登记并释放改道语音的未消费材料。

        Args:
            source_turn_id: 已进入未处理插话片段的原始语音 Turn 编号。
            remember_recent_materials: Episode 输入正式成立时为真；归属失败时为假。
        """

        release = getattr(self, "diverted_material_release_handler", None)
        if release is None:
            return
        try:
            release(source_turn_id, remember_recent_materials)
        except Exception:
            # 释放端口通常会把失败转成统一材料事件；这里不能重新创建普通请求。
            logger.exception(
                "Final ASR 改道后的材料释放失败 turn_id=%s",
                source_turn_id,
            )

    def _settle_diverted_assistant_handoff(
        self,
        *,
        request_id: str,
        now: float,
    ) -> None:
        """撤销 Final ASR 为普通请求预留的 processing 身份。

        ``apply_final_transcript`` 必须先发布带稳定 request_id 的用户事件，才能满足
        Desktop 的公开顺序；当 Attention 随后接纳该输入时，这个临时身份不能留下一个
        永远等不到输出结算的屏障。真正的 interruption reply 会使用自己的 request_id。

        Args:
            request_id: Final ASR 原本为普通请求预留的稳定请求编号。
            now: 产生收口事件时使用的单调时间。
        """

        finish = getattr(
            self.realtime_input_runtime,
            "apply_assistant_output_finished",
            None,
        )
        if not callable(finish):
            return
        try:
            events = finish(
                reason="input_diverted_to_episode",
                now=now,
                request_id=request_id,
            )
            self._enqueue_runtime_events(events, produced_at=now)
        except Exception:
            # 归属已经由 Attention 锁决定；这里只记录状态补偿异常，不能回退成普通请求。
            logger.exception(
                "Final ASR 改道后的 processing 收口失败 request_id=%s",
                request_id,
            )

    def _dispatch_final_asr_request(
        self,
        *,
        request_id: str,
        source_turn_id: str,
        final_text: str,
        requested_at: float,
        source_session_id: str,
        interaction_session_id: str,
        attention_lease: AssistantRequestAdmissionLeaseProtocol | None = None,
        raise_on_failure: bool = False,
    ) -> bool:
        """在顺序屏障清空后准备并提交一条 Final ASR 助手请求。

        Args:
            request_id: 当前语音请求编号。
            source_turn_id: 产生该请求的语音轮次编号。
            final_text: 已经完成质量过滤的用户正文。
            requested_at: Final ASR 收口时的单调时间。
            source_session_id: 语音输入所属的音频会话编号。
            interaction_session_id: 当前交互会话编号。
            attention_lease: Final ASR 从 Attention 单锁取得的 Bridge 提交资格。

        Returns:
            Bridge 接受请求时返回真；准备或提交失败时返回假。
        """

        prepared_context: PreparedAssistantRequestContext | None = None
        try:
            if self.assistant_request_context_preparer is None:
                raise RuntimeError("助手请求上下文准备器不可用")
            prepared_context = (
                self.assistant_request_context_preparer.prepare_final_asr_request_context(
                    request_id=request_id,
                    source_turn_id=source_turn_id,
                )
            )
            snapshot = prepared_context.snapshot
            request = AssistantRequest(
                request_id=request_id,
                source_session_id=source_session_id,
                interaction_session_id=interaction_session_id,
                character_card=snapshot.character_card,
                user_call_name=snapshot.user_call_name,
                user_self_intro=snapshot.user_self_intro,
                user_extra_notes=snapshot.user_extra_notes,
                user_text=final_text,
                source="final_asr",
                source_turn_id=source_turn_id,
                requested_at=requested_at,
                turn_material_context_id=snapshot.turn_material_context_id,
                foreground_context=snapshot.foreground_context,
                environment_snapshot=snapshot.environment_snapshot,
                frozen_visual_input=snapshot.frozen_visual_input,
                runtime_memory_snapshot=snapshot.runtime_memory_snapshot,
                history_policy=snapshot.history_policy,
                activity_progress_references=snapshot.activity_progress_references,
                derived_from_activity_ids=snapshot.derived_from_activity_ids,
            )
            if self.assistant_request_submitter is None:
                raise RuntimeError("助手请求提交端口不可用")
            request_submitted = self.assistant_request_submitter(request)
        except Exception as exception:
            if prepared_context is not None:
                prepared_context.rollback()
            if attention_lease is not None:
                attention_lease.rollback()
            failed_context_id = (
                prepared_context.snapshot.turn_material_context_id
                if prepared_context is not None
                else getattr(exception, "turn_material_context_id", None)
            )
            self._queue_final_asr_material_cleanup_event(
                request_id=request_id,
                source="final_asr",
                source_turn_id=source_turn_id,
                turn_material_context_id=(
                    failed_context_id if isinstance(failed_context_id, str) else None
                ),
            )
            logger.exception("Final ASR 请求准备或提交失败 request_id=%s", request_id)
            if raise_on_failure:
                raise
            return False

        if not request_submitted:
            prepared_context.rollback()
            if attention_lease is not None:
                attention_lease.rollback()
            self._queue_final_asr_material_cleanup_event(
                request_id=request.request_id,
                source=request.source,
                source_turn_id=request.source_turn_id,
                turn_material_context_id=request.turn_material_context_id,
            )
            logger.warning(
                "助手输出侧不可用，Final ASR 请求未提交 request_id=%s",
                request.request_id,
            )
            return False

        if attention_lease is not None and not attention_lease.commit_enqueued():
            logger.warning(
                "Final ASR 请求已入队但 Attention lease 未提交 request_id=%s",
                request.request_id,
            )

        try:
            committed = prepared_context.commit_enqueued()
            if not committed:
                logger.warning(
                    "Final ASR 请求已入队但 Notice lease 未提交 request_id=%s",
                    request.request_id,
                )
        except Exception:
            logger.exception(
                "Final ASR 请求已入队但 Notice lease 提交异常 request_id=%s",
                request.request_id,
            )
        material_commit = self.final_input_material_commit_handler
        if material_commit is not None and request.turn_material_context_id:
            try:
                material_commit(request.turn_material_context_id)
            except Exception:
                # 请求已经进入 Bridge，近期列表刷新失败只能记录诊断，不能回滚请求。
                logger.exception(
                    "Final ASR 请求已入队但近期材料登记失败 request_id=%s",
                    request.request_id,
                )
        self.debug_audio_recorder.record_assistant_request_enqueued(request)
        logger.info(
            "助手请求已入队 request_id=%s source_turn_id=%s interaction_session_id=%s text_length=%s",
            request.request_id,
            request.source_turn_id,
            request.interaction_session_id,
            len(request.user_text),
        )
        if self.latency_recorder is not None:
            self.latency_recorder.record_marker(
                trace_id=source_turn_id,
                marker="assistant_request_enqueued",
                at_wall=self.now(),
            )
        return True

    def _is_current_partial_result(self, result: PartialAsrResult) -> bool:
        """判断 Partial ASR 结果是否仍属于当前输入上下文。

        Args:
            result: Scheduler 已完成的 Partial ASR 结果。

        Returns:
            bool: 结果属于当前 Turn 且比最后已展示版本更新时返回 `True`。
        """

        # track 切换后旧输入音轨代际结果必须丢弃，避免旧音轨文本跨输入边界出现。
        if result.input_track_generation != self.active_input_track_generation:
            return False

        current_turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
        # 没有活跃输入 turn 时，任何 partial 都已经失去可见性归属，不能发布成无轮次事件。
        if current_turn_id is None:
            return False

        # turn_id 是用户语音轮次边界，final/reset 后迟到的 partial 不能落到新 turn 上。
        if result.turn_id != current_turn_id:
            return False

        # 只和最后已经展示的结果比较；未来快照虽然可能已经提交，但不能让当前结果因此被误丢弃。
        # 相同字节版本是重复结果，也不能再次触发插话或覆盖字幕。
        if result.byte_count <= self._last_partial_applied_byte_count:
            return False

        return True

    def _start_barge_in_candidate(self) -> None:
        """为当前用户 turn 创建插话候选。

        Args:
            无。

        Returns:
            None: 当前 turn 缺失时不创建候选。
        """

        turn_id = self.realtime_input_runtime.input_turn_session.current_turn_id
        if turn_id is None:
            return

        self._barge_in_candidate = BargeInCandidate(
            turn_id=turn_id,
            speaker_status=self._speaker_gate_status,
        )

    def _maybe_confirm_barge_in_after_speaker_update(self, *, now: float) -> None:
        """在有效文本已出现后，用新增语音补齐说话人 gate 并尝试确认插话。

        Args:
            now: 当前语音窗口的事实时间。

        Returns:
            None: 未满足插话条件时仅更新内部状态。
        """

        candidate = self._barge_in_candidate
        if candidate is None or not candidate.text_actionable_seen:
            return
        if candidate.speaker_status == "rejected":
            self._barge_in_candidate = None
            return
        if speaker_gate_allows_barge_in(candidate.speaker_status):
            self._try_confirm_barge_in(turn_id=candidate.turn_id, now=now)
            return

        self._update_speaker_gate_state(is_final=False)
        self._try_confirm_barge_in(turn_id=candidate.turn_id, now=now)

    def _confirm_barge_in_if_actionable_transcript(self, *, turn_id: str | None, text: str, now: float) -> None:
        """根据 ASR 文本质量记录候选插话文本事实。"""

        decision = classify_final_transcript(text)
        self._confirm_barge_in_if_actionable_decision(
            turn_id=turn_id,
            should_request_assistant=decision.should_request_assistant,
            now=now,
        )

    def _confirm_barge_in_if_actionable_decision(
        self,
        *,
        turn_id: str | None,
        should_request_assistant: bool,
        now: float,
    ) -> None:
        """记录有效文本事实，并在说话人 gate 通过后取消当前助手播报。"""

        if not should_request_assistant:
            return

        candidate = self._barge_in_candidate
        if candidate is None:
            return
        if turn_id != candidate.turn_id:
            return

        candidate.text_actionable_seen = True
        self._try_confirm_barge_in(turn_id=turn_id, now=now)

    def _try_confirm_barge_in(self, *, turn_id: str | None, now: float) -> None:
        """在文本和说话人 gate 都满足时发送插话取消命令。"""

        if self._self_reflection_attention_active():
            # Self-Reflection 的 Recovery 与正式正文不可被用户输入取消。最终文本仍会
            # 在 Final ASR 边界进入 episode，并由辅助 lane 决定是否需要回应。
            return

        candidate = self._barge_in_candidate
        if candidate is None:
            return

        if turn_id == candidate.turn_id and not self.realtime_input_runtime.interaction_state.assistant_output_state.playout_active:
            self._barge_in_candidate = None
            return

        if not can_confirm_barge_in(
            candidate,
            turn_id=turn_id,
            assistant_playout_active=self.realtime_input_runtime.interaction_state.assistant_output_state.playout_active,
        ):
            return

        candidate.cancel_sent = True
        # 输入侧已经不再读取输出侧 generation 上下文；插话事实统一表达为“取消当前活跃回复”。
        barge_in_events = self.realtime_input_runtime.apply_barge_in_detected(None, now=now)
        self._enqueue_runtime_events(barge_in_events, produced_at=now)

        # 取消真正交给 assistant runtime 执行；generation_id 为空表示由输出侧自行命中当前活跃 generation。
        self._enqueue_barge_in_cancel_command(generation_id=None, now=now)
        self._barge_in_candidate = None

    def _enqueue_barge_in_cancel_command(self, *, generation_id: str | None, now: float) -> None:
        """投递插话取消命令。"""

        # 控制命令必须保留插话事实时间；输出侧运行时会决定 None 目标命中哪个当前 generation。
        if self.assistant_control_submitter is None:
            return
        accepted = self.assistant_control_submitter(
            CancelCurrentGenerationCommand(
                generation_id=generation_id,
                reason="barge_in",
                issued_at=now,
            )
        )
        if not accepted:
            logger.warning("助手输出侧不可用，插话取消命令未提交")

    def _stage_pending_next_turn_speech_window(self, pcm16: bytes, *, now: float) -> None:
        """暂存 final ASR 运行期间到来的下一轮语音窗口。"""

        # 第一段暂存语音代表用户已经重新开口；记录开始时间供后续补发 turn started 事实。
        if not self._pending_next_turn_has_speech:
            self._pending_next_turn_started_at = now

        # 新语音属于下一轮用户输入，不能污染已经提交给 final ASR 的旧 turn snapshot。
        self._pending_next_turn_has_speech = True
        self._pending_next_turn_pcm16 += pcm16
        self._pending_next_turn_speech_pcm16_length += len(pcm16)
        self._pending_next_turn_speaker_pcm16 += pcm16
        # 语音窗口打断暂存区内的连续静音，保持与普通 current turn 的静音累计规则一致。
        self._pending_next_turn_silence_ms = 0.0

    def _stage_pending_next_turn_silence_window(self, pcm16: bytes, *, duration_ms: float) -> None:
        """暂存 final ASR 运行期间下一轮语音后的静音窗口。

        """

        # 没有暂存语音时，静音只是旧 turn finalizing 期间的环境尾音，不应生成下一轮输入。
        if not self._pending_next_turn_has_speech:
            return

        # 用户新语音后的静音也是下一轮 ASR 上下文的一部分，保留它能减少边界切割感。
        self._pending_next_turn_pcm16 += pcm16
        self._pending_next_turn_silence_ms += duration_ms

    def _promote_pending_next_turn_if_needed(self, *, promoted_at: float) -> None:
        """把 finalizing 期间暂存的新语音提升为当前 turn。"""

        # 纯静音或空暂存区不能升级为用户输入，否则前端会看到没有内容的新 turn。
        if not self._pending_next_turn_has_speech:
            self._reset_pending_next_turn_buffers()
            return

        # 先复制暂存内容，再清空暂存区，避免后续字段赋值过程中意外复用同一状态。
        pending_pcm16 = self._pending_next_turn_pcm16
        pending_speech_pcm16_length = self._pending_next_turn_speech_pcm16_length
        pending_speaker_pcm16 = self._pending_next_turn_speaker_pcm16
        pending_started_at = self._pending_next_turn_started_at or promoted_at
        pending_silence_ms = self._pending_next_turn_silence_ms
        self._reset_pending_next_turn_buffers()

        # 旧 turn 已经通过 apply_final_transcript 归档，此时输入 turn 状态机可以安全分配新 turn id。
        self._apply_user_turn_started(now=pending_started_at, produced_at=promoted_at)
        if self.realtime_input_runtime.interaction_state.assistant_output_state.playout_active:
            self._start_barge_in_candidate()

        # 暂存音频成为新 turn 的当前上下文，后续 ASR、Smart Turn 都从这里继续推进。
        self._current_turn_pcm16 = pending_pcm16
        self._current_turn_has_speech = True
        self._current_turn_speech_pcm16_length = pending_speech_pcm16_length
        self._current_turn_speaker_pcm16 = pending_speaker_pcm16
        self._silence_ms = pending_silence_ms

        # 如果暂存音频已经满足 partial ASR 节流条件，立即提交一次快照让字幕追上用户输入。
        promoted_at_ms = promoted_at * 1000
        if self._should_run_partial_asr(now_ms=promoted_at_ms):
            self._submit_partial_asr_snapshot(submitted_at=promoted_at, submitted_at_ms=promoted_at_ms)

    def _submit_partial_asr_snapshot(self, *, submitted_at: float, submitted_at_ms: float) -> None:
        """提交当前 Turn 的 Partial ASR 快照并同步节流游标。

        Args:
            submitted_at: 快照提交时的单调时钟秒数。
            submitted_at_ms: 同一提交时刻的毫秒值，用于后续节流。

        Returns:
            None: Scheduler 拒绝快照时保留原游标，等待后续音频重试。
        """

        # partial ASR 服务实时字幕和后续文本 latch；只有明确非主人时才停止继续提交。
        speaker_status = self._update_speaker_gate_state(is_final=False)
        if speaker_status == "rejected":
            logger.info("说话人确认：partial 跳过 check=%d", self._sv_check_count)
            return
        self._try_confirm_barge_in(
            turn_id=self.realtime_input_runtime.input_turn_session.current_turn_id,
            now=submitted_at,
        )

        # 只有 Scheduler 真正接纳快照时才推进提交游标；拒绝提交后保留节流资格，下一次音频可以重试。
        submitted = self.asr_scheduler.submit_partial(
            pcm16=self._current_turn_pcm16,
            turn_id=self.realtime_input_runtime.input_turn_session.current_turn_id,
            input_track_generation=self.active_input_track_generation,
            byte_count=len(self._current_turn_pcm16),
            submitted_at=submitted_at,
        )
        if not submitted:
            logger.debug(
                "partial ASR 快照未被 Scheduler 接纳 turn_id=%s byte_count=%s",
                self.realtime_input_runtime.input_turn_session.current_turn_id,
                len(self._current_turn_pcm16),
            )
            return

        self._last_partial_submitted_byte_count = len(self._current_turn_pcm16)
        self._last_partial_at_ms = submitted_at_ms

    def _reset_turn_buffers(self) -> None:
        """清空当前用户 turn 的本地音频与节流状态。 """

        self._clear_smart_turn_end_state(reason="turn_buffers_reset", now=self.now())
        self._clear_endpoint_eou_pending_state(reason="turn_buffers_reset")
        # 当前 turn PCM 已经完成 final ASR，必须清空以免下一轮用户语音混入旧上下文。
        self._current_turn_pcm16 = b""
        # 语音标记清零后，下一段静音会再次被视为环境静音而不是旧 turn 尾音。
        self._current_turn_has_speech = False
        # 有效人声长度随 turn 生命周期清零，避免短误触判断继承上一轮语音。
        self._current_turn_speech_pcm16_length = 0
        # 说话人确认有效人声缓存随 turn 清零，避免静音或上一轮语音污染下一轮匹配。
        self._current_turn_speaker_pcm16 = b""
        # 候选插话只属于当前 turn；turn 收束或重置后必须释放。
        self._barge_in_candidate = None
        # partial 游标重置保证下一轮 turn 按完整阈值重新开始节流。
        self._last_partial_submitted_byte_count = 0
        self._last_partial_applied_byte_count = 0
        self._last_partial_at_ms = None
        # 静音累计清零，避免新 turn 继承旧 turn 的尾部静音。
        self._silence_ms = 0.0
        # final 收束状态清空，允许下一轮用户 turn 正常开始。
        self._finalizing_turn_id = None
        self._finalizing_byte_count = 0
        # 说话人确认状态随 turn 清零，下一轮重新判定。
        self._sv_similarities = []
        self._sv_locked_pass = False
        self._sv_locked_reject = False
        self._sv_consecutive_low_count = 0
        self._sv_check_count = 0
        self._sv_last_checked_speech_pcm16_length = 0
        self._set_speaker_gate_status(
            initial_speaker_gate_status(
                speaker_verification_enabled=self.speaker_verification_provider is not None
            )
        )

    def _reset_pending_next_turn_buffers(self) -> None:
        """清空 finalizing 期间下一轮输入暂存区。"""

        # 暂存区只服务 final ASR 等待窗口；提升或输入重置后必须清空，避免跨 turn 复用旧音频。
        self._pending_next_turn_pcm16 = b""
        self._pending_next_turn_speech_pcm16_length = 0
        self._pending_next_turn_speaker_pcm16 = b""
        self._pending_next_turn_has_speech = False
        self._pending_next_turn_started_at = None
        self._pending_next_turn_silence_ms = 0.0

    def _reset_speech_start_candidate(self) -> None:
        """清空尚未确认的 VAD 开口候选状态。"""

        # 候选区只保存未达阈值的开口片段，静音、输入重置或确认提升后都必须清空。
        self._speech_start_candidate_pcm16 = b""
        self._speech_start_candidate_window_count = 0

    def _enqueue_runtime_events(self, events: list[dict], *, produced_at: float) -> None:
        """把 RealtimeInputRuntime 事件字典包装进 worker 事件队列。"""

        # RealtimeInputRuntime 统一返回字典事件，worker 负责边界模型转换，保持下游队列类型稳定。
        for event in events:
            self.event_queue.put(
                RuntimeEvent(
                    type=event["type"],
                    payload=event.get("payload", {}),
                    produced_at=produced_at,
                    source="realtime_input",
                )
            )

    def _enqueue_smart_turn_waiting_user_started(self, *, probability: float, produced_at: float) -> None:
        """发送 Smart Turn 低置信等待提示开始事件。"""

        self.event_queue.put(
            RuntimeEvent(
                type="input.smart_turn_waiting_user_started",
                payload={
                    **self.realtime_input_runtime.current_turn_identity_payload(),
                    "session_id": self.realtime_input_runtime.current_audio_session_id,
                    "probability": probability,
                    "hint_text": "当前我不太清楚您说完了么，如果说完了，可以说一声：我说完了~",
                },
                produced_at=produced_at,
                source="realtime_input",
            )
        )

    def _enqueue_smart_turn_waiting_user_ended(
        self,
        *,
        probability: float,
        reason: str,
        produced_at: float,
    ) -> None:
        """发送 Smart Turn 低置信等待提示结束事件。"""

        self.event_queue.put(
            RuntimeEvent(
                type="input.smart_turn_waiting_user_ended",
                payload={
                    **self.realtime_input_runtime.current_turn_identity_payload(),
                    "session_id": self.realtime_input_runtime.current_audio_session_id,
                    "probability": probability,
                    "reason": reason,
                },
                produced_at=produced_at,
                source="realtime_input",
            )
        )
