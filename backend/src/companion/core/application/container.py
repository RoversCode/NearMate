from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
import mimetypes
from pathlib import Path
from pathlib import PurePath
from threading import RLock
import time
from uuid import uuid4

from companion.activities.runtime.registry import ActivityRuntimeRegistry
from companion.activities.runtime.service import ActivityService
from companion.background_agent.config_commit_lock import (
    BackgroundAgentConfigCommitLock,
)
from companion.background_agent.interaction_registry import (
    BackgroundAgentInteractionRegistry,
)
from companion.background_agent.interaction_service import (
    BackgroundAgentInteractionService,
)
from companion.background_agent.usage_service import BackgroundAgentUsageService
from companion.background_agent.usage_repository import BackgroundAgentUsageRepository
from companion.background_agent.skill_registry import SkillRegistry
from companion.activities.notice.activity_result_context import (
    ActivityNoticeResultContextProjector,
)
from companion.activities.notice.delivery_coordinator import NoticeDeliveryCoordinator
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.activities.terminal_settlement import ActivityTerminalSettlementService
from companion.config.realtime_audio import load_realtime_audio_config
from companion.config.realtime_audio import RealtimeAudioYamlConfig
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.application.assistant_request_context_preparer import (
    AssistantRequestContextPreparer,
)
from companion.core.application.lifecycle import ApplicationLifecycle
from companion.core.application.material_lifecycle import MaterialLifecycleService
from companion.core.application.runtime_reconfiguration import (
    RuntimeReconfigurationCoordinator,
)
from companion.core.application.status_broadcaster import ApplicationStatusBroadcaster
from companion.core.application.user_settings_reconfiguration import (
    UserSettingsCoordinator,
)
from companion.core.application.turn_submission import (
    TextTurnDispatch,
    TurnSubmissionOutcome,
    TurnSubmissionService,
)
from companion.core.application.turn_admission import TurnAdmissionSequencer
from companion.core.broadcast import BroadcastHub
from companion.core.character_voice_service import CharacterVoiceService
from companion.core.tts_setup_service import TtsSetupService
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.core.settings import Settings
from companion.core.speaker_verification_service import SpeakerVerificationConsoleService
from companion.core.streamvox_example_console_service import StreamVoxExampleConsoleService
from companion.core.wake_response_console_service import WakeResponseConsoleService
from companion.livekit.session_service import LiveKitSessionService
from companion.observability.logging_config import get_backend_logger
from companion.observability.prompt_audit import PromptAuditRecorder
from companion.realtime.assistant_process_client import AssistantProcessClient
from companion.realtime.assistant_events import (
    AssistantRequest,
    RequestSelfReflectionFallbackCommand,
    RuntimeEvent,
)
from companion.realtime.assistant_output_contracts import (
    resolve_assistant_output_contract,
)
from companion.realtime.livekit_media_session import (
    LiveKitMediaSession,
    LiveKitMediaSessionController,
)
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.playout_delivery import PlayoutDeliveryRegistry
from companion.realtime.runtime_identity import (
    AssistantRequestIdentityRegistry,
    RuntimeIdentityFactory,
)
from companion.realtime.runtime_public_event_publisher import (
    RuntimePublicEventPublishers,
)
from companion.realtime.runtime_event_service import RuntimeEventService
from companion.realtime.task_result_delivery import (
    TaskResultAcceptanceTrackingResult,
    TaskResultDeliveryCoordinator,
)
from companion.realtime.visible_output_journal import VisibleOutputJournal
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger
from companion.realtime.assistant_request_ownership import AssistantRequestOwnershipLedger
from companion.realtime.notify_queue import NotifyQueue
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.foreground_self_reflection.registry import ForegroundAttentionCoordinator
from companion.foreground_self_reflection.service import ForegroundSelfReflectionService
from companion.foreground_self_reflection.contracts import (
    SelfReflectionEntryContext,
    SelfReflectionInterruptionReplyContext,
    SelfReflectionRecoveryContext,
)
from companion.foreground_self_reflection.models import UnattendedMaterialReference
from companion.materials.models import MaterialAttachment
from companion.timed_reminders.repository import TimedReminderRepository
from companion.timed_reminders.delivery_coordinator import (
    TimedReminderDeliveryCoordinator,
)
from companion.timed_reminders.scheduler import TimedReminderScheduler
from companion.timed_reminders.service import TimedReminderService


logger = get_backend_logger(__name__)


@dataclass(slots=True)
class AppBuildOptions:
    """定义生产装配和测试替身使用的应用构建选项。

    Args:
        settings: 可选控制服务设置。
        realtime_input_runtime: 可选外部实时语音运行时。
        livekit_session_service: 可选 LiveKit 会话服务替身。
        assistant_process_client: 可选外部助手子进程客户端。
        realtime_audio_config_path: 可选实时音频 YAML 路径。
        livekit_media_session_controller: 可选 LiveKit 媒体会话控制器替身。
    """

    settings: Settings | None = None
    realtime_input_runtime: RealtimeInputRuntime | None = None
    livekit_session_service: object | None = None
    assistant_process_client: AssistantProcessClient | None = None
    realtime_audio_config_path: Path | str | None = None
    livekit_media_session_controller: LiveKitMediaSessionController | None = None


