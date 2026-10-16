from __future__ import annotations

import asyncio
from collections.abc import Callable

from companion.config.realtime_audio import RealtimeAudioYamlConfig, load_realtime_audio_config
from companion.core.settings import Settings
from companion.livekit.models import (
    AecSidecarDebugAudioSessionConfig,
    AudioDeviceSessionConfig,
    LiveKitParticipantSession,
    LiveKitSession,
)
from companion.livekit.token_service import LiveKitTokenConfig, LiveKitTokenService
from companion.realtime.livekit_media_session import LiveKitMediaSessionController


class LiveKitSessionService:
    """LiveKit 桌面端会话编排服务。"""

    def __init__(
        self,
        *,
        settings: Settings,
        media_session_controller: LiveKitMediaSessionController | None = None,
        realtime_audio_config_loader: Callable[[], RealtimeAudioYamlConfig] = load_realtime_audio_config,
    ) -> None:
        """初始化 LiveKit 会话编排服务。

        Args:
            settings: LiveKit 服务与参与者身份配置。
            media_session_controller: Backend 媒体会话生命周期控制器。
            realtime_audio_config_loader: 读取实时音频 YAML 配置的端口。
        """

        # Settings 是运行期共享配置对象；服务只读取 LiveKit 相关字段，不修改启动密钥或其它配置。
        self.settings = settings
        self.media_session_controller = media_session_controller
        # realtime_audio_config_loader 允许测试注入稳定配置，同时让生产环境每次 session 请求都读取最新 YAML。
        self._realtime_audio_config_loader = realtime_audio_config_loader
        # 媒体会话启动边界需要串行化，避免并发请求重复拉起同一个 Backend 会话。
        self._media_session_start_lock = asyncio.Lock()
        self._token_service = LiveKitTokenService(
            LiveKitTokenConfig(
                api_key=settings.livekit_api_key,
                api_secret=settings.livekit_api_secret,
            )
        )

    def build_room_name(self) -> str:
        """生成本地 LiveKit room 名称。"""

        # 房间前缀允许通过配置覆盖；裁剪后为空时使用产品默认前缀保持本地链路稳定。
        prefix = self.settings.livekit_room_prefix.strip() or "companion-v1"
        return f"{prefix}-local"

    async def ensure_backend_media_session_started(self) -> None:
        """确保 Backend 媒体会话已进入固定本地房间。"""

        # Desktop 与 Backend 共享固定房间，Backend 重启后重连即可继续消费已发布的桌面音轨。
        await self._ensure_backend_media_session_started_for_room(
            self.build_room_name()
        )

    async def create_desktop_session(self) -> LiveKitSession:
        """创建 desktop 连接 LiveKit 所需的会话信息。"""

        # 产品约定一个固定本地房间，便于 Desktop 与 Backend 在任意一方重连后重新汇合。
        room_name = self.build_room_name()
        # Desktop 会话与 Backend 预热共用同一条媒体会话入房逻辑，避免房间名或身份漂移。
        await self._ensure_backend_media_session_started_for_room(room_name)

        # renderer UI participant 每次请求都带随机后缀，负责接收助手媒体轨和房间事件，不再承担原始麦克风发布职责。
        desktop_identity = LiveKitTokenService.create_participant_identity(
            self.settings.livekit_desktop_identity_prefix,
        )
        # UI token 只赋予加入当前房间的媒体发布和订阅权限，响应中不包含 API secret。
        desktop_token = self._token_service.create_join_token(
            room_name,
            desktop_identity,
            "Companion Desktop",
        )
        # AEC sidecar participant 使用独立身份，方便 backend 后续只订阅 clean mic 音轨并拒绝 renderer 原始麦克风。
        aec_identity = LiveKitTokenService.create_participant_identity(
            self.settings.livekit_aec_identity_prefix,
        )
        # sidecar token 绑定同一个固定房间，名称写入 LiveKit participant 元数据，便于控制台和日志诊断区分职责。
        aec_token = self._token_service.create_join_token(
            room_name,
            aec_identity,
            "Companion AEC Sidecar",
        )
        # 每次签发会话时重读 YAML，让 AEC 调试开关与设备配置及时生效。
        realtime_audio_config = self._realtime_audio_config_loader()
        return LiveKitSession(
            url=self.settings.livekit_url,
            room_name=room_name,
            desktop_ui=LiveKitParticipantSession(
                identity=desktop_identity,
                token=desktop_token,
            ),
            desktop_aec=LiveKitParticipantSession(
                identity=aec_identity,
                token=aec_token,
            ),
            aec_sidecar_debug_audio=AecSidecarDebugAudioSessionConfig(
                enabled=realtime_audio_config.aec_sidecar_debug_audio.enabled,
                output_dir=realtime_audio_config.aec_sidecar_debug_audio.output_dir,
                max_runs=realtime_audio_config.aec_sidecar_debug_audio.max_runs,
            ),
            audio_devices=AudioDeviceSessionConfig(
                mode=self.settings.audio_device_mode,
                microphone_id=self.settings.audio_input_device_id,
                microphone_name=self.settings.audio_input_device_name,
                speaker_loopback_id=self.settings.audio_output_reference_device_id,
                speaker_loopback_name=self.settings.audio_output_reference_device_name,
            ),
        )

    async def shutdown(self) -> None:
        """关闭 LiveKit 会话服务托管的 Backend 媒体资源。"""

        # 轻量运行模式可能不挂载 Backend 媒体会话，此时没有资源需要释放。
        if self.media_session_controller is None:
            return

        # Session Service 只发出关闭命令，具体资源所有权仍在 Controller。
        await self.media_session_controller.shutdown()

    async def _ensure_backend_media_session_started_for_room(
        self,
        room_name: str,
    ) -> None:
        """按给定房间名确保 Backend 媒体会话已启动。

        Args:
            room_name: Backend 媒体会话需要进入的房间名。
        """

        # 轻量接线场景只签发 Desktop token，不要求 Backend 媒体会话真实入房。
        if self.media_session_controller is None:
            return

        # 预热与并发 Session API 请求共用一个启动边界，锁保证只创建一个媒体会话。
        async with self._media_session_start_lock:
            # 该 token 只交给 Backend 媒体会话，不返回 Desktop，也不写入日志或响应。
            media_session_token = self._token_service.create_join_token(
                room_name,
                self.settings.livekit_media_session_identity,
                "Companion Media Session",
            )
            await self.media_session_controller.ensure_session_started(
                url=self.settings.livekit_url,
                room_name=room_name,
                token=media_session_token,
                identity=self.settings.livekit_media_session_identity,
            )
