from __future__ import annotations

from datetime import datetime
import json

import pytest

from companion.llm.tool_protocol import (
    project_tool_calls_for_runtime_memory,
    validate_tool_protocol_messages,
)
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.session.context import SessionContext


def test_visible_dialogue_messages_excludes_current_message_and_limits_filtered_tail() -> None:
    """可见对话应先去掉内部协议和当前消息，再截取最近的真实对话。"""

    context = SessionContext(token_counter=len)
    context.record_user_message(
        "第一条用户消息",
        created_at=datetime(2026, 8, 10, 9, 0),
        llm_context_note="只给模型看的附注",
        metadata={"internal": True},
    )
    second_message = context.record_assistant_message(
        "第二条助手回复",
        created_at=datetime(2026, 8, 10, 9, 1),
        metadata={"internal": True},
    )
    assistant_message, tool_messages = _tool_exchange()
    tool_turn = context.record_tool_turn(
        assistant_message,
        tool_messages,
        "第三条助手回复",
        protocol_transaction_id="tool-visible",
        created_at=datetime(2026, 8, 10, 9, 2),
    )
    current_message = context.record_user_message(
        "本轮当前用户消息",
        created_at=datetime(2026, 8, 10, 9, 3),
    )

    visible_messages = context.visible_dialogue_messages(
        exclude_message_id=current_message.message_id,
        limit=2,
    )

    assert second_message is not None
    assert [
        (message.role, message.content, message.message_id)
        for message in visible_messages
    ] == [
        ("assistant", "第二条助手回复", second_message.message_id),
        ("assistant", "第三条助手回复", tool_turn[-1].message_id),
    ]
    assert all(message.tool_calls == [] for message in visible_messages)
    assert all(message.tool_call_id is None for message in visible_messages)
    assert all(message.llm_context_note == "" for message in visible_messages)
    assert all(message.metadata == {} for message in visible_messages)
    assert context.visible_dialogue_messages(limit=0) == []


def test_visible_dialogue_messages_rejects_negative_limit() -> None:
    """负数上限没有明确业务含义，调用方必须收到清晰错误。"""

    context = SessionContext(token_counter=len)

    with pytest.raises(ValueError, match="limit 不能为负数"):
        context.visible_dialogue_messages(limit=-1)


def test_visible_dialogue_messages_keeps_only_the_latest_eight_messages() -> None:
    """工具二轮的固定窗口应裁掉第九条及更早的可见对话。"""

    context = SessionContext(token_counter=len)
    for index in range(10):
        context.record_user_message(
            f"可见消息 {index}",
            created_at=datetime(2026, 8, 11, 10, index),
        )

    visible_messages = context.visible_dialogue_messages(limit=8)

    assert [message.content for message in visible_messages] == [
        f"可见消息 {index}" for index in range(2, 10)
    ]


def test_visible_dialogue_messages_respects_strict_before_boundary() -> None:
    """冻结当前用户轮的历史时，后来追加的消息不能混入此前对话。"""

    context = SessionContext(token_counter=len)
    context.record_user_message("此前的用户消息")
    current_message = context.record_user_message("当前用户消息")
    context.record_assistant_message("并发追加的助手消息")

    visible_messages = context.visible_dialogue_messages(
        before_message_id=current_message.message_id,
        limit=8,
    )

    assert [message.content for message in visible_messages] == ["此前的用户消息"]


def test_record_tool_turn_requires_nonempty_final_and_writes_one_transaction_id() -> None:
    """完整工具事务必须非空收尾，且所有消息共享一个事务编号。"""

    context = SessionContext(token_counter=len)
    assistant_message, tool_messages = _tool_exchange()

    with pytest.raises(ValueError, match="非空 final"):
        context.record_tool_turn(
            assistant_message,
            tool_messages,
            "",
            protocol_transaction_id="tool-empty-final",
        )
    assert context.recent_messages() == []

    recorded = context.record_tool_turn(
        assistant_message,
        tool_messages,
        "天气和空气质量都查到了。",
        protocol_transaction_id="tool-stable-transaction",
    )

    transaction_ids = {
        message.metadata.get("protocol_transaction_id")
        for message in recorded
    }
    assert len(recorded) == 4
    assert len(transaction_ids) == 1
    assert transaction_ids == {"tool-stable-transaction"}
    context.validate_prompt_history(context.recent_messages())


