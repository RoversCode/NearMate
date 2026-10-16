from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from companion.llm.request_profiles import (
    DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID,
    DEEPSEEK_V4_PRO_CHAT_REQUEST_PROFILE_ID,
    QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID,
    QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID,
)


class LlmCapability(StrEnum):
    """描述模型接入已经通过合同测试的能力。"""

    JSON_OUTPUT = "json_output"
    TOOL_CALLING = "tool_calling"
    IMAGE_INPUT = "image_input"
    WEB_SEARCH = "web_search"
    WEB_EXTRACT = "web_extract"


ApiSurface = Literal["chat_completions"]
QWEN_COGNITIVE_REPAIR_INTEGRATION_ID = "qwen:qwen3.6-flash:chat"


@dataclass(frozen=True, slots=True)
class ProviderCredentialSpec:
    """描述厂商凭据和端点的静态规则。

    Args:
        provider_id: 厂商稳定标识。
        display_name: Console 展示名称。
        default_base_url: 用户未填写端点时可以使用的默认地址。
        default_base_url_allowed: 是否允许直接使用默认地址。
        endpoint_normalizer: 保存前使用的端点规范化规则编号。
        connectivity_probe_integration_id: 验证该厂商 Chat 连通性时使用的内部模型规格。
    """

    provider_id: str
    display_name: str
    default_base_url: str
    default_base_url_allowed: bool
    endpoint_normalizer: Literal["standard_url", "qwen_workspace"]
    connectivity_probe_integration_id: str

    def to_payload(self) -> dict[str, Any]:
        """转换为 Console 可以直接消费的厂商说明。

        Returns:
            不包含用户凭据的静态厂商字段。
        """

        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "default_base_url": self.default_base_url,
            "default_base_url_allowed": self.default_base_url_allowed,
            "endpoint_normalizer": self.endpoint_normalizer,
        }


@dataclass(frozen=True, slots=True)
class ModelIntegrationSpec:
    """描述一种经过项目验证的模型调用方式。

    Args:
        integration_id: 持久化绑定使用的不透明标识。
        provider_id: 复用哪一份厂商凭据。
        model: 发送给厂商接口的真实模型名。
        display_name: Console 展示名称。
        api_surface: 当前接入使用的 SDK 接口形态。
        request_adapter_id: 编译厂商请求方言的 Adapter 编号。
        request_profile_id: 厂家 Adapter 使用的内部模型请求配置编号。
        output_policy_id: 正文释放前使用的输出策略编号。
        capabilities: 已通过合同测试的描述性能力集合。
    """

    integration_id: str
    provider_id: str
    model: str
    display_name: str
    api_surface: ApiSurface
    request_adapter_id: str
    request_profile_id: str
    output_policy_id: str
    capabilities: frozenset[LlmCapability]

    def supports(self, capability: LlmCapability) -> bool:
        """判断接入规格是否声明了指定能力。

        Args:
            capability: 需要核对的能力。

        Returns:
            该能力已经登记时返回真。
        """

        return capability in self.capabilities

    def to_payload(self) -> dict[str, Any]:
        """转换为不暴露内部实现对象的 API 载荷。

        Returns:
            Console 展示和选择模型所需的稳定字段。
        """

        return {
            "integration_id": self.integration_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "display_name": self.display_name,
            "api_surface": self.api_surface,
            "capabilities": sorted(item.value for item in self.capabilities),
        }


PROVIDER_CREDENTIAL_SPECS: tuple[ProviderCredentialSpec, ...] = (
    ProviderCredentialSpec(
        provider_id="deepseek",
        display_name="DeepSeek",
        default_base_url="https://api.deepseek.com/v1",
        default_base_url_allowed=True,
        endpoint_normalizer="standard_url",
        connectivity_probe_integration_id="deepseek:deepseek-flash:chat",
    ),
    ProviderCredentialSpec(
        provider_id="qwen",
        display_name="Qwen",
        default_base_url="",
        default_base_url_allowed=False,
        endpoint_normalizer="qwen_workspace",
        connectivity_probe_integration_id="qwen:qwen3.6-plus:chat",
    ),
)


