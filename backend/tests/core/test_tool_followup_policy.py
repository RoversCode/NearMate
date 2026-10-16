from __future__ import annotations

import pytest

from companion.foreground_tools.orchestration import (
    ExecutedToolCall,
    ToolExecutionChannel,
    ToolFollowupFact,
    ToolFollowupFactKind,
    ToolReplyRequirement,
    tool_outcome_from_payload,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.prompt.tool_followup_policy import (
    ToolFollowupMode,
    VisibleTransitionKind,
    select_tool_followup_plan,
)


@pytest.mark.parametrize(
    "transition_kind",
    [VisibleTransitionKind.WAITING_ACK, VisibleTransitionKind.MODEL_PREFIX],
)
def test_public_transition_suppresses_plain_start_followup(
    transition_kind: VisibleTransitionKind,
) -> None:
    """等待句或模型前缀已经公开后，普通启动成功不应再触发二轮。"""

    call = _executed_call(
        channel="background",
        state="accepted",
        reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
    )

    plan = select_tool_followup_plan(
        (call,),
        visible_transition_kind=transition_kind,
        attached_notices=(),
        tool_followup_facts=(
            ToolFollowupFact(
                kind="activity_started",
                activity_id="activity-a",
                goal="查询天气",
            ),
        ),
    )

    assert plan.mode is None


def test_plain_start_without_public_transition_uses_ack_only() -> None:
    """任务成功启动但首轮没有开口时，二轮只能补一次接单确认。"""

    plan = select_tool_followup_plan(
        (
            _executed_call(
                channel="background",
                state="accepted",
                reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
            ),
        ),
        visible_transition_kind=VisibleTransitionKind.NONE,
        attached_notices=(),
        tool_followup_facts=(
            ToolFollowupFact(
                kind="activity_started",
                activity_id="activity-a",
                goal="查询天气",
            ),
        ),
    )

    assert plan.mode == ToolFollowupMode.ACK_ONLY


def test_recent_material_rejection_survives_public_waiting_transition() -> None:
    """近期材料机械拒绝必须进入二轮，不能被首轮等待句当成成功确认压掉。"""

    call = ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="background-material-rejected",
            name="start_background_work",
            arguments={
                "goal": "整理刚才的录音",
                "material_paths": ["D:\\records\\meeting.m4a"],
            },
        ),
        outcome=tool_outcome_from_payload(
            {
                "state": "rejected",
                "safe_message": "刚才的“meeting.m4a”已经不在原位置了，请重新提供。",
                "material_failure": {
                    "display_name": "meeting.m4a",
                    "detail": "刚才的“meeting.m4a”已经不在原位置了，请重新提供。",
                },
            },
            channel="background",
        ),
        reply_requirement=ToolReplyRequirement.EXPLAIN,
        channel="background",
    )

    plan = select_tool_followup_plan(
        (call,),
        visible_transition_kind=VisibleTransitionKind.WAITING_ACK,
        attached_notices=(),
    )

    assert plan.mode == ToolFollowupMode.RESULT_ONLY


def test_public_transition_with_plain_start_and_notice_uses_notice_only() -> None:
    """首轮已接单时，强 Notice 必须进入二轮但不能重复成功启动事实。"""

    plan = select_tool_followup_plan(
        (
            _executed_call(
                channel="background",
                state="accepted",
                reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
            ),
        ),
        visible_transition_kind=VisibleTransitionKind.WAITING_ACK,
        attached_notices=({"intent": "work_result", "goal": "整理资料"},),
        tool_followup_facts=(
            ToolFollowupFact(
                kind="activity_started",
                activity_id="activity-a",
                goal="查询天气",
            ),
        ),
    )

    assert plan.mode == ToolFollowupMode.NOTICE_ONLY


@pytest.mark.parametrize(
    "fact_kind",
    [
        "activity_start_failed",
        "activity_replaced",
        "activity_replacement_failed",
    ],
)
def test_failure_and_correction_facts_survive_public_transition(
    fact_kind: ToolFollowupFactKind,
) -> None:
    """失败和纠正属于提交后事实，等待句不能把它们从二轮中抹掉。"""

    plan = select_tool_followup_plan(
        (
            _executed_call(
                channel="background",
                state="accepted",
                reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
            ),
        ),
        visible_transition_kind=VisibleTransitionKind.MODEL_PREFIX,
        attached_notices=({"intent": "work_result", "goal": "整理资料"},),
        tool_followup_facts=(
            ToolFollowupFact(
                kind=fact_kind,
                activity_id="activity-a",
            ),
        ),
    )

    assert plan.mode == ToolFollowupMode.RESULT_AND_NOTICE


def test_inline_result_and_notice_use_combined_mode() -> None:
    """Inline 真实结果与附着 Notice 必须在同一个二轮中完整表达。"""

    plan = select_tool_followup_plan(
        (
            _executed_call(
                channel="inline",
                state="completed",
                reply_requirement=ToolReplyRequirement.PRESENT_RESULT,
            ),
        ),
        visible_transition_kind=VisibleTransitionKind.WAITING_ACK,
        attached_notices=({"intent": "work_result", "goal": "整理资料"},),
    )

    assert plan.mode == ToolFollowupMode.RESULT_AND_NOTICE


def test_deferred_acceptance_still_requires_result_followup() -> None:
    """资料回答等延迟动作的真实接收结果不能被当成普通后台接单跳过。"""

    plan = select_tool_followup_plan(
        (
            _executed_call(
                channel="deferred",
                state="accepted",
                reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
            ),
        ),
        visible_transition_kind=VisibleTransitionKind.WAITING_ACK,
        attached_notices=(),
    )

    assert plan.mode == ToolFollowupMode.RESULT_ONLY


def test_plan_keeps_independent_attached_notice_snapshot() -> None:
    """二轮计划必须冻结 Notice，不能跟随后续环境字典一起变化。"""

    notice = {
        "intent": "information_request",
        "goal": "整理发布说明",
        "questions": [
            {
                "question": "请选择发布范围。",
                "options": [{"label": "正式环境"}],
                "multiSelect": False,
            }
        ],
    }
    plan = select_tool_followup_plan(
        (),
        visible_transition_kind=VisibleTransitionKind.NONE,
        attached_notices=(notice,),
    )

    notice["goal"] = "被后续代码修改的目标"
    questions = notice["questions"]
    assert isinstance(questions, list)
    questions[0]["options"][0]["label"] = "测试环境"

    assert plan.attached_notices[0]["goal"] == "整理发布说明"
    frozen_questions = plan.attached_notices[0]["questions"]
    assert isinstance(frozen_questions, list)
    assert frozen_questions[0]["options"][0]["label"] == "正式环境"


def _executed_call(
    *,
    channel: ToolExecutionChannel,
    state: str,
    reply_requirement: ToolReplyRequirement,
) -> ExecutedToolCall:
    """构造策略测试使用的最小工具结果。

    Args:
        channel: 工具执行通道。
        state: 工具执行状态。
        reply_requirement: 这项结果对 Foreground 的表达要求。

    Returns:
        带有稳定调用编号和结果的工具调用。
    """

    result: dict[str, object] = {"state": state}
    if channel == "background" and state in {"accepted", "merged"}:
        result["activity_id"] = "activity-a"
    if channel == "deferred" and state == "accepted":
        result["request_id"] = "request-a"
    return ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="call-a",
            name="test_tool",
            arguments={},
        ),
        outcome=tool_outcome_from_payload(result, channel=channel),
        reply_requirement=reply_requirement,
        channel=channel,
    )
