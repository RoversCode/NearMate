"""根据已提交工具事实决定 Foreground 工具二轮的表达范围。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from companion.foreground_tools.orchestration.contracts import (
    ExecutedToolCall,
    ToolFollowupFact,
    ToolReplyRequirement,
)


class ToolFollowupMode(StrEnum):
    """定义工具二轮最终正文需要承担的表达职责。"""

    ACK_ONLY = "ack_only"
    RESULT_ONLY = "result_only"
    NOTICE_ONLY = "notice_only"
    RESULT_AND_NOTICE = "result_and_notice"


class VisibleTransitionKind(StrEnum):
    """记录首轮已经公开的结构化过渡话术类型。"""

    NONE = "none"
    WAITING_ACK = "waiting_ack"
    MODEL_PREFIX = "model_prefix"


@dataclass(frozen=True, slots=True)
class ToolFollowupPlan:
    """保存二轮是否必要以及二轮需要表达的事实范围。

    Args:
        mode: 二轮表达模式；为空表示不需要工具二轮。
        visible_transition_kind: 首轮已经公开的等待句或模型前缀类型。
        tool_followup_facts: 提交后产生的最终后台工具事实。
        attached_notices: 本轮必须汇报的强 Notice 冻结快照。
    """

    mode: ToolFollowupMode | None
    visible_transition_kind: VisibleTransitionKind
    tool_followup_facts: tuple[ToolFollowupFact, ...] = ()
    attached_notices: tuple[dict[str, object], ...] = ()

    @property
    def required(self) -> bool:
        """判断是否需要调用二轮 Foreground LLM。

        Returns:
            二轮仍有事实需要表达时返回真。
        """

        return self.mode is not None

    @property
    def attached_notice_count(self) -> int:
        """返回冻结快照中的 Notice 数量，便于日志和断言读取。

        Returns:
            本轮二轮必须表达的强 Notice 数量。
        """

        return len(self.attached_notices)


def select_tool_followup_plan(
    executed_calls: Sequence[ExecutedToolCall],
    *,
    visible_transition_kind: VisibleTransitionKind,
    attached_notices: Sequence[dict[str, object]] = (),
    tool_followup_facts: Sequence[ToolFollowupFact] = (),
) -> ToolFollowupPlan:
    """根据结构化过渡、最终工具事实和强 Notice 选择二轮模式。

    Args:
        executed_calls: 当前工具事务按模型调用顺序保存的结果。
        visible_transition_kind: 首轮已经公开的等待句或模型前缀。
        attached_notices: 当前用户轮附着的强 Notice 快照。
        tool_followup_facts: 提交后得到的最终后台工具事实。

    Returns:
        不需要二轮时 mode 为空，否则返回唯一的表达模式。
    """

    visible_calls = tuple(
        call
        for call in executed_calls
        if call.reply_requirement != ToolReplyRequirement.SILENT
    )
    facts = tuple(tool_followup_facts)
    frozen_notices = tuple(deepcopy(dict(notice)) for notice in attached_notices)
    has_notice = bool(frozen_notices)
    transition_was_public = visible_transition_kind != VisibleTransitionKind.NONE

    # 普通异步任务的“已开始”可以由等待句覆盖；失败和纠正属于提交后的新事实，
    # 即使首轮说过“我去看看”也必须在二轮明确交代。
    is_plain_background_acknowledgement_batch = (
        bool(visible_calls)
        and all(call.channel == "background" for call in visible_calls)
        and all(
            call.reply_requirement == ToolReplyRequirement.ACKNOWLEDGE
            for call in visible_calls
        )
    )
    only_successful_start_facts = (
        is_plain_background_acknowledgement_batch
        and bool(facts)
        and all(fact.kind == "activity_started" for fact in facts)
    )
    has_tool_result = any(
        call.reply_requirement
        in {ToolReplyRequirement.PRESENT_RESULT, ToolReplyRequirement.EXPLAIN}
        or (
            call.channel == "deferred"
            and call.reply_requirement == ToolReplyRequirement.ACKNOWLEDGE
        )
        for call in visible_calls
    ) or any(fact.kind != "activity_started" for fact in facts)

    if has_notice:
        if only_successful_start_facts and transition_was_public:
            mode = ToolFollowupMode.NOTICE_ONLY
        elif visible_calls or facts:
            mode = ToolFollowupMode.RESULT_AND_NOTICE
        else:
            mode = ToolFollowupMode.NOTICE_ONLY
    elif only_successful_start_facts:
        mode = None if transition_was_public else ToolFollowupMode.ACK_ONLY
    elif has_tool_result:
        mode = ToolFollowupMode.RESULT_ONLY
    elif is_plain_background_acknowledgement_batch and not transition_was_public:
        # after_commit 事实缺失时仍需要二轮 fail closed；调用方会把缺失事实补成启动失败。
        mode = ToolFollowupMode.ACK_ONLY
    else:
        mode = None

    return ToolFollowupPlan(
        mode=mode,
        visible_transition_kind=visible_transition_kind,
        tool_followup_facts=facts,
        attached_notices=frozen_notices,
    )
