"""验证和风天气配置在联网校验期间的并发提交边界。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from companion.background_agent.interaction_registry import BackgroundAgentInteractionRegistry
from companion.bootstrap.service import BootstrapService
from companion.capabilities.weather.validation import QWeatherValidationError
from companion.core.application import runtime_reconfiguration as runtime_reconfiguration_module
from companion.core.application.errors import InvalidRequest, ServiceUnavailable, StateConflict
from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator
from companion.core.settings import Settings
from companion.llm.config import LlmSettings
from companion.records.bootstrap_secret_store import BootstrapSecretStore




class WeatherValidationGate:
    """暂停指定天气 Key 的校验，让后发请求先完成提交。"""

    def __init__(self, blocked_key: str) -> None:
        """记录要暂停的 Key 以及两个线程间同步信号。

        Args:
            blocked_key: 旧请求使用的 Key；只有它会被暂停。
        """

        self.blocked_key = blocked_key
        self.validation_started = threading.Event()
        self.release_validation = threading.Event()
        self._blocked_once = False

    def __call__(self, *, settings: object, api_key: str, api_host: str) -> None:
        """模拟联网校验，并在旧请求进入校验后等待放行。

        Args:
            settings: 生产校验器接收的运行时设置；测试不读取它。
            api_key: 待校验的天气 Key。
            api_host: 待校验的天气 Host；测试不读取它。

        Raises:
            RuntimeError: 测试没有及时释放暂停的旧请求。
        """

        del settings, api_host
        if api_key != self.blocked_key or self._blocked_once:
            return
        self._blocked_once = True
        self.validation_started.set()
        if not self.release_validation.wait(timeout=5):
            raise RuntimeError("天气并发测试没有及时释放旧请求")


def _ready_llm_settings() -> LlmSettings:
    """构造一个能通过真实 Bootstrap readiness 校验的最小 LLM 配置。"""

    return LlmSettings.model_validate(
        {
            "schema_version": 2,
            "credentials": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "deepseek-key",
                },
                "qwen": {
                    "base_url": (
                        "https://workspace.cn-beijing.maas.aliyuncs.com/"
                        "compatible-mode/v1"
                    ),
                    "api_key": "qwen-key",
                },
            },
            "role_bindings": {
                "foreground": "deepseek:deepseek-flash:chat",
                "summary": "deepseek:deepseek-flash:chat",
                "cognitive": "qwen:qwen3.6-plus:chat",
                "character_authoring": None,
            },
        }
    )


def _build_coordinator(
    tmp_path: Path,
) -> tuple[RuntimeReconfigurationCoordinator, Settings, BootstrapSecretStore]:
    """使用真实 Settings、BootstrapService 和 SQLite 构造测试协调器。

    Args:
        tmp_path: 当前测试独占的临时目录。

    Returns:
        运行时配置协调器、内存设置和真实 SQLite 凭据仓储。
    """

    settings = Settings(
        bootstrap_db_path=str(tmp_path / "bootstrap.sqlite3"),
        user_settings_path=str(tmp_path / "user-settings.json"),
    )
    settings.llm = _ready_llm_settings()
    store = BootstrapSecretStore(settings.bootstrap_db_path)
    store.initialize()
    bootstrap = BootstrapService(
        secret_store=store,
        settings=settings,
    )

    async def no_media_session_to_restart() -> bool:
        """表示测试没有需要重建的 LiveKit 媒体会话。"""

        return False

    coordinator = RuntimeReconfigurationCoordinator(
        settings=settings,
        hub=cast(Any, SimpleNamespace(bootstrap=bootstrap)),
        realtime_input_runtime_owner=cast(Any, object()),
        assistant_process_supervisor=cast(Any, object()),
        activity_service=cast(Any, object()),
        background_agent_interactions=BackgroundAgentInteractionRegistry(),
        interrupt_runtime_activities=lambda _runtime_epoch: [],
        release_runtime_memory_compaction=lambda _runtime_epoch: False,
        restart_livekit_media_session=no_media_session_to_restart,
    )
    # 本测试只验证配置提交门禁；真实运行时是否需要替换由其它事务测试覆盖。
    coordinator._prepare_runtime_replacement = AsyncMock(return_value=None)
    return coordinator, settings, store


async def _wait_for_validation(gate: WeatherValidationGate) -> None:
    """等待旧请求确实进入线程池校验，避免测试依赖调度时序。"""

    started = await asyncio.to_thread(gate.validation_started.wait, 5)
    if not started:
        raise RuntimeError("旧天气请求没有进入联网校验")


@pytest.mark.asyncio
async def test_update_qweather_rejects_stale_validation_after_newer_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧的天气更新不能在后发更新提交后覆盖内存和 SQLite。"""

    coordinator, settings, store = _build_coordinator(tmp_path)
    gate = WeatherValidationGate("older-key")
    monkeypatch.setattr(runtime_reconfiguration_module, "validate_qweather_api_key", gate)

    older_request = asyncio.create_task(
        coordinator.update_qweather_api_key(
            api_key="older-key",
            api_host="older.example.com",
        )
    )
    await _wait_for_validation(gate)

    try:
        await coordinator.update_qweather_api_key(
            api_key="newer-key",
            api_host="newer.example.com",
        )
    finally:
        gate.release_validation.set()

    with pytest.raises(StateConflict, match="和风天气配置已变化"):
        await older_request

    assert settings.qweather_api_key == "newer-key"
    assert settings.qweather_api_host == "https://newer.example.com"
    assert store.get_bootstrap_secret("qweather_api_key") == "newer-key"
    assert store.get_bootstrap_secret("qweather_api_host") == "https://newer.example.com"


