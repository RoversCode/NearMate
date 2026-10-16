from __future__ import annotations

import pytest
from pydantic import ValidationError

from companion.core.api.schemas.bootstrap import LlmConfigRequest
from companion.llm.config import (
    LlmSettings,
    LlmSettingsValidationError,
    resolve_role_binding,
    validate_llm_settings_ready,
)
from companion.llm.role_contracts import validate_role_integration


def _settings(
    *,
    deepseek_api_key: str = "deepseek-key",
    qwen_api_key: str = "qwen-key",
    character_authoring: str | None = "deepseek:deepseek-flash:chat",
) -> LlmSettings:
    """构造使用 V1 默认职责矩阵的 schema v2 配置。"""

    return LlmSettings.model_validate(
        {
            "schema_version": 2,
            "credentials": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": deepseek_api_key,
                },
                "qwen": {
                    "base_url": (
                        "https://workspace.cn-beijing.maas.aliyuncs.com/"
                        "compatible-mode/v1"
                    ),
                    "api_key": qwen_api_key,
                },
            },
            "role_bindings": {
                "foreground": "deepseek:deepseek-flash:chat",
                "summary": "deepseek:deepseek-v4-pro:chat",
                "cognitive": "qwen:qwen3.6-plus:chat",
                "character_authoring": character_authoring,
            },
        }
    )


def test_cognitive_resolves_fixed_qwen_chat_integration() -> None:
    """Cognitive 必须解析到唯一经过验证的 Qwen Chat 接入规格。"""

    binding = resolve_role_binding(_settings(), "cognitive")

    assert binding is not None
    assert binding.integration.integration_id == "qwen:qwen3.6-plus:chat"
    assert binding.integration.api_surface == "chat_completions"
    assert binding.integration.provider_id == "qwen"


@pytest.mark.parametrize(
    ("deepseek_api_key", "qwen_api_key", "missing_field"),
    [
        ("", "qwen-key", "deepseek.api_key"),
        ("deepseek-key", "", "qwen.api_key"),
    ],
)
def test_missing_required_provider_credential_blocks_bootstrap(
    deepseek_api_key: str,
    qwen_api_key: str,
    missing_field: str,
) -> None:
    """任一必需职责使用的厂商凭据缺失都必须阻断 Bootstrap。"""

    settings = _settings(
        deepseek_api_key=deepseek_api_key,
        qwen_api_key=qwen_api_key,
    )

    validation = validate_llm_settings_ready(settings)

    assert validation.is_ready is False
    assert missing_field in validation.missing
    with pytest.raises(LlmSettingsValidationError, match=missing_field):
        resolve_role_binding(
            settings,
            "foreground" if missing_field.startswith("deepseek") else "cognitive",
        )


def test_disabled_optional_character_authoring_does_not_block_bootstrap() -> None:
    """显式关闭 Character Authoring 不影响三个必需职责就绪。"""

    settings = _settings(character_authoring=None)
    validation = validate_llm_settings_ready(settings)

    assert validation.is_ready is True
    assert validation.role_statuses["character_authoring"].enabled is False
    assert resolve_role_binding(settings, "character_authoring") is None


def test_explicit_matrix_rejects_capable_but_unapproved_binding() -> None:
    """能力标签即使看起来满足，也不能自动创建职责候选。"""

    with pytest.raises(ValueError, match="不允许使用"):
        validate_role_integration(
            "cognitive",
            "deepseek:deepseek-v4-pro:chat",
        )


def test_schema_v2_rejects_unknown_config_fields() -> None:
    """配置模型和 API 请求都不能静默接收合同外字段。"""

    invalid_payload = {
        "schema_version": 2,
        "credentials": {},
        "role_bindings": {
            "foreground": "deepseek:deepseek-flash:chat",
            "summary": "deepseek:deepseek-flash:chat",
            "cognitive": "qwen:qwen3.6-plus:chat",
            "character_authoring": None,
        },
        "unexpected_field": "unexpected-value",
    }

    with pytest.raises(ValidationError, match="unexpected_field"):
        LlmSettings.model_validate(invalid_payload)
    with pytest.raises(ValidationError, match="unexpected_field"):
        LlmConfigRequest.model_validate(invalid_payload)
