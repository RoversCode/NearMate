from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import os

from companion.capabilities.weather.config import (
    is_valid_qweather_api_host,
    validate_qweather_api_host,
)
from companion.core.settings import Settings
from companion.llm.config import LlmSettings, validate_llm_settings_ready
from companion.records.bootstrap_secret_store import BootstrapSecretStore


def _project_public_voice_status(value: object) -> dict[str, object]:
    """把语音内部状态压成可以公开广播的最小摘要。

    Args:
        value: 语音服务返回的完整内部状态。

    Returns:
        只包含界面门禁所需字段的脱敏摘要；模型目录、能力探测和原始异常不会外泄。
    """

    if not isinstance(value, dict):
        return {
            "provider": "local_streamvox",
            "is_ready": False,
            "phase": "unconfigured",
            "model_id": None,
            "role_name": None,
            "has_confirmed_voice": False,
        }

    provider = value.get("provider")
    phase = value.get("phase")
    model_id = value.get("model_id")
    role_name = value.get("role_name")
    return {
        "provider": provider if isinstance(provider, str) else "local_streamvox",
        "is_ready": value.get("is_ready") is True,
        "phase": phase if isinstance(phase, str) else "unconfigured",
        "model_id": model_id if isinstance(model_id, str) else None,
        "role_name": role_name if isinstance(role_name, str) else None,
        "has_confirmed_voice": value.get("has_confirmed_voice") is True,
    }


@dataclass(frozen=True)
class BootstrapSecrets:
    """启动初始化密钥归一化结果。"""

    # 和风天气 API Key 是可选服务密钥，只在用户填写且验证通过时保存。
    qweather_api_key: str | None = None
    # 和风天气 API Host 由和风控制台分配，必须和 API Key 配套保存。
    qweather_api_host: str | None = None


