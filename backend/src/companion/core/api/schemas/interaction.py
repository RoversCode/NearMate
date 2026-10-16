from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MaterialItemKind = Literal[
    "clipboard_text",
    "clipboard_image",
    "selected_text",
    "selected_image",
    "selected_web",
    "dragged_file",
    "dragged_folder",
]


class TextTurnReservationRequest(BaseModel):
    """Desktop 第一步提交的文本 reservation。

    Args:
        submission_id: Desktop 在点击发送时创建的标准 UUID。
        message_text: 用户本次希望发送的非空正文。
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: UUID
    message_text: str

    @field_validator("message_text")
    @classmethod
    def trim_message_text(cls, value: str) -> str:
        """裁剪文本正文并拒绝空消息。

        Args:
            value: Desktop 提交的原始文本。

        Returns:
            去除首尾空白后的正文。

        Raises:
            ValueError: 正文为空。
        """

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("message_text must not be empty")
        return normalized_value


class TextTurnContextRequest(BaseModel):
    """Desktop 第二步提交的现场环境准备结果。

    Args:
        environment_snapshot_id: 捕获成功时的现场环境快照编号；降级时为空。
    """

    model_config = ConfigDict(extra="forbid")

    environment_snapshot_id: str | None = None

    @field_validator("environment_snapshot_id")
    @classmethod
    def trim_environment_snapshot_id(cls, value: str | None) -> str | None:
        """裁剪可选环境快照编号，空白编号按未绑定处理。

        Args:
            value: Desktop 提交的原始环境快照编号。

        Returns:
            清洗后的编号；空白或缺失时返回空。
        """

        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None


class TextTurnEditRequest(BaseModel):
    """修改一条尚未派发的文本正文。

    Args:
        message_text: 替换后的非空正文。
    """

    model_config = ConfigDict(extra="forbid")

    message_text: str

    @field_validator("message_text")
    @classmethod
    def trim_message_text(cls, value: str) -> str:
        """裁剪编辑后的正文并拒绝空消息。

        Args:
            value: Desktop 提交的新正文。

        Returns:
            去除首尾空白后的正文。

        Raises:
            ValueError: 正文为空。
        """

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("message_text must not be empty")
        return normalized_value


class TextTurnPreparationFailureRequest(BaseModel):
    """记录 Desktop 环境捕获失败这一种可恢复事实。

    Args:
        reason: 固定的环境捕获失败原因码。
    """

    model_config = ConfigDict(extra="forbid")

    reason: Literal["environment_capture_failed"]


class EnvironmentSnapshotRequest(BaseModel):
    """桌面端提交的当前环境快照。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, max_length=128)
    turn_id: str | None = Field(default=None, max_length=128)
    reason: Literal["chat_focus", "chat_submit", "voice_turn_started"]
    captured_at: str = Field(max_length=64)
    active_window_title: str | None = Field(default=None, max_length=1024)
    capture_error: str | None = Field(default=None, max_length=2048)

    @field_validator("captured_at")
    @classmethod
    def trim_and_reject_blank(cls, value: str) -> str:
        """裁剪快照关键字段并拒绝空值。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("environment snapshot fields must not be blank")
        return normalized_value

    @field_validator(
        "request_id",
        "turn_id",
        "reason",
        "active_window_title",
        "capture_error",
    )
    @classmethod
    def trim_optional_string(cls, value: str | None) -> str | None:
        """裁剪可选字符串字段。"""

        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None

    @model_validator(mode="after")
    def validate_voice_binding(self) -> "EnvironmentSnapshotRequest":
        """约束语音快照必须同时携带 request_id 与 turn_id。

        Returns:
            通过业务组合校验的当前请求。
        """

        is_voice_snapshot = self.reason == "voice_turn_started"
        if is_voice_snapshot and (self.request_id is None or self.turn_id is None):
            raise ValueError("voice environment snapshot requires request_id and turn_id")
        if not is_voice_snapshot and (self.request_id is not None or self.turn_id is not None):
            raise ValueError("text environment snapshot must not carry voice identifiers")
        return self



class MaterialItemRequest(BaseModel):
    """桌面端或前端提交的单个材料项。"""

    model_config = ConfigDict(extra="forbid")

    kind: MaterialItemKind = Field(
        description="材料项类型，用于区分剪贴板内容、选区内容和拖拽文件等捕获形态。"
    )
    source: str = Field(
        description="材料来源标识，通常由桌面端或前端写入，用于表达本次材料的采集渠道。"
    )
    display_name: str = Field(
        description="材料展示名，用于前端列表、确认界面或会话上下文中的人类可读标题。"
    )
    path: str = Field(
        description="材料对应的本地文件或目录路径。",
    )
    managed_asset_id: str | None = Field(
        default=None,
        description="Desktop 实际创建项目副本后登记的受管资产编号。",
    )

    @field_validator("source", "display_name")
    @classmethod
    def trim_and_reject_blank(cls, value: str) -> str:
        """裁剪材料关键字段并拒绝空值。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("material item fields must not be blank")
        return normalized_value

    @field_validator("path")
    @classmethod
    def trim_and_reject_blank_path(cls, value: str) -> str:
        """裁剪材料路径并拒绝空值。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("material path must not be blank")
        return normalized_value

    @field_validator("managed_asset_id")
    @classmethod
    def trim_optional_string(cls, value: str | None) -> str | None:
        """裁剪可选材料字段。"""

        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None


class MaterialItemsRequest(BaseModel):
    """批量提交材料项的请求。"""

    items: list[MaterialItemRequest]


class RecentMaterialForgetRequest(BaseModel):
    """忘记一项近期本地路径能力的请求。

    Args:
        path: Desktop 当前快照中展示的完整本地路径。
    """

    model_config = ConfigDict(extra="forbid")

    path: str

    @field_validator("path")
    @classmethod
    def trim_and_reject_blank_path(cls, value: str) -> str:
        """裁剪路径并拒绝空白忘记命令。

        Args:
            value: Desktop 提交的原始路径。

        Returns:
            去除首尾空白后的路径。

        Raises:
            ValueError: 路径为空。
        """

        normalized_path = value.strip()
        if not normalized_path:
            raise ValueError("recent material path must not be blank")
        return normalized_path


class TextPasteRequest(BaseModel):
    """输入框纯文本粘贴分类请求。"""

    text: str
