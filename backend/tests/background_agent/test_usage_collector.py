from __future__ import annotations

from claude_agent_sdk import AssistantMessage, ResultMessage

from companion.background_agent.usage_collector import AgentTaskUsageCollector


def test_assistant_usage_overwrites_duplicate_message_projection() -> None:
    """同一模型和消息编号的重复投影只能计算最后一份用量。"""

    collector = AgentTaskUsageCollector()
    collector.record_assistant_message(
        _assistant_message(
            model="deepseek-v4-pro",
            message_id="message-a",
            input_tokens=10,
            output_tokens=2,
        )
    )
    collector.record_assistant_message(
        _assistant_message(
            model="deepseek-v4-pro",
            message_id="message-a",
            input_tokens=12,
            output_tokens=3,
        )
    )
    collector.record_assistant_message(
        _assistant_message(
            model="deepseek-v4-pro",
            message_id="message-b",
            input_tokens=4,
            output_tokens=1,
            cache_read_input_tokens=6,
        )
    )

    snapshot = collector.snapshot()

    assert snapshot.is_complete is False
    assert snapshot.reference_cost_usd is None
    assert len(snapshot.by_model) == 1
    usage = snapshot.by_model[0]
    assert usage.non_cached_input_tokens == 16
    assert usage.cache_read_input_tokens == 6
    assert usage.output_tokens == 4


def test_result_model_usage_replaces_process_totals_and_marks_complete() -> None:
    """SDK 终态按模型汇总是权威值，不能与过程消息再次相加。"""

    collector = AgentTaskUsageCollector()
    collector.record_assistant_message(
        _assistant_message(
            model="deepseek-v4-pro",
            message_id="message-a",
            input_tokens=999,
            output_tokens=999,
        )
    )
    collector.record_result_message(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-a",
            total_cost_usd=0.125,
            model_usage={
                "deepseek-v4-pro": {
                    "inputTokens": 20,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 30,
                    "cacheCreationInputTokens": 7,
                    "webSearchRequests": 0,
                    "costUSD": 0.125,
                    "contextWindow": 1000,
                    "maxOutputTokens": 100,
                },
                "claude-haiku-4-5": {
                    "inputTokens": 3,
                    "outputTokens": 2,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 1,
                    "webSearchRequests": 0,
                    "costUSD": 0.0,
                    "contextWindow": 1000,
                    "maxOutputTokens": 100,
                },
            },
        )
    )

    snapshot = collector.snapshot()

    assert snapshot.is_complete is True
    assert snapshot.reference_cost_usd == 0.125
    assert [item.model for item in snapshot.by_model] == [
        "claude-haiku-4-5",
        "deepseek-v4-pro",
    ]
    assert snapshot.by_model[1].non_cached_input_tokens == 20
    assert snapshot.by_model[1].output_tokens == 5


def _assistant_message(
    *,
    model: str,
    message_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
) -> AssistantMessage:
    """构造只含用量、不含正文的 SDK Assistant 消息。"""

    return AssistantMessage(
        content=[],
        model=model,
        message_id=message_id,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": 0,
        },
    )