@pytest.mark.asyncio
async def test_update_qweather_maps_retryable_validation_to_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """代理/TLS/网络失败应返回 503，不能伪装成用户输入 400。"""

    coordinator, settings, store = _build_coordinator(tmp_path)

    def fail_validation(*_: object, **__: object) -> None:
        raise QWeatherValidationError(
            reason="tls_connection_failed",
            retryable=True,
            safe_message="和风天气 API Key 无法通过验证：天气服务 TLS 连接失败，请检查 API Host、系统代理和网络后重试。",
        )

    monkeypatch.setattr(runtime_reconfiguration_module, "validate_qweather_api_key", fail_validation)

    with pytest.raises(ServiceUnavailable) as raised:
        await coordinator.update_qweather_api_key(
            api_key="new-key",
            api_host="weather.example.com",
        )

    assert raised.value.detail == {
        "code": "qweather_tls_connection_failed",
        "message": "和风天气验证暂时失败，密钥尚未保存：天气服务 TLS 连接失败，请检查 API Host、系统代理和网络后重试。",
        "retryable": True,
    }
    assert settings.qweather_api_key == ""
    assert settings.qweather_api_host == ""
    assert store.get_bootstrap_secret("qweather_api_key") is None


@pytest.mark.asyncio
async def test_update_qweather_keeps_authentication_failure_as_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """和风返回认证拒绝时仍明确提示用户修正 Key，并保持 400 语义。"""

    coordinator, settings, _ = _build_coordinator(tmp_path)

    def fail_validation(*_: object, **__: object) -> None:
        raise QWeatherValidationError(
            reason="authentication_failed",
            retryable=False,
            safe_message="和风天气 API Key 无法通过验证：天气服务认证失败，请检查服务配置。",
        )

    monkeypatch.setattr(runtime_reconfiguration_module, "validate_qweather_api_key", fail_validation)

    with pytest.raises(InvalidRequest) as raised:
        await coordinator.update_qweather_api_key(
            api_key="bad-key",
            api_host="weather.example.com",
        )

    assert raised.value.detail == {
        "code": "qweather_authentication_failed",
        "message": "和风天气 API Key 无法通过验证：天气服务认证失败，请检查服务配置。",
        "retryable": False,
    }
    assert settings.qweather_api_key == ""


@pytest.mark.asyncio
async def test_update_qweather_rejects_aba_even_when_values_return_to_original(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A→B→A 期间旧联网校验也不能凭值相等绕过提交门禁。"""

    coordinator, settings, store = _build_coordinator(tmp_path)
    gate = WeatherValidationGate("same-key")
    monkeypatch.setattr(runtime_reconfiguration_module, "validate_qweather_api_key", gate)

    older_request = asyncio.create_task(
        coordinator.update_qweather_api_key(
            api_key="same-key",
            api_host="original.example.com",
        )
    )
    await _wait_for_validation(gate)

    await coordinator.update_qweather_api_key(
        api_key="newer-key",
        api_host="newer.example.com",
    )
    await coordinator.update_qweather_api_key(
        api_key="same-key",
        api_host="original.example.com",
    )
    gate.release_validation.set()

    with pytest.raises(StateConflict, match="和风天气配置已变化"):
        await older_request

    assert settings.qweather_api_key == "same-key"
    assert settings.qweather_api_host == "https://original.example.com"
    assert store.get_bootstrap_secret("qweather_api_key") == "same-key"
    assert store.get_bootstrap_secret("qweather_api_host") == "https://original.example.com"


@pytest.mark.asyncio
async def test_initialize_bootstrap_rejects_stale_weather_validation_after_update_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """首次初始化的旧天气校验不能覆盖后发服务密钥更新。"""

    coordinator, settings, store = _build_coordinator(tmp_path)
    gate = WeatherValidationGate("bootstrap-older-key")
    monkeypatch.setattr(runtime_reconfiguration_module, "validate_qweather_api_key", gate)

    older_request = asyncio.create_task(
        coordinator.initialize_bootstrap(
            qweather_api_key="bootstrap-older-key",
            qweather_api_host="bootstrap-older.example.com",
        )
    )
    await _wait_for_validation(gate)

    try:
        await coordinator.update_qweather_api_key(
            api_key="settings-newer-key",
            api_host="settings-newer.example.com",
        )
    finally:
        gate.release_validation.set()

    with pytest.raises(StateConflict, match="和风天气配置已变化"):
        await older_request

    assert settings.qweather_api_key == "settings-newer-key"
    assert settings.qweather_api_host == "https://settings-newer.example.com"
    assert store.get_bootstrap_secret("qweather_api_key") == "settings-newer-key"
    assert store.get_bootstrap_secret("qweather_api_host") == "https://settings-newer.example.com"
