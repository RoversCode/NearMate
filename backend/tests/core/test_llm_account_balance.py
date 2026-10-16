from __future__ import annotations

import pytest

from companion.llm import account_balance
from companion.llm.account_balance import (
    ACCOUNT_STATUS_ERROR,
    ACCOUNT_STATUS_NOT_CONFIGURED,
    ACCOUNT_STATUS_UNSUPPORTED,
    fetch_provider_account_statuses,
)
from companion.llm.config import LlmSettings, ProviderCredential


def _settings() -> LlmSettings:
    """构造同时配置 DeepSeek 和 Qwen 的最小 LLM 设置。"""

    return LlmSettings(
        credentials={
            "deepseek": ProviderCredential(
                base_url="https://api.deepseek.com/v1",
                api_key="deepseek-key",
            ),
            "qwen": ProviderCredential(
                base_url="workspace",
                api_key="qwen-key",
            ),
        }
    )


def test_account_status_keeps_provider_identity_and_marks_unsupported_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """余额快照包含已登记厂商，不能把 Qwen 的未知余额伪造成零。"""

    monkeypatch.setattr(account_balance, "_fetch_deepseek_balance", lambda _credential: ("12.50", "CNY"))
    payload = fetch_provider_account_statuses(_settings())

    providers = {item["provider_id"]: item for item in payload["providers"]}
    assert providers["deepseek"]["active"] is True
    assert providers["deepseek"]["status"] == "available"
    assert providers["deepseek"]["balance"] == "12.50"
    assert providers["qwen"]["status"] == ACCOUNT_STATUS_UNSUPPORTED
    assert providers["qwen"]["balance"] is None


def test_account_status_marks_missing_key_without_network_call() -> None:
    """缺少凭据时直接返回未配置状态，避免把空 Key 发给厂商。"""

    settings = _settings()
    settings.credentials["deepseek"] = ProviderCredential(
        base_url="https://api.deepseek.com/v1",
        api_key="",
    )

    payload = fetch_provider_account_statuses(settings)

    deepseek = next(item for item in payload["providers"] if item["provider_id"] == "deepseek")
    assert deepseek["status"] == ACCOUNT_STATUS_NOT_CONFIGURED
    assert deepseek["balance"] is None


def test_account_status_converts_unexpected_provider_failure_to_safe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """余额依赖的意外异常只影响该厂商状态，不能让接口返回 500。"""

    def raise_unexpected_failure(_credential: ProviderCredential) -> tuple[str, str]:
        """模拟余额 SDK 抛出的非 HTTP 异常。"""

        raise RuntimeError("provider library failure")

    monkeypatch.setattr(account_balance, "_fetch_deepseek_balance", raise_unexpected_failure)

    payload = fetch_provider_account_statuses(_settings())

    deepseek = next(item for item in payload["providers"] if item["provider_id"] == "deepseek")
    assert deepseek["status"] == ACCOUNT_STATUS_ERROR
    assert deepseek["balance"] is None
    assert deepseek["message"] == "余额服务暂时不可用"
