from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from fastapi import WebSocket

from companion.bootstrap.service import BootstrapService
from companion.contracts.events import (
    ControlEventDomain,
    EventCatalog,
    load_event_catalog,
)
from companion.character_cards.experience_assets import CharacterCardExperienceAssets
from companion.character_cards.runtime_config import (
    CharacterExperienceRuntimeConfig,
    resolve_character_experience_runtime_config,
)
from companion.character_cards.store import CharacterCardStore
from companion.core.settings import Settings
from companion.core.speaker_verification_assets import SpeakerVerificationAssetStore
from companion.core.shortcut_status import ShortcutStatusStore
from companion.core.user_settings_store import (
    SETTINGS_REVISION_FIELD,
    UserSettingsStore,
    apply_user_settings_snapshot,
)
from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.catalog.waiting_policy import (
    ToolWaitingPolicy,
    ToolWaitingPolicyConfig,
    ToolWaitingPolicyResolver,
    ToolWaitingPolicyStore,
)
from companion.environment.service import EnvironmentContextService
from companion.materials.recent_registry import RecentMaterialRegistry
from companion.materials.recent_projection import project_recent_materials
from companion.materials.turn_context import TurnMaterialContextService
from companion.observability.logging_config import get_backend_logger
from companion.records.bootstrap_secret_store import BootstrapSecretStore
from companion.records.runtime_log import RuntimeLogBuffer
from companion.realtime.interaction_state import InteractionState
from companion.realtime.shell_projector import ShellProjector
from companion.wake_response.service import WakeResponseService
from companion.foreground_tools.external.weather_tool import WeatherForegroundTool


logger = get_backend_logger(__name__)

# 单条控制连接的发送最长等待秒数；对端卡死或背压堆积时不能拖垮整轮广播。
CONTROL_CONNECTION_SEND_TIMEOUT_SECONDS = 5.0


class DomainEventPublisher:
    """只发布共享目录中属于一个明确领域的公共事件。

    Args:
        hub: 真正持有控制连接和运行日志的广播中枢。
        domain: 当前 Publisher 唯一允许发布的事件领域。
    """

    def __init__(self, *, hub: "BroadcastHub", domain: ControlEventDomain) -> None:
        self._hub = hub
        self.domain = domain

    async def publish(self, payload: dict[str, object]) -> None:
        """校验领域后广播事件并执行既有 Shell 投影。

        Args:
            payload: 包含 type 和 payload 的公共控制事件。
        """

        await self._hub._publish_domain_event(  # noqa: SLF001
            domain=self.domain,
            payload=payload,
            ephemeral=False,
        )

    async def publish_ephemeral(self, payload: dict[str, object]) -> None:
        """校验领域后广播不写运行日志的瞬时事件。

        Args:
            payload: 包含 type 和 payload 的瞬时公共事件。
        """

        await self._hub._publish_domain_event(  # noqa: SLF001
            domain=self.domain,
            payload=payload,
            ephemeral=True,
        )


def policy_payload_or_none(policy: ToolWaitingPolicy | None) -> dict[str, object] | None:
    """把可选等待策略转换为 API 响应字段。"""

    if policy is None:
        return None
    return policy.to_payload()


@dataclass(eq=False, slots=True)
class _ControlConnection:
    """封装单条控制 WebSocket 及其串行发送边界。"""

    websocket: WebSocket
    scope: str
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, payload: dict[str, object]) -> None:
        """在连接级锁内发送一条 JSON 事件。

        Args:
            payload: 需要发送的控制事件。
        """

        async with self.send_lock:
            await self.websocket.send_json(payload)

    async def close(self) -> None:
        """等待当前发送结束后关闭底层 WebSocket。"""

        async with self.send_lock:
            await self.websocket.close()


