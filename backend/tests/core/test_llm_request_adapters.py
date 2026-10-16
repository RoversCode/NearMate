from __future__ import annotations

import pytest

from companion.llm.request_adapters import (
    DeepSeekChatRequestAdapter,
    QwenChatRequestAdapter,
)
from companion.llm.request_profiles import (
    DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID,
    QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID,
    QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID,
    DeepSeekChatRequestProfile,
    QwenChatRequestProfile,
    get_model_request_profile,
)
from companion.llm.request_intent import LlmInvocationIntent


PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "confirm_contract",
        "description": "确认合同。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


def test_deepseek_adapter_places_standard_and_private_parameters_exactly() -> None:
    """DeepSeek 标准参数必须在顶层，厂商 Thinking 开关只能进入 extra_body。"""

    input_tools = [PROBE_TOOL]
    request = _deepseek_adapter().compile(
        intent=LlmInvocationIntent(
            tool_mode="required",
            required_tool_name="confirm_contract",
            allow_parallel_tools=False,
            thinking_level="high",
            output_format="json_object",
            max_output_tokens=321,
        ),
        tools=input_tools,
    )

    assert request == {
        "reasoning_effort": "high",
        "response_format": {"type": "json_object"},
        "max_tokens": 321,
        "tools": [PROBE_TOOL],
        "parallel_tool_calls": False,
        "tool_choice": {
            "type": "function",
            "function": {"name": "confirm_contract"},
        },
        "extra_body": {"thinking": {"type": "enabled"}},
    }
    assert request["tools"] is not input_tools
    assert request["tools"][0] is not PROBE_TOOL


def test_deepseek_adapter_explicitly_disables_thinking_and_tools() -> None:
    """禁用工具和思考时必须显式发出两个门禁，不能依赖厂商默认值。"""

    request = _deepseek_adapter().compile(
        intent=LlmInvocationIntent.text(thinking_level="off"),
        tools=[],
    )

    assert request == {
        "tool_choice": "none",
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def test_deepseek_vision_exp_profile_matches_flash_request_capabilities() -> None:
    """实验模型使用独立模型名，但保持 Flash/Pro 的 Chat 请求能力。"""

    profile = get_model_request_profile(
        DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID
    )

    assert isinstance(profile, DeepSeekChatRequestProfile)
    assert profile.model == "deepseek-flash"
    assert profile.supports_json_object is True
    assert profile.supports_function_tools is True
    assert profile.supports_reasoning_effort is True


@pytest.mark.parametrize("unsupported_intent", ["web_search", "web_extract"])
def test_deepseek_adapter_rejects_unverified_web_capabilities(
    unsupported_intent: str,
) -> None:
    """未验证的 DeepSeek 联网能力必须失败关闭。

    Args:
        unsupported_intent: 本次尝试启用的联网能力字段。
    """

    kwargs = {unsupported_intent: True}
    with pytest.raises(ValueError, match="未验证联网搜索或网页抓取"):
        _deepseek_adapter().compile(
            intent=LlmInvocationIntent.json_object(**kwargs),  # type: ignore[arg-type]
            tools=[],
        )


@pytest.mark.parametrize(
    ("thinking_level", "enable_thinking"),
    [("off", False), ("minimal", True), ("high", True)],
)
def test_qwen_plus_maps_thinking_tiers_to_boolean_without_budget(
    thinking_level: str,
    enable_thinking: bool,
) -> None:
    """Plus 只发送思考开关，不能伪造厂家没有提供的预算或档位。

    Args:
        thinking_level: 业务请求的思考档位。
        enable_thinking: 百炼请求中预期的布尔开关。
    """

    request = _qwen_adapter().compile(
        intent=LlmInvocationIntent.text(thinking_level=thinking_level),  # type: ignore[arg-type]
        tools=[],
    )

    assert request == {"extra_body": {"enable_thinking": enable_thinking}}
    assert "thinking_budget" not in request["extra_body"]
    assert "reasoning_effort" not in request


@pytest.mark.parametrize("web_capability", ["web_search", "web_extract"])
def test_qwen_plus_forces_search_for_web_capabilities(
    web_capability: str,
) -> None:
    """搜索和网页抓取都必须要求 Qwen 厂商实际执行联网检索。

    Args:
        web_capability: 本次打开的厂家无关联网能力字段。
    """

    request = _qwen_adapter().compile(
        intent=LlmInvocationIntent.text(
            thinking_level="minimal",
            **{web_capability: True},  # type: ignore[arg-type]
        ),
        tools=[],
    )

    assert request == {
        "extra_body": {
            "enable_thinking": True,
            "enable_search": True,
            "search_options": {"forced_search": True},
        }
    }
    assert "response_format" not in request


def test_qwen_plus_logs_thinking_tier_degradation(caplog: pytest.LogCaptureFixture) -> None:
    """厂家只能表达布尔开关时必须留下不含正文和凭据的结构化 INFO 日志。"""

    with caplog.at_level("INFO", logger="companion.llm.request_adapters.qwen_chat"):
        _qwen_adapter().compile(
            intent=LlmInvocationIntent.text(thinking_level="high"),
            tools=[],
        )

    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "llm_capability_degraded"
    ]
    assert len(records) == 1
    fields = records[0].structured_fields
    assert fields == {
        "provider": "qwen",
        "model": "qwen3.6-plus",
        "request_profile_id": QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID,
        "requested_thinking_level": "high",
        "actual_mapping": "enable_thinking=true",
        "reason": "qwen_chat_supports_boolean_thinking_only",
    }
    assert "prompt" not in caplog.text.lower()
    assert "api_key" not in caplog.text.lower()


