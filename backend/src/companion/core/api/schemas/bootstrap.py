from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from companion.capabilities.weather.config import validate_qweather_api_host
from companion.llm.config import ProviderCredential


class BootstrapInitializeRequest(BaseModel):
    """启动初始化密钥提交请求。"""

    model_config = ConfigDict(extra="forbid")
    qweather_api_key: str | None = None
    qweather_api_host: str | None = None


    @field_validator("qweather_api_key")
    @classmethod
    def trim_optional_qweather_api_key(cls, value: str | None) -> str | None:
        """裁剪可选和风天气 API Key。"""

        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None

    @field_validator("qweather_api_host")
    @classmethod
    def normalize_optional_qweather_api_host(cls, value: str | None) -> str | None:
        """裁剪并归一化可选和风天气 API Host。"""

        if value is None:
            return None
        if not value.strip():
            return None
        normalized_value = validate_qweather_api_host(value)
        return normalized_value or None


class TtsSetupStartRequest(BaseModel):
    """选择一个本地 StreamVox 模型并启动语音配置任务。"""

    model_id: str


class QWeatherApiKeyRequest(BaseModel):
    """和风天气 API Key 提交请求。"""

    qweather_api_key: str
    qweather_api_host: str

    @field_validator("qweather_api_key")
    @classmethod
    def trim_and_reject_blank(cls, value: str) -> str:
        """裁剪和风天气 API Key 并拒绝空白值。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("qweather api key must not be blank")
        return normalized_value

    @field_validator("qweather_api_host")
    @classmethod
    def normalize_and_reject_blank_host(cls, value: str) -> str:
        """裁剪和风天气 API Host，必要时补全 https 协议，并拒绝空白值。"""

        return validate_qweather_api_host(value)


class LlmConfigRequest(BaseModel):
    """schema v2 LLM 配置保存请求。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    credentials: dict[str, ProviderCredential]
    role_bindings: dict[str, str | None]