def test_background_material_paths_are_removed_before_session_history_commit() -> None:
    """执行期材料路径不得进入会话历史，但工具协议和安全参数必须保留。"""

    raw_tool_calls = [
        {
            "id": "background-a",
            "type": "function",
            "function": {
                "name": "start_background_work",
                "arguments": json.dumps(
                    {
                        "goal": "继续修改应用",
                        "deliverable": "完成日志功能",
                        "material_paths": ["D:\\project\\app.py"],
                    },
                    ensure_ascii=False,
                ),
            },
        },
        {
            "id": "weather-a",
            "type": "function",
            "function": {
                "name": "weather_overview",
                "arguments": '{"location":"D盘园区"}',
            },
        },
    ]

    projected = project_tool_calls_for_runtime_memory(raw_tool_calls)

    assert json.loads(projected[0]["function"]["arguments"]) == {
        "goal": "继续修改应用",
        "deliverable": "完成日志功能",
    }
    assert projected[1] == raw_tool_calls[1]
    assert "material_paths" in raw_tool_calls[0]["function"]["arguments"]
    assert project_tool_calls_for_runtime_memory(
        [{
            "id": "invalid-background",
            "type": "function",
            "function": {
                "name": "start_background_work",
                "arguments": "D:\\project\\app.py",
            },
        }]
    )[0]["function"]["arguments"] == "{}"

    context = SessionContext(token_counter=len)
    recorded = context.record_tool_turn(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [raw_tool_calls[0]],
        },
        [{
            "role": "tool",
            "content": '{"state":"accepted"}',
            "tool_call_id": "background-a",
        }],
        "任务已经开始。",
        protocol_transaction_id="tool-safe-history",
    )
    commit = context.build_memory_commit(request_id="request-safe-history")

    assert recorded[0].tool_calls[0]["id"] == "background-a"
    assert json.loads(recorded[0].tool_calls[0]["function"]["arguments"]) == {
        "goal": "继续修改应用",
        "deliverable": "完成日志功能",
    }
    assert recorded[1].tool_call_id == "background-a"
    assert sum(message.token_count for message in recorded) == context.context_token_count
    assert "material_paths" not in str(commit.messages)
    assert "D:\\project\\app.py" not in str(commit.messages)


@pytest.mark.parametrize("failure_call", [1, 2, 3, 4])
def test_record_tool_turn_failure_never_leaves_partial_protocol(
    failure_call: int,
) -> None:
    """工具事务任意消息准备失败时，会话账本必须完全保持原样。"""

    context = SessionContext(token_counter=len)
    existing = context.record_user_message("原有消息")
    original_token_count = context.context_token_count
    original_next_message_id = context._next_message_id  # noqa: SLF001
    counter_calls = 0

    def flaky_counter(text: str) -> int:
        """在指定消息的 token 计算处注入失败。

        Args:
            text: 当前待计算的协议消息文本。

        Returns:
            未达到故障点时返回文本长度。
        """

        nonlocal counter_calls
        counter_calls += 1
        if counter_calls == failure_call:
            raise RuntimeError("token counter failed")
        return len(text)

    context.token_counter = flaky_counter
    assistant_message, tool_messages = _tool_exchange()

    with pytest.raises(RuntimeError, match="token counter failed"):
        context.record_tool_turn(
            assistant_message,
            tool_messages,
            "天气和空气质量都查到了。",
            protocol_transaction_id="tool-failure-test",
        )

    assert context.recent_messages() == [existing]
    assert context.context_token_count == original_token_count
    assert context._next_message_id == original_next_message_id  # noqa: SLF001


@pytest.mark.parametrize("keep_recent_messages", [1, 2, 3, 4, 5])
def test_compaction_never_splits_tool_transaction(keep_recent_messages: int) -> None:
    """固定切点落在事务任意位置时都不得拆开工具协议。"""

    context = SessionContext(interaction_session_id="session-a", token_counter=len)
    context.record_user_message("旧消息")
    assistant_message, tool_messages = _tool_exchange()
    context.record_tool_turn(
        assistant_message,
        tool_messages,
        "天气和空气质量都查到了。",
        protocol_transaction_id="tool-compaction-test",
    )
    context.record_user_message("后续消息")

    store = RuntimeMemoryStore()
    store.apply_memory_commit(context.build_memory_commit(request_id="request-a"))
    plan = store.create_compaction_plan(
        owner_runtime_epoch=1,
        max_context_tokens=1,
        keep_recent_messages=keep_recent_messages,
    )

    assert plan is not None
    summarized_transaction_ids = {
        message.metadata.get("protocol_transaction_id")
        for message in plan.messages_to_summarize
        if message.metadata.get("protocol_transaction_id")
    }
    kept_transaction_ids = {
        message.metadata.get("protocol_transaction_id")
        for message in store.snapshot().recent_messages
        if message.message_id > plan.source_message_id
        if message.metadata.get("protocol_transaction_id")
    }
    assert summarized_transaction_ids.isdisjoint(kept_transaction_ids)


def test_protocol_validator_rejects_orphan_and_out_of_order_tool_results() -> None:
    """共享校验器必须拒绝孤立 tool 和乱序多工具结果。"""

    assistant_message, tool_messages = _tool_exchange()
    with pytest.raises(ValueError, match="孤立 tool"):
        validate_tool_protocol_messages([tool_messages[0]])
    with pytest.raises(ValueError, match="顺序或编号错误"):
        validate_tool_protocol_messages([
            assistant_message,
            tool_messages[1],
            tool_messages[0],
        ])


def _tool_exchange() -> tuple[dict[str, object], list[dict[str, object]]]:
    """构造两个 Inline 调用组成的稳定工具交换。"""

    assistant_message: dict[str, object] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "weather-a",
                "type": "function",
                "function": {"name": "weather_overview", "arguments": "{}"},
            },
            {
                "id": "air-a",
                "type": "function",
                "function": {"name": "weather_air_quality", "arguments": "{}"},
            },
        ],
    }
    tool_messages: list[dict[str, object]] = [
        {"role": "tool", "content": "晴", "tool_call_id": "weather-a"},
        {"role": "tool", "content": "优", "tool_call_id": "air-a"},
    ]
    return assistant_message, tool_messages