@dataclass(slots=True)
class ApplicationContainer:
    """持有控制服务单个进程生命周期内的全部显式依赖。"""

    # 全局服务配置，驱动 LLM、LiveKit 等所有外部连接。
    settings: Settings
    # 实时音频参数（VAD、AEC 等）的 YAML 配置。
    realtime_audio_config: RealtimeAudioYamlConfig
    # 进程内事件与共享会话态的广播中枢，几乎所有 service 依赖它。
    hub: BroadcastHub
    # 授权与 Bootstrap 的脱敏状态事件发布器。
    application_status: ApplicationStatusBroadcaster
    # 主进程实时输入运行时所有者，负责输入状态与输入资源热替换。
    realtime_input_runtime_owner: RealtimeInputRuntimeOwner
    # 助手子进程所有者，负责稳定世代句柄与子进程热替换。
    assistant_process_supervisor: AssistantProcessSupervisor
    # Backend 进程级 UUID 身份工厂，所有主进程运行期请求共用。
    runtime_identity_factory: RuntimeIdentityFactory
    # Backend 进程级助手请求身份表，请求编号 claim 后永久保留到进程退出。
    assistant_request_identity_registry: AssistantRequestIdentityRegistry
    # Backend 进程级请求所有权账本，媒体会话重建不能丢失未结算请求。
    assistant_request_ownership_ledger: AssistantRequestOwnershipLedger
    # Backend 进程级输出健康快照，控制首帧和 HTTP 准入读取同一事实。
    assistant_output_health_ledger: AssistantOutputHealthLedger
    # Backend 进程级运行时事件入口，媒体会话重建时仍由同一个 Drainer 消费迟到事件。
    assistant_runtime_event_queue: NotifyQueue[RuntimeEvent]
    # Backend 进程唯一的运行时事件消费者；媒体会话只绑定临时媒体操作端口。
    runtime_event_service: RuntimeEventService
    # Backend 进程级可见输出账本，媒体会话重建后仍保留结算墓碑。
    visible_output_journal: VisibleOutputJournal
    # 材料生命周期服务，管理材料在轮次间的产出、暂存与回收。
    material_lifecycle: MaterialLifecycleService
    # Activity 服务，是 Desktop、Console、事件投影和取消能力的唯一访问入口。
    activity_service: ActivityService
    # 定时提醒服务，是工具、双前台、调度器和 Notice 交付共用的运行期真相源。
    timed_reminder_service: TimedReminderService
    # 到点批次交付账本，负责请求租约、取消失效和 text final 生命周期收口。
    timed_reminder_delivery: TimedReminderDeliveryCoordinator
    # 单任务绝对时刻调度器，生命周期由 ApplicationLifecycle 统一管理。
    timed_reminder_scheduler: TimedReminderScheduler
    # 后台智能体待答请求账本，负责 request_id、请求类型和运行时世代校验。
    background_agent_interactions: BackgroundAgentInteractionRegistry
    # 工作交互服务，让鼠标和语音共用 Registry claim 与同一运行时命令出口。
    background_agent_interaction_service: BackgroundAgentInteractionService
    # 后台工作用量服务，负责终态旁路落库和长期统计查询。
    background_agent_usage_service: BackgroundAgentUsageService
    # 助手请求上下文，汇聚实时运行时、材料与认知交付，为请求组装当前态势。
    assistant_request_context_preparer: AssistantRequestContextPreparer
    # 轮次提交服务，把一轮用户输入投递进实时运行时。
    turn_submission: TurnSubmissionService
    # Turn 准入服务，持有待发文本、语音优先级和输出结算屏障。
    turn_admission: TurnAdmissionSequencer
    # 用户设置服务，负责持久化字段、Skill 偏好和完整快照事务。
    user_settings: UserSettingsCoordinator
    # 运行时配置服务，负责启动密钥、LLM 与运行时世代热替换。
    runtime_reconfiguration: RuntimeReconfigurationCoordinator
    # 首次初始化阶段独立运行的本地 TTS 下载、音色准备和试听服务。
    tts_setup: TtsSetupService
    # 说话人（声纹）确认控制台：管理开关、样本与录音门槛。
    speaker_verification: SpeakerVerificationConsoleService
    # 角色语音服务：维护角色卡语音绑定与 StreamVox 运行音色的匹配。
    character_voice: CharacterVoiceService
    # 唤醒回复控制台：管理角色卡唤醒词回复的读写、生成与合成。
    wake_response: WakeResponseConsoleService
    # StreamVox 示例控制台：生成并管理角色卡的 StreamVox 示例。
    streamvox_examples: StreamVoxExampleConsoleService
    # LiveKit 媒体会话控制器，延迟创建并维护当前会话生命周期。
    livekit_media_session_controller: LiveKitMediaSessionController
    # 当前 Backend 进程内的一次性助手语音交付账本。
    playout_delivery_registry: PlayoutDeliveryRegistry
    # 当前 Backend 进程内的结果卡交付账本和重连快照。
    task_result_delivery_coordinator: TaskResultDeliveryCoordinator
    # Backend 进程级 Foreground 聊天、滚动摘要和任务详情权威账本。
    runtime_memory_store: RuntimeMemoryStore
    # Backend 进程级 Self-Reflection Attention、episode 和恢复租约控制面。
    self_reflection_service: ForegroundSelfReflectionService
    # Backend 进程级 Activity Progress 临时账本。
    activity_progress_ledger: ActivityProgressLedger
    # LiveKit 远程会话服务；未配置凭证时为 None，表示不接入远程实时会话。
    livekit_session_service: object | None
    # 应用生命周期，统一收口启动与停止流程。
    lifecycle: ApplicationLifecycle


