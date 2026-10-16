from __future__ import annotations

from dataclasses import dataclass

from companion.llm.chat_client import ChatCompletionsClient
from companion.llm.client_protocol import LlmClientProtocol
from companion.llm.config import (
    LlmRuntimeConfig,
    LlmSettings,
    LlmSettingsValidationError,
    resolve_role_binding,
)
from companion.llm.model_registry import (
    ModelIntegrationSpec,
    get_model_integration,
)
from companion.llm.output_policies import (
    DeepSeekDsmlOutputPolicy,
    NoOpOutputPolicy,
    OutputPolicy,
)
from companion.llm.request_adapters import (
    DeepSeekChatRequestAdapter,
    QwenChatRequestAdapter,
    RequestAdapter,
)
from companion.llm.request_profiles import (
    DeepSeekChatRequestProfile,
    QwenChatRequestProfile,
    get_model_request_profile,
)
from companion.observability.logging_config import get_backend_logger
from companion.observability.prompt_audit import PromptAuditRecorder


logger = get_backend_logger(__name__)


class LlmRoleDisabledError(RuntimeError):
    """表示调用方请求了已经显式关闭的可选 LLM 职责。

    Args:
        role_id: 当前被关闭的 LLM 职责标识。
    """

    def __init__(self, role_id: str) -> None:
        self.role_id = role_id
        super().__init__(f"LLM 职责未启用：{role_id}")


@dataclass(frozen=True, slots=True)
class LlmClientFactory:
    """根据显式职责绑定创建唯一的 Chat 客户端实现。

    Args:
        settings: 当前 schema v2 LLM 设置。
        prompt_audit_recorder: 当前进程生命周期共用的 Prompt 审查记录器。
    """

    settings: LlmSettings
    prompt_audit_recorder: PromptAuditRecorder

    def client(self, role_id: str) -> LlmClientProtocol:
        """创建一个已完成 Adapter 和 Output Policy 注入的客户端。

        Args:
            role_id: 需要创建客户端的 LLM 职责。

        Returns:
            绑定到显式模型接入规格的 Chat 客户端。

        Raises:
            LlmRoleDisabledError: 可选职责当前未绑定模型。
            ValueError: 注册表引用了未知 Adapter 或 Output Policy。
        """

        binding = resolve_role_binding(self.settings, role_id)
        if binding is None:
            raise LlmRoleDisabledError(role_id)
        return self._build_client(
            integration=binding.integration,
            runtime_config=binding.runtime_config,
            role_name=binding.role_contract.role_id,
        )

    def integration_client(
        self,
        integration_id: str,
        *,
        purpose: str,
    ) -> LlmClientProtocol:
        """为不参与职责候选的已验证内部依赖创建客户端。

        Args:
            integration_id: 已在静态注册表登记的接入规格编号。
            purpose: 日志中区分内部调用用途的稳定名称。

        Returns:
            复用对应厂商凭据、Adapter 和 Output Policy 的 Chat 客户端。

        Raises:
            LlmSettingsValidationError: 接入规格所需凭据没有配置。
            ValueError: 接入规格、Adapter、Output Policy 或用途无效。
        """

        normalized_purpose = purpose.strip()
        if not normalized_purpose:
            raise ValueError("内部 LLM 客户端 purpose 不能为空")
        integration = get_model_integration(integration_id)
        credential = self.settings.credentials.get(integration.provider_id)
        if (
            credential is None
            or not credential.base_url.strip()
            or not credential.api_key.strip()
        ):
            raise LlmSettingsValidationError(
                f"LLM 内部依赖 {integration.integration_id} 缺少厂商凭据："
                f"{integration.provider_id}"
            )
        runtime_config = LlmRuntimeConfig(
            integration_id=integration.integration_id,
            provider_id=integration.provider_id,
            model=integration.model,
            base_url=credential.base_url,
            api_key=credential.api_key,
        )
        return self._build_client(
            integration=integration,
            runtime_config=runtime_config,
            role_name=normalized_purpose,
        )

    def _build_client(
        self,
        *,
        integration: ModelIntegrationSpec,
        runtime_config: LlmRuntimeConfig,
        role_name: str,
    ) -> LlmClientProtocol:
        """把已解析接入规格装配成统一 Chat 客户端。

        Args:
            integration: 已验证的模型接入规格。
            runtime_config: 已补齐凭据和端点的运行时配置。
            role_name: 日志和调用审计使用的业务用途。

        Returns:
            完成 Adapter 与 Output Policy 注入的 Chat 客户端。
        """

        request_adapter = _request_adapter_for(integration)
        output_policy = _output_policy_for(integration)
        logger.info(
            "创建 LLM 客户端 role=%s integration=%s provider=%s model=%s "
            "api_surface=%s timeout_s=%s",
            role_name,
            integration.integration_id,
            integration.provider_id,
            integration.model,
            integration.api_surface,
            runtime_config.timeout_s,
        )
        return ChatCompletionsClient(
            runtime_config=runtime_config,
            integration=integration,
            request_adapter=request_adapter,
            output_policy=output_policy,
            role_name=role_name,
            prompt_audit_recorder=self.prompt_audit_recorder,
        )


def _request_adapter_for(integration: ModelIntegrationSpec) -> RequestAdapter:
    """为模型接入规格创建请求 Adapter。

    Args:
        integration: 已完成静态校验的模型接入规格。

    Returns:
        与注册表编号对应的无状态 Adapter。

    Raises:
        ValueError: 注册表引用了不存在的 Adapter。
    """

    adapter_id = integration.request_adapter_id
    request_profile = get_model_request_profile(integration.request_profile_id)
    if request_profile.model != integration.model:
        raise ValueError(
            "LLM 模型请求配置与接入规格的真实模型不一致："
            f"{integration.integration_id}"
        )
    if adapter_id == "deepseek_chat":
        if not isinstance(request_profile, DeepSeekChatRequestProfile):
            raise ValueError("DeepSeek Chat Adapter 收到错误类型的模型请求配置")
        return DeepSeekChatRequestAdapter(request_profile)
    if adapter_id == "qwen_chat":
        if not isinstance(request_profile, QwenChatRequestProfile):
            raise ValueError("Qwen Chat Adapter 收到错误类型的模型请求配置")
        return QwenChatRequestAdapter(request_profile)
    raise ValueError(f"未知 LLM Request Adapter：{adapter_id}")


def _output_policy_for(integration: ModelIntegrationSpec) -> OutputPolicy:
    """为模型接入规格创建正文输出策略。

    Args:
        integration: 已完成静态校验的模型接入规格。

    Returns:
        与注册表编号对应的无状态 Output Policy。

    Raises:
        ValueError: 注册表引用了不存在的 Output Policy。
    """

    policy_id = integration.output_policy_id
    if policy_id == "deepseek_dsml":
        return DeepSeekDsmlOutputPolicy()
    if policy_id == "noop":
        return NoOpOutputPolicy()
    raise ValueError(f"未知 LLM Output Policy：{policy_id}")
