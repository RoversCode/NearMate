"""控制服务按业务域组织的请求模型。"""

from companion.core.api.schemas.bootstrap import (
    BootstrapInitializeRequest,
    LlmConfigRequest,
    QWeatherApiKeyRequest,
    TtsSetupStartRequest,
)
from companion.core.api.schemas.character_cards import (
    CharacterCardCloneRequest,
    StreamVoxExampleGenerationRequest,
    WakeResponseConfigRequest,
    WakeResponseItemRequest,
    WakeResponsePreviewRequest,
    WakeResponseSynthesizeRequest,
)
from companion.core.api.schemas.interaction import (
    EnvironmentSnapshotRequest,
    MaterialItemKind,
    MaterialItemRequest,
    MaterialItemsRequest,
    RecentMaterialForgetRequest,
    TextTurnContextRequest,
    TextTurnEditRequest,
    TextTurnPreparationFailureRequest,
    TextTurnReservationRequest,
    TextPasteRequest,
)
from companion.core.api.schemas.settings import (
    AudioDeviceApplyResultRequest,
    AudioDeviceSettingsRequest,
    AudioDeviceTestRequest,
    SettingsRequest,
    ShortcutId,
    ShortcutStatus,
    ShortcutStatusItemRequest,
    ShortcutStatusReportRequest,
    ToolWaitingFamilyPolicyRequest,
    ToolWaitingToolPolicyRequest,
)
from companion.core.api.schemas.speech import (
    SpeakerVerificationProfileRequest,
    StreamVoxConfigRequest,
    StreamVoxRolePreviewRequest,
)


__all__ = [
    "AudioDeviceApplyResultRequest",
    "AudioDeviceSettingsRequest",
    "AudioDeviceTestRequest",
    "BootstrapInitializeRequest",
    "CharacterCardCloneRequest",
    "EnvironmentSnapshotRequest",
    "LlmConfigRequest",
    "MaterialItemKind",
    "MaterialItemRequest",
    "MaterialItemsRequest",
    "RecentMaterialForgetRequest",
    "TextPasteRequest",
    "TextTurnContextRequest",
    "TextTurnEditRequest",
    "TextTurnPreparationFailureRequest",
    "TextTurnReservationRequest",
    "QWeatherApiKeyRequest",
    "TtsSetupStartRequest",
    "SettingsRequest",
    "ShortcutId",
    "ShortcutStatus",
    "ShortcutStatusItemRequest",
    "ShortcutStatusReportRequest",
    "SpeakerVerificationProfileRequest",
    "StreamVoxConfigRequest",
    "StreamVoxExampleGenerationRequest",
    "StreamVoxRolePreviewRequest",
    "ToolWaitingFamilyPolicyRequest",
    "ToolWaitingToolPolicyRequest",
    "WakeResponseConfigRequest",
    "WakeResponseItemRequest",
    "WakeResponsePreviewRequest",
    "WakeResponseSynthesizeRequest",
]
