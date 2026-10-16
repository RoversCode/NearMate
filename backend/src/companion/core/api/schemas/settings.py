from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from companion.core.user_settings_store import MAX_SAFE_SETTINGS_REVISION


class SettingsRequest(BaseModel):
    """普通设置页提交的窄字段 patch。

    Args:
        expected_revision: Console 最近读取到的完整设置版本。
        show_speech_bubbles: 是否显示角色语音气泡。
        followup_toggle_shortcut_enabled: 是否启用追问热区快捷键。
        followup_toggle_shortcut: 追问热区快捷键。
        avatar_visibility_shortcut_enabled: 是否启用桌宠显隐快捷键。
        avatar_visibility_shortcut: 桌宠显隐快捷键。
        selection_capture_shortcut_enabled: 是否启用选中内容捕获快捷键。
        selection_capture_shortcut: 选中内容捕获快捷键。
        selected_live2d_model_id: 当前选择的 Live2D 模型编号。
        user_self_intro: 用户主动填写的自我简介。
        user_extra_notes: 用户主动填写的补充说明。
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=0, le=MAX_SAFE_SETTINGS_REVISION)
    show_speech_bubbles: bool
    followup_toggle_shortcut_enabled: bool = True
    followup_toggle_shortcut: str = "CommandOrControl+Alt+Space"
    avatar_visibility_shortcut_enabled: bool = True
    avatar_visibility_shortcut: str = "CommandOrControl+Q"
    selection_capture_shortcut_enabled: bool = True
    selection_capture_shortcut: str = "CommandOrControl+Alt+C"
    selected_live2d_model_id: str
    user_self_intro: str = ""
    user_extra_notes: str = ""

    @field_validator("followup_toggle_shortcut")
    @classmethod
    def trim_followup_toggle_shortcut_or_default(cls, value: str) -> str:
        """裁剪并校验追问热区快捷键。"""

        # Electron accelerator 必须是非空字符串；更细的冲突与平台兼容交给 desktop main 注册结果处理。
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("followup_toggle_shortcut must be non-empty")
        return normalized_value

    @field_validator("avatar_visibility_shortcut")
    @classmethod
    def trim_avatar_visibility_shortcut_or_default(cls, value: str) -> str:
        """裁剪并校验桌宠召回快捷键。"""

        # Electron accelerator 必须是非空字符串；平台冲突留给 desktop main 注册结果处理。
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("avatar_visibility_shortcut must be non-empty")
        return normalized_value

    @field_validator("selection_capture_shortcut")
    @classmethod
    def trim_selection_capture_shortcut_or_default(cls, value: str) -> str:
        """裁剪并校验选中内容捕获快捷键。"""

        # Electron accelerator 必须是非空字符串；注册冲突由 desktop main 记录并兜底。
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("selection_capture_shortcut must be non-empty")
        return normalized_value

ShortcutId = Literal["followup_toggle", "avatar_visibility", "selection_capture"]
ShortcutStatus = Literal["registered", "disabled", "internal_conflict", "unavailable", "unknown"]


class ShortcutStatusItemRequest(BaseModel):
    """桌面端上报的单个快捷键注册状态。"""

    shortcut_id: ShortcutId
    accelerator: str
    enabled: bool
    status: ShortcutStatus
    settings_revision: StrictInt = Field(ge=0, le=MAX_SAFE_SETTINGS_REVISION)
    checked_at: str
    message: str

    @field_validator("accelerator", "checked_at", "message")
    @classmethod
    def trim_string_fields(cls, value: str) -> str:
        """裁剪状态字段字符串。"""

        return value.strip()


class ShortcutStatusReportRequest(BaseModel):
    """桌面端批量上报快捷键注册结果。"""

    settings_revision: StrictInt = Field(ge=0, le=MAX_SAFE_SETTINGS_REVISION)
    items: list[ShortcutStatusItemRequest]


class AudioDeviceSettingsRequest(BaseModel):
    """音频设备设置保存请求。"""

    mode: Literal["system_default", "manual"] = "system_default"
    microphone_id: str | None = None
    microphone_name: str | None = None
    speaker_loopback_id: str | None = None
    speaker_loopback_name: str | None = None


class ToolWaitingFamilyPolicyRequest(BaseModel):
    """工具族等待反馈策略保存请求。"""

    enabled: bool
    phrases: list[str] = []


class ToolWaitingToolPolicyRequest(BaseModel):
    """单工具等待反馈策略保存或清除请求。"""

    inherit_family: bool = False
    enabled: bool = False
    phrases: list[str] = []


class AudioDeviceApplyResultRequest(BaseModel):
    """桌面端音频设备热更新结果。"""

    request_id: str
    status: Literal["applied", "failed"]
    reason: str | None = None

    @field_validator("request_id")
    @classmethod
    def trim_request_id(cls, value: str) -> str:
        """校验并裁剪热更新请求编号。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("request_id must not be empty")
        return normalized_value

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str | None) -> str | None:
        """裁剪失败原因，避免把无意义空白广播给控制台。"""

        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None


class AudioDeviceTestRequest(BaseModel):
    """麦克风设备测试请求。"""

    microphone_id: str | None = None
    microphone_name: str | None = None
