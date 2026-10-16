from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from queue import Empty, Full
from typing import Literal, Protocol

from livekit import rtc

from companion.activities.notice.models import (
    ActivityEventApplyResult,
    ActivityNoticeInterruptionStrategy,
    NoticeDeliveryCommand,
    NoticeDeliveryResult,
)
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplyResult,
    PendingAgentInteraction,
)
from companion.core.application.assistant_request_models import (
    AssistantRequestContextPreparerProtocol,
    ForegroundContext,
)
from companion.foreground_self_reflection.contracts import (
    AssistantRequestAdmissionLeaseProtocol,
    ForegroundAttentionAdmissionPort,
)
from companion.realtime.asr_scheduler import AsrScheduler
from companion.realtime.assistant_events import (
    AssistantControlCommand,
    CancelCurrentGenerationCommand,
    CancelSupersededSystemNoticeCommand,
    CancelTimedReminderNoticeCommand,
    AssistantRequest,
    AssistantRequestSource,
    RuntimeEvent,
)
from companion.realtime.assistant_process_client import AssistantProcessClient
from companion.realtime.assistant_audio_output import AssistantAudioOutputScheduler
from companion.realtime.assistant_process_bridge import (
    AssistantOutputUnavailableError,
    AssistantProcessBridge,
)
from companion.realtime.audio_runtime_thread import AudioRuntimeThread
from companion.realtime.audio_track_bridge import read_pcm16_from_audio_event
from companion.realtime.realtime_input_worker import (
    AudioControlCommand,
    AudioInputItem,
    RealtimeInputWorker,
    TimedReminderInputTakeover,
)
from companion.realtime.media_tracks import (
    AssistantAudioPublisher,
    create_assistant_audio_publisher,
)
from companion.realtime.notify_queue import NotifyQueue
from companion.realtime.runtime_event_service import (
    RuntimeEventService,
    RuntimeEventSessionBindings,
)
from companion.realtime.runtime_identity import (
    AssistantRequestIdentityRegistry,
    RuntimeIdentityFactory,
)
from companion.realtime.runtime_public_event_publisher import (
    RuntimePublicEventPublishers,
)
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.playout_delivery import PlayoutDeliveryRegistry
from companion.realtime.permission_acknowledgement import (
    permission_acknowledgement_request_id,
    permission_acknowledgement_text,
)
from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
from companion.realtime.system_notice_playout import SystemNoticePlayoutTracker
from companion.realtime.visible_output_journal import (
    VisibleOutputJournal,
    VisibleOutputSettlement,
)
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger
from companion.realtime.assistant_request_ownership import AssistantRequestOwnershipLedger
from companion.foreground_self_reflection.models import UnattendedMaterialReference
from companion.observability.logging_config import get_backend_logger
from companion.observability.realtime_latency import RealtimeLatencyRecorder
from companion.records.debug import DebugAudioRecorder, NoopDebugAudioRecorder
from companion.timed_reminders.delivery_coordinator import (
    TimedReminderDeliveryCoordinator,
)


logger = get_backend_logger(__name__)
SYSTEM_NOTICE_REQUEST_TEXT = (
    "现在轮到你主动对他说话。他没有发新消息，也没有人催你——"
    "上面 <system_notice_environment> 里的每条 notice，是你手头一件事刚刚发生的真实变化："
    "发生了什么、你之前已经对他说过哪些话、这次要怎么说，都写在对应字段里。"
    "照着每条 notice 的 response_instruction，用你自己的口吻把变化直接说给他听，"
    "就像事情有了结果、顺口跟他交代一声。你的整段输出只能是你对他说出口的话："
    "不要回应或重复 <visible_conversation_history> 里已经说过的旧话，"
    "不要说“我这就去办”“稍等”“回头告诉你”这类把事情推到将来的话，"
    "也不要承诺任何还没做的动作。"
)
TIMED_REMINDER_REQUEST_TEXT = (
    "现在请根据 <timed_reminder_notice_environment> 里的冻结事实，"
    "用当前角色的自然口吻完成这一次到点提醒。"
)


@dataclass(slots=True)
class _PendingPermissionAcknowledgement:
    """保存一条鼠标允许等待权威事实和旧播报结算的最小状态。

    Args:
        interaction: 已成功提交给原 Activity 的权限交互快照。
        waiting_notice_request_ids: 仍可能产生旧权限播报正文的请求编号。
        grant_confirmed: Gate 是否已经发布真实授权生效事实。
    """

    interaction: PendingAgentInteraction
    waiting_notice_request_ids: set[str]
    grant_confirmed: bool = False


def should_consume_livekit_audio_participant(
    identity: str | None,
    *,
    aec_identity_prefix: str = "desktop-aec",
) -> bool:
    """判断 LiveKit participant 是否是后端应消费的 AEC 音频来源。

    Args:
        identity: 远端参与者身份标识。
        aec_identity_prefix: 后端签发给 AEC sidecar 的身份前缀。

    Returns:
        身份有效且属于指定 AEC 来源时返回真。
    """

    # LiveKit SDK 在异常或测试替身场景下可能不给 identity；未知来源不能进入实时识别链路。
    if identity is None:
        return False

    # 去除首尾空白后再判断来源，兼容控制台、配置或测试里意外带空格的身份字符串。
    normalized_identity = identity.strip()
    if not normalized_identity:
        return False

    # AEC 前缀来自可配置 Settings；空白前缀无法区分来源，必须拒绝而不是退化成宽松匹配。
    normalized_aec_identity_prefix = aec_identity_prefix.strip()
    if not normalized_aec_identity_prefix:
        return False

    # 只有配置前缀签发的 AEC sidecar 才发布回声消除后的 clean mic，renderer 与 Backend 媒体会话音频必须隔离。
    return normalized_identity.startswith(f"{normalized_aec_identity_prefix}-")


class LiveKitMediaSessionLike(Protocol):
    """LiveKit Media Session Controller 可启动对象协议。"""

    async def start(
        self, *, url: str, token: str, identity: str, source_session_id: str
    ) -> None:
        """启动媒体会话并进入目标 LiveKit room。

        Args:
            url: LiveKit 服务地址。
            token: 进入目标房间的访问令牌。
            identity: Backend 媒体会话的参与者身份。
            source_session_id: 当前媒体输入来源会话编号。
        """
        ...

    async def close(self) -> None:
        """关闭媒体会话持有的房间连接与后台资源。"""

        ...

    def enqueue_assistant_request(self, request: AssistantRequest) -> bool:
        """把助手请求交给当前媒体会话的 Bridge 队列。

        Args:
            request: 需要跨进程提交的助手请求。

        Returns:
            请求成功进入 bridge 队列时返回真。
        """

        ...

    def enqueue_timed_reminder_notice(self) -> bool:
        """尝试冻结并提交当前全部可响的 Reminder Notice。"""

        ...

    def assistant_output_available(self) -> bool:
        """返回当前会话是否还能接收新的助手输出请求。"""

        ...

    def interrupt_current_output(self, *, issued_at: float) -> tuple[str, ...]:
        """停止当前语音输出，返回覆盖的请求编号。

        Args:
            issued_at: 用户打断意图的单调时间。

        Returns:
            被取消的请求编号，不改变后台 Activity。
        """
        ...

    def enqueue_assistant_control(self, command: AssistantControlCommand) -> bool:
        """把助手控制命令交给当前媒体会话的 Bridge 队列。

        Args:
            command: 需要跨进程提交的助手控制命令。

        Returns:
            命令成功进入 bridge 队列时返回真。
        """

        ...

    def handle_permission_card_decision(
        self,
        interaction: PendingAgentInteraction,
        outcome: Literal["allow", "deny"],
    ) -> None:
        """停止对应权限播报，并登记鼠标允许的后续短确认。

        Args:
            interaction: 已经成功提交给原 Activity 的权限交互快照。
            outcome: 用户点击的允许或拒绝决定。
        """

        ...

    def interrupt_runtime_activities(
        self,
        runtime_epoch: int,
    ) -> list[dict[str, object]]:
        """关闭指定运行时世代的提醒与 TaskResult 交付状态。

        Args:
            runtime_epoch: 已失效的助手运行时世代。

        Returns:
            旧 TaskResult 材料释放产生的瞬时事件。
        """

        ...


class TurnAdmissionSequencerLike(Protocol):
    """LiveKit 媒体会话使用的最小 Turn 准入端口。"""

    def hold_for_voice(self, turn_id: str) -> None:
        """在 VAD 确认开口后阻止待发文本派发。

        Args:
            turn_id: 当前已经确认开口的语音轮次编号。
        """

        ...

    def resolve_voice_turn(
        self,
        *,
        turn_id: str,
        assistant_request_id: str | None,
        dispatch_request: Callable[[], bool] | None = None,
    ) -> None:
        """按 Final ASR 有效性暂停文本或解除 hold。

        Args:
            turn_id: Final ASR 对应的语音轮次编号。
            assistant_request_id: 有效语音的助手请求编号；无效语音为空。
            dispatch_request: 顺序屏障清空后执行的语音助手请求提交回调。
        """

        ...

    def reserve_system_notice(self, request_id: str) -> bool:
        """原子预留独立 System Notice 的提交位置。"""

        ...

    def reserve_timed_reminder(self, request_id: str) -> bool:
        """预留可以抢占普通 Foreground 的定时提醒提交位置。"""

        ...

    def submit_reserved_system_notice(
        self,
        request_id: str,
        submit_request: Callable[[], bool],
    ) -> bool:
        """确认 reservation 仍有效并在同一边界提交 System Notice。"""

        ...

    def submit_reserved_timed_reminder(
        self,
        request_id: str,
        submit_request: Callable[[], bool],
    ) -> bool:
        """确认预留仍有效并提交定时提醒请求。"""

        ...

    def release_notice_reservation(self, request_id: str) -> None:
        """释放尚未进入 Bridge 的 System Notice 或 Reminder Notice reservation。"""

        ...

    def register_assistant_request(
        self,
        request_id: str,
        source: AssistantRequestSource,
    ) -> None:
        """登记已经进入 Bridge 的所有来源请求。

        Args:
            request_id: 已由 Bridge 接管的助手请求编号。
            source: 文本、语音或 System Notice 来源。
        """

        ...

    def is_request_unsettled(self, request_id: str) -> bool:
        """判断指定助手请求是否仍在等待可见输出结算。

        Args:
            request_id: 需要查询的助手请求编号。

        Returns:
            请求仍占用 Turn 顺序屏障时返回真。
        """

        ...

    def handle_request_submission_failed(self, request_id: str) -> None:
        """释放未能进入 Bridge 的预登记语音屏障。

        Args:
            request_id: 构造或提交失败的语音助手请求编号。
        """

        ...

    def handle_output_settled(
        self,
        settlement: VisibleOutputSettlement,
    ) -> None:
        """在可见输出归档后释放请求屏障。

        Args:
            settlement: 已完成正确账本归档的输出结算事实。
        """

        ...

    def allows_system_notice(self) -> bool:
        """判断当前输入侧是否允许独立 Notice 抢占空档。

        Returns:
            没有待发文本或语音 hold 时返回真。
        """

        ...

