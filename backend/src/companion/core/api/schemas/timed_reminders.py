"""声明定时提醒 HTTP 写接口的严格请求结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CreateTimedReminderRequest(BaseModel):
    """接收自然交互或前端创建提醒所需的最小字段。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=500)
    trigger_at: datetime | None = None
    delay_seconds: int | None = Field(default=None, gt=0)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """拒绝只含空白的提醒事项。

        Args:
            value: Pydantic 完成类型检查后的原始正文。

        Returns:
            去除首尾空白后的提醒事项。
        """

        normalized = value.strip()
        if not normalized:
            raise ValueError("提醒内容不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_time_choice(self) -> "CreateTimedReminderRequest":
        """确保绝对时间和相对时长严格二选一。

        Returns:
            时间选择合法的请求本身。
        """

        if (self.trigger_at is None) == (self.delay_seconds is None):
            raise ValueError("trigger_at 和 delay_seconds 必须且只能提供一个")
        return self


class UpdateTimedReminderRequest(BaseModel):
    """接收提醒内容或触发时间的部分修改。"""

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=500)
    trigger_at: datetime | None = None
    delay_seconds: int | None = Field(default=None, gt=0)

    @field_validator("content")
    @classmethod
    def validate_optional_content(cls, value: str | None) -> str | None:
        """清理可选正文并拒绝空白值。

        Args:
            value: 调用方可选的新提醒事项。

        Returns:
            空值或去除首尾空白后的正文。
        """

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("提醒内容不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_update_fields(self) -> "UpdateTimedReminderRequest":
        """确保存在变更字段且两种时间输入不会并存。

        Returns:
            字段组合合法的请求本身。
        """

        if self.trigger_at is not None and self.delay_seconds is not None:
            raise ValueError("trigger_at 和 delay_seconds 不能同时提供")
        if self.content is None and self.trigger_at is None and self.delay_seconds is None:
            raise ValueError("至少提供一项需要修改的字段")
        return self


class DismissTimedRemindersRequest(BaseModel):
    """接收界面或 Foreground 当前看见的响铃批次。"""

    model_config = ConfigDict(extra="forbid")

    runtime_id: str = Field(min_length=1)
    reminder_ids: list[str] = Field(min_length=1, max_length=5)

    @field_validator("runtime_id")
    @classmethod
    def validate_runtime_id(cls, value: str) -> str:
        """清理运行编号并拒绝只含空白的输入。

        Args:
            value: 调用方快照携带的 Backend 运行编号。

        Returns:
            去除首尾空白后的运行编号。
        """

        normalized = value.strip()
        if not normalized:
            raise ValueError("runtime_id 不能为空")
        return normalized

    @field_validator("reminder_ids")
    @classmethod
    def validate_reminder_ids(cls, value: list[str]) -> list[str]:
        """拒绝空编号和重复编号，避免批次边界含糊。

        Args:
            value: 当前气泡或 Prompt 展示的提醒编号。

        Returns:
            已清理且保持展示顺序的编号列表。
        """

        normalized = [reminder_id.strip() for reminder_id in value]
        if any(not reminder_id for reminder_id in normalized):
            raise ValueError("reminder_ids 不能包含空编号")
        if len(set(normalized)) != len(normalized):
            raise ValueError("reminder_ids 不能重复")
        return normalized