class BootstrapService:
    """启动初始化服务。"""

    def __init__(
        self,
        *,
        secret_store: BootstrapSecretStore,
        settings: Settings,
        voice_status_provider: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        """保存首次初始化依赖，不在构造时启动语音或助手。

        Args:
            secret_store: 保存 LLM 和可选天气密钥的仓储。
            settings: 当前应用配置。
            voice_status_provider: 读取首次语音配置状态的端口。
        Returns:
            无返回值；运行时门禁由初始化所有者推进。
        """

        # 服务只保存已有对象引用，避免在业务层重复创建仓储或复制 Settings 导致运行时配置不同步。
        self.secret_store = secret_store
        self.settings = settings
        self.runtime_blocked = False
        self.runtime_status_provider: Callable[[], dict[str, object]] | None = None
        self._voice_status_provider = voice_status_provider

    def bind_voice_status_provider(self, provider: Callable[[], dict[str, object]]) -> None:
        """接入应用容器持有的语音配置服务，首次启动不需要先运行助手。

        Args:
            provider: 返回角色语音配置状态的函数。

        Returns:
            无返回值；后续状态请求读取该服务的当前结果。
        """
        self._voice_status_provider = provider

    def status_payload(self) -> dict[str, object]:
        """生成不含凭据和本机路径的首次初始化摘要。

        Returns:
            包含 llm、voice、has_qweather_api_key 和 is_initialized 的四字段状态。
        """

        qweather_api_key = self.secret_store.get_bootstrap_secret("qweather_api_key")
        qweather_api_host = self.secret_store.get_bootstrap_secret("qweather_api_host")
        effective_qweather_api_key = qweather_api_key or self.settings.qweather_api_key
        effective_qweather_api_host = qweather_api_host or self.settings.qweather_api_host
        # 首次初始化门禁只需要总体结果；逐职责状态通过 /api/llm/config 单独返回，
        # 这样 Bootstrap HTTP、控制 WebSocket 和 Console 门禁始终使用同一窄契约。
        llm_status = validate_llm_settings_ready(self.settings.llm).to_bootstrap_payload()
        voice_status = _project_public_voice_status(
            self._voice_status_provider()
            if self._voice_status_provider is not None
            else {"provider": "local_streamvox", "is_ready": False, "phase": "unconfigured"}
        )
        # 只有安装包才强制要求首次完成本地语音配置；开发态继续允许无模型运行控制面和单元测试。
        require_tts = os.environ.get("NEARMATE_PACKAGED") == "1"

        return {
            "llm": llm_status,
            "voice": voice_status,
            "has_qweather_api_key": bool(
                (effective_qweather_api_key or "").strip()
                and is_valid_qweather_api_host(effective_qweather_api_host or "")
            ),
            "is_initialized": (
                not self.runtime_blocked
                and bool(llm_status["is_ready"])
                and (bool(voice_status["is_ready"]) or not require_tts)
            ),
        }

    def normalize_secrets(
        self,
        *,
        qweather_api_key: str | None = None,
        qweather_api_host: str | None = None,
    ) -> BootstrapSecrets:
        """归一化并校验启动密钥。"""

        normalized_qweather_api_key = qweather_api_key.strip() if qweather_api_key is not None else ""
        normalized_qweather_api_host = (
            validate_qweather_api_host(qweather_api_host)
            if qweather_api_host and qweather_api_host.strip()
            else ""
        )

        if normalized_qweather_api_key and not normalized_qweather_api_host:
            raise ValueError("qweather api host must not be blank when qweather api key is provided")

        return BootstrapSecrets(
            qweather_api_key=normalized_qweather_api_key or None,
            qweather_api_host=normalized_qweather_api_host or None,
        )

    def hydrate_runtime_settings(self) -> dict[str, object]:
        """从已持久化启动配置注水运行时配置。"""

        llm_config_json = self.secret_store.get_bootstrap_secret("llm_config_json")
        if llm_config_json is not None:
            try:
                self.settings.llm = LlmSettings.model_validate_json(llm_config_json)
            except ValueError:
                # V1 不迁移旧 URL、Key 或绑定；只清除 LLM 键，让用户重新完成 Bootstrap。
                self.secret_store.delete_llm_config()
                self.settings.llm = LlmSettings()

        # 和风天气是可选能力；存在持久化 key 时注入运行时配置，缺失时不影响初始化门禁。
        qweather_api_key = self.secret_store.get_bootstrap_secret("qweather_api_key")
        if qweather_api_key is not None:
            self.settings.qweather_api_key = qweather_api_key
        qweather_api_host = self.secret_store.get_bootstrap_secret("qweather_api_host")
        if qweather_api_host is not None:
            self.settings.qweather_api_host = qweather_api_host

        return self.status_payload()

    def save_llm_settings(self, llm_settings: LlmSettings) -> dict[str, object]:
        """保存 LLM 设置并同步运行时配置。"""

        self.secret_store.save_llm_config_json(llm_settings.model_dump_json())
        self.settings.llm = llm_settings
        return self.status_payload()

    def initialize(
        self,
        *,
        qweather_api_key: str | None = None,
        qweather_api_host: str | None = None,
    ) -> dict[str, object]:
        """保存启动密钥并同步更新运行时配置。"""

        secrets = self.normalize_secrets(
            qweather_api_key=qweather_api_key,
            qweather_api_host=qweather_api_host,
        )

        self.secret_store.save_bootstrap_secrets(
            qweather_api_key=secrets.qweather_api_key,
            qweather_api_host=secrets.qweather_api_host,
        )
        if secrets.qweather_api_key is not None:
            self.settings.qweather_api_key = secrets.qweather_api_key
        if secrets.qweather_api_host is not None:
            self.settings.qweather_api_host = secrets.qweather_api_host

        return self.status_payload()

    def save_qweather_api_key(self, *, api_key: str, api_host: str) -> dict[str, object]:
        """保存验证通过的和风天气 API Key 与 API Host 并同步运行时配置。"""

        normalized_api_key = api_key.strip()
        normalized_api_host = validate_qweather_api_host(api_host)
        if not normalized_api_key:
            raise ValueError("qweather api key must not be blank")
        if not normalized_api_host:
            raise ValueError("qweather api host must not be blank")

        self.secret_store.save_qweather_api_key(
            api_key=normalized_api_key,
            api_host=normalized_api_host,
        )
        self.settings.qweather_api_key = normalized_api_key
        self.settings.qweather_api_host = normalized_api_host
        return self.status_payload()
