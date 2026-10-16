from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LiveKitParticipantSession:
    """表示单个 LiveKit participant 的入房信息。"""

    identity: str
    token: str

    def to_payload(self) -> dict[str, str]:
        """转换为可直接返回给客户端的 participant 载荷。"""

        # participant payload 只暴露客户端入房所需字段，避免调用方依赖 dataclass 内部结构。
        return {
            "identity": self.identity,
            "token": self.token,
        }


@dataclass(frozen=True)
class AecSidecarDebugAudioSessionConfig:
    """表示 AEC sidecar 的连续调试录音会话配置。"""

    enabled: bool
    output_dir: str
    max_runs: int

    def to_payload(self) -> dict[str, object]:
        """转换为 sidecar 会话配置载荷。"""

        # payload 使用 snake_case，与后端其它会话响应字段风格保持一致，降低协议歧义。
        return {
            "enabled": self.enabled,
            "output_dir": self.output_dir,
            "max_runs": self.max_runs,
        }


@dataclass(frozen=True)
class AudioDeviceSessionConfig:
    """表示 desktop sidecar 应使用的音频设备选择。"""

    mode: str
    microphone_id: str | None
    microphone_name: str | None
    speaker_loopback_id: str | None
    speaker_loopback_name: str | None

    def to_payload(self) -> dict[str, object]:
        """转换为 sidecar 设备选择载荷。"""

        return {
            "mode": self.mode,
            "microphone_id": self.microphone_id,
            "microphone_name": self.microphone_name,
            "speaker_loopback_id": self.speaker_loopback_id,
            "speaker_loopback_name": self.speaker_loopback_name,
        }


@dataclass(frozen=True)
class LiveKitSession:
    """表示桌面端连接 LiveKit 所需的完整会话信息。"""

    url: str
    room_name: str
    desktop_ui: LiveKitParticipantSession
    desktop_aec: LiveKitParticipantSession
    aec_sidecar_debug_audio: AecSidecarDebugAudioSessionConfig
    audio_devices: AudioDeviceSessionConfig

    def to_payload(self) -> dict[str, object]:
        """转换为桌面端当前嵌套 participant 协议的会话载荷。"""

        return {
            "url": self.url,
            "room_name": self.room_name,
            "desktop_ui": self.desktop_ui.to_payload(),
            "desktop_aec": self.desktop_aec.to_payload(),
            "aec_sidecar_debug_audio": self.aec_sidecar_debug_audio.to_payload(),
            "audio_devices": self.audio_devices.to_payload(),
        }