def test_qwen_plus_rejects_json_object_with_web_search() -> None:
    """已知会返回 400 的 JSON Mode 与联网搜索组合必须在本地拒绝。"""

    with pytest.raises(ValueError, match="联网搜索不支持 json_object"):
        _qwen_adapter().compile(
            intent=LlmInvocationIntent.json_object(web_search=True),
            tools=[],
        )


def test_qwen_plus_rejects_json_object_without_web_search() -> None:
    """Plus 接入只承载标记文本，不能退回无联网 JSON Mode。"""

    with pytest.raises(ValueError, match="只允许 text 输出"):
        _qwen_adapter().compile(
            intent=LlmInvocationIntent.json_object(),
            tools=[],
        )


def test_qwen_flash_maps_non_thinking_json_object_at_top_level() -> None:
    """Flash 修复调用必须关闭思考，并在顶层发送 JSON Object 参数。"""

    request = _qwen_adapter(QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID).compile(
        intent=LlmInvocationIntent.json_object(thinking_level="off"),
        tools=[],
    )

    assert request == {
        "extra_body": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }


def test_qwen_flash_rejects_plain_text_output() -> None:
    """Flash 接入只承担最终 JSON 收口，不能被当作普通文本模型调用。"""

    with pytest.raises(ValueError, match="只允许 json_object 输出"):
        _qwen_adapter(QWEN_36_FLASH_CHAT_REQUEST_PROFILE_ID).compile(
            intent=LlmInvocationIntent.text(thinking_level="off"),
            tools=[],
        )


def test_qwen_adapter_rejects_project_function_tools() -> None:
    """Qwen Chat 接入没有验证项目函数工具，传入工具时必须失败关闭。"""

    with pytest.raises(ValueError, match="不支持项目函数工具"):
        _qwen_adapter().compile(
            intent=LlmInvocationIntent.text(),
            tools=[PROBE_TOOL],
        )


def _deepseek_adapter(
    profile_id: str = DEEPSEEK_FLASH_CHAT_REQUEST_PROFILE_ID,
) -> DeepSeekChatRequestAdapter:
    """构造绑定指定 DeepSeek 模型请求配置的 Adapter。

    Args:
        profile_id: Flash、Vision Exp 或 Pro 的内部请求配置编号。

    Returns:
        可用于参数映射测试的 DeepSeek Adapter。
    """

    profile = get_model_request_profile(profile_id)
    assert isinstance(profile, DeepSeekChatRequestProfile)
    return DeepSeekChatRequestAdapter(profile)


def _qwen_adapter(
    profile_id: str = QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID,
) -> QwenChatRequestAdapter:
    """构造绑定指定 Qwen 模型请求配置的 Adapter。

    Args:
        profile_id: Plus 或 Flash 的内部请求配置编号。

    Returns:
        可用于参数映射测试的 Qwen Adapter。
    """

    profile = get_model_request_profile(profile_id)
    assert isinstance(profile, QwenChatRequestProfile)
    return QwenChatRequestAdapter(profile)
