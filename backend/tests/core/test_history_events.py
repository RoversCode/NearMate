from __future__ import annotations

from companion.runtime_memory.models import TaskMemoryRecord
from companion.runtime_memory.task_memory import render_task_memory


def test_task_memory_is_self_contained_and_links_origin_tool() -> None:
    """任务短记忆必须自己说明来源、目标、终态和结果。"""

    content = render_task_memory(
        TaskMemoryRecord(
            activity_id="activity-001",
            origin_tool_call_ids=("call-001", "call-002"),
            source="cognitive",
            goal="核查最新英雄",
            terminal_state="completed",
            outcome_brief="已确认甲正式上线，乙仍在测试。",
            based_on_activity_id="activity-parent",
            based_on_relationship="activity_correction",
        )
    )

    assert content.startswith("<task_memory>")
    assert "<activity_id>activity-001</activity_id>" in content
    assert "<origin_tool_call_ids>call-001,call-002</origin_tool_call_ids>" in content
    assert "<source>cognitive</source>" in content
    assert "<goal>" in content
    assert "核查最新英雄" in content
    assert "<terminal_state>completed</terminal_state>" in content
    assert "已确认甲正式上线，乙仍在测试。" in content
    assert "<based_on_activity_id>activity-parent</based_on_activity_id>" in content
    assert (
        "<based_on_relationship>activity_correction</based_on_relationship>"
        in content
    )
    assert "本任务是对父任务的纠正替代" in content
    assert "detail_available" not in content
    assert "internal_history_event" not in content
    assert "result_carrier" not in content


def test_task_memory_without_parent_does_not_invent_lineage() -> None:
    """原始任务没有父任务时不能输出空父任务字段。"""

    content = render_task_memory(
        TaskMemoryRecord(
            activity_id="activity-002",
            origin_tool_call_ids=(),
            source="background_agent",
            goal="生成文件",
            terminal_state="failed",
            outcome_brief="输入文件缺失，任务没有完成。",
        )
    )

    assert "based_on_activity_id" not in content
    assert "based_on_relationship" not in content
    assert "based_on_meaning" not in content
    assert "cancellation_reason" not in content
    assert "cancellation_meaning" not in content
    assert "detail_available" not in content


def test_cancelled_task_memory_keeps_reason_separate_from_outcome() -> None:
    """取消原因说明为什么停止，结果概况仍只说明任务没有完成。"""

    content = render_task_memory(
        TaskMemoryRecord(
            activity_id="activity-cancelled",
            origin_tool_call_ids=("call-start",),
            source="cognitive",
            goal="核查原目标",
            terminal_state="cancelled",
            cancellation_reason="user_correction",
            outcome_brief="任务已取消，没有完成。",
        )
    )

    assert "<terminal_state>cancelled</terminal_state>" in content
    assert "<cancellation_reason>user_correction</cancellation_reason>" in content
    assert "用户纠正了这项任务的目标" in content
    assert "任务已取消，没有完成。" in content


def test_task_memory_projects_plain_language_meanings() -> None:
    """普通派生与系统侧停止也要给出不会误导的大白话投影。"""

    derived = render_task_memory(
        TaskMemoryRecord(
            activity_id="activity-derived",
            origin_tool_call_ids=(),
            source="background_agent",
            goal="重做报告",
            terminal_state="completed",
            outcome_brief="新版报告已经生成。",
            based_on_activity_id="activity-old",
        )
    )
    stopped = render_task_memory(
        TaskMemoryRecord(
            activity_id="activity-stopped",
            origin_tool_call_ids=(),
            source="background_agent",
            goal="整理文件",
            terminal_state="cancelled",
            cancellation_reason="agent_cancelled",
            outcome_brief="任务已取消，没有完成。",
        )
    )

    assert "父任务的结束不是本任务造成的" in derived
    assert "based_on_relationship" not in derived
    assert "不是用户主动要求" in stopped
