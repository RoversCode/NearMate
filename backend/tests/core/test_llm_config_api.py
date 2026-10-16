from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from companion.core.api.dependencies import get_bootstrap_dependencies
from companion.core.api.routers import bootstrap
from companion.core.api.routers.character_cards import (
    generate_character_card_streamvox_examples,
)
from companion.core.api.routers.wake_response import (
    generate_character_card_wake_response_config,
)
from companion.llm.client_factory import LlmRoleDisabledError
from companion.llm.config import ProviderCredential


class RecordingLlmCoordinator:
    """记录 LLM 配置 API 传入的严格 v2 参数。"""

    def __init__(self) -> None:
        """初始化空更新记录和固定安全配置视图。"""

        self.updates: list[dict[str, object]] = []
        self.account_status_payload = {
            "providers": [{
                "provider_id": "deepseek",
                "display_name": "DeepSeek",
                "active": True,
                "status": "available",
                "balance": "12.50",
                "currency": "CNY",
                "fetched_at": "2026-09-25T05:00:00Z",
                "message": None,
            }],
        }
        self.config_payload = {
            "schema_version": 2,
            "credentials": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key_configured": True,
                },
                "qwen": {
                    "base_url": (
                        "https://workspace.cn-beijing.maas.aliyuncs.com/"
                        "compatible-mode/v1"
                    ),
                    "api_key_configured": True,
                },
            },
            "role_bindings": _valid_role_bindings(),
            "status": {
                "is_ready": True,
                "missing": [],
                "errors": [],
                "roles": {},
            },
        }

    async def current_llm_config_payload(self) -> dict[str, object]:
        """返回不包含明文 Key 的固定配置。

        Returns:
            schema v2 安全配置视图。
        """

        return self.config_payload

    async def current_llm_account_status_payload(self) -> dict[str, object]:
        """返回不包含明文 Key 的账户余额安全快照。

        Returns:
            固定的厂商余额投影。
        """

        return self.account_status_payload

    async def update_llm_config(
        self,
        *,
        credentials: dict[str, ProviderCredential],
        role_bindings: dict[str, str | None],
    ) -> dict[str, object]:
        """记录 Pydantic 解析后的凭据和职责绑定。

        Args:
            credentials: 已拒绝额外字段的厂商凭据。
            role_bindings: 四个 LLM 职责的显式绑定。

        Returns:
            固定 schema v2 安全配置视图。
        """

        self.updates.append(
            {
                "credentials": credentials,
                "role_bindings": role_bindings,
            }
        )
        return self.config_payload


def test_supported_models_exposes_exact_v1_registry_and_role_edges() -> None:
    """公开注册表包含四个接入规格，但 Qwen Flash 不属于职责候选。"""

    client, _coordinator = _client()

    response = client.get("/api/llm/supported-models")

    assert response.status_code == 200
    payload = response.json()
    assert [item["provider_id"] for item in payload["providers"]] == [
        "deepseek",
        "qwen",
    ]
    assert [item["integration_id"] for item in payload["integrations"]] == [
        "deepseek:deepseek-flash:chat",
        "deepseek:deepseek-v4-pro:chat",
        "qwen:qwen3.6-plus:chat",
        "qwen:qwen3.6-flash:chat",
    ]
    role_contracts = {
        item["role_id"]: item for item in payload["role_contracts"]
    }
    assert set(role_contracts) == {
        "foreground",
        "summary",
        "cognitive",
        "character_authoring",
    }
    assert role_contracts["foreground"]["allowed_integration_ids"] == [
        "deepseek:deepseek-flash:chat",
    ]
    assert role_contracts["summary"]["allowed_integration_ids"] == [
        "deepseek:deepseek-flash:chat",
        "deepseek:deepseek-v4-pro:chat",
    ]
    assert role_contracts["character_authoring"][
        "allowed_integration_ids"
    ] == [
        "deepseek:deepseek-flash:chat",
        "deepseek:deepseek-v4-pro:chat",
    ]
    assert role_contracts["cognitive"]["allowed_integration_ids"] == [
        "qwen:qwen3.6-plus:chat"
    ]
    assert role_contracts["cognitive"]["required_capabilities"] == [
        "image_input",
        "web_extract",
        "web_search",
    ]
    assert all(
        "qwen:qwen3.6-flash:chat" not in contract["allowed_integration_ids"]
        for contract in role_contracts.values()
    )
    assert all(
        "request_profile_id" not in integration
        for integration in payload["integrations"]
    )
    assert all(
        "connectivity_probe_integration_id" not in provider
        for provider in payload["providers"]
    )


def test_account_status_returns_safe_provider_balance_projection() -> None:
    """余额接口只返回脱敏的厂商账户投影。"""

    client, _coordinator = _client()

    response = client.get("/api/llm/account-status")

    assert response.status_code == 200
    assert response.json() == _coordinator.account_status_payload


