from __future__ import annotations

import pytest

from companion.llm.config import LlmSettings, resolve_role_binding
from companion.llm.model_registry import (
    list_model_integrations,
    list_provider_credential_specs,
)
from companion.llm.role_contracts import (
    LLM_ROLE_NAMES,
    ROLE_INTEGRATION_MATRIX,
    role_contracts_payload,
    validate_role_integration,
)


ACTIVE_LLM_ROLES = {
    "foreground",
    "summary",
    "cognitive",
    "character_authoring",
}


@pytest.mark.parametrize("integration_id", [
    "deepseek:deepseek-v4-flash:chat",
    "deepseek:deepseek-v4-flash-vision-exp:chat",
    "deepseek:deepseek-v4-pro:chat",
])
def test_foreground_rejects_retired_or_nonvisual_models(integration_id: str) -> None:
    """Foreground 不能退回旧接入或只有文本能力的模型。"""

    with pytest.raises(ValueError):
        validate_role_integration("foreground", integration_id)


def test_v1_registry_contains_exactly_verified_providers_and_integrations() -> None:
    """V1 注册表不能夹带未验证厂商、模型或接口形态。"""

    assert {
        provider.provider_id for provider in list_provider_credential_specs()
    } == {"deepseek", "qwen"}
    integrations = list_model_integrations()
    assert {integration.integration_id for integration in integrations} == {
        "deepseek:deepseek-flash:chat",
        "deepseek:deepseek-v4-pro:chat",
        "qwen:qwen3.6-plus:chat",
        "qwen:qwen3.6-flash:chat",
    }
    assert {integration.api_surface for integration in integrations} == {
        "chat_completions"
    }
    assert all(
        "qwen:qwen3.6-flash:chat" not in allowed_integration_ids
        for allowed_integration_ids in ROLE_INTEGRATION_MATRIX.values()
    )


@pytest.mark.parametrize(
    "role_id",
    ["foreground", "summary", "character_authoring"],
)
def test_deepseek_flash_supports_vision_on_allowed_roles(
    role_id: str,
) -> None:
    """原生视觉 Flash 可以进入明确验证过的三个职责。

    Args:
        role_id: 本次校验的 DeepSeek LLM 职责。
    """

    integration = validate_role_integration(
        role_id,
        "deepseek:deepseek-flash:chat",
    )

    assert integration.model == "deepseek-flash"
    assert {capability.value for capability in integration.capabilities} == {
        "json_output",
        "tool_calling",
        "image_input",
    }


def test_cognitive_rejects_deepseek_without_web_search() -> None:
    """原生视觉也不能替代 Cognitive 必需的联网能力。"""

    with pytest.raises(ValueError, match="不允许使用接入规格"):
        validate_role_integration(
            "cognitive",
            "deepseek:deepseek-flash:chat",
        )


def test_role_contract_payload_uses_only_explicit_matrix_edges() -> None:
    """公开候选必须逐字等于显式矩阵，不能由能力标签反推。"""

    payload_by_role = {
        payload["role_id"]: payload for payload in role_contracts_payload()
    }

    assert set(LLM_ROLE_NAMES) == ACTIVE_LLM_ROLES
    for role_id, allowed_integration_ids in ROLE_INTEGRATION_MATRIX.items():
        assert payload_by_role[role_id]["allowed_integration_ids"] == list(
            allowed_integration_ids
        )


@pytest.mark.parametrize("retired_role", ["router", "preflight", "knowledge"])
def test_unknown_llm_roles_fail_closed(retired_role: str) -> None:
    """未登记职责不能通过运行时解析入口恢复模型调用。"""

    with pytest.raises(ValueError, match="未知 LLM 职责"):
        resolve_role_binding(LlmSettings(), retired_role)