class LiveKitMediaSessionFactory:
    """使用应用容器中的稳定服务构造 LiveKit 媒体会话。"""

    def __init__(
        self,
        *,
        settings: Settings,
        hub: BroadcastHub,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
        assistant_process_supervisor: AssistantProcessSupervisor,
        assistant_request_context_preparer: AssistantRequestContextPreparer,
        notice_delivery_coordinator: NoticeDeliveryCoordinator,
        timed_reminder_delivery: TimedReminderDeliveryCoordinator,
        material_lifecycle: MaterialLifecycleService,
        activity_service: ActivityService,
        background_agent_interactions: BackgroundAgentInteractionRegistry,
        background_agent_interaction_service: BackgroundAgentInteractionService,
        background_agent_usage_service: BackgroundAgentUsageService,
        runtime_event_service: RuntimeEventService,
        playout_delivery_registry: PlayoutDeliveryRegistry | None = None,
        task_result_delivery_coordinator: TaskResultDeliveryCoordinator | None = None,
        activity_progress_ledger: ActivityProgressLedger | None = None,
        turn_admission_sequencer: TurnAdmissionSequencer | None = None,
        runtime_identity_factory: RuntimeIdentityFactory | None = None,
        assistant_request_identity_registry: AssistantRequestIdentityRegistry | None = None,
        visible_output_journal: VisibleOutputJournal | None = None,
        assistant_request_ownership_ledger: AssistantRequestOwnershipLedger | None = None,
        assistant_output_health_ledger: AssistantOutputHealthLedger | None = None,
        runtime_event_queue: NotifyQueue[RuntimeEvent] | None = None,
        input_diversion_port: object | None = None,
    ) -> None:
        """初始化媒体会话工厂所需的稳定依赖。

        Args:
            settings: 当前应用配置。
            hub: 主进程控制事件广播中枢。
            realtime_input_runtime_owner: 主进程实时输入运行时所有者。
            assistant_process_supervisor: 助手子进程及稳定句柄所有者。
            assistant_request_context_preparer: 助手请求上下文组装服务。
            notice_delivery_coordinator: Notice 预留、交付与补偿的唯一状态所有者。
            timed_reminder_delivery: 定时提醒到点批次的主进程交付账本。
            material_lifecycle: 材料生命周期协调服务。
            activity_service: Activity 查询、投影和取消服务。
            background_agent_interactions: 后台智能体待答请求的主进程账本。
            background_agent_interaction_service: 鼠标与语音共用的工作交互提交服务。
            background_agent_usage_service: 后台工作终态用量的保存服务。
            runtime_event_service: Backend 进程唯一的 RuntimeEvent 消费服务。
            activity_progress_ledger: Backend 进程级 Activity Progress 临时账本。
            turn_admission_sequencer: Backend 主进程唯一的 Turn 准入与顺序屏障。
            runtime_identity_factory: Backend 主进程共享的 UUID 身份工厂。
            assistant_request_identity_registry: Backend 进程级请求身份表。
            visible_output_journal: Backend 进程级可见输出和永久墓碑账本。
            runtime_event_queue: Backend 进程级运行时事件入口；媒体会话重建时不重新创建。
            input_diversion_port: Attention active 时接纳 Final ASR 的主进程端口。
        """
        self.settings = settings
        self.hub = hub
        self.realtime_input_runtime_owner = realtime_input_runtime_owner
        self.assistant_process_supervisor = assistant_process_supervisor
        self.assistant_request_context_preparer = assistant_request_context_preparer
        self.notice_delivery_coordinator = notice_delivery_coordinator
        self.timed_reminder_delivery = timed_reminder_delivery
        self.material_lifecycle = material_lifecycle
        self.activity_service = activity_service
        self.background_agent_interactions = background_agent_interactions
        self.background_agent_interaction_service = background_agent_interaction_service
        self.background_agent_usage_service = background_agent_usage_service
        self.runtime_event_service = runtime_event_service
        self.playout_delivery_registry = playout_delivery_registry or PlayoutDeliveryRegistry()
        self.task_result_delivery_coordinator = (
            task_result_delivery_coordinator or TaskResultDeliveryCoordinator()
        )
        self.activity_progress_ledger = (
            activity_progress_ledger or ActivityProgressLedger()
        )
        self.turn_admission_sequencer = turn_admission_sequencer
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
        self.runtime_event_queue = runtime_event_queue or NotifyQueue()
        self.input_diversion_port = input_diversion_port
        # tuple 保存同一语音输入首次看到的 staged 快照，None 是本媒体会话内的已结算墓碑。
        # 墓碑不能按容量淘汰，否则迟到重放会把下一轮刚上传的材料当成旧输入附件。
        self._diversion_materials_by_input: dict[
            str, tuple[MaterialAttachment, ...] | None
        ] = {}
        self._diversion_material_lock = RLock()
        self._material_broadcast_loop: asyncio.AbstractEventLoop | None = None
        self._material_broadcast_tasks: set[asyncio.Task[object]] = set()

    def __call__(self) -> LiveKitMediaSession:
        """创建读取最新可热替换运行时的媒体会话。

        Returns:
            绑定当前应用依赖的新实时媒体会话。
        """

        # 输入身份只保证在一个媒体会话内稳定。Controller 会先完整关闭旧会话，
        # 所以新会话从空表开始，既不会无限保留墓碑，也不会混淆两个会话的 turn-1。
        with self._diversion_material_lock:
            self._diversion_materials_by_input.clear()

        # 媒体会话的音频 worker 在独立线程中运行；记录创建会话时的主事件循环，
        # 释放 staged 材料时由该循环负责异步广播，避免跨线程直接操作 WebSocket。
        try:
            self._material_broadcast_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._material_broadcast_loop = None

        runtime_public_events = RuntimePublicEventPublishers(
            publish_interaction=self.hub.publisher("interaction").publish,
            publish_assistant_output=self.hub.publisher("assistant_output").publish,
            publish_activity=self.hub.publisher("activity").publish,
            publish_activity_ephemeral=(
                self.hub.publisher("activity").publish_ephemeral
            ),
            publish_materials_ephemeral=(
                self.hub.publisher("materials").publish_ephemeral
            ),
        )
        return LiveKitMediaSession(
            realtime_input_runtime=self.realtime_input_runtime_owner.require_for_livekit(),
            public_event_publishers=runtime_public_events,
            assistant_runtime=(
                self.assistant_process_supervisor.require_for_output()
                if self.assistant_process_supervisor.output_enabled
                else None
            ),
            assistant_output_enabled=self.assistant_process_supervisor.output_enabled,
            aec_participant_identity_prefix=self.settings.livekit_aec_identity_prefix,
            assistant_request_handoff_timeout_sec=(
                self.settings.assistant_request_handoff_timeout_s
            ),
            event_queue=self.runtime_event_queue,
            assistant_request_context_preparer=self.assistant_request_context_preparer,
            pending_notice_provider=self.notice_delivery_coordinator.has_pending_notice,
            timed_reminder_delivery=self.timed_reminder_delivery,
            activity_notice_handler=self.assistant_request_context_preparer.handle_activity_event,
            background_agent_interaction_event_handler=(
                self.background_agent_interaction_service.apply_runtime_event
            ),
            background_agent_interaction_interruption_handler=(
                self.background_agent_interaction_service.invalidate_runtime_epoch
            ),
            background_agent_usage_handler=(
                self.background_agent_usage_service.record_terminal_event
            ),
            activity_apply_handler=self.activity_service.apply_source_event,
            activity_broadcast_handler=self.activity_service.broadcast_projection,
            current_runtime_epoch=lambda: self.assistant_process_supervisor.client.current_epoch,
            activity_interruption_handler=self.activity_service.interrupt_active,
            activity_notice_interruption_handler=(
                self.assistant_request_context_preparer.interrupt_activity_notices
            ),
            notice_delivery_handler=(
                self.assistant_request_context_preparer.apply_notice_delivery
            ),
            turn_material_retain_handler=self.material_lifecycle.retain_turn,
            turn_material_final_handler=self.material_lifecycle.finish_turn,
            turn_material_release_handler=self.material_lifecycle.release_turn,
            playout_delivery_registry=self.playout_delivery_registry,
            task_result_delivery_coordinator=self.task_result_delivery_coordinator,
            activity_progress_ledger=self.activity_progress_ledger,
            turn_admission_sequencer=self.turn_admission_sequencer,
            runtime_identity_factory=self.runtime_identity_factory,
            assistant_request_identity_registry=self.assistant_request_identity_registry,
            visible_output_journal=self.visible_output_journal,
            assistant_request_ownership_ledger=self.assistant_request_ownership_ledger,
            assistant_output_health_ledger=self.assistant_output_health_ledger,
            runtime_event_service=self.runtime_event_service,
            input_diversion_port=self.input_diversion_port,
            final_input_materials_provider=self._project_staged_materials,
            final_input_materials_discard_handler=self._discard_projected_staged_materials,
            final_input_material_commit_handler=(
                self._remember_committed_final_asr_materials
            ),
            diverted_material_release_handler=self._release_diverted_staged_materials,
        )

    def _project_staged_materials(
        self,
        input_identity: str,
    ) -> tuple[UnattendedMaterialReference, ...]:
        """为一项 Final ASR 输入冻结安全材料引用，不泄漏本机路径。

        Args:
            input_identity: Final ASR 语音 turn 的稳定身份。

        Returns:
            只包含文件名和媒体类型的材料引用。
        """

        normalized_identity = input_identity.strip()
        if not normalized_identity:
            return ()
        with self._diversion_material_lock:
            if normalized_identity in self._diversion_materials_by_input:
                frozen_attachments = self._diversion_materials_by_input[
                    normalized_identity
                ]
                attachments = frozen_attachments or ()
            else:
                # 首次读取和写表必须在同一把锁内完成，并发重放才能始终复用同一快照。
                attachments = tuple(self.hub.turn_materials.staged_materials())
                self._diversion_materials_by_input[normalized_identity] = attachments

        references: list[UnattendedMaterialReference] = []
        for attachment in attachments:
            display_name = str(attachment.display_name or "material").strip()
            safe_name = PurePath(display_name.replace("\\", "/")).name or "material"
            media_type = mimetypes.guess_type(safe_name)[0]
            if not media_type:
                media_type = {
                    "audio": "audio/*",
                    "video": "video/*",
                    "text": "text/plain",
                    "mixed_or_document": "application/octet-stream",
                }.get(attachment.media_class, "application/octet-stream")
            try:
                references.append(
                    UnattendedMaterialReference(
                        file_name=safe_name,
                        media_type=media_type,
                    )
                )
            except ValueError:
                # 单项材料名称异常不能阻塞正文改道；该项只是不进入历史安全投影。
                continue
        return tuple(references)

    def _release_diverted_staged_materials(
        self,
        input_identity: str,
        remember_recent_materials: bool,
    ) -> None:
        """按输入归属结果登记并释放改道 Final ASR 的 staged 材料。

        Args:
            input_identity: 触发改道的 Final ASR 语音 turn 身份。
            remember_recent_materials: Episode 输入正式成立时为真；归属失败时为假。
        """

        normalized_identity = input_identity.strip()
        if not normalized_identity:
            return
        with self._diversion_material_lock:
            attachments = self._diversion_materials_by_input.get(normalized_identity)
            # 即使回调先于 provider 或原快照为空，也要留下墓碑，禁止迟到重放捕获新材料。
            self._diversion_materials_by_input[normalized_identity] = None
        if not attachments:
            return

        recent_materials_changed = (
            self.material_lifecycle.remember_recent_materials(attachments)
            if remember_recent_materials
            else False
        )
        material_ids = tuple(
            attachment.material_id
            for attachment in attachments
            if attachment.material_id.strip()
        )
        if not material_ids:
            if recent_materials_changed:
                self._schedule_material_broadcast([])
            return

        # 如果 staged 集合仍是 provider 看到的完整快照，使用批量清理保持一次 revision；
        # 若用户期间又上传了下一轮材料，则逐项删除旧快照，不能误伤新增内容。
        current_materials = self.hub.turn_materials.staged_materials()
        current_ids = tuple(item.material_id for item in current_materials)
        if set(current_ids) == set(material_ids):
            released_asset_ids = self.hub.turn_materials.clear_staged()
        else:
            released_asset_ids = []
            for material_id in material_ids:
                removal = self.hub.turn_materials.remove_staged(material_id)
                if removal is None:
                    continue
                released_asset_ids.extend(
                    removal.released_managed_asset_ids
                )
        self._schedule_material_broadcast(released_asset_ids)

    def _remember_committed_final_asr_materials(self, context_id: str) -> None:
        """登记已经成功提交助手请求的普通 Final ASR 材料。

        Args:
            context_id: 请求准备器刚激活并冻结的材料上下文编号。
        """

        normalized_context_id = context_id.strip()
        if not normalized_context_id:
            return
        context = self.hub.turn_materials.active_context(normalized_context_id)
        if context is None:
            return
        if self.material_lifecycle.remember_recent_materials(context.attachments):
            self._schedule_material_broadcast([])

    def _discard_projected_staged_materials(self, input_identity: str) -> None:
        """丢弃未改道 Final ASR 的临时材料索引，不改变材料本身。

        Args:
            input_identity: 已确定继续走普通请求的 Final ASR 输入身份。
        """

        normalized_identity = input_identity.strip()
        if not normalized_identity:
            return
        with self._diversion_material_lock:
            # 普通 Final ASR 已接管 staged 材料；墓碑阻止同一语音输入重放时重新投影。
            self._diversion_materials_by_input[normalized_identity] = None

    def _schedule_material_broadcast(self, released_asset_ids: list[str]) -> None:
        """在主事件循环中广播 staged 材料已经释放的事实。

        Args:
            released_asset_ids: 本次租约释放后不再被引用的资产编号。
        """

        loop = self._material_broadcast_loop
        if loop is None or loop.is_closed():
            return

        def create_task() -> None:
            """在主事件循环创建并登记一次材料变更广播任务。"""

            task = asyncio.create_task(
                self.material_lifecycle.broadcast_changed(released_asset_ids)
            )
            self._material_broadcast_tasks.add(task)
            task.add_done_callback(self._material_broadcast_tasks.discard)

        loop.call_soon_threadsafe(create_task)


