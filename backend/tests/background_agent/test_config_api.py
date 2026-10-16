from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.background_agent.settings import BackgroundAgentSettings
from companion.core.api.dependencies import get_background_agent_dependencies
from companion.core.api.routers import background_agent_settings


class StubBackgroundAgentConfigCoordinator:
    """记录 Console 配置请求的应用服务替身。"""

    def __init__(self) -> None:
        self.updated: dict[str, object] | None = None
        self.response_payload: dict[str, object] = _safe_payload()

    async def current_background_agent_config_payload(self) -> dict[str, object]:
        """返回不含密钥明文的固定安全视图。"""

        return self.response_payload

    async def update_background_agent_config(self, **payload: object) -> dict[str, object]:
        """记录更新参数并返回安全视图。"""

        self.updated = payload
        return {
            **self.response_payload,
            "permission_mode": "bypassPermissions",
        }


def test_background_agent_config_api_never_returns_secret_value() -> None:
    """Console 只能看到凭据是否配置，不能读回 SQLite 明文。"""

    coordinator = StubBackgroundAgentConfigCoordinator()
    client = _client(coordinator)

    response = client.get("/api/background-agent/config")

    assert response.status_code == 200
    assert response.json()["credential_configured"] is True
    assert "credential_value" not in response.json()
    assert "ANTHROPIC_API_KEY" not in response.text


def test_background_agent_config_api_accepts_trusted_full_access_setting() -> None:
    """全权限模式只能通过 Console 配置接口写入可信用户设置。"""

    coordinator = StubBackgroundAgentConfigCoordinator()
    client = _client(coordinator)

    response = client.put(
        "/api/background-agent/config",
        json={
            "settings": {
                "environment": {
                    "ANTHROPIC_BASE_URL": "https://endpoint.example.com",
                    "ANTHROPIC_API_KEY": "must-not-enter-json",
                },
                "model": "claude-sonnet-4-5",
                "effort_level": "high",
                "max_turns": 40,
                "permission_mode": "bypassPermissions",
            },
            "credential_type": "api_key",
            "credential_value": "secret-value",
            "clear_credential": False,
        },
    )

    assert response.status_code == 202
    assert coordinator.updated is not None
    settings = coordinator.updated["settings"]
    assert isinstance(settings, BackgroundAgentSettings)
    assert settings.permission_mode == "bypassPermissions"
    assert settings.model == "claude-sonnet-4-5"
    assert settings.effort_level == "high"
    assert "ANTHROPIC_API_KEY" not in settings.environment
    assert coordinator.updated["credential_value"] == "secret-value"


def test_background_agent_config_api_forwards_authoritative_settings_snapshot() -> None:
    """后台配置保存结果必须保留统一设置事务生成的完整快照。"""

    coordinator = StubBackgroundAgentConfigCoordinator()
    authoritative_settings = {
        "settings_revision": 12,
        "show_speech_bubbles": False,
    }
    coordinator.response_payload = {
        **_safe_payload(),
        "settings": authoritative_settings,
    }

    response = _client(coordinator).put(
        "/api/background-agent/config",
        json={
            "settings": {
                "environment": {},
                "model": "claude-sonnet-4-5",
                "effort_level": None,
                "max_turns": 30,
                "permission_mode": "acceptEdits",
            }
        },
    )

    assert response.status_code == 202
    assert response.json()["settings"] == authoritative_settings


def test_background_agent_config_api_requires_model_in_openapi() -> None:
    """PUT 输入必须把主模型声明为真正的必填字段。"""

    client = _client(StubBackgroundAgentConfigCoordinator())

    schema = client.app.openapi()["components"]["schemas"]
    settings_schema = schema["BackgroundAgentConfigSettingsRequest"]

    assert "model" in settings_schema["required"]


def test_background_agent_config_api_rejects_boolean_max_turns() -> None:
    """JSON 布尔值不能被悄悄转换成无限或单轮执行。"""

    client = _client(StubBackgroundAgentConfigCoordinator())
    payload = {
        "settings": {
            "environment": {"ANTHROPIC_BASE_URL": "https://endpoint.example.com"},
            "model": "claude-sonnet-4-5",
            "effort_level": None,
            "max_turns": False,
            "permission_mode": "acceptEdits",
        }
    }

    response = client.put("/api/background-agent/config", json=payload)

    assert response.status_code == 422


def test_background_agent_config_api_rejects_non_object_environment() -> None:
    """PUT 不能把数组环境静默归一成空对象并清除现有端点。"""

    client = _client(StubBackgroundAgentConfigCoordinator())
    payload = {
        "settings": {
            "environment": ["invalid"],
            "model": "claude-sonnet-4-5",
            "effort_level": None,
            "max_turns": 30,
            "permission_mode": "acceptEdits",
        }
    }

    response = client.put("/api/background-agent/config", json=payload)

    assert response.status_code == 422


def _client(coordinator: StubBackgroundAgentConfigCoordinator) -> TestClient:
    """构造只挂载后台智能体配置路由的测试客户端。"""

    app = FastAPI()
    app.include_router(background_agent_settings.router)
    app.dependency_overrides[get_background_agent_dependencies] = lambda: SimpleNamespace(
        user_settings=coordinator,
    )
    return TestClient(app)


def _safe_payload(permission_mode: str = "acceptEdits") -> dict[str, object]:
    """构造不含凭据明文的接口响应。"""

    return {
        "environment": {"ANTHROPIC_BASE_URL": "https://endpoint.example.com"},
        "model": "claude-sonnet-4-5",
        "effort_level": None,
        "max_turns": 30,
        "permission_mode": permission_mode,
        "credential_type": "api_key",
        "credential_configured": True,
    }
