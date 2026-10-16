from __future__ import annotations

from pydantic import BaseModel, field_validator

from companion.character_cards.models import CharacterCardDefinition


class WakeResponseItemRequest(BaseModel):
    """单条唤醒回复保存请求。"""

    id: str
    text: str
    audio_path: str | None = None
    synthesized_at: float | None = None

    @field_validator("text")
    @classmethod
    def trim_and_reject_blank_text(cls, value: str) -> str:
        """裁剪唤醒回复文本并拒绝空白。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("wake response text must not be blank")
        return normalized_value


class WakeResponseConfigRequest(BaseModel):
    """唤醒回复配置保存请求。"""

    buckets: dict[str, list[WakeResponseItemRequest]]


class WakeResponsePreviewRequest(BaseModel):
    """唤醒回复试听请求。"""

    text: str

    @field_validator("text")
    @classmethod
    def trim_and_reject_blank_text(cls, value: str) -> str:
        """裁剪试听文本并拒绝空白。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("wake response preview text must not be blank")
        return normalized_value


class WakeResponseSynthesizeRequest(BaseModel):
    """单行唤醒回复合成请求。"""

    bucket: str
    text: str

    @field_validator("bucket")
    @classmethod
    def trim_bucket(cls, value: str) -> str:
        """裁剪时段名称。"""

        return value.strip().lower()

    @field_validator("text")
    @classmethod
    def trim_and_reject_blank_text(cls, value: str) -> str:
        """裁剪并拒绝空白回复。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("wake response text must not be blank")
        return normalized_value


class StreamVoxExampleGenerationRequest(BaseModel):
    """角色卡 StreamVox 示例生成请求。"""

    card: CharacterCardDefinition
    target_model: str

    @field_validator("target_model")
    @classmethod
    def trim_target_model(cls, value: str) -> str:
        """裁剪目标示例槽位。"""

        normalized_value = value.strip().lower()
        if not normalized_value:
            raise ValueError("target_model must not be blank")
        return normalized_value


class CharacterCardCloneRequest(BaseModel):
    """角色卡复制请求。"""

    card_id: str
    name: str
    title: str

    @field_validator("card_id", "name", "title")
    @classmethod
    def trim_and_reject_blank(cls, value: str) -> str:
        """裁剪并拒绝空白字段。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("character card clone fields must not be blank")
        return normalized_value