class LiveKitMediaSession:
    """拥有一间 LiveKit 房间内的实时输入、助手输出和主进程控制事件编排。

    该对象是媒体会话的主进程所有者，而不是 LLM 或 Activity 的状态所有者。它把三条
    独立链路接在同一房间生命周期内：

    - 上行音频：AEC clean mic → ``audio_queue`` → ``RealtimeInputWorker``；
    - 助手输出：最终用户输入 → ``AssistantProcessBridge`` → 子进程 LLM/TTS → LiveKit 音轨；
    - 控制事件：Bridge/输入 Worker → 进程级 ``event_queue`` → ``RuntimeEventService`` → Router。

    它也是 Activity Notice 的媒体会话包装器：Coordinator 返回“某些 System Notice 已
    过期”的结果后，只有本类同时持有 Bridge 队列和控制队列，能够移除尚未桥接的请求或
    向已桥接请求发送条件取消命令。故 Router 和 Activity Handler 只交出活动事实，不直接
    操作 System Notice 请求。
    """

    def __init__(
        self,
        *,
        realtime_input_runtime: RealtimeInputRuntime,
        public_event_publishers: RuntimePublicEventPublishers,
        assistant_runtime: AssistantProcessClient | None,
        assistant_output_enabled: bool = True,
        assistant_audio_publisher: AssistantAudioPublisher | None = None,
        assistant_audio_track_factory: Callable[
            [], tuple[AssistantAudioPublisher, object]
        ] = create_assistant_audio_publisher,
        assistant_audio_output_scheduler: AssistantAudioOutputScheduler | None = None,
        room: object | None = None,
        latency_recorder: RealtimeLatencyRecorder | None = None,
        aec_participant_identity_prefix: str = "desktop-aec",
        assistant_request_handoff_timeout_sec: float = 1.0,
        event_queue: NotifyQueue[RuntimeEvent] | None = None,
        assistant_request_context_preparer: AssistantRequestContextPreparerProtocol | None = None,
        pending_notice_provider: Callable[[str], bool] | None = None,
        timed_reminder_delivery: TimedReminderDeliveryCoordinator | None = None,
        activity_notice_handler: Callable[
            [str, dict[str, object], int],
            ActivityEventApplyResult | None,
        ]
        | None = None,
        background_agent_interaction_event_handler: Callable[
            [str, dict[str, object], int], AgentInteractionEventApplyResult
        ]
        | None = None,
        background_agent_interaction_interruption_handler: Callable[
            [int], list[dict[str, object]]
        ]
        | None = None,
        background_agent_usage_handler: Callable[[str, dict[str, object]], None]
        | None = None,
        activity_apply_handler: Callable[
            [str, dict[str, object], int], ActivityApplyResult
        ]
        | None = None,
        activity_broadcast_handler: Callable[[ActivityProjection], Awaitable[object]]
        | None = None,
        current_runtime_epoch: Callable[[], int] | None = None,
        activity_interruption_handler: Callable[[int, str], Awaitable[object]] | None = None,
        activity_notice_interruption_handler: Callable[
            [int, ActivityNoticeInterruptionStrategy], ActivityEventApplyResult | None
        ]
        | None = None,
        notice_delivery_handler: Callable[
            [NoticeDeliveryCommand], NoticeDeliveryResult | None
        ]
        | None = None,
        turn_material_retain_handler: Callable[[str], bool] | None = None,
        turn_material_final_handler: Callable[[str], list[dict[str, object]]]
        | None = None,
        turn_material_release_handler: Callable[[str], list[dict[str, object]]]
        | None = None,
        playout_delivery_registry: PlayoutDeliveryRegistry | None = None,
        task_result_delivery_coordinator: TaskResultDeliveryCoordinator | None = None,
        system_notice_playout_tracker: SystemNoticePlayoutTracker | None = None,
        activity_progress_ledger: ActivityProgressLedger | None = None,
        turn_admission_sequencer: TurnAdmissionSequencerLike | None = None,
        runtime_identity_factory: RuntimeIdentityFactory | None = None,
        assistant_request_identity_registry: AssistantRequestIdentityRegistry | None = None,
        visible_output_journal: VisibleOutputJournal | None = None,
        assistant_request_ownership_ledger: AssistantRequestOwnershipLedger | None = None,
        assistant_output_health_ledger: AssistantOutputHealthLedger | None = None,
        runtime_event_service: RuntimeEventService | None = None,
        input_diversion_port: ForegroundAttentionAdmissionPort | None = None,
        final_input_materials_provider: Callable[[str], tuple[UnattendedMaterialReference, ...]] | None = None,
        final_input_materials_discard_handler: Callable[[str], object] | None = None,
        final_input_material_commit_handler: Callable[[str], object] | None = None,
        diverted_material_release_handler: Callable[[str, bool], object] | None = None,
    ) -> None:
        """初始化 LiveKit 实时媒体会话。

        Args:
            realtime_input_runtime (RealtimeInputRuntime): 持有 ASR、VAD、唤醒词与智能打断等实时能力提供者的运行时管理器。
            public_event_publishers: 按 Interaction、Assistant Output、Activity 和 Materials
                职责绑定的公共事件发布端口。
            assistant_runtime (AssistantProcessClient | None): 助手运行时客户端，输出侧启用时负责承接最终识别文本并产出 LLM、TTS 事件。
            assistant_output_enabled (bool, optional): 是否启用 LLM/TTS 输出侧；关闭时只运行输入链路。默认为 True。
            assistant_audio_publisher (AssistantAudioPublisher | None, optional): 可选的助手音频发布器测试替身；未提供时由音频轨工厂和调度器接管真实发布流程。默认为 None。
            assistant_audio_track_factory (Callable[[], tuple[AssistantAudioPublisher, object]], optional): 创建助手本地音频发布器与音轨对象的工厂函数，通常在首次需要发布下行语音时延迟调用。默认为 create_assistant_audio_publisher。
            assistant_audio_output_scheduler (AssistantAudioOutputScheduler | None, optional): 可选的助手下行音频调度器；输出侧启用且未提供时在构造阶段创建默认实现。默认为 None。
            room (object | None, optional): 可选的 LiveKit room 实例或测试替身；未提供时创建真实 `rtc.Room`。默认为 None。
            latency_recorder (RealtimeLatencyRecorder | None, optional): 可选本地耗时记录器；为 None 时按 realtime_audio.yaml 的 latency 开关决定是否创建实例。默认为 None。
            aec_participant_identity_prefix (str, optional): 合法 AEC sidecar participant 的身份前缀。默认为 "desktop-aec"。
            assistant_request_handoff_timeout_sec: 媒体会话关闭或断连时等待旧世代请求终态的确认窗口秒数。
            event_queue: Backend 进程级运行时事件入口；媒体会话重建时继续复用同一队列。
            assistant_request_context_preparer: 冻结助手请求事实并管理提醒事务的端口。
            pending_notice_provider: 判断当前交互会话是否存在待预留提醒的轻量回调。
            timed_reminder_delivery: 到点批次与 Foreground 请求的进程级交付账本。
            activity_notice_handler: 活动事实写入 Notice 账本的同步处理器。生产环境由
                ``AssistantRequestContextPreparer.handle_activity_event`` 提供：它映射来源
                事件并调用 ``NoticeDeliveryCoordinator.apply_event``。本会话随后消费其返回
                的 supersession 结果，取消已过期的 System Notice 请求。
            background_agent_interaction_event_handler: 后台智能体待答请求的登记与收口端口。
            background_agent_interaction_interruption_handler: 作废失效世代待答请求的端口。
            background_agent_usage_handler: 保存并从公共事件中剥离后台工作用量的端口。
            activity_apply_handler: Activity 来源事件同步应用端口。
            activity_broadcast_handler: Activity 投影异步广播端口。
            current_runtime_epoch: 读取当前已发布助手运行时世代的端口。
            activity_interruption_handler: 助手运行时失效中断端口。
            activity_notice_interruption_handler: 活动提醒整体中断端口。生产环境由
                ``NoticeDeliveryCoordinator.interrupt_active`` 提供。
            notice_delivery_handler: 提醒批次交付结果同步处理器。生产环境由
                ``ApplicationContainer`` 注入当前会话共享的
                ``NoticeDeliveryCoordinator.apply_delivery``：本会话在 Bridge 入队结果与
                过期请求清理时直接调用它；Runtime Event Router 也经
                ``AssistantOutputEventHandler`` 和 ``AssistantRequestFailureHandler`` 把
                输出文本、音频终态、用户打断、输出不可用等事件映射为
                ``NoticeDeliveryCommand`` 后调用它。该方法是已预留批次进入已入队、
                已交付、重排或丢弃状态的唯一写入口。
            turn_material_retain_handler: 异步任务材料租约取得函数。
            turn_material_final_handler: 本轮材料终态清理函数。
            turn_material_release_handler: 异步任务材料租约释放函数。
            playout_delivery_registry: 当前 Backend 进程内的语音播放交付账本。
            task_result_delivery_coordinator: 当前 Backend 进程内的结果卡交付账本。
            system_notice_playout_tracker: 当前媒体会话内权限提醒与语音请求的短期映射。
            activity_progress_ledger: Backend 进程级 Activity Progress 临时账本。
            turn_admission_sequencer: Backend 主进程唯一的 Turn 准入与顺序屏障。
            runtime_identity_factory: Backend 主进程共享的运行期身份工厂。
            assistant_request_identity_registry: Backend 进程级请求身份永久登记表。
            visible_output_journal: Backend 进程级可见输出与永久墓碑账本。
            runtime_event_service: Backend 进程唯一的事件消费者；会话只注册媒体操作端口。
            input_diversion_port: Attention active 时接纳 Final ASR 的主进程端口。
            current_runtime_epoch: 读取当前助手运行时世代的函数。
            final_input_materials_provider: 提供安全材料引用的端口。
            final_input_materials_discard_handler: 普通 Final ASR 未改道时清理材料快照索引的端口。
            final_input_material_commit_handler: 普通 Final ASR 请求成功提交后的近期材料登记端口。
            diverted_material_release_handler: 按 Episode 是否成立登记并释放改道材料的端口。

        """

        self.realtime_input_runtime = realtime_input_runtime
        self.runtime_identity_factory = runtime_identity_factory or RuntimeIdentityFactory()
        self.assistant_request_identity_registry = (
            assistant_request_identity_registry or AssistantRequestIdentityRegistry()
        )
        self.visible_output_journal = visible_output_journal or VisibleOutputJournal()
        self.assistant_request_ownership_ledger = (
            assistant_request_ownership_ledger or AssistantRequestOwnershipLedger()
        )
        self.assistant_output_health_ledger = (
            assistant_output_health_ledger or AssistantOutputHealthLedger()
        )
        self.runtime_event_service = runtime_event_service
        self._runtime_event_session_bindings: RuntimeEventSessionBindings | None = None
        if assistant_request_handoff_timeout_sec <= 0:
            raise ValueError("assistant_request_handoff_timeout_sec 必须大于零")
        self.assistant_request_handoff_timeout_sec = (
            assistant_request_handoff_timeout_sec
        )
        self.assistant_output_enabled = assistant_output_enabled

        if self.assistant_output_enabled and assistant_runtime is None:
            raise ValueError(
                "assistant_output_enabled=True 时必须提供 assistant_runtime"
            )

        # 输出侧依赖只在启用助手语音时存在，input-only 模式不应误启动 LLM/TTS 链路。
        self.assistant_runtime = assistant_runtime
        self.assistant_audio_publisher = assistant_audio_publisher
        self.assistant_audio_track_factory = assistant_audio_track_factory
        self.assistant_audio_output_scheduler = assistant_audio_output_scheduler

        # 本地助手音轨与发布标志共同保证一个 room 只发布一条下行语音轨。
        self._assistant_audio_track: object | None = None
        self._assistant_audio_track_published = False

        self.public_event_publishers = public_event_publishers

        # latency 记录器由 realtime_audio.yaml 控制：关闭时不创建实例，下游消费方均已按 None 判空，热路径零开销。
        latency_config = self.realtime_input_runtime.realtime_audio_config.latency
        if latency_recorder is not None:
            self.latency_recorder = latency_recorder
        elif latency_config.enabled:
            self.latency_recorder = RealtimeLatencyRecorder(
                jsonl_path=latency_config.output_path
            )
        else:
            self.latency_recorder = None
        # AEC 身份前缀与 session service 的签发规则共享，订阅边界只接收合法 clean mic 来源。
        self.aec_participant_identity_prefix = aec_participant_identity_prefix
        # 请求准备器在每次入队前冻结配置、材料、环境和提醒事实。
        self.assistant_request_context_preparer = assistant_request_context_preparer
        self.pending_notice_provider = pending_notice_provider
        self.timed_reminder_delivery = timed_reminder_delivery
        # 这是 Coordinator 写入端口；会话包装器负责消费其返回的 supersession 结果。
        self.activity_notice_handler = activity_notice_handler
        self.background_agent_interaction_event_handler = (
            background_agent_interaction_event_handler
        )
        self.background_agent_interaction_interruption_handler = (
            background_agent_interaction_interruption_handler
        )
        self.background_agent_usage_handler = background_agent_usage_handler
        self.activity_apply_handler = activity_apply_handler
        self.activity_broadcast_handler = activity_broadcast_handler
        self.current_runtime_epoch = current_runtime_epoch
        self.foreground_attention_admission_port = input_diversion_port
        self.activity_interruption_handler = activity_interruption_handler
        self.activity_notice_interruption_handler = activity_notice_interruption_handler
        self.notice_delivery_handler = notice_delivery_handler
        self.playout_delivery_registry = playout_delivery_registry or PlayoutDeliveryRegistry()
        self.system_notice_playout_tracker = (
            system_notice_playout_tracker or SystemNoticePlayoutTracker()
        )
        self.task_result_delivery_coordinator = (
            task_result_delivery_coordinator
            or TaskResultDeliveryCoordinator(
                retain_material=turn_material_retain_handler,
                release_material=turn_material_release_handler,
            )
        )
        self.activity_progress_ledger = (
            activity_progress_ledger or ActivityProgressLedger()
        )
        self.turn_admission_sequencer = turn_admission_sequencer
        # 这里只保存已经成功提交的鼠标允许，生命周期止于 Gate 事实、旧播报 settlement
        # 或用户轮抢占；它不是通用输出队列。
        self._pending_permission_acknowledgements: dict[
            str,
            _PendingPermissionAcknowledgement,
        ] = {}

        # room 默认使用真实 LiveKit SDK；测试可注入 fake room，避免构造阶段依赖事件循环或 FFI。
        self.room = room or rtc.Room()
        # 单音轨消费任务通过 stop_event 协作退出，锁用于串行化并发订阅和切轨。
        self._audio_task: asyncio.Task | None = None
        self._audio_stop_event: asyncio.Event | None = None
        self._audio_task_lock = asyncio.Lock()

        # 受监督任务集合负责保活裸 task 并统一收口异常，正常取消不进入错误列表。
        self._background_tasks: set[asyncio.Task] = set()
        self.background_task_errors: list[BaseException] = []
        # 三个队列隔离不同节奏：音频帧必须可丢弃，输入控制必须及时，运行时事件必须按顺序
        # 交给 Router。event_queue 是 Backend 进程级入口，媒体会话销毁不影响唯一 Drainer 消费。
        self.audio_queue: NotifyQueue[AudioInputItem] = NotifyQueue(maxsize=300)
        self.control_queue: NotifyQueue[AudioControlCommand] = NotifyQueue()
        self.event_queue: NotifyQueue[RuntimeEvent] = event_queue or NotifyQueue()

        if (
            self.assistant_output_enabled
            and self.assistant_audio_output_scheduler is None
        ):
            self.assistant_audio_output_scheduler = AssistantAudioOutputScheduler(
                event_queue=self.event_queue,
                assistant_audio_track_factory=self.assistant_audio_track_factory,
                latency_recorder=self.latency_recorder,
                system_notice_playout_tracker=self.system_notice_playout_tracker,
            )
        bind_audio_finished = getattr(
            self.assistant_audio_output_scheduler,
            "bind_audio_finished_handler",
            None,
        )
        if callable(bind_audio_finished):
            bind_audio_finished(
                (
                    self.timed_reminder_delivery.observe_audio_finished
                    if self.timed_reminder_delivery is not None
                    else None
                )
            )

        # asr_scheduler 独立管理后台 partial ASR，避免慢 ASR 阻塞 realtime_input_worker 消费音频队列。
        self.asr_scheduler = AsrScheduler(
            asr_provider=self.realtime_input_runtime.asr_provider,
            text_eou_provider=getattr(self.realtime_input_runtime, "text_eou_provider", None),
        )

        # 调试音频录音器只在 realtime_audio.yaml 显式开启时写本地 WAV；关闭时使用 no-op 保持热路径简单。
        realtime_audio_config = self.realtime_input_runtime.realtime_audio_config
        if realtime_audio_config.debug_audio.enabled:
            debug_audio_recorder = DebugAudioRecorder(
                enabled=True,
                output_dir=realtime_audio_config.debug_audio.output_dir,
                session_id_provider=lambda: (
                    self.realtime_input_runtime.current_audio_session_id
                ),
                max_session_files=realtime_audio_config.debug_audio.max_session_files,
            )
        else:
            debug_audio_recorder = NoopDebugAudioRecorder()

        self.assistant_process_bridge: AssistantProcessBridge | None = None
        if self.assistant_output_enabled:
            if (
                self.assistant_runtime is None
                or self.assistant_audio_output_scheduler is None
            ):
                raise RuntimeError(
                    "助手输出侧依赖缺失，无法启动 assistant runtime bridge"
                )
            # 请求与控制队列只交给 Bridge 持有，所有生产者统一经过其健康提交端口。
            self.assistant_process_bridge = AssistantProcessBridge(
                assistant_runtime=self.assistant_runtime,
                assistant_request_queue=NotifyQueue(),
                assistant_control_queue=NotifyQueue(),
                event_queue=self.event_queue,
                audio_output_scheduler=self.assistant_audio_output_scheduler,
                system_notice_playout_tracker=self.system_notice_playout_tracker,
                request_committed_handler=self._handle_assistant_request_committed,
                request_identity_registry=self.assistant_request_identity_registry,
                request_ownership_ledger=self.assistant_request_ownership_ledger,
                output_health_ledger=self.assistant_output_health_ledger,
            )

        # realtime_input_worker 消费输入音频和控制命令，只通过 Bridge 端口提交助手工作。
        self.realtime_input_worker = RealtimeInputWorker(
            realtime_input_runtime=self.realtime_input_runtime,
            vad_provider=self.realtime_input_runtime.vad_provider,
            smart_turn_provider=self.realtime_input_runtime.smart_turn_provider,
            speaker_verification_provider=getattr(
                self.realtime_input_runtime, "speaker_verification_provider", None
            ),
            realtime_audio_config=self.realtime_input_runtime.realtime_audio_config,
            audio_queue=self.audio_queue,
            control_queue=self.control_queue,
            event_queue=self.event_queue,
            assistant_request_submitter=(
                self.enqueue_assistant_request
                if self.assistant_process_bridge is not None
                else None
            ),
            assistant_control_submitter=(
                self.assistant_process_bridge.submit_control
                if self.assistant_process_bridge is not None
                else None
            ),
            now=time.monotonic,
            latency_recorder=self.latency_recorder,
            asr_scheduler=self.asr_scheduler,
            debug_audio_recorder=debug_audio_recorder,
            assistant_output_enabled=self.assistant_output_enabled,
            assistant_request_context_preparer=self.assistant_request_context_preparer,
            voice_turn_started_handler=(
                self.turn_admission_sequencer.hold_for_voice
                if self.turn_admission_sequencer is not None
                else None
            ),
            voice_turn_resolved_handler=(
                lambda turn_id, request_id, dispatch_request=None: (
                    self.turn_admission_sequencer.resolve_voice_turn(
                        turn_id=turn_id,
                        assistant_request_id=request_id,
                        dispatch_request=dispatch_request,
                    )
                )
                if self.turn_admission_sequencer is not None
                else None
            ),
            assistant_request_submission_failed_handler=(
                self.turn_admission_sequencer.handle_request_submission_failed
                if self.turn_admission_sequencer is not None
                else None
            ),
            runtime_identity_factory=self.runtime_identity_factory,
            input_diversion_port=input_diversion_port,
            current_runtime_epoch=current_runtime_epoch,
            final_input_materials_provider=final_input_materials_provider,
            final_input_materials_discard_handler=final_input_materials_discard_handler,
            final_input_material_commit_handler=final_input_material_commit_handler,
            diverted_material_release_handler=diverted_material_release_handler,
        )
        self.realtime_input_runtime.bind_realtime_input_worker(self.realtime_input_worker)
        # audio_runtime_thread 在独立事件循环线程中推进 realtime_input_worker，让 LiveKit 主事件循环只负责媒体入队。
        self.audio_runtime_thread = AudioRuntimeThread(worker=self.realtime_input_worker)

        self._input_track_generation = 0
        # 输入轨代际、帧序号和丢帧计数共同支持切轨隔离与实时性观测。
        self._input_frame_sequence_no = 0
        self.dropped_input_frames_due_to_queue_full = 0
        # _room_disconnect_reset_sent 防止 SDK 重复 disconnected 回调投递重复断连命令。
        self._room_disconnect_reset_sent = False
        # SDK 彻底断开房间时的通知回调，由 Controller 在创建会话后注入；
        # 会话自身不持有 Controller 引用，避免双向依赖。
        self._on_final_disconnect_callback: Callable[[], None] | None = None

    def set_on_final_disconnect(self, callback: Callable[[], None] | None) -> None:
        """注册或替换 LiveKit SDK 最终断连时要执行的通知回调。

        Args:
            callback: 最终断连通知；传空清除已注册回调。
        """

        self._on_final_disconnect_callback = callback

    async def start(
        self, *, url: str, token: str, identity: str, source_session_id: str
    ) -> None:
        """连接 LiveKit room 并注册事件处理器。

        Args:
            url: LiveKit 服务地址。
            token: 进入目标房间的访问令牌。
            identity: Backend 媒体会话的参与者身份。
            source_session_id: 当前媒体输入来源会话编号。
        """

        # LiveKit room 是当前实时媒体会话边界，必须在任何运行时事件产生前绑定到 RealtimeInputRuntime。
        self.realtime_input_runtime.bind_audio_session(source_session_id)

        # 事件处理器必须在 connect 前注册，避免刚入房时已有远端 track 的订阅事件丢失。
        self._setup_event_handlers()
        await self.room.connect(
            url, token, options=rtc.RoomOptions(auto_subscribe=True)
        )
        # 输出侧启用时才发布助手音轨；input-only 模式只订阅用户 clean mic 输入。
        if self.assistant_output_enabled:
            await self._publish_assistant_audio_track_once()
        if self.runtime_event_service is not None:
            self._runtime_event_session_bindings = RuntimeEventSessionBindings(
                wake_response_audio_output=self.assistant_audio_output_scheduler,
                foreground_notice_handler=self._enqueue_system_notice_request,
                activity_notice_handler=(
                    self._handle_activity_event
                    if self.activity_notice_handler is not None
                    else None
                ),
                activity_notice_interruption_handler=self.interrupt_activity_notices,
                output_available=self._assistant_output_available,
                task_memory_lookup_result_handler=(
                    self.assistant_process_bridge.submit_control
                    if self.assistant_process_bridge is not None
                    else None
                ),
                runtime_memory_compaction_plan_handler=(
                    self.assistant_process_bridge.submit_control
                    if self.assistant_process_bridge is not None
                    else None
                ),
                progress_acknowledgement_handler=(
                    self.assistant_process_bridge.submit_control
                    if self.assistant_process_bridge is not None
                    else None
                ),
                identity_conflict_handler=(
                    self.assistant_process_bridge.fail_for_identity_conflict
                    if self.assistant_process_bridge is not None
                    else None
                ),
                output_settled_handler=self._handle_visible_output_settled,
            )
            self.runtime_event_service.bind_session(self._runtime_event_session_bindings)
            await self.runtime_event_service.start()
        self._start_runtime_tasks()
        # Reminder 可能在媒体链路尚未建立时已经到点。会话完全可用后重新开放请求，
        # 才能让关闭旧会话时暂停的批次进入新的 Bridge。
        if self.timed_reminder_delivery is not None:
            self.timed_reminder_delivery.resume_requests()

    async def _publish_assistant_audio_track_once(self) -> None:
        """发布助手本地音频轨。"""

        # input-only 模式没有助手下行音频，私有方法被误调用时也保持安全空操作。
        if not self.assistant_output_enabled:
            return

        # 已发布过时不重复 publish，避免 desktop 收到多条含相同助手语音的轨道。
        if self._assistant_audio_track_published:
            return

        # 没有现成 track 时启动专用音频输出调度器并取得它在线程内创建的本地音频轨。
        if self._assistant_audio_track is None:
            if self.assistant_audio_output_scheduler is None:
                raise RuntimeError("助手音频输出调度器缺失，无法发布助手音轨")
            self._assistant_audio_track = self.assistant_audio_output_scheduler.start()

        # 音轨对象由调度线程持有，发布完成后才允许后续音频块进入 room。
        await self.room.local_participant.publish_track(
            self._assistant_audio_track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        self._assistant_audio_track_published = True
        logger.info(
            "Published assistant LiveKit audio track track_name=assistant-audio identity=%s",
            getattr(self.room.local_participant, "identity", "unknown"),
        )

    def _assistant_output_available(self) -> bool:
        """读取 Bridge 持有的唯一输出健康状态。

        Returns:
            当前 Bridge 能接收新工作时返回真。
        """

        bridge = self.assistant_process_bridge
        return bridge is not None and bridge.is_output_available()

    def assistant_output_available(self) -> bool:
        """返回当前媒体会话是否具备真实助手输出能力。"""

        return self._assistant_output_available()

    def interrupt_current_output(self, *, issued_at: float) -> tuple[str, ...]:
        """停止当前角色已经提交的回复和唤醒短答。

        Args:
            issued_at: 快捷键取消意图形成的单调时间。

        Returns:
            被打断的请求编号；没有助手 Bridge 时只清理已有音频。
        """

        bridge = self.assistant_process_bridge
        if bridge is None:
            scheduler = self.assistant_audio_output_scheduler
            return scheduler.clear_pending_audio(interrupt_before=issued_at) if scheduler is not None else ()
        return bridge.interrupt_current_output(
            issued_at=issued_at,
            output_identity=self.realtime_input_runtime.interaction_state.current_assistant_output_identity(),
        )

    def _handle_assistant_request_committed(self, request: AssistantRequest) -> None:
        """在 Bridge 接收请求后登记所有来源共用的助手输出身份。

        Args:
            request: 已进入 Bridge 请求队列的助手请求。

        Returns:
            无返回值；登记输出身份并将当前交互快照交给进程级事件队列。
        """

        # 请求已经越过 Bridge 提交边界。顺序屏障是后续输入正确性的硬约束，必须
        # 先独立登记，不能被运行期记忆或产品状态的旁路异常一起跳过。
        if self.turn_admission_sequencer is not None:
            try:
                self.turn_admission_sequencer.register_assistant_request(
                    request.request_id,
                    request.source,
                )
            except Exception:
                logger.exception(
                    "助手请求已入队但 Turn 屏障登记失败 request_id=%s source=%s",
                    request.request_id,
                    request.source,
                )

        try:
            self.assistant_request_context_preparer.runtime_memory_store.commit_request(
                request
            )
        except Exception:
            logger.exception(
                "助手请求已入队但运行期记忆登记失败 request_id=%s source=%s",
                request.request_id,
                request.source,
            )

        try:
            self.realtime_input_runtime.interaction_state.register_assistant_request(
                time.monotonic(),
                request_id=request.request_id,
            )
            # System Notice 和 Recovery 也会改变输出与跟进窗口，不能只等用户轮次事件补齐桌面状态。
            state_event = self.realtime_input_runtime.interaction_state_event()
            self.event_queue.put(RuntimeEvent(
                type=state_event["type"],
                payload=state_event["payload"],
                produced_at=time.monotonic(),
                source="realtime_input",
            ))
        except Exception:
            # 请求已经提交，产品状态旁路异常只能留作本地诊断，不能伪装成入队失败。
            logger.exception(
                "助手请求已入队但实时输出身份登记失败 request_id=%s source=%s",
                request.request_id,
                request.source,
            )
        self._notify_request_enqueue_state(request, enqueued=True)

    def enqueue_assistant_request(self, request: AssistantRequest) -> bool:
        """把助手请求放入复用 final ASR 的 bridge 请求队列。

        Args:
            request: 已构造好的助手请求事实。

        Returns:
            成功入队并唤醒 bridge 时返回 True；输出侧不可用时返回 False。
        """

        bridge = self.assistant_process_bridge
        # 输出侧未启用或 bridge 尚未就绪时，直接返回 False，让调用方退回到直连运行时的路径。
        if not self.assistant_output_enabled or bridge is None:
            return False

        try:
            # 用户请求入队前，先把 bridge 里还没开始消费的 system notice 剔除掉，
            # 防止用户说话时系统突然插嘴打断。
            self._prepare_assistant_request_enqueue(request)
            # 将请求放入 bridge 队列并唤醒消费线程；返回值表示队列是否真正接收。
            accepted = bridge.submit_request(request)
        except Exception:
            # 提交过程中抛异常，先通知提醒协调器入队失败，再原样上抛供外层回滚 notice 事务。
            self._notify_request_enqueue_state(request, enqueued=False)
            raise
        if not accepted:
            # 队列拒绝或消费线程不可用，按入队失败同步状态。
            self._notify_request_enqueue_state(request, enqueued=False)
        return accepted

    def _notify_request_enqueue_state(
        self,
        request: AssistantRequest,
        *,
        enqueued: bool,
    ) -> None:
        """把请求入队事务结果同步给提醒协调器。

        Args:
            request: 已尝试进入 bridge 队列的助手请求。
            enqueued: queue.put 是否已经成功。
        """

        # 这是对象级便捷入口；system notice 路径在构造出 AssistantRequest 前就会失败，
        # 所以真正发通知的逻辑拆到了字段级函数 _notify_request_enqueue_fields。
        self._notify_request_enqueue_fields(
            request_id=request.request_id,
            source=request.source,
            source_turn_id=request.source_turn_id,
            enqueued=enqueued,
        )

    def _notify_request_enqueue_fields(
        self,
        *,
        request_id: str,
        source: str,
        source_turn_id: str | None,
        enqueued: bool,
    ) -> None:
        """在不改变 queue.put 事务结果的前提下通知提醒协调器。

        Args:
            request_id: 本次助手请求编号。
            source: 请求来源。
            source_turn_id: 可选输入轮次编号。
            enqueued: 请求是否已经越过 queue.put 边界。
        """

        # 字段级入口，供 system notice 构造失败路径直接调用，无需先组装 AssistantRequest。
        if self.notice_delivery_handler is None:
            return
        try:
            self.notice_delivery_handler(
                NoticeDeliveryCommand(
                    request_id=request_id,
                    outcome=(
                        "request_enqueued" if enqueued else "request_enqueue_failed"
                    ),
                )
            )
        except Exception:
            # 协调器通知是入队事务的旁路观测，绝不能反向改变已经确定的 queue.put 结果。
            logger.warning(
                "助手请求入队状态通知失败 request_id=%s source=%s enqueued=%s",
                request_id,
                source,
                enqueued,
                exc_info=True,
            )

    def _prepare_assistant_request_enqueue(self, request: AssistantRequest) -> None:
        """用户请求入队前释放尚未桥接的 system notice。

        Args:
            request: 即将进入助手输出侧的请求。
        """

        if request.source == "system_notice":
            return
        self._discard_pending_system_delivery_requests()

    def _discard_pending_system_delivery_requests(self) -> None:
        """丢弃队列中尚未启动的系统交付请求，并回写 superseded。"""

        bridge = self.assistant_process_bridge
        if bridge is None:
            return

        dropped_requests = bridge.remove_pending_requests(
            lambda queued_request: (
                isinstance(queued_request, AssistantRequest)
                and queued_request.source == "system_notice"
            )
        )
        if not dropped_requests:
            return

        for dropped_request in dropped_requests:
            logger.info(
                "用户请求抢占待处理系统交付请求 request_id=%s source_turn_id=%s",
                dropped_request.request_id,
                dropped_request.source_turn_id,
            )
            if self.notice_delivery_handler is None:
                continue
            try:
                self.notice_delivery_handler(
                    NoticeDeliveryCommand(
                        request_id=dropped_request.request_id,
                        outcome="superseded",
                    )
                )
            except Exception:
                logger.warning(
                    "被用户请求抢占的 system notice 闭环通知失败 request_id=%s",
                    dropped_request.request_id,
                    exc_info=True,
                )

    def handle_permission_card_decision(
        self,
        interaction: PendingAgentInteraction,
        outcome: Literal["allow", "deny"],
    ) -> None:
        """在鼠标决定成功提交后精确停止对应权限请求播报。

        这里的成功只表示原 Activity 已经收到决定，不代表允许已经生效。允许结果会
        等待后续 ``permission_granted`` 权威事实；拒绝结果不生成额外确认。

        Args:
            interaction: 已经越过 Registry 和 Runtime 提交边界的权限交互快照。
            outcome: 用户点击的允许或拒绝决定。
        """

        if interaction.kind != "permission" or outcome not in {"allow", "deny"}:
            return
        invalidation_payload: dict[str, object] = {
            "request_id": interaction.request_id,
            "activity_id": interaction.activity_id,
            "interaction_session_id": interaction.interaction_session_id,
            "outcome": "allowed" if outcome == "allow" else "denied",
        }
        invalidation_result = (
            self.activity_notice_handler(
                "background_agent.interaction_resolved",
                invalidation_payload,
                interaction.runtime_epoch,
            )
            if self.activity_notice_handler is not None
            else None
        )
        in_flight_notice_request_ids = set(
            self._apply_expired_notice_requests(invalidation_result)
        )
        tracked_notice_request_ids = self._cancel_resolved_permission_playout(
            "background_agent.interaction_resolved",
            invalidation_payload,
        )
        if outcome == "deny":
            self._pending_permission_acknowledgements.pop(
                interaction.request_id,
                None,
            )
            return

        # Coordinator 会在 text_final 时先闭合 Notice，但 Turn Admission 要到 output_settled
        # 才释放输出屏障。因此 Tracker 找到的旧请求还要向真实屏障核对，避免漏等或永远等。
        waiting_request_ids = in_flight_notice_request_ids
        turn_admission = self.turn_admission_sequencer
        if turn_admission is not None:
            waiting_request_ids.update(
                request_id
                for request_id in tracked_notice_request_ids
                if turn_admission.is_request_unsettled(request_id)
            )
        self._pending_permission_acknowledgements.setdefault(
            interaction.request_id,
            _PendingPermissionAcknowledgement(
                interaction=interaction,
                waiting_notice_request_ids=waiting_request_ids,
            ),
        )

    def _handle_activity_event(
        self,
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> None:
        """在媒体会话中消费 Activity Runtime Event。

        这是 ``ActivityRuntimeEventHandler`` 注入的 ``activity_notice_handler`` 在生产环境中
        实际调用的方法。Gate 发布的 ``permission_granted`` 只交给当前媒体会话驱动短确认；
        其它事件才委托应用容器注入的 ``AssistantRequestContextPreparer.handle_activity_event``，
        由它完成来源事件映射并更新 ``NoticeDeliveryCoordinator``。Coordinator 返回
        supersession 请求编号后，本会话再按请求位于 Bridge 队列或助手子进程的真实位置取消。

        Args:
            event_type: 认知或后台工作来源事件名称。
            payload: 来源事件的 JSON 友好载荷。
            runtime_epoch: 事件所属助手运行时世代。
        """

        if event_type == "background_agent.permission_granted":
            # Gate 授权事实只驱动鼠标短确认，不再进入 Notice Mapper 或 Coordinator。
            self._observe_permission_granted(event_type, payload, runtime_epoch)
            return

        result = (
            self.activity_notice_handler(event_type, payload, runtime_epoch)
            if self.activity_notice_handler is not None
            else None
        )
        self._apply_expired_notice_requests(result)
        self._cancel_resolved_permission_playout(event_type, payload)

    def _cancel_resolved_permission_playout(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> tuple[str, ...]:
        """权限决定生效后，精确停止仍在表达该请求的 System Notice 语音。

        Args:
            event_type: 当前后台工作来源事件名称。
            payload: 包含权限交互请求编号的来源事件载荷。
        """

        if event_type != "background_agent.interaction_resolved":
            return ()
        interaction_request_id = payload.get("request_id")
        if not isinstance(interaction_request_id, str):
            return ()
        system_notice_request_ids = (
            self.system_notice_playout_tracker.resolve_interaction(
                interaction_request_id
            )
        )
        scheduler = self.assistant_audio_output_scheduler
        if scheduler is not None:
            for system_notice_request_id in system_notice_request_ids:
                scheduler.clear_pending_audio_for_request(system_notice_request_id)
        return system_notice_request_ids

    def _observe_permission_granted(
        self,
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> None:
        """把鼠标允许的 Gate 权威事实交给确定性短确认时序。

        Args:
            event_type: 当前后台工作来源事件名称。
            payload: 包含交互、Activity 和授权来源身份的事件载荷。
            runtime_epoch: 权威事实所属助手运行时世代。
        """

        if (
            event_type != "background_agent.permission_granted"
            or payload.get("suppress_notice") is True
            or payload.get("grant_scope") != "activity"
        ):
            return
        interaction_request_id = payload.get("request_id")
        if not isinstance(interaction_request_id, str):
            return
        pending = self._pending_permission_acknowledgements.get(
            interaction_request_id
        )
        if pending is None:
            return
        interaction = pending.interaction
        if (
            interaction.runtime_epoch != runtime_epoch
            or payload.get("activity_id") != interaction.activity_id
            or payload.get("interaction_session_id")
            != interaction.interaction_session_id
        ):
            return
        pending.grant_confirmed = True
        if not pending.waiting_notice_request_ids:
            self._submit_permission_acknowledgement(interaction_request_id)

    def _handle_visible_output_settled(
        self,
        settlement: VisibleOutputSettlement,
    ) -> None:
        """在旧权限播报结算后尝试投递已经生效的鼠标授权短确认。

        Args:
            settlement: 已完成归档并释放 Turn 屏障的可见输出结算。
        """

        ready_interaction_ids: list[str] = []
        for interaction_request_id, pending in list(
            self._pending_permission_acknowledgements.items()
        ):
            pending.waiting_notice_request_ids.discard(settlement.request_id)
            if pending.grant_confirmed and not pending.waiting_notice_request_ids:
                ready_interaction_ids.append(interaction_request_id)
        for interaction_request_id in ready_interaction_ids:
            self._submit_permission_acknowledgement(interaction_request_id)

    def _submit_permission_acknowledgement(
        self,
        interaction_request_id: str,
    ) -> None:
        """通过现有 System Notice 准入边界提交一次确定性权限短确认。

        Args:
            interaction_request_id: 已经满足 Gate 与旧播报结算条件的交互编号。
        """

        pending = self._pending_permission_acknowledgements.pop(
            interaction_request_id,
            None,
        )
        if pending is None or not pending.grant_confirmed:
            return
        turn_admission = self.turn_admission_sequencer
        context_preparer = self.assistant_request_context_preparer
        if (
            not self.assistant_output_enabled
            or turn_admission is None
            or context_preparer is None
        ):
            return
        interaction_state = getattr(
            self.realtime_input_runtime,
            "interaction_state",
            None,
        )
        if (
            interaction_state is None
            or interaction_state.interaction_session_id
            != pending.interaction.interaction_session_id
        ):
            return

        request_id = permission_acknowledgement_request_id(
            pending.interaction.request_id
        )
        if not turn_admission.reserve_system_notice(request_id):
            # 用户文字、语音或无关输出已经取得优先级时，本次短确认永久省略。
            return
        try:
            snapshot = context_preparer.prepare_system_output_snapshot()
            request = AssistantRequest(
                request_id=request_id,
                source_session_id=(
                    self.realtime_input_runtime.current_audio_session_id
                ),
                interaction_session_id=pending.interaction.interaction_session_id,
                character_card=snapshot.character_card,
                user_call_name=snapshot.user_call_name,
                user_self_intro=snapshot.user_self_intro,
                user_extra_notes=snapshot.user_extra_notes,
                user_text="",
                source="system_notice",
                source_turn_id=None,
                requested_at=time.monotonic(),
                foreground_context=snapshot.foreground_context,
                runtime_memory_snapshot=snapshot.runtime_memory_snapshot,
                history_policy=snapshot.history_policy,
                activity_progress_references=(
                    snapshot.activity_progress_references
                ),
                derived_from_activity_ids=snapshot.derived_from_activity_ids,
                direct_output_text=permission_acknowledgement_text(
                    pending.interaction.request_id
                ),
            )
            accepted = turn_admission.submit_reserved_system_notice(
                request_id,
                lambda: self.enqueue_assistant_request(request),
            )
        except Exception:
            turn_admission.release_notice_reservation(request_id)
            logger.exception(
                "权限卡短确认构造或提交失败 interaction_request_id=%s",
                pending.interaction.request_id,
            )
            return
        if not accepted:
            turn_admission.release_notice_reservation(request_id)

    def interrupt_activity_notices(
        self,
        runtime_epoch: int,
        strategy: ActivityNoticeInterruptionStrategy = "silent",
    ) -> None:
        """关闭指定世代的活动提醒，并收束已经进入 System Notice 链路的旧请求。

        这是运行时中断路径使用的会话包装器。Coordinator 决定哪些提醒废止、是否补强终态
        以及哪些请求应被 supersede；本会话只根据返回的请求编号处理真实 Bridge 队列。

        Args:
            runtime_epoch: 已失效的助手运行时世代。
            strategy: 计划内热替换静默收口；意外中断补充强终态提醒。
        """

        if self.activity_notice_interruption_handler is not None:
            self._apply_expired_notice_requests(
                self.activity_notice_interruption_handler(runtime_epoch, strategy)
            )

    def interrupt_runtime_activities(
        self,
        runtime_epoch: int,
    ) -> list[dict[str, object]]:
        """关闭指定世代的提醒与 TaskResult 材料租约。

        Args:
            runtime_epoch: 已失效的助手运行时世代。

        Returns:
            旧任务材料释放产生的瞬时事件。
        """

        notice_error: Exception | None = None
        try:
            self.interrupt_activity_notices(runtime_epoch)
        except Exception as exception:
            # Notice 属于可见表达链路；即使它暂时失败，TaskResult 材料仍必须在本次
            # 旧世代收口中释放，不能把资源清理绑定在用户提醒是否成功上。
            notice_error = exception

        try:
            if self.runtime_event_service is not None:
                release_events = (
                    self.runtime_event_service.interrupt_task_result_deliveries(
                        runtime_epoch
                    )
                )
            else:
                release_events = self.task_result_delivery_coordinator.interrupt_runtime_epoch(
                    runtime_epoch
                )
        except Exception:
            if notice_error is not None:
                logger.exception(
                    "Notice 中断失败后 TaskResult 材料释放也失败 runtime_epoch=%s",
                    runtime_epoch,
                )
            raise
        if notice_error is not None:
            raise notice_error
        return release_events

    def _apply_expired_notice_requests(
        self,
        result: ActivityEventApplyResult | None,
    ) -> tuple[str, ...]:
        """按 Notice Coordinator 返回的请求编号移除或强制取消过期 System Notice。

        同一份结果可能对应两种位置：请求仍在 Bridge 本地队列中，可直接移除；或请求已被
        Bridge 接管并可能正在子进程中生成，只能提交带 request_id 的专用取消命令。必须先
        区分位置，不能一律取消，否则可能误伤新一轮提醒。

        Args:
            result: 提醒协调器返回的过期请求集合。
        """

        if result is None or not result.superseded_request_ids:
            return ()

        # 先处理尚未交给助手子进程的请求：移除即可保证它永远不会产生输出。
        superseded_ids = set(result.superseded_request_ids)
        bridge = self.assistant_process_bridge
        if bridge is None:
            return ()
        # 原子移除尚未被 Bridge 接管且满足条件的请求。
        removed_requests = bridge.remove_pending_requests(
            lambda request: (
                isinstance(request, AssistantRequest)
                and request.source == "system_notice"
                and request.request_id in superseded_ids
            )
        )
        removed_request_ids = {request.request_id for request in removed_requests}

        for request in removed_requests:
            logger.info(
                "移除尚未桥接的过期系统提醒 request_id=%s",
                request.request_id,
            )
            # 请求尚未离开主进程队列，没有产生任何输出；直接以 superseded 完成账本对账。
            if self.notice_delivery_handler is not None:
                self.notice_delivery_handler(
                    NoticeDeliveryCommand(
                        request_id=request.request_id,
                        outcome="superseded",
                    )
                )
            # 这条请求已经在主进程提交时登记过 Turn 屏障，但现在不会再进入子进程，
            # 因而不可能产生 settlement。必须当场释放，避免后续输入永久等待。
            if self.turn_admission_sequencer is not None:
                self.turn_admission_sequencer.handle_request_submission_failed(
                    request.request_id
                )

        # 找出“已经被新请求取代（superseded），但尚未被移除（removed）”的请求 ID。
        for request_id in sorted(superseded_ids - removed_request_ids):
            try:
                accepted = self.enqueue_assistant_control(
                    CancelSupersededSystemNoticeCommand(
                        reason="activity_notice_superseded",
                        issued_at=time.monotonic(),
                        request_id=request_id,
                    )
                )  # command命令
                if accepted:
                    continue
                logger.warning(
                    "助手输出侧不可用，无法提交过期系统提醒取消 request_id=%s",
                    request_id,
                )
            except RuntimeError:
                logger.exception(
                    "提交过期系统提醒取消失败 request_id=%s",
                    request_id,
                )
        return tuple(sorted(superseded_ids - removed_request_ids))

    def _enqueue_system_notice_request(self) -> None:
        """趁当前输出侧空闲，把积压的提醒打包成一条 system notice 助手请求。

        它不是“只要有 notice 就一定发”，而是一次带条件的尝试：
        先确认输出侧能力齐全；
        再确认当前会话确实有 pending notice；
        然后确认用户没在说话、助手也没在播音；
        只有这些都满足时，才真的创建一条 system_notice 请求入队。

        Returns:
            无。该方法只提交助手请求，不直接产生公共控制事件。
        """

        # 到点提醒优先于普通 Activity Notice；它可以抢占普通 Foreground 和未完成语音，
        # 但 Self-Reflection 或另一条独立 Notice 正在表达时只能留在协调器等待。
        if (
            self.timed_reminder_delivery is not None
            and self.timed_reminder_delivery.has_pending()
        ):
            self.enqueue_timed_reminder_notice()
            if self.timed_reminder_delivery.has_pending():
                return

        # 输出侧没开、assistant runtime 不存在、或者没法冻结 prompt 资料时，
        # 这条 system notice 根本无法构造，直接放弃。
        if (
            not self.assistant_output_enabled
            or not self._assistant_output_available()
            or self.assistant_request_context_preparer is None
        ):
            return

        state = self.realtime_input_runtime.interaction_state

        if (
            self.turn_admission_sequencer is not None
            and not self.turn_admission_sequencer.allows_system_notice()
        ):
            logger.debug("跳过 system notice 入队：存在待发文本或语音 hold")
            return

        # 查看coordinator是否存在需要汇报的 notice，如果没有就没必要继续往下探了。
        if (
            self.pending_notice_provider is not None
            and not self.pending_notice_provider(state.interaction_session_id)
        ):
            logger.debug(
                "跳过 system notice 入队：当前会话没有 pending 候选 interaction_session_id=%s",
                state.interaction_session_id,
            )
            return

        # 派生状态通过 InteractionState 的统一锁读取。Final ASR 正在把输入交给输出侧时，
        # 这里会等到交接完成再判断，不能把两次状态写入之间的中间态误认成空闲。
        turn_state = state.turn_state
        if turn_state != "idle":
            logger.debug(
                "跳过 system notice 入队：实时 turn 未空闲 input_active=%s assistant_playout_active=%s "
                "turn_state=%s interaction_session_id=%s",
                state.input_state.active,
                state.assistant_output_state.playout_active,
                turn_state,
                state.interaction_session_id,
            )
            return

        # 给这次系统提醒生成一个独立 request_id，后面好做交付、取消和 supersede 闭环。
        runtime_identity_factory = getattr(
            self,
            "runtime_identity_factory",
            RuntimeIdentityFactory(),
        )
        request_id = runtime_identity_factory.new("system-notice")
        attention_lease: AssistantRequestAdmissionLeaseProtocol | None = None
        attention_port = self.foreground_attention_admission_port
        if attention_port is not None:
            epoch_reader = self.current_runtime_epoch
            if epoch_reader is None:
                logger.warning(
                    "跳过 system notice 入队：Attention 已接线但 runtime epoch 不可用"
                )
                return
            try:
                runtime_epoch = epoch_reader()
                attention_lease = attention_port.reserve_assistant_request(
                    request_id=request_id,
                    runtime_epoch=runtime_epoch,
                    request_source="system_notice",
                )
            except Exception:
                logger.exception(
                    "System Notice Attention 准入失败 request_id=%s",
                    request_id,
                )
                return
            if attention_lease is None:
                logger.debug("跳过 system notice 入队：Foreground Attention 已占用")
                return
        if (
            self.turn_admission_sequencer is not None
            and not self.turn_admission_sequencer.reserve_system_notice(request_id)
        ):
            if attention_lease is not None:
                attention_lease.rollback()
            logger.debug("跳过 system notice 入队：Turn 准入位置已被用户输入占用")
            return

        # 冻结这次请求要用的 prompt/profile/route context。
        # 这里会顺手去 notice coordinator 那边正式“领出”一批 notice。
        prepared_context = None
        try:
            prepared_context = self.assistant_request_context_preparer.prepare(
                request_id=request_id,
                request_source="system_notice",
            )
        except Exception:
            if attention_lease is not None:
                attention_lease.rollback()
            if self.turn_admission_sequencer is not None:
                self.turn_admission_sequencer.release_notice_reservation(
                    request_id
                )
            self._notify_request_enqueue_fields(
                request_id=request_id,
                source="system_notice",
                source_turn_id=None,
                enqueued=False,
            )
            logger.exception("构造 system notice 请求资料失败 request_id=%s", request_id)
            return

        # 如果冻结出来的 foreground_context 里根本没有 notice_batch，
        # 说明虽然前面看起来像有候选，但最终这次并没有成功领到可表达提醒。
        snapshot = prepared_context.snapshot
        notice_batch = self._notice_batch_payload(snapshot.foreground_context)
        if not notice_batch:
            prepared_context.rollback()
            if attention_lease is not None:
                attention_lease.rollback()
            if self.turn_admission_sequencer is not None:
                self.turn_admission_sequencer.release_notice_reservation(
                    request_id
                )
            logger.debug(
                "跳过 system notice 入队：没有待表达提醒 request_id=%s", request_id
            )
            return
        logger.info(
            "system notice 空闲检查通过 request_id=%s interaction_session_id=%s notice_count=%s",
            request_id,
            state.interaction_session_id,
            len(notice_batch),
        )

        # System Notice 自己不携带业务正文，而是让 Foreground 读取 notice_batch，
        # 再按每条显式 intent 自然表达；不同任务不能为了追求简短被硬揉成一句。
        notice_user_text = SYSTEM_NOTICE_REQUEST_TEXT

        # 构造一条真正会进入 assistant 输出链路的请求。
        try:
            request = AssistantRequest(
                request_id=request_id,
                source_session_id=self.realtime_input_runtime.current_audio_session_id,
                interaction_session_id=state.interaction_session_id,
                character_card=snapshot.character_card,
                user_call_name=snapshot.user_call_name,
                user_self_intro=snapshot.user_self_intro,
                user_extra_notes=snapshot.user_extra_notes,
                user_text=notice_user_text,
                source="system_notice",
                source_turn_id=None,
                requested_at=time.monotonic(),
                turn_material_context_id=None,
                foreground_context=snapshot.foreground_context,
                environment_snapshot=None,
                runtime_memory_snapshot=snapshot.runtime_memory_snapshot,
                history_policy=snapshot.history_policy,
                activity_progress_references=(
                    snapshot.activity_progress_references
                ),
                derived_from_activity_ids=snapshot.derived_from_activity_ids,
            )
        except Exception:
            prepared_context.rollback()
            if attention_lease is not None:
                attention_lease.rollback()
            if self.turn_admission_sequencer is not None:
                self.turn_admission_sequencer.release_notice_reservation(
                    request_id
                )
            self._notify_request_enqueue_fields(
                request_id=request_id,
                source="system_notice",
                source_turn_id=None,
                enqueued=False,
            )
            logger.exception("system notice 构造或入队失败 request_id=%s", request_id)
            return
        permission_interaction_request_ids = tuple(
            getattr(prepared_context, "permission_interaction_request_ids", ())
        )
        tracker_registered = bool(permission_interaction_request_ids)
        try:
            if tracker_registered:
                self.system_notice_playout_tracker.register_request(
                    request_id=request_id,
                    interaction_request_ids=permission_interaction_request_ids,
                )
            if self.turn_admission_sequencer is None:
                accepted = self.enqueue_assistant_request(request)
            else:
                accepted = self.turn_admission_sequencer.submit_reserved_system_notice(
                    request_id,
                    lambda: self.enqueue_assistant_request(request),
                )
        except Exception:
            if tracker_registered:
                self.system_notice_playout_tracker.finish_request(request_id)
            if self.turn_admission_sequencer is not None:
                self.turn_admission_sequencer.release_notice_reservation(
                    request_id
                )
            prepared_context.rollback()
            if attention_lease is not None:
                attention_lease.rollback()
            # enqueue_assistant_request 已经回写失败状态，此处只保留提交异常诊断。
            logger.exception("system notice 入队失败 request_id=%s", request_id)
            return
        if not accepted:
            if tracker_registered:
                self.system_notice_playout_tracker.finish_request(request_id)
            prepared_context.rollback()
            if attention_lease is not None:
                attention_lease.rollback()
            logger.warning(
                "system notice 构造完成但输出链路不可用 request_id=%s",
                request_id,
            )
            return
        if attention_lease is not None and not attention_lease.commit_enqueued():
            logger.warning(
                "System Notice 请求已入队但 Attention lease 未提交 request_id=%s",
                request_id,
            )
        # 请求已进入 Bridge 后不能因 Notice lease 通知失败而重复回滚或重试请求。
        try:
            committed = prepared_context.commit_enqueued()
            if not committed:
                logger.warning(
                    "System Notice 请求已入队但 Notice lease 未提交 "
                    "request_id=%s",
                    request_id,
                )
        except Exception as exception:
            logger.exception(
                "System Notice 请求已入队但 Notice lease 提交异常 "
                "request_id=%s",
                request_id,
                exc_info=exception,
            )
        logger.info(
            "system notice 已入队 request_id=%s interaction_session_id=%s notice_count=%s text_length=%s",
            request.request_id,
            request.interaction_session_id,
            len(notice_batch),
            len(request.user_text),
        )

    def _allows_timed_reminder_output_takeover(
        self,
        output_identity: tuple[str, str | None] | None,
    ) -> bool:
        """只允许提醒接管身份明确的普通回复，保护仍在待播或播放的独立输出。

        Args:
            output_identity: 实时状态中当前活跃输出的请求与生成编号。

        Returns:
            当前没有输出，或输出确实属于普通文本与语音回复时返回真。
        """

        if output_identity is None:
            return True
        request_id, generation_id = output_identity
        identity = self.assistant_request_identity_registry.get(request_id)
        if identity is None or identity.source not in {"text_input", "final_asr"}:
            return False
        return generation_id is None or identity.generation_id == generation_id

    def enqueue_timed_reminder_notice(self) -> bool:
        """把当前全部可响任务提交成独立 Reminder Notice。

        Returns:
            本次实际进入 Bridge 时返回真；受更高优先级输出阻塞时返回假。
        """

        coordinator = self.timed_reminder_delivery
        context_preparer = self.assistant_request_context_preparer
        turn_admission = self.turn_admission_sequencer
        if (
            coordinator is None
            or not coordinator.has_pending()
            or context_preparer is None
            or turn_admission is None
            or not self.assistant_output_enabled
            or not self._assistant_output_available()
        ):
            return False

        state = self.realtime_input_runtime.interaction_state
        current_output = state.current_assistant_output_identity()
        # Reminder 只能抢占普通用户回复。永久身份表保留独立输出的来源，
        # 完整请求在文字与合成结算后释放不代表音频已经播完。
        if not self._allows_timed_reminder_output_takeover(current_output):
            return False

        request_id = self.runtime_identity_factory.new("timed-reminder")
        attention_lease: AssistantRequestAdmissionLeaseProtocol | None = None
        attention_port = self.foreground_attention_admission_port
        if attention_port is not None:
            if self.current_runtime_epoch is None:
                return False
            attention_lease = attention_port.reserve_assistant_request(
                request_id=request_id,
                runtime_epoch=self.current_runtime_epoch(),
                request_source="timed_reminder_notice",
            )
            if attention_lease is None:
                return False
        # 先占住 Turn 准入位置，再让输入 owner 执行不可逆清理。reservation 获得前
        # 已经形成的待发文本、Final ASR 或高优先级输出都会直接阻止本次 Reminder。
        if not turn_admission.reserve_timed_reminder(request_id):
            if attention_lease is not None:
                attention_lease.rollback()
            return False
        try:
            # 取得预留期间输出可能已经变化；不可逆输入清理前必须重新读取当前身份。
            current_output = state.current_assistant_output_identity()
            if not self._allows_timed_reminder_output_takeover(current_output):
                turn_admission.release_notice_reservation(request_id)
                if attention_lease is not None:
                    attention_lease.rollback()
                return False
            interrupted_user = self._take_over_unfinished_voice_turn()
            if interrupted_user is None:
                turn_admission.release_notice_reservation(request_id)
                if attention_lease is not None:
                    attention_lease.rollback()
                return False
            current_output = state.current_assistant_output_identity()
            if not self._allows_timed_reminder_output_takeover(current_output):
                turn_admission.release_notice_reservation(request_id)
                if attention_lease is not None:
                    attention_lease.rollback()
                return False
            interrupted_assistant = False
            if current_output is not None:
                interrupted_assistant = self.enqueue_assistant_control(
                    CancelCurrentGenerationCommand(
                        generation_id=current_output[1],
                        reason="timed_reminder_takeover",
                        issued_at=time.monotonic(),
                    )
                )
            reminder_batch = coordinator.freeze_ready_batch(
                request_id=request_id,
                interrupted_user=interrupted_user,
                interrupted_assistant=interrupted_assistant,
            )
            if reminder_batch is None:
                if attention_lease is not None:
                    attention_lease.rollback()
                turn_admission.release_notice_reservation(request_id)
                return False
            snapshot = context_preparer.prepare_timed_reminder_notice_snapshot(
                reminder_batch
            )
            request = AssistantRequest(
                request_id=request_id,
                source_session_id=self.realtime_input_runtime.current_audio_session_id,
                interaction_session_id=state.interaction_session_id,
                character_card=snapshot.character_card,
                user_call_name=snapshot.user_call_name,
                user_self_intro=snapshot.user_self_intro,
                user_extra_notes=snapshot.user_extra_notes,
                user_text=TIMED_REMINDER_REQUEST_TEXT,
                source="timed_reminder_notice",
                source_turn_id=None,
                requested_at=time.monotonic(),
                foreground_context=snapshot.foreground_context,
                runtime_memory_snapshot=snapshot.runtime_memory_snapshot,
                history_policy=snapshot.history_policy,
                activity_progress_references=snapshot.activity_progress_references,
                derived_from_activity_ids=snapshot.derived_from_activity_ids,
                generation_preemption_reason="timed_reminder_takeover",
            )
            accepted = turn_admission.submit_reserved_timed_reminder(
                request_id,
                lambda: self.enqueue_assistant_request(request),
            )
        except Exception:
            coordinator.rollback_prepared_request(request_id)
            if attention_lease is not None:
                attention_lease.rollback()
            turn_admission.release_notice_reservation(request_id)
            logger.exception(
                "定时提醒 takeover 构造或提交失败 request_id=%s",
                request_id,
            )
            return False
        if not accepted:
            coordinator.rollback_prepared_request(request_id)
            if attention_lease is not None:
                attention_lease.rollback()
            return False
        if attention_lease is not None and not attention_lease.commit_enqueued():
            logger.warning(
                "定时提醒已入队但 Attention lease 未提交 request_id=%s",
                request_id,
            )
        logger.info(
            "定时提醒 takeover 已入队 request_id=%s reminder_count=%s",
            request_id,
            len(reminder_batch.reminders),
        )
        return True

    def _take_over_unfinished_voice_turn(self) -> bool | None:
        """在输入 worker 所属线程清空未完成语音 Turn。

        Returns:
            控制命令确实观察到未完成用户语音时返回真；命令在开始前超时取消时返回空。
        """

        takeover = TimedReminderInputTakeover()
        self.control_queue.put_nowait(
            AudioControlCommand(
                kind="timed_reminder_takeover",
                payload={"takeover": takeover},
                issued_at=time.monotonic(),
            )
        )
        self.realtime_input_worker.notify_work_available()
        if takeover.wait(timeout=0.25):
            return (
                takeover.interrupted
                if takeover.completed_successfully
                else None
            )
        if takeover.cancel_if_pending():
            logger.warning("Reminder takeover 等待输入 worker 超时，已取消迟到清理")
            return None
        # worker 已经开始时，cancel_if_pending 会等待同一原子清理锁释放；此时操作
        # 已完成，必须沿用结果继续提交 Reminder，不能回滚后留下已发生的输入清理。
        return (
            takeover.interrupted
            if takeover.completed_successfully
            else None
        )

    def cancel_timed_reminder_request(self, request_id: str) -> None:
        """移除或定向取消一条因用户手动取消而失效的提醒请求。

        Args:
            request_id: Reminder coordinator 返回的独立请求编号。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return

        # Reminder 正文和 TTS 是两条独立 lane。先在主进程登记正文和音频墓碑，
        # 才能保证子进程取消命令前后到达的迟到事件都不会重新出现在用户面前。
        runtime_event_service = self.runtime_event_service
        if runtime_event_service is not None:
            runtime_event_service.cancel_request(normalized_request_id)
        audio_output_scheduler = self.assistant_audio_output_scheduler
        if audio_output_scheduler is not None:
            audio_output_scheduler.clear_pending_audio_for_request(
                normalized_request_id
            )

        bridge = self.assistant_process_bridge
        if bridge is None:
            return
        removed = bridge.remove_pending_requests(
            lambda request: (
                isinstance(request, AssistantRequest)
                and request.source == "timed_reminder_notice"
                and request.request_id == normalized_request_id
            )
        )
        if removed:
            if self.notice_delivery_handler is not None:
                self.notice_delivery_handler(
                    NoticeDeliveryCommand(
                        request_id=normalized_request_id,
                        outcome="superseded",
                    )
                )
            if self.turn_admission_sequencer is not None:
                self.turn_admission_sequencer.handle_request_submission_failed(
                    normalized_request_id
                )
            return
        self.enqueue_assistant_control(
            CancelTimedReminderNoticeCommand(
                reason="timed_reminder_cancelled",
                issued_at=time.monotonic(),
                request_id=normalized_request_id,
            )
        )

    def _notice_batch_payload(self, foreground_context: ForegroundContext) -> list[dict[str, object]]:
        """读取请求快照中的统一提醒批次。

        Args:
            foreground_context: 主进程为助手请求冻结的产品态势。

        Returns:
            过滤掉非字典项后的提醒列表。
        """

        notice_batch = foreground_context.get("notice_batch")
        if not isinstance(notice_batch, list):
            return []
        return [notice for notice in notice_batch if isinstance(notice, dict)]

    def enqueue_assistant_control(self, command: AssistantControlCommand) -> bool:
        """把助手控制命令放入复用插话的 bridge 控制队列。

        Args:
            command: 已构造好的助手控制命令。

        Returns:
            成功入队并唤醒 bridge 时返回 True；输出侧不可用时返回 False。
        """

        bridge = self.assistant_process_bridge
        if not self.assistant_output_enabled or bridge is None:
            return False

        return bridge.submit_control(command)

    def _setup_event_handlers(self) -> None:
        """注册 LiveKit room 事件处理器。"""

        # track_subscribed 是用户媒体进入 backend 的唯一入口；具体音频切换在异步任务中完成。
        self.room.on("track_subscribed")(self._on_track_subscribed)
        # disconnected 需要尽快停掉音频消费，避免旧音轨继续向输入队列写入已失效的帧。
        self.room.on("disconnected")(self._on_disconnected)

    def _on_track_subscribed(
        self, track: object, publication: object, participant: object
    ) -> None:
        """处理远端 track 订阅事件。

        Args:
            track: LiveKit SDK 提供的远端媒体轨。
            publication: 远端媒体轨发布信息。
            participant: 发布该媒体轨的远端参与者。
        """

        # track.kind 是 SDK 暴露的媒体类型；V1 不处理视频或数据轨，避免错误媒体进入音频运行时。
        if getattr(track, "kind", None) != rtc.TrackKind.KIND_AUDIO:
            return

        # participant.identity 是订阅边界唯一可信的来源标识；缺失或非 AEC 来源都不能继续进入 worker。
        participant_identity = getattr(participant, "identity", None)
        if not should_consume_livekit_audio_participant(
            participant_identity,
            aec_identity_prefix=self.aec_participant_identity_prefix,
        ):
            # 忽略日志只记录 participant 身份和 track sid，帮助排查错误发布者，同时避免记录 token 或媒体内容。
            logger.error(
                "Ignoring LiveKit audio track from participant identity=%r track_sid=%r",
                participant_identity,
                getattr(track, "sid", None),
            )
            return

        # LiveKit 回调是同步触发点，受监督的后台任务可以避免阻塞 SDK 事件循环并收口异常。
        self._create_supervised_task(
            self._switch_audio_task(track), name="livekit audio track switch"
        )

    def _on_disconnected(self, *args: object) -> None:
        """LiveKit room 断开时停止当前音频任务并通知音频 worker。

        Args:
            args: LiveKit SDK 不同版本可能附带的断开回调参数。
        """

        # 当前任务不需要区分断开原因，但保留 args 可以兼容 SDK 传参，避免回调签名差异造成异常。
        _ = args

        # 协作式 stop 让消费循环在下一帧前自然退出，减少正常断开时的取消噪音。
        if self._audio_stop_event is not None:
            self._audio_stop_event.set()

        # cancel 用于打断 AudioStream 等待下一帧的阻塞点，避免断开后后台任务悬挂。
        if self._audio_task is not None:
            self._audio_task.cancel()

        # audio worker 有独立线程生命周期，断开时必须同步停止，避免旧 room 的音频状态机继续消费队列。
        self.audio_runtime_thread.stop(timeout=2.0)
        # ASR lane 持有独立后台线程；room 断开后必须跟随 runtime 收口，避免旧请求继续占用识别资源。
        self.asr_scheduler.stop(timeout=2.0)
        # 助手 bridge 与下行音频调度器同样绑定 room 生命周期，断连时必须停止旧 room 的输出侧资源。
        if self.assistant_process_bridge is not None:
            self.assistant_process_bridge.handoff(
                timeout=self.assistant_request_handoff_timeout_sec
            )
        if self.runtime_event_service is not None:
            self._create_supervised_task(
                self.runtime_event_service.drain_once(),
                name="runtime event disconnect closeout",
            )
        if self.assistant_audio_output_scheduler is not None:
            self.assistant_audio_output_scheduler.stop(timeout=2.0)

        # SDK 只在房间彻底关闭时派发 disconnected（临时重连走 reconnecting/reconnected），
        # 此时必须归还 Controller 的当前会话占位；否则 ensure 的同房间幂等早退会让
        # Backend 永远不再入房。回调失败不能阻断本方法剩余的断连收口。
        if self._on_final_disconnect_callback is not None:
            try:
                self._on_final_disconnect_callback()
            except Exception:
                logger.exception("LiveKit 最终断连通知回调执行失败")

        # 重复断连回调仍可反复 stop/cancel 当前任务，但相同断连命令只投递一次，避免 worker 重复清理同一代输入。
        if self._room_disconnect_reset_sent:
            return

        # 断连命令进入高优先级控制队列，worker 可在继续处理旧音频帧前感知房间已失效并清理状态。
        self._room_disconnect_reset_sent = True
        self.control_queue.put(
            AudioControlCommand(
                kind="room_disconnected",
                payload={},
                issued_at=time.monotonic(),
                input_track_generation=self._input_track_generation,
            )
        )
        self.realtime_input_worker.notify_work_available()

    async def close(self) -> None:
        """关闭 LiveKit 媒体会话持有的全部运行时资源。"""

        # Coordinator 是跨媒体会话的 owner。旧 Bridge 仍可取消请求时先释放它持有的
        # prepared/enqueued 账本，避免重建后永久认为上一条 Reminder 仍在消费。
        if self.timed_reminder_delivery is not None:
            await self.timed_reminder_delivery.release_active_requests()

        # 音频消费循环和 runtime 后台循环都依赖各自 stop_event；先置位可以让协作式路径尽量自然退出。
        if self._audio_stop_event is not None:
            self._audio_stop_event.set()
        # 当前音频任务可能正阻塞在 AudioStream 等待下一帧；cancel 是 shutdown 场景下的兜底打断。
        tasks_to_cancel: list[asyncio.Task] = []
        if self._audio_task is not None:
            self._audio_task.cancel()
            tasks_to_cancel.append(self._audio_task)

        # Bridge 先完成旧世代请求确认或补偿；进程级 Drainer 始终存活，能把补偿事件送到
        # Journal/Turn 账本，不能在 handoff 前撤销当前会话端口。
        if self.assistant_process_bridge is not None:
            await asyncio.to_thread(
                self.assistant_process_bridge.handoff,
                timeout=self.assistant_request_handoff_timeout_sec,
            )
            if self.runtime_event_service is not None:
                await self.runtime_event_service.drain_once()

        # 受监督后台任务包括助手运行时事件拉取、事件 Router 和切轨任务；应用退出时必须统一取消。
        for task in list(self._background_tasks):
            task.cancel()
            tasks_to_cancel.append(task)

        # 等待取消完成，让 task 内部 finally 块有机会释放 SDK 或队列资源。
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        # 后台任务集合只描述仍被媒体会话托管的运行中任务，关闭后清空避免重复 shutdown 再次处理已完成 task。
        self._background_tasks.clear()
        self._audio_task = None

        try:
            # 主动断开 room，覆盖应用 shutdown 时 LiveKit SDK 未必会先触发 disconnected 回调的场景。
            disconnect_room = getattr(self.room, "disconnect", None)
            if callable(disconnect_room):
                result = disconnect_room()
                if asyncio.iscoroutine(result):
                    await result
        finally:
            if (
                self.runtime_event_service is not None
                and self._runtime_event_session_bindings is not None
            ):
                self.runtime_event_service.unbind_session(
                    self._runtime_event_session_bindings
                )
                self._runtime_event_session_bindings = None
            # room.disconnect 即使失败，也不能阻止线程型资源停止；否则 shutdown 会在最坏路径留下后台 lane。
            self.audio_runtime_thread.stop(timeout=2.0)
            self.asr_scheduler.stop(timeout=2.0)
            if self.assistant_process_bridge is not None:
                self.assistant_process_bridge.stop(timeout=2.0)
            if self.assistant_audio_output_scheduler is not None:
                self.assistant_audio_output_scheduler.stop(timeout=2.0)

    def _start_runtime_tasks(self) -> None:
        """启动实时队列运行时后台循环。"""

        if self.assistant_process_bridge is not None:
            logger.info(f"启动assistant runtime bridge thread")
            # 先开放 Bridge 提交边界，再启动输入 worker，避免启动窗口内的合法请求被误判为输出不可用。
            self.assistant_process_bridge.start()
        logger.info(f"启动asr消费 thread")
        self.asr_scheduler.start()
        logger.info(f"启动唤醒、语义人声 thread")
        # audio worker 跑在专用线程中，避免慢输入语义处理挤占 LiveKit 主事件循环。
        self.audio_runtime_thread.start()

    async def _switch_audio_task(self, track: object) -> None:
        """切换当前用户音频订阅任务。

        Args:
            track: 新订阅的 AEC clean mic 音频轨。
        """

        async with self._audio_task_lock:
            # 先通知旧循环退出，再 cancel 打断可能正在等待的 AudioStream。
            if self._audio_stop_event is not None:
                self._audio_stop_event.set()
            if self._audio_task is not None:
                self._audio_task.cancel()
                try:
                    await asyncio.wait_for(self._audio_task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    # 正常切换和断开都会触发取消；吞掉可避免任务切换刷成未处理异常。
                    pass

            # 新 stop_event 只服务新任务，避免旧任务后续 finally 误影响新音轨。
            stop_event = asyncio.Event()
            self._audio_stop_event = stop_event
            # 新音轨代表输入侧恢复可用，允许未来真正断连时重新投递一次 room_disconnected 命令。
            self._room_disconnect_reset_sent = False
            # 新代际使旧音轨迟到帧可以在 worker 边界被识别并丢弃。
            self._input_track_generation += 1
            captured_input_track_generation = self._input_track_generation
            logger.info(f"推送control 命令：track_switched")
            self.control_queue.put(
                AudioControlCommand(
                    kind="track_switched",
                    payload={},
                    issued_at=time.monotonic(),
                    input_track_generation=captured_input_track_generation,
                )
            )  # 启动的control会给一个 track_switched
            self.realtime_input_worker.notify_work_available()
            self._audio_task = asyncio.create_task(
                self._consume_audio_track(
                    track,
                    stop_event,
                    input_track_generation=captured_input_track_generation,
                )
            )

    async def _consume_audio_track(
        self, track: object, stop_event: asyncio.Event, *, input_track_generation: int
    ) -> None:
        """消费 LiveKit 音频轨并写入实时音频输入队列。

        Args:
            track: 当前订阅的远端音频轨。
            stop_event: 切轨或断连时触发的协作退出信号。
            input_track_generation: 当前输入轨的单调代际编号。
        """

        # AudioStream 是唯一负责 SDK 重采样和声道转换的入口；bridge 会再次校验实际帧格式。
        audio_stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        try:
            async for audio_event in audio_stream:
                # stop_event 已设置时不再处理新帧，避免旧轨道切换后继续污染当前语音 turn。
                if stop_event.is_set():
                    break

                pcm16 = read_pcm16_from_audio_event(audio_event)
                # 帧序号只在成功读取 SDK 帧后递增，用于定位输入侧乱序与丢帧。
                self._input_frame_sequence_no += 1

                # audio_queue 是新实时输入主路径；入队必须非阻塞，避免 LiveKit async 消费任务卡住事件循环。
                self._enqueue_audio_item(
                    AudioInputItem(
                        pcm16=pcm16,
                        received_at=time.monotonic(),
                        input_frame_sequence_no=self._input_frame_sequence_no,
                        input_track_generation=input_track_generation,
                        sample_rate=16000,
                        num_channels=1,
                        duration_ms=_estimate_pcm16_duration_ms(
                            pcm16, sample_rate=16000
                        ),
                    )
                )
        except asyncio.CancelledError:
            # 任务切换和 room 断开都使用 cancel 收口；重新抛出让 wait_for 能正确识别取消完成。
            raise
        finally:
            # AudioStream 持有 SDK 资源；无论自然结束、切换取消还是上游异常都必须尝试关闭。
            close_stream = getattr(audio_stream, "aclose", None)
            if callable(close_stream):
                await close_stream()

    def _enqueue_audio_item(self, item: AudioInputItem) -> None:
        """以实时保新策略把音频帧写入输入队列。

        Args:
            item: 已完成格式归一化的实时音频帧。
        """

        try:
            # 正常路径直接非阻塞入队，确保音频消费任务不会因为下游积压而挂住事件循环。
            self.audio_queue.put_nowait(item)
            self.realtime_input_worker.notify_work_available()
            return
        except Full:
            # 队列满表示实时输入已经积压；丢弃最旧帧可以保留更接近当前说话状态的新音频。
            try:
                self.audio_queue.get_nowait()
                self.dropped_input_frames_due_to_queue_full += 1
                logger.warning(
                    f"audio queue已经积压溢出！！音频chunk开始丢弃，到目前为止，共丢弃 {self.dropped_input_frames_due_to_queue_full} 个chunk"
                )
            except Empty:
                # 消费者可能已经抢先取走旧帧；此时不计为本方法主动丢弃，只继续尝试写入新帧。
                pass

        try:
            # 第二次尝试仍必须非阻塞；如果队列又被填满，丢弃当前帧以保护事件循环实时性。
            self.audio_queue.put_nowait(item)
            self.realtime_input_worker.notify_work_available()
        except Full:
            # 竞争场景下当前新帧未能入队，也属于队列满导致的丢帧，需要计入观测指标。
            self.dropped_input_frames_due_to_queue_full += 1
            logger.warning(
                f"audio queue已经积压溢出！！音频chunk开始丢弃，到目前为止，共丢弃 {self.dropped_input_frames_due_to_queue_full} 个chunk"
            )

    def _create_supervised_task(
        self, coroutine: Awaitable[object], *, name: str
    ) -> asyncio.Task:
        """创建并监督后台任务。

        Args:
            coroutine: 需要在当前事件循环调度的协程。
            name: 用于诊断和日志的任务名称。

        Returns:
            已加入媒体会话监督集合的异步任务。
        """

        # create_task 仍然是实际调度入口，但所有后台任务都必须进入集合，避免裸 task 丢失。
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)

        def on_done(done_task: asyncio.Task) -> None:
            """观察后台任务完成结果并清理监督集合。

            Args:
                done_task: 已经结束的受监督异步任务。
            """

            # 任务完成后立即移出集合，避免长时间运行后集合保留已结束任务。
            self._background_tasks.discard(done_task)
            try:
                exception = done_task.exception()
            except asyncio.CancelledError:
                return

            # 无异常表示任务自然完成，不需要记录。
            if exception is None:
                return

            # 真实异常必须被保存和记录；日志只包含任务名与异常信息，不包含 token 或 secret。
            self.background_task_errors.append(exception)
            logger.exception(
                "LiveKit background task failed: %s", name, exc_info=exception
            )

        task.add_done_callback(on_done)
        return task


def _estimate_pcm16_duration_ms(pcm16: bytes, *, sample_rate: int) -> float:
    """估算 PCM16 单声道音频的持续毫秒数。

    Args:
        pcm16: 单声道 PCM16 音频数据。
        sample_rate: 音频采样率。

    Returns:
        音频持续毫秒数；采样率无效时返回零。
    """

    # 非法采样率无法表达真实时长，返回 0.0 让调用方保留帧但不产生除零异常。
    if sample_rate <= 0:
        return 0.0

    # PCM16 每个采样占 2 字节；当前 LiveKit AudioStream 已统一为单声道，故无需再除以通道数。
    return (len(pcm16) / 2) / sample_rate * 1000.0


@dataclass(frozen=True, slots=True)
class _LiveKitMediaSessionConnection:
    """保存重建当前 Backend 媒体会话所需的连接事实。

    Args:
        url: LiveKit 服务地址。
        room_name: 当前固定房间名称。
        token: Backend 媒体参与者访问令牌。
        identity: Backend 媒体参与者身份。
    """

    url: str
    room_name: str
    token: str
    identity: str


class LiveKitMediaSessionController:
    """启动、查询并关闭当前 LiveKit 媒体会话。"""

    def __init__(
        self,
        *,
        media_session_factory: Callable[[], LiveKitMediaSessionLike],
    ) -> None:
        """初始化 LiveKit Media Session Controller。

        Args:
            media_session_factory: 延迟创建实时媒体会话的工厂。
        """

        # 工厂延迟创建媒体会话，确保 bootstrap 替换输入运行时后能捕获最新所有者。
        self.media_session_factory = media_session_factory
        # _started_room_name 记录当前已启动 room，用于同 room 幂等。
        self._started_room_name: str | None = None
        # 当前媒体会话引用支撑同房间幂等判断和资源回收。
        self._media_session: LiveKitMediaSessionLike | None = None
        # 连接事实由 Controller 持有，使输入 Runtime 热替换可以重建当前会话而不依赖外部重新签发。
        self._connection: _LiveKitMediaSessionConnection | None = None
        # 候选会话已接管助手事件，但尚未发布为当前稳定会话。
        self._media_session_starting = False
        # ensure 可能被多个 session 请求并发调用，锁能防止重复启动同一 room。
        self._lock = asyncio.Lock()
        # 最终断连通知是同步回调；异步收口任务必须被 Controller 持有，避免变成无人监管的裸任务。
        self._final_disconnect_cleanup_tasks: set[asyncio.Task] = set()

    @property
    def has_started_session(self) -> bool:
        """判断当前是否已有 LiveKit 媒体会话接管运行时事件。

        Returns:
            已存在稳定或正在启动的媒体会话时返回真。
        """

        return self._media_session_starting or (
            self._media_session is not None and self._started_room_name is not None
        )

    async def ensure_session_started(
        self,
        *,
        url: str,
        room_name: str,
        token: str,
        identity: str,
    ) -> None:
        """确保 Backend 媒体会话已经进入指定 room。

        Args:
            url: LiveKit 服务地址。
            room_name: 需要接管的目标房间名称。
            token: 进入目标房间的访问令牌。
            identity: Backend 媒体会话的参与者身份。
        """

        # 并发 session 请求会进入同一个 Controller；加锁后才能可靠判断和更新当前 room 状态。
        async with self._lock:
            if (
                self._started_room_name == room_name
                and self._media_session is not None
            ):
                return

            # 切换 room 前必须先关闭旧会话，避免后台任务、ASR 通道或 LiveKit 连接失去回收入口。
            old_media_session = self._media_session
            if old_media_session is not None:
                await old_media_session.close()
                self._media_session = None
                self._started_room_name = None

            connection = _LiveKitMediaSessionConnection(
                url=url,
                room_name=room_name,
                token=token,
                identity=identity,
            )
            self._connection = connection
            # 启动中状态覆盖候选会话已接管事件、但尚未成为稳定实例的窗口。
            self._media_session_starting = True
            try:
                media_session = await self._build_started_session(connection)
            except (Exception, asyncio.CancelledError):
                self._media_session_starting = False
                raise

            self._media_session = media_session
            self._started_room_name = room_name
            # 候选实例已稳定发布，无论成功或失败都要清除启动中语义。
            self._media_session_starting = False

    async def restart_current_session(self) -> bool:
        """使用已保存连接事实重建当前媒体会话。

        Returns:
            存在可重建会话并成功发布新实例时返回真；尚未启动媒体会话时返回假。
        """

        async with self._lock:
            media_session = self._media_session
            connection = self._connection
            if connection is None:
                return False

            # 输入 Runtime 的 Provider 与消费线程都在会话构造时冻结；必须先完整关闭旧会话，
            # 再由工厂读取 Owner 已发布的新 Runtime，禁止留下同时推进两份输入状态机的窗口。
            self._media_session_starting = True
            try:
                if media_session is not None:
                    await media_session.close()
                self._media_session = None
                self._started_room_name = None
                replacement = await self._build_started_session(connection)
            except (Exception, asyncio.CancelledError):
                self._media_session_starting = False
                raise

            self._media_session = replacement
            self._started_room_name = connection.room_name
            self._media_session_starting = False
            return True

    async def _build_started_session(
        self,
        connection: _LiveKitMediaSessionConnection,
    ) -> LiveKitMediaSessionLike:
        """构造并完整启动一个尚未发布的媒体会话候选。

        Args:
            connection: 启动候选所需的不可变连接事实。

        Returns:
            已完成 LiveKit 连接和内部循环启动的候选会话。
        """

        media_session = self.media_session_factory()
        # 最终断连的会话必须归还 Controller 的当前占位，否则僵尸会话会让
        # ensure 的同房间幂等判断永远早退；测试替身可以不实现该端口。
        set_on_final_disconnect = getattr(media_session, "set_on_final_disconnect", None)
        if callable(set_on_final_disconnect):
            set_on_final_disconnect(
                lambda: self._release_after_final_disconnect(media_session)
            )
        try:
            await media_session.start(
                url=connection.url,
                token=connection.token,
                identity=connection.identity,
                source_session_id=connection.room_name,
            )
        except (Exception, asyncio.CancelledError):
            # start 可能已经连接 room 或启动部分资源；失败时尽力关闭候选会话。
            with suppress(Exception, asyncio.CancelledError):
                await media_session.close()
            raise
        return media_session

    def _release_after_final_disconnect(
        self,
        media_session: LiveKitMediaSessionLike,
    ) -> None:
        """安排已最终断连会话的完整收口，让下一次 ensure 能重新入房。

        Args:
            media_session: 发起最终断连通知的会话；只在它仍是当前会话时生效。
        """

        # 热替换窗口内旧会话的迟到断连通知不能误删新会话的占位。
        if self._media_session is not media_session:
            return
        # 先撤销同房间幂等条件，但保留旧对象到 close 完成；这样下一次 ensure
        # 会在同一把锁下先收口旧会话，避免两份输入状态机短暂并行。
        self._started_room_name = None
        cleanup_task = asyncio.create_task(
            self._close_after_final_disconnect(media_session),
            name="livekit final disconnect closeout",
        )
        self._final_disconnect_cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._forget_final_disconnect_cleanup_task)

    async def _close_after_final_disconnect(
        self,
        media_session: LiveKitMediaSessionLike,
    ) -> None:
        """在 Controller 锁内关闭最终断连的媒体会话并释放引用。

        Args:
            media_session: 触发最终断连通知的媒体会话。

        Returns:
            无；关闭失败也会释放当前引用，让后续入房有机会重试。
        """

        async with self._lock:
            if self._media_session is not media_session:
                return
            try:
                clear_callback = getattr(media_session, "set_on_final_disconnect", None)
                if callable(clear_callback):
                    clear_callback(None)
                await media_session.close()
            except Exception:
                logger.exception("LiveKit 最终断连会话收口失败，释放占位等待下次重试")
            finally:
                if self._media_session is media_session:
                    self._media_session = None
                    self._started_room_name = None

    def _forget_final_disconnect_cleanup_task(self, task: asyncio.Task) -> None:
        """从监督集合移除已结束的最终断连收口任务。"""

        self._final_disconnect_cleanup_tasks.discard(task)

    def enqueue_assistant_request(self, request: AssistantRequest) -> bool:
        """把助手请求转发给当前已启动媒体会话。

        Args:
            request: 已构造好的助手请求事实。

        Returns:
            当前存在可接收媒体会话且入队成功时返回真，否则返回假。
        """

        media_session = self._media_session
        if not self._has_usable_media_session():
            if self._media_session_starting:
                raise AssistantOutputUnavailableError("当前媒体会话正在切换输入运行时")
            return False

        if media_session.enqueue_assistant_request(request):
            return True
        raise AssistantOutputUnavailableError("当前媒体会话的助手输出链路不可用")

    def enqueue_timed_reminder_notice(self) -> bool:
        """让当前稳定媒体会话同步尝试提交一条 Reminder Notice。

        Returns:
            当前会话存在且接收请求时返回真。
        """

        media_session = self._media_session
        if not self._has_usable_media_session():
            return False
        return media_session.enqueue_timed_reminder_notice()

    def cancel_timed_reminder_requests(self, request_ids: tuple[str, ...]) -> None:
        """把手动取消产生的独立请求失效转发给当前会话。

        Args:
            request_ids: 需要移除或定向取消的 Reminder 请求编号。
        """

        media_session = self._media_session
        if media_session is None:
            return
        # 重建期间旧会话引用一直保留到 close 返回。此时仍要把定向取消交给旧 owner，
        # 否则用户关闭提醒后，冻结请求会在重建窗口继续生成或播放。
        cancel_request = getattr(media_session, "cancel_timed_reminder_request", None)
        if not callable(cancel_request):
            return
        for request_id in request_ids:
            cancel_request(request_id)

    def interrupt_current_output(self, *, issued_at: float) -> tuple[str, ...]:
        """把打断交给当前持有音轨的会话，重建期间也清理旧 owner。

        Args:
            issued_at: 用户打断意图的单调时间。

        Returns:
            被当前媒体会话停止的请求编号；没有会话时为空。
        """

        media_session = self._media_session
        if media_session is None:
            return ()
        return media_session.interrupt_current_output(issued_at=issued_at)

    def assistant_output_available(self) -> bool:
        """读取当前稳定媒体会话的真实 Bridge 健康状态。"""

        media_session = self._media_session
        return self._has_usable_media_session() and media_session.assistant_output_available()

    def handle_permission_card_decision(
        self,
        interaction: PendingAgentInteraction,
        outcome: Literal["allow", "deny"],
    ) -> None:
        """把已经成功提交的权限卡决定转发给当前媒体会话。

        Args:
            interaction: 已由 Registry 原子认领的权限交互快照。
            outcome: 用户点击的允许或拒绝决定。
        """

        media_session = self._media_session
        if not self._has_usable_media_session():
            return
        media_session.handle_permission_card_decision(interaction, outcome)

    def enqueue_assistant_control(self, command: AssistantControlCommand) -> bool:
        """把助手控制命令转发给当前已启动媒体会话。

        Args:
            command: 已构造好的助手控制命令。

        Returns:
            当前存在可接收媒体会话且入队成功时返回真，否则返回假。
        """

        media_session = self._media_session
        if not self._has_usable_media_session():
            if self._media_session_starting:
                raise AssistantOutputUnavailableError("当前媒体会话正在切换输入运行时")
            return False

        if media_session.enqueue_assistant_control(command):
            return True
        raise AssistantOutputUnavailableError("当前媒体会话的助手输出链路不可用")

    def _has_usable_media_session(self) -> bool:
        """判断当前会话是否仍可接收新请求。

        Returns:
            只有稳定发布且尚未进入最终断连收口的会话返回真。
        """

        return (
            self._media_session is not None
            and self._started_room_name is not None
            and not self._media_session_starting
        )

    def interrupt_runtime_activities(
        self,
        runtime_epoch: int,
    ) -> list[dict[str, object]]:
        """让当前媒体会话关闭指定世代活动并返回材料释放事件。

        Args:
            runtime_epoch: 已失效的助手运行时世代。

        Returns:
            当前没有媒体会话时为空，否则返回旧 Activity 材料释放事件。
        """

        media_session = self._media_session
        if media_session is None:
            return []
        return media_session.interrupt_runtime_activities(runtime_epoch)

    async def shutdown(self) -> None:
        """关闭当前托管的 LiveKit 媒体会话并清空 room 状态。"""

        # 与启动路径共用锁，避免会话请求正在启动时 shutdown 同时清空状态。
        async with self._lock:
            media_session = self._media_session

            # 当前没有托管会话时表示尚未预热或已经关闭，直接返回保持 shutdown 幂等。
            if media_session is None:
                self._connection = None
                self._started_room_name = None
                self._media_session_starting = False
                return

            await media_session.close()
            self._media_session = None
            self._started_room_name = None
            self._connection = None
            self._media_session_starting = False
