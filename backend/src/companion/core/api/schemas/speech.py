from __future__ import annotations

from pydantic import BaseModel, field_validator


class StreamVoxConfigRequest(BaseModel):
    """StreamVox 运行配置保存请求。"""

    model: str
    device: str = "auto"
    llama_backend: str | None = None
    role_name: str
    streamvox_seed: int | None = 42
    inference_options: dict[str, object] = {}

    @field_validator("model", "role_name")
    @classmethod
    def trim_and_reject_blank(cls, value: str) -> str:
        """裁剪模型或角色名并拒绝空值。"""

        # 模型和角色名是 StreamVox 管理协议的关键键，空白值会导致 SDK 报错难以定位，因此在 API 层提前拒绝。
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("streamvox model and role_name must not be blank")
        return normalized_value

    @field_validator("device")
    @classmethod
    def trim_device_or_default(cls, value: str) -> str:
        """裁剪设备字段、限制可选值并为空值提供 auto 默认。"""

        # 控制台不处理多卡编号，gpu 始终表达 StreamVox 默认 0 卡。
        normalized_value = value.strip().lower() or "auto"
        if normalized_value not in {"auto", "cpu", "gpu"}:
            raise ValueError("streamvox device must be one of auto, cpu or gpu")
        return normalized_value

    @field_validator("llama_backend")
    @classmethod
    def trim_llama_backend_or_default(cls, value: str | None) -> str | None:
        """裁剪 llama.cpp 后端字段并限制为控制台支持的窄集合。"""

        # None 表示不显式传参，只有用户明确选择时才把 cuda/vulkan 送入 SDK。
        if value is None:
            return None
        normalized_value = value.strip().lower()
        if not normalized_value:
            return None
        if normalized_value not in {"cuda", "vulkan"}:
            raise ValueError("streamvox llama_backend must be cuda, vulkan or null")
        return normalized_value

    @field_validator("streamvox_seed")
    @classmethod
    def reject_invalid_seed(cls, value: int | None) -> int | None:
        """校验 StreamVox 推理 seed。"""

        if value is None:
            return None
        if value < 0:
            raise ValueError("streamvox_seed must be greater than or equal to 0")
        return value


class StreamVoxRolePreviewRequest(BaseModel):
    """StreamVox 角色推理试听请求。"""

    text: str

    @field_validator("text")
    @classmethod
    def trim_and_reject_blank(cls, value: str) -> str:
        """裁剪试听文本并拒绝空白值。"""

        # 试听文本会直接进入 TTS 推理，空白内容只会浪费运行时资源，因此在 API 边界先拒绝。
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("preview text must not be blank")
        return normalized_value


class SpeakerVerificationProfileRequest(BaseModel):
    """说话人确认开关保存请求。"""

    enabled: bool
