from __future__ import annotations

from pathlib import Path

import pytest

from companion.bootstrap.service import BootstrapService
from companion.core.settings import Settings
from companion.llm.config import LlmSettings
from companion.records.bootstrap_secret_store import BootstrapSecretStore


@pytest.mark.parametrize(
    "invalid_llm_json",
    [
        "not-json",
        '{"schema_version": 1, "provider": "deepseek"}',
        '{"schema_version": 99, "credentials": {}, "role_bindings": {}}',
    ],
)
def test_invalid_persisted_llm_schema_clears_only_llm_config(
    tmp_path: Path,
    invalid_llm_json: str,
) -> None:
    """旧版、损坏或未知 LLM schema 只能清除自己的持久化键。

    Args:
        tmp_path: Pytest 临时目录。
        invalid_llm_json: 本次模拟的无效持久化配置。
    """

    settings, store, service = _bootstrap_service(tmp_path)
    store.save_llm_config_json(invalid_llm_json)
    store.save_bootstrap_secrets(
        qweather_api_key="weather-secret",
        qweather_api_host="https://weather.example.com",
    )
    store.save_background_agent_credential(
        credential_type="auth_token",
        credential_value="background-secret",
    )

    service.hydrate_runtime_settings()

    persisted = store.snapshot_service_secrets()
    assert persisted == {
        "llm_config_json": None,
        "qweather_api_key": "weather-secret",
        "qweather_api_host": "https://weather.example.com",
        "background_agent_api_key": None,
        "background_agent_auth_token": "background-secret",
    }
    assert settings.llm == LlmSettings()
    assert settings.qweather_api_key == "weather-secret"
    assert settings.qweather_api_host == "https://weather.example.com"


def test_valid_schema_v2_restart_performs_static_hydration_without_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合法 v2 配置重启时只能静态注水，不能创建客户端或执行网络探测。"""

    settings, store, service = _bootstrap_service(tmp_path)
    persisted_llm = _valid_llm_settings()
    store.save_llm_config_json(persisted_llm.model_dump_json())
    client_constructions = 0

    def reject_client_construction(*_args: object, **_kwargs: object) -> object:
        """记录并拒绝任何意外的 LLM Client 构造。

        Returns:
            此函数始终抛错，不会返回。
        """

        nonlocal client_constructions
        client_constructions += 1
        raise AssertionError("重启静态注水不得构造 LLM Client")

    monkeypatch.setattr(
        "companion.llm.client_factory.LlmClientFactory.client",
        reject_client_construction,
    )

    service.hydrate_runtime_settings()

    assert settings.llm == persisted_llm
    assert client_constructions == 0
    assert store.get_bootstrap_secret("llm_config_json") == (
        persisted_llm.model_dump_json()
    )


def test_public_bootstrap_status_projects_voice_without_paths_or_raw_errors(
    tmp_path: Path,
) -> None:
    """公开 Bootstrap 状态只能返回语音门禁摘要，不能暴露模型目录和底层异常。"""

    _settings, _store, service = _bootstrap_service(tmp_path)
    service.bind_voice_status_provider(
        lambda: {
            "provider": "local_streamvox",
            "is_ready": False,
            "phase": "repair_required",
            "model_id": "qwen3-tts-clone-0.6b-gguf",
            "role_name": "huohua",
            "execution": {"model_directory": "C:/Users/demo/NearMate/models"},
            "error": "加载失败：C:/Users/demo/NearMate/runtime-temp/trace.log",
        }
    )

    voice = service.status_payload()["voice"]

    assert voice == {
        "provider": "local_streamvox",
        "is_ready": False,
        "phase": "repair_required",
        "model_id": "qwen3-tts-clone-0.6b-gguf",
        "role_name": "huohua",
        "has_confirmed_voice": False,
    }


def test_public_bootstrap_status_keeps_voice_shape_when_provider_is_malformed(
    tmp_path: Path,
) -> None:
    """语音状态提供方异常时也必须返回完整的公开字段形状。"""

    _settings, _store, service = _bootstrap_service(tmp_path)
    service.bind_voice_status_provider(lambda: "malformed")  # type: ignore[arg-type]

    voice = service.status_payload()["voice"]

    assert voice == {
        "provider": "local_streamvox",
        "is_ready": False,
        "phase": "unconfigured",
        "model_id": None,
        "role_name": None,
        "has_confirmed_voice": False,
    }


def test_public_bootstrap_status_keeps_llm_gate_as_narrow_summary(
    tmp_path: Path,
) -> None:
    """Bootstrap 门禁只返回总体验证结果，逐职责详情留给 LLM 配置接口。"""

    _settings, _store, service = _bootstrap_service(tmp_path)

    llm_status = service.status_payload()["llm"]

    assert set(llm_status) == {"is_ready", "missing", "errors"}
    assert llm_status["is_ready"] is False
    assert isinstance(llm_status["missing"], list)
    assert isinstance(llm_status["errors"], list)


def _bootstrap_service(
    tmp_path: Path,
) -> tuple[Settings, BootstrapSecretStore, BootstrapService]:
    """构造使用独立 SQLite 和用户设置路径的 Bootstrap 服务。

    Args:
        tmp_path: Pytest 临时目录。

    Returns:
        可以联合断言内存设置与持久化结果的三个对象。
    """

    settings = Settings(
        bootstrap_db_path=str(tmp_path / "bootstrap.sqlite3"),
        user_settings_path=str(tmp_path / "user-settings.json"),
    )
    store = BootstrapSecretStore(settings.bootstrap_db_path)
    store.initialize()
    return settings, store, BootstrapService(secret_store=store, settings=settings)


def _valid_llm_settings() -> LlmSettings:
    """构造同时满足三个必需 LLM 职责的合法 v2 配置。

    Returns:
        可直接持久化的 DeepSeek 与 Qwen 配置。
    """

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