def build_application_container(options: AppBuildOptions | None = None) -> ApplicationContainer:
    """按依赖顺序构造完整应用容器。

    Args:
        options: 生产配置或测试替身；为空时使用默认生产配置。

    Returns:
        可交给 FastAPI 组合根注册的完整应用容器。
    """

    # 组合根：按依赖顺序逐层构造 service 并相互注入，顺序即依赖关系，不可随意调整。
    # 解析构建选项：优先采用调用方注入的测试替身，缺省时回落到生产默认值。
    options = options or AppBuildOptions()
    settings = options.settings or Settings()
    prompt_audit_recorder = PromptAuditRecorder(
        enabled=settings.prompt_audit.enabled,
        output_dir=settings.prompt_audit.resolved_output_dir(),
        max_runs=settings.prompt_audit.max_runs,
        process_role="backend-main",
    )
    # 实时语音的一些配置
    realtime_audio_config = (
        load_realtime_audio_config(options.realtime_audio_config_path)
        if options.realtime_audio_config_path is not None
        else load_realtime_audio_config()
    )
    # hub 是进程内事件与共享会话态的广播中枢，几乎所有后续 service 都依赖它。
    hub = BroadcastHub(settings=settings)
    # 重资源统一交给异步初始化任务，容器构造时只建立控制面。
    hub.bootstrap.runtime_blocked = True
    # 首次语音配置不能依赖尚未启动的助手子进程；服务只持有角色卡、模型目录和受管任务线程。
    tts_setup = TtsSetupService(
        settings=settings,
        active_card_provider=hub.character_cards.load_active_card,
        save_card=hub.character_cards.save_card,
    )
    hub.bootstrap.bind_voice_status_provider(tts_setup.status_payload)
    application_status = ApplicationStatusBroadcaster(hub=hub)
    hub.wake_response_service.voice_fingerprint_provider = lambda: tts_setup.status_payload()['fingerprint']
    runtime_identity_factory = RuntimeIdentityFactory()
    timed_reminder_service = TimedReminderService(
        repository=TimedReminderRepository(runtime_id=str(uuid4())),
        broadcast_snapshot=hub.publisher("reminder").publish,
    )
    timed_reminder_delivery = TimedReminderDeliveryCoordinator(
        service=timed_reminder_service,
    )
    assistant_request_identity_registry = AssistantRequestIdentityRegistry()
    visible_output_journal = VisibleOutputJournal()
    assistant_request_ownership_ledger = AssistantRequestOwnershipLedger()
    assistant_output_health_ledger = AssistantOutputHealthLedger()
    assistant_runtime_event_queue: NotifyQueue[RuntimeEvent] = NotifyQueue()

    def enqueue_self_reflection_material_event(event: dict[str, object]) -> None:
        """把 Self-Reflection 释放后的材料事实送入主进程事件通道。

        Args:
            event: `MaterialLifecycleService` 返回的材料瞬时事件。
        """

        event_type = event.get("type")
        payload = event.get("payload")
        if (
            not isinstance(event_type, str)
            or not event_type.startswith("materials.")
            or not isinstance(payload, dict)
        ):
            return
        assistant_runtime_event_queue.put_nowait(
            RuntimeEvent(
                type=event_type,
                source="main_process",
                payload=dict(payload),
                produced_at=time.time(),
                runtime_epoch=None,
            )
        )
    # 输入与助手输出属于两个独立资源生命周期，组合根分别构造所有者并显式注入调用方。
    realtime_input_runtime_owner = RealtimeInputRuntimeOwner(
        settings=settings,
        hub=hub,
        runtime=options.realtime_input_runtime,
        runtime_identity_factory=runtime_identity_factory,
        voice_status_provider=tts_setup.status_payload,
    )
    # 用量服务先于助手进程所有者构造，任何世代在退出阶段补发的终态都能进入同一私有旁路。
    background_agent_usage_service = BackgroundAgentUsageService(
        BackgroundAgentUsageRepository(settings.background_agent_usage_db_path)
    )
    skill_registry = SkillRegistry(asset_root=settings.background_agent_asset_root)
    assistant_process_supervisor = AssistantProcessSupervisor(
        settings=settings,
        hub=hub,
        output_enabled=not realtime_audio_config.realtime_input_only_mode,
        process_client=options.assistant_process_client,
        shutdown_event_handler=background_agent_usage_service.record_terminal_event,
        retired_event_handler=assistant_runtime_event_queue.put_nowait,
        runtime_identity_factory=runtime_identity_factory,
    )
    # Progress、任务记忆和 Activity 中断共用同一主进程终态结算边界。
    runtime_memory_store = RuntimeMemoryStore()
    foreground_attention = ForegroundAttentionCoordinator(
        runtime_epoch=max(assistant_process_supervisor.client.current_epoch, 1),
    )
    activity_progress_ledger = ActivityProgressLedger()
    activity_terminal_settlement_service = ActivityTerminalSettlementService(
        activity_progress_ledger=activity_progress_ledger,
        runtime_memory_store=runtime_memory_store,
    )
    # Activity 运行时只保存本次进程事实；取消命令通过稳定客户端动态路由到当前助手子进程。
    # runtime_id 标识本次 Backend 进程，供双前台识别服务重启，不持久化。
    activity_service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id=str(uuid4())),
        broadcast_snapshot=hub.publisher("activity").publish,
        submit_cancel_command=lambda command: (
            assistant_process_supervisor.require_for_output().submit_control_command(command)
        ),
        current_runtime_epoch=lambda: assistant_process_supervisor.client.current_epoch,
        interruption_terminal_handler=(
            activity_terminal_settlement_service.settle_runtime_interruption
        ),
    )
    background_agent_interactions = BackgroundAgentInteractionRegistry()
    background_agent_interaction_service = BackgroundAgentInteractionService(
        registry=background_agent_interactions,
        runtime_provider=assistant_process_supervisor.require_for_output,
        current_runtime_epoch=lambda: assistant_process_supervisor.client.current_epoch,
    )
    playout_delivery_registry = PlayoutDeliveryRegistry()
    # 材料生命周期：管理材料在轮次之间的产出、暂存与回收。
    material_lifecycle = MaterialLifecycleService(hub=hub)
    task_result_delivery_coordinator = TaskResultDeliveryCoordinator(
        retain_material=material_lifecycle.retain_turn,
        release_material=material_lifecycle.release_turn,
    )
    # 助手请求上下文：粘合实时运行时、材料与认知交付，为请求组装当前产品态势。
    notice_delivery_coordinator = NoticeDeliveryCoordinator()
    assistant_request_context_preparer = AssistantRequestContextPreparer(
        hub=hub,
        realtime_input_runtime_owner=realtime_input_runtime_owner,
        notice_coordinator=notice_delivery_coordinator,
        activity_notice_result_context=ActivityNoticeResultContextProjector(
            result_provider=activity_service.completion_result,
        ),
        pending_work_interactions_provider=(
            background_agent_interactions.foreground_interactions
        ),
        runtime_memory_store=runtime_memory_store,
        activity_progress_ledger=activity_progress_ledger,
        timed_reminder_service=timed_reminder_service,
        timed_reminder_delivery=timed_reminder_delivery,
    )
    livekit_media_session_controller: LiveKitMediaSessionController | None = None

    def submit_self_reflection_turn(
        context: (
            SelfReflectionEntryContext
            | SelfReflectionRecoveryContext
            | SelfReflectionInterruptionReplyContext
        ),
        source_request: AssistantRequest,
    ) -> bool:
        """把入场、Recovery 或辅助回应提交到对应 Foreground 输出域。

        Args:
            context: 入场、完整 Recovery 或辅助打扰回应的结构化上下文。
            source_request: 提供角色、交互会话和可信历史的来源请求。

        Returns:
            当前世代和 Attention gate 接受表达请求时返回真。
        """

        runtime_epoch = assistant_process_supervisor.client.current_epoch
        # 表达上下文属于旧世代时必须直接拒绝，不能把热替换前的迟到结果送进新 Bridge。
        if context.runtime_epoch != runtime_epoch:
            return False
        is_entry = isinstance(context, SelfReflectionEntryContext)
        is_interruption_reply = isinstance(
            context,
            SelfReflectionInterruptionReplyContext,
        )
        if is_entry:
            request_source = "self_reflection_entry"
        elif is_interruption_reply:
            request_source = "self_reflection_interruption_reply"
        else:
            request_source = "self_reflection_recovery"
        if not foreground_attention.can_submit_self_reflection_expression(
            runtime_epoch=runtime_epoch,
            request_source=request_source,
        ):
            return False
        request_text = (
            context.instruction
            if is_entry or is_interruption_reply
            else source_request.user_text
        )
        foreground_context = (
            {}
            if is_entry or is_interruption_reply
            else source_request.foreground_context
        )
        request = AssistantRequest(
            request_id=(
                context.request_id
            ),
            source_session_id=source_request.source_session_id,
            interaction_session_id=source_request.interaction_session_id,
            character_card=source_request.character_card,
            user_call_name=source_request.user_call_name,
            user_self_intro=source_request.user_self_intro,
            user_extra_notes=source_request.user_extra_notes,
            user_text=request_text,
            source=request_source,
            source_turn_id=None,
            requested_at=time.monotonic(),
            turn_material_context_id=(
                None
                if is_entry or is_interruption_reply
                else source_request.turn_material_context_id
            ),
            foreground_context=foreground_context,
            environment_snapshot=(
                None
                if is_entry or is_interruption_reply
                else source_request.environment_snapshot
            ),
            frozen_visual_input=(
                None if is_entry or is_interruption_reply
                else source_request.frozen_visual_input
            ),
            runtime_memory_snapshot=source_request.runtime_memory_snapshot,
            history_policy=resolve_assistant_output_contract(
                request_source=request_source,
                foreground_context=foreground_context,
            )[0],
            self_reflection_context=context,
            generation_preemption_reason=(
                "self_reflection_takeover"
                if is_entry
                else "new_interruption_input"
                if is_interruption_reply
                else None
            ),
        )
        controller = livekit_media_session_controller
        if controller is None:
            return False
        # Bridge 的 canonical commit callback 统一登记 Turn 屏障。这里再次登记会在
        # 极快 settlement 已释放屏障后把同一请求重新放回未结算表。
        return controller.enqueue_assistant_request(request)

    def request_self_reflection_fallback(
        self_reflection_id: str,
        runtime_epoch: int,
        reason: str,
    ) -> bool:
        """把 Recovery deadline 兜底请求送到对应助手世代。"""

        try:
            assistant_process_supervisor.client.submit_control_command_for_epoch(
                RequestSelfReflectionFallbackCommand(
                    self_reflection_id=self_reflection_id,
                    runtime_epoch=runtime_epoch,
                    reason=reason,
                    issued_at=time.monotonic(),
                ),
                runtime_epoch=runtime_epoch,
            )
        except Exception:
            return False
        return True

    def current_ordinary_foreground_output_identity(
    ) -> tuple[str, str | None] | None:
        """读取当前仍在输出的普通用户请求身份。

        Returns:
            普通文本或语音 Foreground 的请求与 generation 身份；System Notice 和
            Self-Reflection 表达不属于需要主动打断的普通交流，因此返回空。
        """

        identity = (
            realtime_input_runtime_owner.current_interaction_state()
            .current_assistant_output_identity()
        )
        if identity is None:
            return None
        request = assistant_request_ownership_ledger.get_request(identity[0])
        if request is None or request.source not in {"text_input", "final_asr"}:
            return None
        return identity

    self_reflection_service = ForegroundSelfReflectionService(
        attention=foreground_attention,
        runtime_memory=runtime_memory_store,
        request_snapshots=assistant_request_ownership_ledger,
        material_lifecycle=material_lifecycle,
        publish_material_event=enqueue_self_reflection_material_event,
        publish_episode_committed=assistant_runtime_event_queue.put_nowait,
        request_recovery_fallback=request_self_reflection_fallback,
        runtime_epoch_mutation_lock=hub.configuration_mutation_lock,
        current_runtime_epoch=lambda: max(
            assistant_process_supervisor.client.current_epoch,
            1,
        ),
        current_foreground_output_identity=(
            current_ordinary_foreground_output_identity
        ),
        submit_control=lambda command, runtime_epoch: assistant_process_supervisor.client.submit_control_command_for_epoch(
            command,
            runtime_epoch=runtime_epoch,
        ),
        submit_expression=submit_self_reflection_turn,
    )

    async def settle_self_reflection_background_material(
        event: RuntimeEvent,
        tracking_result: TaskResultAcceptanceTrackingResult,
    ) -> bool:
        """把 Recovery 材料租约交接给已接纳的 Background TaskResult。

        Args:
            event: `start_background_work` 的 canonical acceptance 事件。
            tracking_result: TaskResult 已完成身份绑定和 retain 尝试后的事实。

        Returns:
            该 acceptance 属于当前 Recovery 且所有权已经完成交接时返回真。
        """

        if not tracking_result.material_context_id:
            return False
        return self_reflection_service.settle_background_material_acceptance(
            event,
            material_context_id=tracking_result.material_context_id,
            task_result_material_retained=tracking_result.material_retained,
            adopt_material_lease=task_result_delivery_coordinator.adopt_material_lease,
        )
    turn_submission_service: TurnSubmissionService | None = None

    def dispatch_admitted_text_turn(
        text_turn: TextTurnDispatch,
    ) -> Awaitable[TurnSubmissionOutcome]:
        """把 Sequencer 已获准的文本交给真实派发服务。

        Args:
            text_turn: 已冻结 reservation 与环境身份的文本提交。

        Returns:
            等待用户消息公开和助手请求启动结果的异步任务。

        Raises:
            RuntimeError: 应用组合根尚未绑定真实派发服务。
        """

        if turn_submission_service is None:
            raise RuntimeError("TurnSubmissionService 尚未完成装配")
        return turn_submission_service.dispatch(text_turn)

    def cancel_admitted_system_notice(request_id: str) -> None:
        """把待发文本触发的 Notice 取消交给真实派发服务。

        Args:
            request_id: Sequencer 在线性化锁内选定的旧 Notice 请求编号。

        Raises:
            RuntimeError: 应用组合根尚未绑定真实派发服务。
        """

        request = assistant_request_ownership_ledger.get_request(request_id)
        if request is not None and request.source == "timed_reminder_notice":
            controller = livekit_media_session_controller
            if controller is not None:
                controller.cancel_timed_reminder_requests((request_id,))
            return
        if turn_submission_service is None:
            raise RuntimeError("TurnSubmissionService 尚未完成装配")
        turn_submission_service.cancel_superseded_system_notice(request_id)

    turn_admission = TurnAdmissionSequencer(
        hub=hub,
        material_lifecycle=material_lifecycle,
        dispatch_text_turn=dispatch_admitted_text_turn,
        cancel_system_notice=cancel_admitted_system_notice,
        try_divert_reserved_text=(
            self_reflection_service.try_divert_reserved_text
        ),
        current_runtime_epoch=lambda: max(
            assistant_process_supervisor.client.current_epoch,
            1,
        ),
    )
    runtime_event_service = RuntimeEventService(
        event_queue=assistant_runtime_event_queue,
        hub=hub,
        realtime_input_runtime_owner=realtime_input_runtime_owner,
        assistant_process_supervisor=assistant_process_supervisor,
        assistant_request_context_preparer=assistant_request_context_preparer,
        material_lifecycle=material_lifecycle,
        activity_apply_handler=activity_service.apply_source_event,
        activity_broadcast_handler=activity_service.broadcast_projection,
        activity_interruption_handler=activity_service.interrupt_active,
        activity_cancellation_handler=activity_service.cancel,
        activity_cancellation_result_handler=lambda command, runtime_epoch: (
            assistant_process_supervisor.client.submit_control_command_for_epoch(
                command,
                runtime_epoch=runtime_epoch,
            )
        ),
        timed_reminder_service=timed_reminder_service,
        timed_reminder_command_result_handler=lambda command, runtime_epoch: (
            assistant_process_supervisor.client.submit_control_command_for_epoch(
                command,
                runtime_epoch=runtime_epoch,
            )
        ),
        background_agent_interaction_event_handler=(
            background_agent_interaction_service.apply_runtime_event
        ),
        background_agent_interaction_interruption_handler=(
            background_agent_interaction_service.invalidate_runtime_epoch
        ),
        background_agent_usage_handler=(
            background_agent_usage_service.record_terminal_event
        ),
        notice_delivery_handler=(
            assistant_request_context_preparer.apply_notice_delivery
        ),
        task_result_delivery_coordinator=task_result_delivery_coordinator,
        playout_delivery_registry=playout_delivery_registry,
        runtime_memory_store=runtime_memory_store,
        activity_progress_ledger=activity_progress_ledger,
        turn_admission=turn_admission,
        request_identity_registry=assistant_request_identity_registry,
        visible_output_journal=visible_output_journal,
        assistant_output_health_ledger=assistant_output_health_ledger,
        task_result_acceptance_handler=settle_self_reflection_background_material,
        self_reflection_event_handler=(
            self_reflection_service.observe_outcome
            if self_reflection_service is not None
            else None
        ),
        self_reflection_service=self_reflection_service,
    )
    # LiveKit 媒体会话控制器经工厂延迟创建会话，启动期不绑定可热替换运行时。
    livekit_media_session_controller = (
        options.livekit_media_session_controller
        or LiveKitMediaSessionController(
            media_session_factory=LiveKitMediaSessionFactory(
                settings=settings,
                hub=hub,
                realtime_input_runtime_owner=realtime_input_runtime_owner,
                assistant_process_supervisor=assistant_process_supervisor,
                assistant_request_context_preparer=assistant_request_context_preparer,
                notice_delivery_coordinator=notice_delivery_coordinator,
                timed_reminder_delivery=timed_reminder_delivery,
                material_lifecycle=material_lifecycle,
                activity_service=activity_service,
                background_agent_interactions=background_agent_interactions,
                background_agent_interaction_service=background_agent_interaction_service,
                background_agent_usage_service=background_agent_usage_service,
                playout_delivery_registry=playout_delivery_registry,
                task_result_delivery_coordinator=task_result_delivery_coordinator,
                activity_progress_ledger=activity_progress_ledger,
                turn_admission_sequencer=turn_admission,
                runtime_identity_factory=runtime_identity_factory,
                assistant_request_identity_registry=assistant_request_identity_registry,
                visible_output_journal=visible_output_journal,
                assistant_request_ownership_ledger=assistant_request_ownership_ledger,
                assistant_output_health_ledger=assistant_output_health_ledger,
                runtime_event_queue=assistant_runtime_event_queue,
                runtime_event_service=runtime_event_service,
                input_diversion_port=self_reflection_service,
            )
        )
    )
    timed_reminder_scheduler = TimedReminderScheduler(
        service=timed_reminder_service,
        notify_ready=timed_reminder_delivery.wake,
    )
    timed_reminder_service.bind_schedule_changed(timed_reminder_scheduler.wake)
    timed_reminder_service.bind_ringing_dismissed(
        timed_reminder_delivery.dismiss_reminders
    )
    timed_reminder_delivery.bind_request_notice(
        livekit_media_session_controller.enqueue_timed_reminder_notice
    )
    timed_reminder_delivery.bind_cancel_requests(
        livekit_media_session_controller.cancel_timed_reminder_requests
    )
    # LiveKit 会话服务仅在配置了 API 凭证时启用，凭证缺失则留空，表示不接入远程实时会话。
    livekit_session_service = options.livekit_session_service
    if (
        livekit_session_service is None
        and settings.livekit_api_key.strip()
        and settings.livekit_api_secret.strip()
    ):
        livekit_session_service = LiveKitSessionService(
            settings=settings,
            media_session_controller=livekit_media_session_controller,
        )
    # 轮次提交服务通过媒体会话控制器把用户输入交给助手输出链路。
    turn_submission_service = TurnSubmissionService(
        hub=hub,
        realtime_input_runtime_owner=realtime_input_runtime_owner,
        livekit_media_session_controller=livekit_media_session_controller,
        assistant_request_context_preparer=assistant_request_context_preparer,
        material_lifecycle=material_lifecycle,
        assistant_runtime_provider=assistant_process_supervisor.require_for_output,
        input_diversion_port=self_reflection_service,
        current_runtime_epoch=lambda: assistant_process_supervisor.client.current_epoch,
    )
    # Hub 持有唯一配置门禁，让写事务、HTTP 读取和控制连接初始快照共享同一边界。
    configuration_mutation_lock = hub.configuration_mutation_lock
    user_settings = UserSettingsCoordinator(
        settings=settings,
        hub=hub,
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            settings.user_settings_path
        ),
        skill_registry=skill_registry,
        configuration_mutation_lock=configuration_mutation_lock,
    )
    # 运行时配置服务：负责启动密钥、LLM 设置和运行时世代热替换。
    async def stop_voice_setup_consumers() -> None:
        """语音配置前停止媒体和主动任务，防止旧轮次继续占用推理资源。"""

        await timed_reminder_scheduler.shutdown()
        await timed_reminder_delivery.shutdown()
        await turn_admission.clear_pending_for_runtime_transition()
        if livekit_session_service is not None:
            await livekit_session_service.shutdown()

    def resume_runtime_consumers() -> None:
        """正式助手运行时就绪后恢复新轮次和提醒准入。"""

        turn_admission.resume_after_runtime_transition()
        timed_reminder_delivery.start()
        timed_reminder_scheduler.start()

    async def restart_livekit_media_session() -> bool:
        """重建当前媒体会话；连接事实已被清理时按启动预热语义重新入房。

        Returns:
            当前存在已入房的媒体会话时返回真。
        """

        if await livekit_media_session_controller.restart_current_session():
            return True
        if livekit_session_service is None:
            return False
        # 运行时替换和语音配置 retire 都会抹掉 Controller 保存的连接事实，
        # 只重启会静默返回假，最终留下“有运行时、无音频”的半成品状态。
        # 此时按启动预热同样的语义重新签发并入房。
        try:
            await livekit_session_service.ensure_backend_media_session_started()
        except Exception:
            # 与启动预热一致的宽限：媒体层暂时不可用只记录诊断，不能让配置
            # 事务因此整体回滚；Desktop 下次请求会话时仍会重新触发入房。
            logger.exception("LiveKit 媒体会话重建失败，等待下一次会话请求重新入房")
            return False
        return True

    runtime_reconfiguration = RuntimeReconfigurationCoordinator(
        settings=settings,
        hub=hub,
        realtime_input_runtime_owner=realtime_input_runtime_owner,
        assistant_process_supervisor=assistant_process_supervisor,
        activity_service=activity_service,
        background_agent_interactions=background_agent_interactions,
        interrupt_runtime_activities=livekit_media_session_controller.interrupt_runtime_activities,
        interrupt_transition_materials=runtime_event_service.interrupt_task_result_deliveries,
        interrupt_transition_notices=notice_delivery_coordinator.interrupt_active,
        release_runtime_memory_compaction=(
            runtime_memory_store.release_compaction_plan_for_runtime
        ),
        terminate_self_reflection_runtime=(
            lambda old_epoch, new_epoch: self_reflection_service.terminate_runtime_epoch(
                old_epoch,
                new_runtime_epoch=new_epoch,
            )
        ),
        publish_assistant_candidate=(
            lambda candidate, mark_committed: self_reflection_service.publish_runtime_candidate(
                lambda: assistant_process_supervisor.publish_candidate(candidate),
                mark_committed,
            )
        ),
        restart_livekit_media_session=restart_livekit_media_session,
        configuration_mutation_lock=configuration_mutation_lock,
        tts_setup=tts_setup,
        # Key 变化可能不改变就绪摘要；显式配置提交后仍需通知前端重取账户等关联数据。
        publish_application_status=partial(application_status.publish_changed, force=True),
        stop_voice_setup_consumers=stop_voice_setup_consumers,
        resume_runtime_consumers=resume_runtime_consumers,
    )
    # 应用生命周期：统一收口启动与停止流程，按需挂载 LiveKit 会话。
    lifecycle = ApplicationLifecycle(
        hub=hub,
        application_status=application_status,
        realtime_input_runtime_owner=realtime_input_runtime_owner,
        assistant_process_supervisor=assistant_process_supervisor,
        livekit_session_service=livekit_session_service,
        background_agent_usage_service=background_agent_usage_service,
        runtime_event_service=runtime_event_service,
        timed_reminder_delivery=timed_reminder_delivery,
        timed_reminder_scheduler=timed_reminder_scheduler,
        tts_setup=tts_setup,
    )

    # 装配完成：注入全部显式依赖；末尾内联构造的是仅供调试/辅助的控制台服务。
    return ApplicationContainer(
        settings=settings,
        realtime_audio_config=realtime_audio_config,
        hub=hub,
        application_status=application_status,
        realtime_input_runtime_owner=realtime_input_runtime_owner,
        assistant_process_supervisor=assistant_process_supervisor,
        runtime_identity_factory=runtime_identity_factory,
        assistant_request_identity_registry=assistant_request_identity_registry,
        assistant_request_ownership_ledger=assistant_request_ownership_ledger,
        assistant_output_health_ledger=assistant_output_health_ledger,
        assistant_runtime_event_queue=assistant_runtime_event_queue,
        runtime_event_service=runtime_event_service,
        visible_output_journal=visible_output_journal,
        material_lifecycle=material_lifecycle,
        activity_service=activity_service,
        timed_reminder_service=timed_reminder_service,
        timed_reminder_delivery=timed_reminder_delivery,
        timed_reminder_scheduler=timed_reminder_scheduler,
        background_agent_interactions=background_agent_interactions,
        background_agent_interaction_service=background_agent_interaction_service,
        background_agent_usage_service=background_agent_usage_service,
        assistant_request_context_preparer=assistant_request_context_preparer,
        turn_submission=turn_submission_service,
        turn_admission=turn_admission,
        user_settings=user_settings,
        runtime_reconfiguration=runtime_reconfiguration,
        tts_setup=tts_setup,
        speaker_verification=SpeakerVerificationConsoleService(
            hub=hub,
            realtime_input_runtime_owner=realtime_input_runtime_owner,
        ),
        character_voice=CharacterVoiceService(
            hub=hub,
            assistant_process_supervisor=assistant_process_supervisor,
        ),
        wake_response=WakeResponseConsoleService(
            hub=hub,
            assistant_process_supervisor=assistant_process_supervisor,
            prompt_audit_recorder=prompt_audit_recorder,
        ),
        streamvox_examples=StreamVoxExampleConsoleService(
            hub=hub,
            prompt_audit_recorder=prompt_audit_recorder,
        ),
        livekit_media_session_controller=livekit_media_session_controller,
        playout_delivery_registry=playout_delivery_registry,
        task_result_delivery_coordinator=task_result_delivery_coordinator,
        runtime_memory_store=runtime_memory_store,
        self_reflection_service=self_reflection_service,
        activity_progress_ledger=activity_progress_ledger,
        livekit_session_service=livekit_session_service,
        lifecycle=lifecycle,
    )
