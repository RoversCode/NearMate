from __future__ import annotations

import pytest

from companion.llm.chat_client import ChatCompletionsClient
from companion.llm.config import LLM_SDK_MAX_RETRIES, LlmRuntimeConfig
from companion.llm.model_registry import get_model_integration
from companion.llm.output_policies import DeepSeekDsmlOutputPolicy
from companion.llm.request_adapters import DeepSeekChatRequestAdapter
from companion.llm.request_profiles import get_model_request_profile


class _FakeOpenAI:
    """记录真实构造参数的 OpenAI 替身。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def test_chat_client_passes_explicit_max_retries_to_openai_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """唯一 Chat Transport 必须显式传入项目固定 SDK 重试次数。"""

    created: list[_FakeOpenAI] = []

    def factory(**kwargs: object) -> _FakeOpenAI:
        instance = _FakeOpenAI(**kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr("companion.llm.chat_client.OpenAI", factory)
    integration = get_model_integration("deepseek:deepseek-v4-pro:chat")
    ChatCompletionsClient(
        runtime_config=LlmRuntimeConfig(
            integration_id=integration.integration_id,
            provider_id=integration.provider_id,
            model=integration.model,
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
        ),
        integration=integration,
        request_adapter=DeepSeekChatRequestAdapter(
            get_model_request_profile("deepseek-v4-pro-chat-v1")
        ),
        output_policy=DeepSeekDsmlOutputPolicy(),
        role_name="foreground",
    )

    assert len(created) == 1
    assert created[0].kwargs["max_retries"] == LLM_SDK_MAX_RETRIES == 2
    assert created[0].kwargs["api_key"] == "test-key"
