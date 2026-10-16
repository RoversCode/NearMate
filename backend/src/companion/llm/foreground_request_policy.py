"""集中定义不同 Foreground 请求可以读取和写入哪些上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from companion.llm.foreground_models import ForegroundRequestMode
from companion.llm.request_intent import ThinkingLevel

ForegroundHistoryView = Literal["full_protocol", "visible_dialogue", "none"]


@dataclass(frozen=True, slots=True)
class ForegroundRequestPolicy:
    """描述一种 Foreground 请求的上下文和副作用边界。

    Args:
        history_view: 本次请求允许读取的历史视图。
        records_user_message: 是否把当前输入记为真实用户消息。
        records_assistant_reply: 是否把最终回复写入会话历史。
        accepts_materials: 是否读取本轮正式小文本材料。
        includes_activity_state: 是否现场读取 Activity 并编译动态状态。
        allows_tools: 是否向模型注册原生工具。
        requires_notice_batch: 请求失去 Notice 批次时是否必须空结果收口。
        touches_session: 是否允许构造或更新当前请求的子进程历史视图。
        thinking_level: 本次请求沿用模型默认思考配置，或要求关闭/高档思考。
    """

    history_view: ForegroundHistoryView
    records_user_message: bool
    records_assistant_reply: bool
    accepts_materials: bool
    includes_activity_state: bool
    allows_tools: bool
    requires_notice_batch: bool
    touches_session: bool
    thinking_level: ThinkingLevel


_POLICIES: dict[ForegroundRequestMode, ForegroundRequestPolicy] = {
    "user_turn": ForegroundRequestPolicy(
        history_view="full_protocol",
        records_user_message=True,
        records_assistant_reply=True,
        accepts_materials=True,
        includes_activity_state=True,
        allows_tools=True,
        requires_notice_batch=False,
        touches_session=True,
        thinking_level="inherit",
    ),
    "system_notice": ForegroundRequestPolicy(
        history_view="visible_dialogue",
        records_user_message=False,
        records_assistant_reply=True,
        accepts_materials=False,
        includes_activity_state=True,
        allows_tools=False,
        requires_notice_batch=True,
        touches_session=True,
        thinking_level="inherit",
    ),
    "timed_reminder_notice": ForegroundRequestPolicy(
        history_view="none",
        records_user_message=False,
        records_assistant_reply=True,
        accepts_materials=False,
        includes_activity_state=False,
        allows_tools=False,
        requires_notice_batch=False,
        touches_session=True,
        thinking_level="inherit",
    ),
    "self_reflection_entry": ForegroundRequestPolicy(
        history_view="none",
        records_user_message=False,
        records_assistant_reply=True,
        accepts_materials=False,
        includes_activity_state=False,
        allows_tools=False,
        requires_notice_batch=False,
        touches_session=True,
        thinking_level="off",
    ),
    "self_reflection_recovery": ForegroundRequestPolicy(
        history_view="full_protocol",
        records_user_message=False,
        records_assistant_reply=True,
        accepts_materials=True,
        includes_activity_state=False,
        allows_tools=True,
        requires_notice_batch=False,
        touches_session=True,
        thinking_level="high",
    ),
    "self_reflection_interruption_reply": ForegroundRequestPolicy(
        history_view="none",
        records_user_message=False,
        records_assistant_reply=False,
        accepts_materials=False,
        includes_activity_state=False,
        allows_tools=False,
        requires_notice_batch=False,
        touches_session=False,
        thinking_level="off",
    ),
}


def foreground_request_policy(
    request_mode: ForegroundRequestMode,
) -> ForegroundRequestPolicy:
    """返回指定请求模式唯一的上下文策略。

    Args:
        request_mode: 用户轮、系统提醒或自我反思表达。

    Returns:
        不可变的请求策略。
    """

    return _POLICIES[request_mode]
