from __future__ import annotations

from dataclasses import dataclass

import pytest

from companion.llm import connectivity as connectivity_module
from companion.llm.config import LlmSettings, ProviderCredential
from companion.llm.connectivity import (
    ProviderConnectivityError,
    check_configured_provider_connectivity,
)


@dataclass
class RecordingClient:
    """记录最小请求和关闭动作，并可模拟 Provider 失败。

    Args:
        integration_id: 当前客户端绑定的探测模型规格。
        events: 所有线程共用的调用记录。
        warmup_failure: 可选的最小请求失败。
    """

    integration_id: str
    events: list[tuple[str, str]]
    warmup_failure: Exception | None = None

    def warmup(self) -> None:
        """记录最小 Chat 请求，并按需模拟失败。

        Raises:
            Exception: 测试注入的 Provider 失败。
        """

        self.events.append(("warmup", self.integration_id))
        if self.warmup_failure is not None:
            raise self.warmup_failure

    def close(self) -> None:
        """记录临时客户端已经释放网络资源。"""

        self.events.append(("close", self.integration_id))


class RecordingFactory:
    """代替真实工厂，记录每个 Provider 选择的内部探测模型。"""

    events: list[tuple[str, str]] = []
    failures: dict[str, Exception] = {}

    def __init__(self, **_kwargs: object) -> None:
        """接受生产工厂参数，但不创建真实 SDK Client。

        Args:
            **_kwargs: 候选设置与关闭态 Prompt Audit Recorder。
        """

    def integration_client(self, integration_id: str, *, purpose: str) -> RecordingClient:
        """返回绑定到指定内部模型的记录客户端。

        Args:
            integration_id: Provider 注册的连通性探测模型。
            purpose: 生产代码使用的稳定内部用途。

        Returns:
            可记录 warmup 和 close 的客户端替身。
        """

        assert purpose == "provider_connectivity"
        self.events.append(("create", integration_id))
        return RecordingClient(
            integration_id=integration_id,
            events=self.events,
            warmup_failure=self.failures.get(integration_id),
        )


@pytest.fixture(autouse=True)
def reset_recording_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """让每个测试使用独立调用记录并阻止真实网络访问。

    Args:
        monkeypatch: Pytest 属性替换工具。
    """

    RecordingFactory.events = []
    RecordingFactory.failures = {}
    monkeypatch.setattr(connectivity_module, "LlmClientFactory", RecordingFactory)


@pytest.mark.asyncio
async def test_all_configured_providers_are_checked_once_and_closed() -> None:
    """两家凭据都填好时，每家只发一次最小请求并关闭客户端。"""

    await check_configured_provider_connectivity(_settings())

    assert RecordingFactory.events.count(
        ("warmup", "deepseek:deepseek-flash:chat")
    ) == 1
    assert RecordingFactory.events.count(
        ("warmup", "qwen:qwen3.6-plus:chat")
    ) == 1
    assert RecordingFactory.events.count(
        ("close", "deepseek:deepseek-flash:chat")
    ) == 1
    assert RecordingFactory.events.count(
        ("close", "qwen:qwen3.6-plus:chat")
    ) == 1


@pytest.mark.asyncio
async def test_provider_without_role_binding_is_still_checked() -> None:
    """连通性目标来自凭据池，不能依赖当前职责是否引用该 Provider。"""

    settings = _settings().model_copy(deep=True)
    settings.role_bindings = {
        "foreground": "deepseek:deepseek-flash:chat",
        "summary": "deepseek:deepseek-flash:chat",
        "cognitive": "deepseek:deepseek-flash:chat",
        "character_authoring": None,
    }

    await check_configured_provider_connectivity(settings)

    assert ("warmup", "qwen:qwen3.6-plus:chat") in RecordingFactory.events


@pytest.mark.asyncio
async def test_provider_with_incomplete_credentials_is_not_checked() -> None:
    """URL 或 Key 任一为空时，该 Provider 不属于本次网络检查目标。"""

    settings = _settings().model_copy(deep=True)
    settings.credentials["qwen"] = ProviderCredential(
        base_url=settings.credentials["qwen"].base_url,
        api_key="",
    )

    await check_configured_provider_connectivity(settings)

    assert all("qwen" not in integration_id for _, integration_id in RecordingFactory.events)


@pytest.mark.asyncio
async def test_failure_detail_contains_only_provider_and_safe_failure_type() -> None:
    """Provider 原始响应、Prompt 和 API Key 都不能进入可见错误。"""

    class AuthenticationError(RuntimeError):
        """模拟 OpenAI SDK 的鉴权异常类名。"""

    secret_key = "sk-secret-qwen"
    provider_body = "provider raw body with prompt ping"
    settings = _settings(qwen_api_key=secret_key)
    RecordingFactory.failures["qwen:qwen3.6-plus:chat"] = AuthenticationError(
        provider_body
    )

    with pytest.raises(ProviderConnectivityError) as captured:
        await check_configured_provider_connectivity(settings)

    error_text = str(captured.value)
    assert "Qwen [qwen]" in error_text
    assert "authentication_failed" in error_text
    assert secret_key not in error_text
    assert provider_body not in error_text
    assert "ping" not in error_text
    assert ("close", "qwen:qwen3.6-plus:chat") in RecordingFactory.events


def _settings(*, qwen_api_key: str = "qwen-key") -> LlmSettings:
    """构造两家 Provider 都已填写的合法候选配置。

    Args:
        qwen_api_key: Qwen 测试凭据，可用于验证错误不会泄密。

    Returns:
        可进入连通性检查的 schema v2 设置。
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
                    "api_key": qwen_api_key,
                },
            },
            "role_bindings": {
                "foreground": "deepseek:deepseek-flash:chat",
                "summary": "deepseek:deepseek-v4-pro:chat",
                "cognitive": "qwen:qwen3.6-plus:chat",
                "character_authoring": "deepseek:deepseek-flash:chat",
            },
        }
    )
