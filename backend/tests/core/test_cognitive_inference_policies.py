from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion.foreground_tools.cognitive.contracts import (
    CognitiveExecutionPlan,
    CognitiveGoal,
    CognitiveGoalKind,
    CognitiveReasoning,
)
from companion.foreground_tools.cognitive.inference_policies import (
    cognitive_invocation_intent,
)
from companion.llm.chat_client import ChatCompletionsClient
from companion.llm.config import LlmRuntimeConfig
from companion.llm.model_registry import get_model_integration
from companion.llm.output_policies import NoOpOutputPolicy
from companion.llm.request_adapters.qwen_chat import QwenChatRequestAdapter
from companion.llm.request_profiles import (
    QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID,
    QwenChatRequestProfile,
    get_model_request_profile,
)


@pytest.mark.parametrize(
    ("reasoning", "enable_thinking"),
    [
        ("off", False),
        ("minimal", True),
        ("high", True),
    ],
)
def test_qwen_cognitive_thinking_uses_boolean_private_chat_parameter(
    reasoning: CognitiveReasoning,
    enable_thinking: bool,
) -> None:
    """三档认知思考意图必须降级为 Qwen extra_body 的布尔开关。"""

    intent = cognitive_invocation_intent(
        _plan(kind="analyze_deeply", reasoning=reasoning)
    )
    request = _qwen_adapter().compile(
        intent=intent,
        tools=[],
    )

    assert request["extra_body"]["enable_thinking"] is enable_thinking
    assert "thinking_budget" not in request["extra_body"]
    assert "response_format" not in request
    assert "reasoning" not in request
    assert "reasoning_effort" not in request
    assert "tools" not in request


def test_qwen_search_forces_search_without_server_tools() -> None:
    """当前事实查询必须强制使用 Qwen Chat 搜索扩展，不生成项目函数工具。"""

    request = _qwen_adapter().compile(
        intent=cognitive_invocation_intent(
            _plan(
                kind="lookup_current_fact",
                reasoning="minimal",
                web_search=True,
            )
        ),
        tools=[],
    )

    assert request["extra_body"]["enable_search"] is True
    assert request["extra_body"]["search_options"] == {"forced_search": True}
    assert "tools" not in request
    assert "tool_choice" not in request


def test_qwen_extract_forces_search_without_changing_thinking_intent() -> None:
    """网页抓取必须强制使用厂商联网开关，Adapter 不得擅自改变业务思考意图。"""

    request = _qwen_adapter().compile(
        intent=cognitive_invocation_intent(
            _plan(
                kind="lookup_current_fact",
                reasoning="off",
                web_extract=True,
            )
        ),
        tools=[],
    )

    assert request["extra_body"] == {
        "enable_thinking": False,
        "enable_search": True,
        "search_options": {"forced_search": True},
    }


def test_qwen_chat_client_preserves_image_messages_and_adapter_output() -> None:
    """Chat Client 必须原样保留图片消息，并只发送 Adapter 编译字段。"""

    captured_request: dict[str, object] = {}

    def create_completion(**kwargs: object):  # type: ignore[no-untyped-def]
        captured_request.update(kwargs)
        return iter(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content='{"schema_version":1}',
                                reasoning_content=None,
                            ),
                            finish_reason=None,
                        )
                    ]
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                reasoning_content=None,
                            ),
                            finish_reason="stop",
                        )
                    ]
                ),
            ]
        )

    integration = get_model_integration("qwen:qwen3.6-plus:chat")
    client = ChatCompletionsClient(
        runtime_config=LlmRuntimeConfig(
            integration_id=integration.integration_id,
            provider_id=integration.provider_id,
            model=integration.model,
            base_url="https://workspace.example/v1",
            api_key="test-key",
        ),
        integration=integration,
        request_adapter=_qwen_adapter(),
        output_policy=NoOpOutputPolicy(),
        role_name="cognitive",
        openai_client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create_completion)
            )
        ),
    )
    messages = [
        {"role": "system", "content": "只输出一个 JSON 对象"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "分析这张图"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        },
    ]

    chunks = list(
        client.stream_text(
            messages=messages,
            intent=cognitive_invocation_intent(
                _plan(kind="lookup_current_fact", reasoning="minimal")
            ),
            audit_source="activity",
            audit_operation="execute",
        )
    )

    assert [chunk.text for chunk in chunks if chunk.text] == [
        '{"schema_version":1}'
    ]
    assert captured_request["messages"] == messages
    assert captured_request["extra_body"] == {
        "enable_thinking": True,
    }
    assert "response_format" not in captured_request
    assert "reasoning" not in captured_request
    assert "reasoning_effort" not in captured_request
    assert "tools" not in captured_request
    assert "max_tokens" not in captured_request


def _plan(
    *,
    kind: CognitiveGoalKind,
    reasoning: CognitiveReasoning,
    web_search: bool = False,
    web_extract: bool = False,
) -> CognitiveExecutionPlan:
    """构造最小认知计划。

    Args:
        kind: 主认知工具类型。
        reasoning: 推理档位。
        web_search: 是否查询当前事实。
        web_extract: 是否抓取指定网页。

    Returns:
        可投影为调用意图的执行计划。
    """

    return CognitiveExecutionPlan(
        goals=(
            CognitiveGoal(
                tool_call_id="call-a",
                kind=kind,
                brief="完成测试目标",
            ),
        ),
        reasoning=reasoning,
        web_search=web_search,
        web_extract=web_extract,
        images=(),
        source_urls=("https://example.com",) if web_extract else (),
        progress_policy="reasoning_driven" if reasoning != "off" else "disabled",
    )


def _qwen_adapter() -> QwenChatRequestAdapter:
    """构造绑定 Plus 请求配置的 Qwen Chat Adapter。

    Returns:
        使用生产 Plus 参数映射的 Adapter。
    """

    profile = get_model_request_profile(QWEN_36_PLUS_CHAT_REQUEST_PROFILE_ID)
    assert isinstance(profile, QwenChatRequestProfile)
    return QwenChatRequestAdapter(profile)