class BroadcastHub:
    """控制面广播中心。"""

    def __init__(
        self,
        settings: Settings,
        *,
        interaction_state: InteractionState | None = None,
    ) -> None:
        self.settings = settings
        # 所有会临时改写 Settings 或启动密钥的事务共用这一把门禁。Hub 持有
        # 它，控制连接初始化才能和设置写事务使用同一个可见性边界。
        self.configuration_mutation_lock = asyncio.Lock()
        self._event_catalog: EventCatalog = load_event_catalog()
        self._domain_publishers: dict[ControlEventDomain, DomainEventPublisher] = {}
        # 启动密钥仓储，管理 LLM/TTS 等 provider 所需的敏感配置。
        self.bootstrap_secret_store = BootstrapSecretStore(settings.bootstrap_db_path)
        self.bootstrap_secret_store.initialize()
        # 运行日志缓冲区，聚合后台日志供控制面展示。
        self.runtime_logs = RuntimeLogBuffer()
        # 用户设置持久化仓储，负责控制台可编辑项的跨重启恢复。
        self.user_settings_store = UserSettingsStore(settings.user_settings_path)
        # 快捷键状态仓储，记录热键注册与触发状态。
        self.shortcut_status_store = ShortcutStatusStore()
        # 工具等待策略仓储，管理调用工具前的等待确认规则。
        self.tool_waiting_policy_store = ToolWaitingPolicyStore(settings.tool_waiting_policies_path)
        # 回合素材上下文服务，聚合当前交互所需的上下文数据。
        self.turn_materials = TurnMaterialContextService()
        # 近期材料独立保存用户仍可直接指代的实时路径，不与 staged/active/retained 生命周期混用。
        self.recent_materials = RecentMaterialRegistry()
        # 环境上下文服务，提供设备、位置与环境感知信息。
        self.environment = EnvironmentContextService()
        # 角色卡仓储，管理角色卡资产与默认角色初始化。
        self.character_cards = CharacterCardStore(
            settings.character_cards_dir,
            seed_dir=settings.character_card_seed_dir,
        )
        self.character_cards.ensure_seed_assets()
        # 角色卡经验资产，管理角色相关的音频、图片等体验资源。
        self.character_card_assets = CharacterCardExperienceAssets(
            settings.character_cards_dir, seed_dir=settings.character_card_seed_dir
        )
        # 声纹验证资产仓储，管理说话人样本与识别白名单。
        self.speaker_verification_assets = SpeakerVerificationAssetStore(
            self.bootstrap_secret_store.db_path.parent / "speaker_verification_samples"
        )
        # 当前进行中的声纹录制会话，键为会话标识，值为音频数据。
        self.speaker_recording_sessions: dict[str, bytes] = {}
        # 启动服务，负责密钥注水与运行时配置装配。
        self.bootstrap = BootstrapService(
            secret_store=self.bootstrap_secret_store,
            settings=self.settings,
        )
        # 依赖真实密钥的运行时装配之前先完成启动密钥注水，确保后续 LLM/TTS provider 读取到最新配置。
        self.bootstrap.hydrate_runtime_settings()

        # 控制台允许用户在运行期修改的设置，需要跨重启恢复；新版本优先读取本地用户设置文件。
        user_settings_snapshot = self.user_settings_store.load(self.settings)
        persisted_settings_revision = 0
        if user_settings_snapshot is not None:
            # 用户偏好文件包含公开字段和 revision 元数据；字段集中回填，
            # revision 单独恢复，避免把元数据误写进 Settings 模型。
            apply_user_settings_snapshot(self.settings, user_settings_snapshot)
            persisted_settings_revision = int(
                user_settings_snapshot[SETTINGS_REVISION_FIELD]
            )

        # 说话人白名单唯一来源是 console 录音资产；启动恢复时必须用资产仓储覆盖用户设置文件中的旧路径。
        speaker_samples = self.speaker_verification_assets.list_samples()
        self.settings.speaker_whitelist_audio_paths = [str(sample.audio_path) for sample in speaker_samples]
        if not speaker_samples:
            self.settings.speaker_verification_enabled = False

        # Shell 投射器，负责在桌面显示语音气泡等交互反馈。
        self.projector = ShellProjector(show_speech_bubbles=settings.show_speech_bubbles)
        # 唤醒响应服务，管理角色唤醒时的应答音频与展示逻辑。
        self.wake_response_service = WakeResponseService(
            asset_dir=self.character_card_assets.wake_response_audio_dir(self.character_cards.load_active_card_id())
        )

        # bootstrap 未完成时还没有真实 RealtimeInputRuntime，这里保留一份可被后续运行时接管的交互状态源。
        self.interaction_state = interaction_state or InteractionState(followup_timeout_s=settings.followup_timeout_s)
        # 已建立的控制连接列表，用于向所有控制端广播消息。
        self._connections: list[_ControlConnection] = []
        # 运行时进入清理后，运行连接只能再收到应用状态事件。
        self.runtime_events_enabled = True
        # 设置版本必须跨 Backend 重启单调恢复，否则仍在线的 Console 会把新进程
        # 较小的 revision 误判成迟到事件并永久拒绝权威快照。
        self.settings_revision = persisted_settings_revision
        # 运行时配置事务使用独立的进程内代际。它不把天气/LLM 密钥混入用户偏好
        # 文件，但能让联网校验跨越 A→B→A 时仍然识别出中间发生过一次提交。
        self.runtime_configuration_revision = 0
        # 素材版本号，控制端通过该版本判断是否需要同步最新素材。
        self.material_revision = 0

    async def subscribe_control(
        self,
        websocket: WebSocket,
        initial_events_factory: Callable[[], Iterable[dict[str, object]]],
        *,
        scope: str = "runtime",
    ) -> None:
        """登记控制连接，并保证初始快照先于该连接的实时广播。

        Args:
            websocket: 已完成 accept 的控制 WebSocket。
            initial_events_factory: 构造首帧的同步函数；状态范围只读取应用安全快照。
            scope: 状态连接只读初始化进度；业务连接须在配置门禁内冻结首帧。

        Returns:
            无返回值；初始事件全部发送完成后连接才进入正常广播队列。
        """

        connection = _ControlConnection(websocket=websocket, scope=scope)
        if scope == "status":
            # 模型加载会长时间持有配置门禁，状态连接必须能在加载期间接收进度。
            # 新连接的发送锁尚无人持有；同步冻结首帧到登记连接之间不会让出事件循环。
            initial_events = tuple(initial_events_factory())
            await connection.send_lock.acquire()
            self._connections.append(connection)
        else:
            # 业务首帧仍按“配置门禁 → 连接发送锁”取锁，不能读取事务中的临时配置。
            async with self.configuration_mutation_lock:
                initial_events = tuple(initial_events_factory())
                await connection.send_lock.acquire()
                self._connections.append(connection)

        try:
            # 网络发送不能占着全局配置门禁，慢客户端只阻塞自己的事件队列。
            # 连接锁保持到全部首帧发送结束，后续广播会自然排在首帧之后。
            try:
                for event in initial_events:
                    await websocket.send_json(event)
            except BaseException:
                self._remove_connection(connection)
                raise
        finally:
            connection.send_lock.release()

    def unsubscribe_control(self, websocket: WebSocket) -> None:
        """移除指定控制 WebSocket 的订阅。

        Args:
            websocket: 已断开或主动关闭的控制连接。
        """

        self._connections = [
            connection
            for connection in self._connections
            if connection.websocket is not websocket
        ]

    async def close_control_connections(self) -> None:
        """从订阅表摘除并串行关闭当前全部控制连接。"""

        connections = tuple(self._connections)
        self._connections.clear()
        for connection in connections:
            try:
                await connection.close()
            except Exception as exception:
                # 应用关闭不能因单条客户端连接状态异常而阻塞其它资源收口。
                logger.warning(
                    "Control websocket close failed during shutdown: %s",
                    exception,
                )

    async def close_runtime_control_connections(self) -> None:
        """只关闭业务运行连接，保留状态连接继续接收授权变化。"""

        runtime_connections = tuple(
            connection for connection in self._connections if connection.scope == "runtime"
        )
        self._connections = [
            connection for connection in self._connections if connection.scope != "runtime"
        ]
        for connection in runtime_connections:
            try:
                await connection.close()
            except Exception as exception:
                logger.warning("Runtime control websocket close failed: %s", exception)

    def current_material_context_payload(self) -> dict[str, object]:
        """读取材料上下文视图。

        Returns:
            包含 staged 与 active turn 材料上下文的公开载荷。
        """

        return {
            "revision": self.material_revision,
            **self.turn_materials.current_payload(),
            "recent_materials": project_recent_materials(
                self.recent_materials.snapshot()
            ),
        }

    async def _broadcast(self, payload: dict[str, object]) -> None:
        """把已通过领域校验的事件广播给所有活动连接。"""

        event_payload = payload.get("payload")
        self.runtime_logs.append(
            event_type=str(payload.get("type") or "unknown"),
            payload=dict(event_payload) if isinstance(event_payload, dict) else {},
        )
        await self._send_to_control_connections(payload)

    def publisher(self, domain: ControlEventDomain) -> DomainEventPublisher:
        """取得一个领域绑定且可复用的公共事件 Publisher。

        Args:
            domain: 共享事件目录定义的业务领域。

        Returns:
            每次发布都会核对事件领域的稳定 Publisher。
        """

        publisher = self._domain_publishers.get(domain)
        if publisher is None:
            publisher = DomainEventPublisher(hub=self, domain=domain)
            self._domain_publishers[domain] = publisher
        return publisher

    async def _publish_domain_event(
        self,
        *,
        domain: ControlEventDomain,
        payload: dict[str, object],
        ephemeral: bool,
    ) -> None:
        """校验公共事件领域并执行唯一发送流程。

        Args:
            domain: 调用 Publisher 已绑定的领域。
            payload: 准备发送的公共控制事件。
            ephemeral: 为真时只发送，不写运行日志或追加 Shell 投影。
        """

        event_name = payload.get("type")
        if not isinstance(event_name, str) or not event_name.strip():
            self._event_catalog.require_core_domain("", domain)
        self._event_catalog.require_core_domain(str(event_name), domain)
        if ephemeral:
            await self._send_to_control_connections(payload)
            return

        await self._broadcast(payload)
        if event_name != "assistant.text_final":
            return
        projected = self.projector.project_assistant_reply(
            text=str(payload.get("payload", {}).get("text", ""))
            if isinstance(payload.get("payload"), dict)
            else ""
        )
        if projected is None:
            return
        self._event_catalog.require_core_domain(
            str(projected.get("type") or ""),
            "shell",
        )
        await self._broadcast(projected)

    async def _send_to_control_connections(self, payload: dict[str, object]) -> None:
        """向订阅快照中的连接串行发送事件并淘汰失效连接。

        Args:
            payload: 需要广播的控制事件。
        """

        stale_connections: list[_ControlConnection] = []
        event_name = payload.get("type")
        event_domain = (
            self._event_catalog.domain_for(str(event_name))
            if isinstance(event_name, str)
            else None
        )
        for connection in tuple(self._connections):
            # 状态连接只接收 application 领域事件，授权无效时也不能看到业务快照。
            if connection.scope == "status" and event_domain != "application":
                continue
            if connection.scope == "runtime" and not getattr(self, "runtime_events_enabled", True) and event_domain != "application":
                continue
            try:
                # 逐连接限时发送；单条坏连接（对端重置、发送缓冲爆满）只淘汰自己，
                # 不能中止整轮广播，运行时收口前的强制广播必须送达其余连接。
                await asyncio.wait_for(
                    connection.send(payload),
                    timeout=CONTROL_CONNECTION_SEND_TIMEOUT_SECONDS,
                )
            except Exception as exception:
                # 载荷可能包含用户正文，日志只保留事件类型与异常类型。
                logger.warning(
                    "控制连接发送失败，按失效连接淘汰 event_type=%s error_type=%s",
                    event_name,
                    type(exception).__name__,
                )
                stale_connections.append(connection)
        for connection in stale_connections:
            self._remove_connection(connection)
            try:
                # 仅从列表移除不会唤醒路由里的 receive_text 循环；主动关闭才能真正释放
                # WebSocket、发送缓冲和 ASGI 任务，且关闭本身同样不能拖住其它连接。
                await asyncio.wait_for(
                    connection.close(),
                    timeout=CONTROL_CONNECTION_SEND_TIMEOUT_SECONDS,
                )
            except Exception as exception:
                logger.warning(
                    "控制连接关闭失败 event_type=%s error_type=%s",
                    event_name,
                    type(exception).__name__,
                )

    def _remove_connection(self, connection: _ControlConnection) -> None:
        """按订阅对象身份移除一条失效连接。

        Args:
            connection: 需要从当前订阅列表删除的连接对象。
        """

        self._connections = [
            current for current in self._connections if current is not connection
        ]

    def current_settings_payload(self, *, settings_revision: int | None = None) -> dict:
        """返回控制台可编辑的设置快照。

        Args:
            settings_revision: 可选设置版本号；保存新快照前用于构造下一版载荷。

        Returns:
            只包含控制台普通设置页可编辑字段的快照；角色人格名称由角色卡接口提供，不再通过普通设置暴露。
        """

        # 角色卡是独立的可编辑资产，偶发的损坏或外部文件锁不能阻断普通设置
        # 的读取和保存。字幕校准只是展示用派生字段；角色卡不可读时返回空值，
        # 等角色卡恢复后下一次快照会自动带回真实值。
        active_character_caption_timing = None
        try:
            active_card = self.character_cards.load_active_card()
        except Exception as exception:  # noqa: BLE001
            logger.warning(
                "当前角色卡不可读，设置快照暂不包含字幕校准：error_type=%s error=%s",
                type(exception).__name__,
                exception,
            )
        else:
            if active_card.meta.voice_binding.caption_timing is not None:
                active_character_caption_timing = (
                    active_card.meta.voice_binding.caption_timing.model_dump(mode="json")
                )

        return {
            "settings_revision": self.settings_revision if settings_revision is None else settings_revision,
            "show_speech_bubbles": self.settings.show_speech_bubbles,
            "active_character_caption_timing": active_character_caption_timing,
            "followup_toggle_shortcut_enabled": self.settings.followup_toggle_shortcut_enabled,
            "followup_toggle_shortcut": self.settings.followup_toggle_shortcut,
            "avatar_visibility_shortcut_enabled": self.settings.avatar_visibility_shortcut_enabled,
            "avatar_visibility_shortcut": self.settings.avatar_visibility_shortcut,
            "selection_capture_shortcut_enabled": self.settings.selection_capture_shortcut_enabled,
            "selection_capture_shortcut": self.settings.selection_capture_shortcut,
            "selected_live2d_model_id": self.settings.selected_live2d_model_id,
            "user_self_intro": self.settings.user_self_intro,
            "user_extra_notes": self.settings.user_extra_notes,
            "streamvox_device": self.settings.streamvox_device,
            "streamvox_llama_backend": self.settings.streamvox_llama_backend,
            "speaker_verification_enabled": self.settings.speaker_verification_enabled,
            "speaker_whitelist_audio_paths": list(self.settings.speaker_whitelist_audio_paths),
            "audio_device_mode": self.settings.audio_device_mode,
            "audio_input_device_id": self.settings.audio_input_device_id,
            "audio_input_device_name": self.settings.audio_input_device_name,
            "audio_output_reference_device_id": self.settings.audio_output_reference_device_id,
            "audio_output_reference_device_name": self.settings.audio_output_reference_device_name,
            "background_agent": self.settings.background_agent.model_dump(mode="json"),
        }

    def current_character_experience_runtime_config(self) -> CharacterExperienceRuntimeConfig:
        """返回当前激活角色卡派生的运行时体验配置。"""

        active_card = self.character_cards.load_active_card()
        return resolve_character_experience_runtime_config(
            card=active_card,
            wake_response_audio_dir=self.character_card_assets.wake_response_audio_dir(active_card.meta.card_id),
            streamvox_reference_audio_path=self.character_card_assets.resolve_voice_reference_audio(active_card),
            streamvox_prompt_text=self.character_card_assets.read_voice_prompt_text(active_card),
        )

    def current_prompt_profile_payload(self) -> dict[str, object]:
        """返回本轮助手请求需要携带的提示词资料快照。

        Returns:
            包含当前角色卡快照和用户资料的字典。
        """

        character_card = self.character_cards.load_active_card()
        return {
            "character_card": character_card,
            "user_call_name": character_card.meta.user_call_name,
            "user_self_intro": self.settings.user_self_intro,
            "user_extra_notes": self.settings.user_extra_notes,
        }

    def current_tool_waiting_policies_payload(self) -> dict[str, object]:
        """返回控制台工具等待反馈策略视图。"""

        # 控制台只需要目录和策略，不应为了展示配置而构造真实天气服务或触发外部 API。
        foreground_tools = ForegroundToolService(
            weather_tools=WeatherForegroundTool(service=None),
        )
        resolver = ToolWaitingPolicyResolver(store=self.tool_waiting_policy_store)
        raw_config = self.tool_waiting_policy_store.load()
        family_by_id = {
            family.family_id: family
            for family in foreground_tools.tool_family_catalog()
            if family.waiting_feedback_eligible
        }
        tool_catalog = [
            metadata
            for metadata in foreground_tools.tool_catalog()
            if metadata.family_id in family_by_id
        ]
        configurable_tool_names = {metadata.name for metadata in tool_catalog}
        config = ToolWaitingPolicyConfig(
            families={
                family_id: policy
                for family_id, policy in raw_config.families.items()
                if family_id in family_by_id
            },
            tools={
                tool_name: policy
                for tool_name, policy in raw_config.tools.items()
                if tool_name in configurable_tool_names
            },
        )
        tools_by_family: dict[str, list[dict[str, object]]] = {family_id: [] for family_id in family_by_id}

        for metadata in tool_catalog:
            family_metadata = family_by_id.get(metadata.family_id)
            effective_policy = resolver.resolve(metadata=metadata, family_metadata=family_metadata)
            tools_by_family.setdefault(metadata.family_id, []).append(
                {
                    **metadata.to_payload(),
                    "user_override": policy_payload_or_none(config.tools.get(metadata.name)),
                    "effective_policy": effective_policy.to_payload(),
                }
            )

        families: list[dict[str, object]] = []
        for family in family_by_id.values():
            user_policy = config.families.get(family.family_id)
            builtin_policy = ToolWaitingPolicy(
                enabled=family.default_waiting_enabled,
                phrases=list(family.default_waiting_phrases),
            )
            effective_family_policy = user_policy or builtin_policy
            family_source = "family" if user_policy is not None else ("builtin" if builtin_policy.enabled else "disabled")
            families.append(
                {
                    **family.to_payload(),
                    "builtin_policy": builtin_policy.to_payload(),
                    "user_policy": policy_payload_or_none(user_policy),
                    "effective_policy": {
                        **effective_family_policy.to_payload(),
                        "source": family_source,
                    },
                    "tools": tools_by_family.get(family.family_id, []),
                }
            )

        return {
            "families": families,
            "user_config": config.to_payload(),
        }