def test_config_get_and_put_use_strict_schema_v2() -> None:
    """配置读写必须只接受 credentials 与 role_bindings，并保持 Key 脱敏。"""

    client, coordinator = _client()
    update_payload = {
        "schema_version": 2,
        "credentials": {
            "deepseek": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "deepseek-key",
            },
            "qwen": {
                "base_url": "workspace",
                "api_key": "qwen-key",
            },
        },
        "role_bindings": _valid_role_bindings(),
    }

    get_response = client.get("/api/llm/config")
    put_response = client.put("/api/llm/config", json=update_payload)

    assert get_response.status_code == 200
    assert put_response.status_code == 202
    assert get_response.json() == coordinator.config_payload
    assert put_response.json() == coordinator.config_payload
    assert all(
        "api_key" not in credential
        for credential in get_response.json()["credentials"].values()
    )
    assert len(coordinator.updates) == 1
    credentials = coordinator.updates[0]["credentials"]
    assert isinstance(credentials, dict)
    assert credentials["deepseek"] == ProviderCredential(
        base_url="https://api.deepseek.com/v1",
        api_key="deepseek-key",
    )
    assert coordinator.updates[0]["role_bindings"] == _valid_role_bindings()


def test_config_put_rejects_unknown_field() -> None:
    """schema v2 合同外字段不得被 API 静默忽略。"""

    client, coordinator = _client()
    unexpected_field = "unexpected_field"
    payload = {
        "schema_version": 2,
        "credentials": {},
        "role_bindings": _valid_role_bindings(),
        unexpected_field: "unexpected-value",
    }

    response = client.put("/api/llm/config", json=payload)

    assert response.status_code == 422
    assert coordinator.updates == []
    assert any(
        issue["type"] == "extra_forbidden"
        and issue["loc"][-1] == unexpected_field
        for issue in response.json()["detail"]
    )


@pytest.mark.asyncio
async def test_character_authoring_disabled_maps_both_generation_apis_to_503() -> None:
    """Character Authoring 关闭时唤醒回复和示例生成都必须明确返回 503。"""

    disabled_error = LlmRoleDisabledError("character_authoring")

    def reject_wake_response(_card_id: str) -> object:
        """模拟唤醒回复服务请求已关闭职责。

        Args:
            _card_id: 当前角色卡编号。

        Raises:
            LlmRoleDisabledError: 固定模拟可选职责关闭。
        """

        raise disabled_error

    def reject_streamvox_examples(**_kwargs: object) -> object:
        """模拟 StreamVox 示例服务请求已关闭职责。

        Args:
            **_kwargs: 生成服务收到的角色草稿和目标模型。

        Raises:
            LlmRoleDisabledError: 固定模拟可选职责关闭。
        """

        raise disabled_error

    wake_container = cast(
        Any,
        SimpleNamespace(
            wake_response=SimpleNamespace(generate_texts=reject_wake_response)
        ),
    )
    character_container = cast(
        Any,
        SimpleNamespace(
            streamvox_examples=SimpleNamespace(
                generate_examples=reject_streamvox_examples
            )
        ),
    )

    with pytest.raises(HTTPException) as wake_error:
        await generate_character_card_wake_response_config(
            "card-1",
            wake_container,
        )
    with pytest.raises(HTTPException) as streamvox_error:
        await generate_character_card_streamvox_examples(
            "card-1",
            cast(Any, SimpleNamespace(card=object(), target_model="model")),
            character_container,
        )

    assert wake_error.value.status_code == 503
    assert streamvox_error.value.status_code == 503
    assert "character_authoring" in str(wake_error.value.detail)
    assert "character_authoring" in str(streamvox_error.value.detail)


def _client() -> tuple[TestClient, RecordingLlmCoordinator]:
    """构造只注册 LLM 控制路由的测试客户端。

    Returns:
        FastAPI 客户端和可检查的应用协调器。
    """

    coordinator = RecordingLlmCoordinator()
    app = FastAPI()
    app.include_router(bootstrap.router)
    app.dependency_overrides[get_bootstrap_dependencies] = lambda: SimpleNamespace(
        runtime_reconfiguration=coordinator
    )
    return TestClient(app), coordinator


def _valid_role_bindings() -> dict[str, str | None]:
    """返回通过 V1 显式矩阵校验的四职责绑定。

    Returns:
        可直接进入 schema v2 请求的绑定字典。
    """

    return {
        "foreground": "deepseek:deepseek-flash:chat",
        "summary": "deepseek:deepseek-v4-pro:chat",
        "cognitive": "qwen:qwen3.6-plus:chat",
        "character_authoring": None,
    }
