"""定义普通设置页提交给应用层的无副作用候选命令。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from companion.core.settings import Settings


GENERAL_SETTINGS_FIELD_NAMES: tuple[str, ...] = (
    "show_speech_bubbles",
    "followup_toggle_shortcut_enabled",
    "followup_toggle_shortcut",
    "avatar_visibility_shortcut_enabled",
    "avatar_visibility_shortcut",
    "selection_capture_shortcut_enabled",
    "selection_capture_shortcut",
    "selected_live2d_model_id",
    "user_self_intro",
    "user_extra_notes",
)


@dataclass(frozen=True, slots=True)
class GeneralSettingsUpdate:
    """保存普通设置页拥有的全部可编辑值。

    Args:
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

    show_speech_bubbles: bool
    followup_toggle_shortcut_enabled: bool
    followup_toggle_shortcut: str
    avatar_visibility_shortcut_enabled: bool
    avatar_visibility_shortcut: str
    selection_capture_shortcut_enabled: bool
    selection_capture_shortcut: str
    selected_live2d_model_id: str
    user_self_intro: str
    user_extra_notes: str

    def build_candidate(
        self,
        current_settings: Settings,
    ) -> Settings:
        """在当前最新设置上构造普通设置候选，不修改共享对象。

        Args:
            current_settings: 进入统一提交锁后读取的当前权威设置。

        Returns:
            只覆盖普通设置字段的深拷贝候选。
        """

        return current_settings.model_copy(
            update={
                "show_speech_bubbles": self.show_speech_bubbles,
                "followup_toggle_shortcut_enabled": self.followup_toggle_shortcut_enabled,
                "followup_toggle_shortcut": self.followup_toggle_shortcut,
                "avatar_visibility_shortcut_enabled": self.avatar_visibility_shortcut_enabled,
                "avatar_visibility_shortcut": self.avatar_visibility_shortcut,
                "selection_capture_shortcut_enabled": self.selection_capture_shortcut_enabled,
                "selection_capture_shortcut": self.selection_capture_shortcut,
                "selected_live2d_model_id": self.selected_live2d_model_id,
                "user_self_intro": self.user_self_intro,
                "user_extra_notes": self.user_extra_notes,
            },
            deep=True,
        )


def general_settings_field_updates(
    candidate: Settings,
) -> dict[str, object]:
    """提取普通设置页有权修改的字段。

    Args:
        candidate: 已经完成业务校验的普通设置候选。

    Returns:
        只包含普通设置页字段的独立值副本。
    """

    return {
        field_name: deepcopy(getattr(candidate, field_name))
        for field_name in GENERAL_SETTINGS_FIELD_NAMES
    }