MODEL_INTEGRATION_SPECS: tuple[ModelIntegrationSpec, ...] = (
    ModelIntegrationSpec(
        integration_id="deepseek:deepseek-flash:chat",
        provider_id="deepseek",
        model="deepseek-flash",
        display_name="DeepSeek V4.1 Flash",
        api_surface="chat_completions",
        request_adapter_id="deepseek_chat",
        request_profile_id=DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID,
        output_policy_id="deepseek_dsml",
        capabilities=frozenset(
            {LlmCapability.JSON_OUTPUT, LlmCapability.TOOL_CALLING, LlmCapability.IMAGE_INPUT}
        ),
    ),
    ModelIntegrationSpec(
        integration_id="deepseek:deepseek-v4-pro:chat",
        provider_id="deepseek",
        model="deepseek-v4-pro",
        display_name="DeepSeek V4 Pro",
        api_surface="chat_completions",
        request_adapter_id="deepseek_chat",
        request_profile_id=DEEPSEEK_V4_PRO_CHAT_REQUEST_PROFILE_ID,
        output_policy_id="deepseek_dsml",
        capabilities=frozenset(
            {LlmCapability.JSON_OUTPUT, LlmCapability.TOOL_CALLING}
        ),
    ),
    ModelIntegrationSpec(
        integration_id="qwen:qwen3.6-plus:chat",
        provider_id="qwen",
        model="qwen3.6-plus",
        display_name="Qwen 3.6 Plus",
        api_surface="chat_completions",
        request_adapter_id="qwen_chat",
        request_profile_id=QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID,
        output_policy_id="noop",
        capabilities=frozenset(
            {
                LlmCapability.IMAGE_INPUT,
                LlmCapability.WEB_SEARCH,
                LlmCapability.WEB_EXTRACT,
            }
        ),
    ),
    ModelIntegrationSpec(
        integration_id=QWEN_COGNITIVE_REPAIR_INTEGRATION_ID,
        provider_id="qwen",
        model="qwen3.6-flash",
        display_name="Qwen 3.6 Flash",
        api_surface="chat_completions",
        request_adapter_id="qwen_chat",
        request_profile_id=QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID,
        output_policy_id="noop",
        capabilities=frozenset({LlmCapability.JSON_OUTPUT}),
    ),
)


def list_provider_credential_specs() -> list[ProviderCredentialSpec]:
    """返回 V1 全部厂商凭据规格。

    Returns:
        新列表，调用方不能修改静态注册表。
    """

    return list(PROVIDER_CREDENTIAL_SPECS)


def get_provider_credential_spec(provider_id: str) -> ProviderCredentialSpec:
    """读取一个已登记的厂商凭据规格。

    Args:
        provider_id: 需要查询的厂商标识。

    Returns:
        匹配的静态厂商规格。

    Raises:
        ValueError: 厂商未经过项目适配。
    """

    normalized_provider_id = provider_id.strip()
    for provider_spec in PROVIDER_CREDENTIAL_SPECS:
        if provider_spec.provider_id == normalized_provider_id:
            return provider_spec
    raise ValueError(f"未支持的 LLM 厂商：{normalized_provider_id}")


def list_model_integrations() -> list[ModelIntegrationSpec]:
    """返回 V1 全部模型接入规格。

    Returns:
        新列表，调用方不能修改静态注册表。
    """

    return list(MODEL_INTEGRATION_SPECS)


def get_model_integration(integration_id: str) -> ModelIntegrationSpec:
    """按不透明编号读取模型接入规格。

    Args:
        integration_id: 配置保存的模型接入规格编号。

    Returns:
        匹配的模型接入规格。

    Raises:
        ValueError: 编号为空或没有登记。
    """

    normalized_integration_id = integration_id.strip()
    for integration in MODEL_INTEGRATION_SPECS:
        if integration.integration_id == normalized_integration_id:
            return integration
    raise ValueError(f"未支持的 LLM 模型接入规格：{normalized_integration_id}")


def model_registry_payload() -> dict[str, list[dict[str, Any]]]:
    """构造已验证模型注册表的公开载荷。

    Returns:
        厂商规格和模型接入规格两个列表。
    """

    return {
        "providers": [item.to_payload() for item in PROVIDER_CREDENTIAL_SPECS],
        "integrations": [item.to_payload() for item in MODEL_INTEGRATION_SPECS],
    }
