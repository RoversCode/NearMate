from __future__ import annotations

import asyncio
from dataclasses import dataclass

from companion.llm.client_factory import LlmClientFactory
from companion.llm.config import LlmSettings
from companion.llm.model_registry import list_provider_credential_specs
from companion.observability.prompt_audit import PromptAuditRecorder


_DISABLED_PROMPT_AUDIT_RECORDER = PromptAuditRecorder(
    enabled=False,
    output_dir=".",
    max_runs=1,
    process_role="provider-connectivity",
)
_SAFE_FAILURE_TYPE_BY_EXCEPTION_CLASS = {
    "AuthenticationError": "authentication_failed",
    "PermissionDeniedError": "authentication_failed",
    "APITimeoutError": "request_timeout",
    "APIConnectionError": "connection_failed",
    "NotFoundError": "endpoint_or_model_unavailable",
    "BadRequestError": "chat_request_rejected",
    "RateLimitError": "rate_limited",
    "APIStatusError": "provider_error",
    "LlmSettingsValidationError": "client_configuration_invalid",
    "ValueError": "client_configuration_invalid",
}


@dataclass(frozen=True, slots=True)
class ProviderConnectivityTarget:
    """保存一次厂商连通性检查所需的内部目标。

    Args:
        provider_id: 厂商稳定标识。
        display_name: 面向 Console 的厂商名称。
        integration_id: 本厂商用于最小 Chat 请求的模型接入规格。
    """

    provider_id: str
    display_name: str
    integration_id: str


@dataclass(frozen=True, slots=True)
class ProviderConnectivityFailure:
    """保存可以安全返回给 Console 的单个厂商失败信息。

    Args:
        provider_id: 失败的厂商稳定标识。
        display_name: 面向 Console 的厂商名称。
        failure_type: 不包含供应商响应正文的稳定失败类别。
    """

    provider_id: str
    display_name: str
    failure_type: str


class ProviderConnectivityError(ValueError):
    """表示至少一个已填写厂商无法完成最小 Chat 请求。

    Args:
        failures: 已按注册表顺序排列的安全失败信息。
    """

    def __init__(self, failures: tuple[ProviderConnectivityFailure, ...]) -> None:
        self.failures = failures
        failure_text = "；".join(
            f"{failure.display_name} [{failure.provider_id}]：{failure.failure_type}"
            for failure in failures
        )
        super().__init__(f"LLM Provider 连通性检查失败：{failure_text}")


async def check_configured_provider_connectivity(settings: LlmSettings) -> None:
    """并发检查所有 URL 和 Key 均已填写的厂商 Chat 连通性。

    这里只发送一条最小 `ping`，不会验证任何 LLM 职责的 Prompt、工具、
    Thinking、联网能力或结果协议。同步 SDK 请求放到工作线程中，避免卡住
    Backend 事件循环。

    Args:
        settings: 已通过静态校验但尚未提交的候选 LLM 配置。

    Raises:
        ProviderConnectivityError: 任一厂商无法访问 Chat 接口或探测模型。
    """

    targets = _configured_provider_targets(settings)
    if not targets:
        return

    results = await asyncio.gather(
        *(
            asyncio.to_thread(_ping_provider, settings, target)
            for target in targets
        ),
        return_exceptions=True,
    )
    failures = tuple(
        ProviderConnectivityFailure(
            provider_id=target.provider_id,
            display_name=target.display_name,
            failure_type=_safe_failure_type(result),
        )
        for target, result in zip(targets, results, strict=True)
        if isinstance(result, Exception)
    )
    if failures:
        raise ProviderConnectivityError(failures)


def _configured_provider_targets(
    settings: LlmSettings,
) -> tuple[ProviderConnectivityTarget, ...]:
    """选择本次必须检查的厂商，并确保每个厂商只出现一次。

    Args:
        settings: 候选 LLM 配置。

    Returns:
        按厂商注册表顺序排列的连通性目标。
    """

    targets: list[ProviderConnectivityTarget] = []
    seen_provider_ids: set[str] = set()
    for provider_spec in list_provider_credential_specs():
        if provider_spec.provider_id in seen_provider_ids:
            continue
        credential = settings.credentials.get(provider_spec.provider_id)
        if (
            credential is None
            or not credential.base_url.strip()
            or not credential.api_key.strip()
        ):
            continue
        seen_provider_ids.add(provider_spec.provider_id)
        targets.append(
            ProviderConnectivityTarget(
                provider_id=provider_spec.provider_id,
                display_name=provider_spec.display_name,
                integration_id=provider_spec.connectivity_probe_integration_id,
            )
        )
    return tuple(targets)


def _ping_provider(
    settings: LlmSettings,
    target: ProviderConnectivityTarget,
) -> None:
    """创建临时 Chat Client，发送最小请求并始终关闭网络资源。

    Args:
        settings: 候选 LLM 配置。
        target: 当前工作线程负责的唯一厂商目标。

    Raises:
        Exception: 客户端创建、最小请求或关闭阶段的原始异常。
    """

    client = LlmClientFactory(
        settings=settings,
        prompt_audit_recorder=_DISABLED_PROMPT_AUDIT_RECORDER,
    ).integration_client(
        target.integration_id,
        purpose="provider_connectivity",
    )
    request_failure: Exception | None = None
    try:
        client.warmup()
    except Exception as exception:
        request_failure = exception

    try:
        client.close()
    except Exception:
        # 请求已经失败时，关闭异常不能掩盖更有用的连通性失败类别。
        if request_failure is None:
            raise

    if request_failure is not None:
        raise request_failure


def _safe_failure_type(exception: Exception) -> str:
    """把第三方异常归并成不含响应正文的稳定失败类别。

    Args:
        exception: 工作线程返回的原始异常对象。

    Returns:
        可安全写入 HTTP 400 detail 的失败类别。
    """

    for exception_class in type(exception).__mro__:
        failure_type = _SAFE_FAILURE_TYPE_BY_EXCEPTION_CLASS.get(
            exception_class.__name__
        )
        if failure_type is not None:
            return failure_type
    return "unexpected_client_error"
