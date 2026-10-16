from __future__ import annotations

from datetime import date, datetime

from companion.runtime_memory.models import RuntimeMemoryMessage, TaskMemoryRecord
from companion.runtime_memory.summary_prompt import (
    build_runtime_memory_summary_prompt,
    format_runtime_messages_for_summary,
)
from companion.runtime_memory.task_memory import render_task_memory


def test_summary_prompt_reads_self_contained_task_memory() -> None:
    """摘要输入应直接读取任务记忆，不依赖随后 assistant 补充事实。"""

    task_memory = render_task_memory(
        TaskMemoryRecord(
            activity_id="activity-001",
            origin_tool_call_ids=("call-001",),
            source="cognitive",
            goal="核查天气",
            terminal_state="completed",
            outcome_brief="已确认当天有雨。",
        )
    )
    created_at = datetime(2026, 7, 18, 9, 0)
    messages = (
            RuntimeMemoryMessage(
                message_id=1,
                role="system",
                content=task_memory,
                token_count=1,
                created_at=created_at,
            ),
            RuntimeMemoryMessage(
                message_id=2,
                role="assistant",
                content="外部核查结果显示今天有雨。",
                token_count=1,
                created_at=created_at,
            ),
        )
    lines = format_runtime_messages_for_summary(messages)
    prompt = build_runtime_memory_summary_prompt(
        messages,
        covered_from=date(2026, 7, 18),
        covered_to=date(2026, 7, 18),
    )

    assert lines[1].startswith("<task_memory>")
    assert "<activity_id>activity-001</activity_id>" in lines[1]
    assert "detail_available" not in lines[1]
    assert lines[2] == "assistant: 外部核查结果显示今天有雨。"
    assert "<task_memory> 是已经结束的任务事实" in prompt
    assert "自行决定摘要结构" in prompt
    assert "activity_id" in prompt
    assert "不得复制到摘要" in prompt
    assert "【已经结束的任务与结果】" not in prompt
    assert "本次摘要覆盖范围：2026年7月18日 至 2026年7月18日" in prompt


def test_summary_input_keeps_previous_summary_and_complete_tool_exchange() -> None:
    """旧摘要、工具前缀、调用参数和结果必须全部进入本轮 Summary。"""

    created_at = datetime(2026, 8, 4, 10, 30)
    messages = (
        RuntimeMemoryMessage(
            message_id=1,
            role="assistant",
            content="我先核对一下。",
            token_count=1,
            created_at=created_at,
            tool_calls=({
                "id": "call-weather",
                "type": "function",
                "function": {
                    "name": "lookup_current_fact",
                    "arguments": '{"brief":"核查天气"}',
                },
            },),
        ),
        RuntimeMemoryMessage(
            message_id=2,
            role="tool",
            content='{"state":"accepted","activity_id":"activity-weather"}',
            token_count=1,
            created_at=created_at,
            tool_call_id="call-weather",
        ),
    )

    prompt = build_runtime_memory_summary_prompt(
        messages,
        existing_summary="user 此前要求优先说明天气风险。",
        covered_from=date(2026, 8, 3),
        covered_to=date(2026, 8, 4),
    )

    assert "【已有滚动摘要】\nuser 此前要求优先说明天气风险。" in prompt
    assert "assistant: 我先核对一下。" in prompt
    assert "tool_call_id=call-weather" in prompt
    assert '参数={"brief":"核查天气"}' in prompt
    assert '结果={"state":"accepted","activity_id":"activity-weather"}' in prompt


def test_summary_defensively_removes_background_material_paths() -> None:
    """即使旧消息仍带执行期路径，Summary 输入也只能看到非路径参数。"""

    created_at = datetime(2026, 8, 4, 10, 30)
    messages = (
        RuntimeMemoryMessage(
            message_id=1,
            role="assistant",
            content="",
            token_count=1,
            created_at=created_at,
            tool_calls=({
                "id": "background-a",
                "type": "function",
                "function": {
                    "name": "start_background_work",
                    "arguments": (
                        '{"goal":"修改应用","material_paths":'
                        '["D:\\\\project\\\\app.py"]}'
                    ),
                },
            },),
        ),
        RuntimeMemoryMessage(
            message_id=2,
            role="tool",
            content='{"state":"accepted"}',
            token_count=1,
            created_at=created_at,
            tool_call_id="background-a",
        ),
    )

    lines = format_runtime_messages_for_summary(messages)
    summary_input = "\n".join(lines)

    assert "tool_call_id=background-a" in summary_input
    assert "start_background_work" in summary_input
    assert '参数={"goal":"修改应用"}' in summary_input
    assert "material_paths" not in summary_input
    assert "D:\\project\\app.py" not in summary_input


def test_summary_input_uses_task_summary_instead_of_full_result() -> None:
    """任务记忆只向滚动摘要提供短结果，不重新注入完整正文。"""

    task_memory = render_task_memory(
        TaskMemoryRecord(
            activity_id="activity-002",
            origin_tool_call_ids=("call-002",),
            source="cognitive",
            goal="核查版本",
            terminal_state="failed",
            outcome_brief="外部服务不可用，未能完成核查。",
            full_result="不应进入短记忆的完整底稿",
        )
    )
    created_at = datetime(2026, 8, 4, 9, 0)
    lines = format_runtime_messages_for_summary(
        (
            RuntimeMemoryMessage(
                message_id=1,
                role="system",
                content=task_memory,
                token_count=1,
                created_at=created_at,
            ),
            RuntimeMemoryMessage(
                message_id=2,
                role="assistant",
                content="这次查询没有成功。",
                token_count=1,
                created_at=created_at,
            ),
        )
    )

    rendered = "\n".join(lines)
    assert "不应进入短记忆的完整底稿" not in rendered
    assert "外部服务不可用，未能完成核查" in rendered
    assert "这次查询没有成功" in rendered
