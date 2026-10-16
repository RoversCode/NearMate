from __future__ import annotations

from companion.llm.client_factory import LlmClientFactory
from companion.llm.config import LlmSettings
from companion.llm.model_registry import QWEN_COGNITIVE_REPAIR_INTEGRATION_ID
from companion.llm.output_policies import DeepSeekDsmlOutputPolicy
from companion.llm.request_adapters import (
    DeepSeekChatRequestAdapter,
    QwenChatRequestAdapter,
)
from companion.llm.request_profiles import (
    DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID,
    QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID,
)
from companion.observability.prompt_audit import PromptAuditRecorder


def test_client_factory_log_excludes_api_key_and_auth_token(
    monkeypatch,
    caplog,
) -> None:
    """创建客户端的日志只能包含接入路由字段，不能泄露凭据。"""

    api_key = "API-KEY-SHOULD-NOT-LOG"
    settings = LlmSettings.model_validate(
        {
            "schema_version": 2,
            "credentials": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": api_key,
                },
                "qwen": {
                    "base_url": (
                        "https://workspace.cn-beijing.maas.aliyuncs.com/"
                        "compatible-mode/v1"
                    ),
                    "api_key": "qwen-key",
                },
            },
        }
    )
    monkeypatch.setattr(
        "companion.llm.client_factory.ChatCompletionsClient",
        lambda **_kwargs: object(),
    )

    with caplog.at_level("INFO", logger="companion.llm.client_factory"):
        LlmClientFactory(
            settings=settings,
            prompt_audit_recorder=_disabled_prompt_audit_recorder(),
        ).client("foreground")

    assert "deepseek:deepseek-flash:chat" in caplog.text
    assert "deepseek-flash" in caplog.text
    assert api_key not in caplog.text
    assert "AUTH-TOKEN" not in caplog.text


def test_factory_creates_internal_flash_client(
    monkeypatch,
) -> None:
    """内部 Flash 复用 Qwen 凭据并使用独立的调用用途。"""

    settings = LlmSettings.model_validate(
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
                    "api_key": "qwen-key",
                },
            },
        }
    )
    captured: dict[str, object] = {}

    def capture_client(**kwargs: object) -> object:
        """保存 Factory 交给统一 Chat Client 的装配参数。"""

        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "companion.llm.client_factory.ChatCompletionsClient",
        capture_client,
    )
    recorder = _disabled_prompt_audit_recorder()
    LlmClientFactory(
        settings=settings,
        prompt_audit_recorder=recorder,
    ).integration_client(
        QWEN_COGNITIVE_REPAIR_INTEGRATION_ID,
        purpose="cognitive_result_repair",
    )

    assert captured["integration"].model == "qwen3.6-flash"  # type: ignore[union-attr]
    assert captured["runtime_config"].api_key == "qwen-key"  # type: ignore[union-attr]
    assert captured["role_name"] == "cognitive_result_repair"
    assert captured["prompt_audit_recorder"] is recorder
    assert isinstance(captured["request_adapter"], QwenChatRequestAdapter)
    assert (
        captured["request_adapter"].request_profile.profile_id
        == QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID
    )


def test_factory_creates_deepseek_vision_exp_role_client(monkeypatch) -> None:
    """实验模型必须使用自己的模型名和请求配置，并复用 DeepSeek 行为。"""

    settings = LlmSettings.model_validate(
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
                    "api_key": "qwen-key",
                },
            },
            "role_bindings": {
                "foreground": (
                    "deepseek:deepseek-flash:chat"
                ),
            },
        }
    )
    captured: dict[str, object] = {}

    def capture_client(**kwargs: object) -> object:
        """保存 Factory 为实验模型装配的客户端参数。

        Args:
            **kwargs: Factory 交给统一 Chat 客户端的全部装配参数。

        Returns:
            代替真实 Chat 客户端的空对象。
        """

        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "companion.llm.client_factory.ChatCompletionsClient",
        capture_client,
    )

    LlmClientFactory(
        settings=settings,
        prompt_audit_recorder=_disabled_prompt_audit_recorder(),
    ).client("foreground")

    assert captured["integration"].model == (  # type: ignore[union-attr]
        "deepseek-flash"
    )
    assert captured["runtime_config"].model == (  # type: ignore[union-attr]
        "deepseek-flash"
    )
    assert isinstance(captured["request_adapter"], DeepSeekChatRequestAdapter)
    assert (
        captured["request_adapter"].request_profile.profile_id
        == DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID
    )
    assert isinstance(captured["output_policy"], DeepSeekDsmlOutputPolicy)


def _disabled_prompt_audit_recorder() -> PromptAuditRecorder:
    """构造不会访问文件系统的测试审查记录器。

    Returns:
        关闭状态的 Backend 主进程记录器。
    """

    return PromptAuditRecorder(
        enabled=False,
        output_dir="unused",
        max_runs=20,
        process_role="backend-main",
    )
