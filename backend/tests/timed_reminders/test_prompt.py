from __future__ import annotations

from companion.prompt.capabilities import (
    compile_foreground_capabilities,
    render_foreground_protocol,
)
from companion.prompt.system_state import render_dynamic_environment
from companion.prompt.timed_reminder_prompt import (
    render_timed_reminder_notice_environment,
)
from companion.prompt.tool_followup_policy import (
    ToolFollowupMode,
    select_tool_followup_plan,
)


def _current_list() -> dict[str, object]:
    """构造普通 Foreground 可读取的完整提醒清单。"""

    return {
        "timed_reminders": [
            {
                "reminder_id": "reminder-a",
                "content": "参加下午三点的面试",
                "state": "ringing",
                "trigger_at": "2026-09-03T15:00:00+08:00",
            }
        ],
        "timed_reminder_runtime_id": "runtime-a",
    }


def _notice_context(*, interrupted_user: bool, interrupted_assistant: bool) -> dict[str, object]:
    """构造独立 Reminder Notice 的最小冻结上下文。"""

    return {
        "timed_reminder_notice": {
            "reminders": [
                {
                    "content": "参加下午三点的面试",
                    "trigger_at": "2026-09-03T15:00:00+08:00",
                }
            ],
            "interrupted_user": interrupted_user,
            "interrupted_assistant": interrupted_assistant,
        }
    }


def test_user_turn_prompt_exposes_list_without_due_batch() -> None:
    """普通用户轮只能查看完整清单，不能承担到点播报。"""

    rendered = render_dynamic_environment(
        "2026年9月3日 星期四 下午15时00分00秒 UTC+08:00",
        _current_list(),
        capability_profile=compile_foreground_capabilities(()),
    )

    assert "<timed_reminders>" in rendered
    assert "参加下午三点的面试" in rendered
    assert "用户查看提醒时直接根据这里回答" in rendered
    assert "timed_reminder_batch" not in rendered
    assert "timed_reminder_notice_environment" not in rendered


def test_foreground_protocol_explains_how_to_dismiss_ringing_reminders() -> None:
    """稳定能力协议必须明确自然语言关闭和无指代批次关闭规则。"""

    rendered = render_foreground_protocol(
        compile_foreground_capabilities(("manage_timed_reminder",))
    )

    assert "说‘知道了’‘别再提醒’或表达同义的关闭意图" in rendered
    assert "使用 dismiss" in rendered
    assert "全部 can_dismiss 为 true 的编号" in rendered
    assert "响铃中的提醒只能关闭，不能再修改或取消" in rendered
    assert "批次关闭没有实际交集时" in rendered


def test_notice_prompt_requires_takeover_phrase_only_after_real_interruption() -> None:
    """只有真实发生抢占时，独立提醒才允许声称自己打断了当前交流。"""

    interrupted = render_timed_reminder_notice_environment(
        "2026年9月3日 星期四 下午15时00分00秒 UTC+08:00",
        _notice_context(interrupted_user=True, interrupted_assistant=False),
    )
    uninterrupted = render_timed_reminder_notice_environment(
        "2026年9月3日 星期四 下午15时00分00秒 UTC+08:00",
        _notice_context(interrupted_user=False, interrupted_assistant=False),
    )

    assert "开头先自然说“等等”或“我打断一下”" in interrupted
    assert "禁止声称自己刚才打断" in uninterrupted
    assert "不要回应任何旧用户消息" in interrupted
    assert "不要调用工具" in interrupted
    assert "reminder_id" not in interrupted


def test_tool_followup_does_not_repeat_ringing_reminder() -> None:
    """工具二轮没有工具结果或 Activity Notice 时不因响铃任务额外生成回复。"""

    plan = select_tool_followup_plan(
        (),
        visible_transition_kind="none",  # type: ignore[arg-type]
    )

    assert plan.mode is None
    assert plan.attached_notice_count == 0
    assert ToolFollowupMode.NOTICE_ONLY != plan.mode
