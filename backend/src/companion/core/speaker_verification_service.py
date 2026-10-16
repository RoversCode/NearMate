from __future__ import annotations

from companion.audio.pcm16_utils import REQUIRED_SPEAKER_VERIFICATION_SPEECH_SECONDS
from companion.core.broadcast import BroadcastHub
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.core.settings import Settings
from companion.core.speaker_verification_assets import MAX_SPEAKER_VERIFICATION_SAMPLE_COUNT


class SpeakerVerificationConsoleService:
    """管理控制台说话人确认状态。

    Args:
        hub: 控制面广播和资产仓储中心。
        realtime_input_runtime_owner: 当前实时输入运行时所有者。
    """

    def __init__(
        self,
        *,
        hub: BroadcastHub,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
    ) -> None:
        self.hub = hub
        self.realtime_input_runtime_owner = realtime_input_runtime_owner

    def current_profile(self) -> dict[str, object]:
        """构造说话人确认控制台状态。

        Returns:
            控制台可直接消费的说话人确认开关、样本列表和录音门槛。
        """

        samples = self.hub.speaker_verification_assets.list_samples()
        return {
            "enabled": self.hub.settings.speaker_verification_enabled,
            "samples": [sample.to_payload() for sample in samples],
            "max_sample_count": MAX_SPEAKER_VERIFICATION_SAMPLE_COUNT,
            "required_speech_seconds": REQUIRED_SPEAKER_VERIFICATION_SPEECH_SECONDS,
            "can_enable": len(samples) > 0,
        }

    def current_whitelist_audio_paths(self) -> list[str]:
        """读取当前 console 录音资产对应的白名单路径。

        Returns:
            当前全部说话人样本音频文件的绝对路径字符串。
        """

        samples = self.hub.speaker_verification_assets.list_samples()
        return [str(sample.audio_path) for sample in samples]

    def build_candidate_settings(
        self,
        current_settings: Settings,
        *,
        enabled: bool,
    ) -> Settings:
        """根据当前样本资产构造热替换 provider 所需的 Settings 候选。

        Args:
            current_settings: 统一提交锁保护下的最新设置。
            enabled: 用户希望保存的说话人确认开关。

        Returns:
            已同步白名单路径的 Settings 副本。
        """

        speaker_whitelist_audio_paths = self.current_whitelist_audio_paths()
        return current_settings.model_copy(
            update={
                "speaker_verification_enabled": enabled and bool(speaker_whitelist_audio_paths),
                "speaker_whitelist_audio_paths": speaker_whitelist_audio_paths,
            },
            deep=True,
        )

    def settings_patch(self, candidate_settings: Settings) -> dict[str, object]:
        """提取说话人确认领域拥有的设置字段。

        Args:
            candidate_settings: 已同步当前样本路径的候选设置。

        Returns:
            说话人确认开关和白名单路径字段 patch。
        """

        return {
            "speaker_verification_enabled": (
                candidate_settings.speaker_verification_enabled
            ),
            "speaker_whitelist_audio_paths": list(
                candidate_settings.speaker_whitelist_audio_paths
            ),
        }

    def prepare_settings_patch(
        self,
        current_settings: Settings,
        *,
        enabled: bool,
    ) -> dict[str, object]:
        """同步最新样本、热替换 provider，并生成设置 patch。

        Args:
            current_settings: 统一提交锁保护下的最新设置。
            enabled: 用户希望保留的说话人确认开关。

        Returns:
            与实际样本列表一致的开关和白名单路径。
        """

        candidate_settings = self.build_candidate_settings(
            current_settings,
            enabled=enabled,
        )
        self.rebuild_provider(candidate_settings)
        return self.settings_patch(candidate_settings)

    def rebuild_provider(self, settings_for_provider: Settings) -> None:
        """在真实运行时存在时热替换说话人确认 provider。"""

        self.realtime_input_runtime_owner.rebuild_speaker_verification_provider(
            settings_for_provider
        )
