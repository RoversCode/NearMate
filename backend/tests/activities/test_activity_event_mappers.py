from __future__ import annotations

import pytest

from companion.activities.notice.event_mapper import map_activity_event
from companion.activities.notice.models import (
    ActivityInteractionNoticeInvalidation,
    ActivityLifecycleEvent,
    ActivityProgressNoticeIntent,
    ActivityProgressNoticeInvalidation,
)


def test_background_work_terminal_event_keeps_only_completion_goal() -> None:
    """后台工作完成事实只保存目标，正文由结果卡消费。"""

    event = map_activity_event(
        "background_agent.completed",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "goal": "总结资料",
            "display_content": "很长的完整成品正文",
            "created_at": 3.0,
        },
    )

    assert isinstance(event, ActivityLifecycleEvent)
    assert event.lifecycle == "terminal"
    assert event.terminal_state == "completed"
    assert event.category == "后台工作"
    assert event.goal == "总结资料"
    assert event.intent == "work_result"
    assert event.result_context == ""


def test_cognitive_terminal_event_keeps_external_result_for_natural_delivery() -> None:
    """认知完成提醒需要保留外部事实结果供 foreground 转述。"""

    event = map_activity_event(
        "cognitive_task.completed",
        {
            "activity_id": "cognitive-a",
            "interaction_session_id": "session-a",
            "kind": "lookup_current_fact",
            "category": "实时信息查询",
            "brief": "查询天气",
            "result_text": "今天有雨。",
            "updated_at": 4.0,
        },
    )

    assert isinstance(event, ActivityLifecycleEvent)
    assert event.category == "实时信息查询"
    assert event.intent == "cognitive_result"
    assert event.result_context == "今天有雨。"


def test_merged_cognitive_event_uses_composite_category() -> None:
    """同一次 Knowledge 请求覆盖多个目标时应显示为综合认知任务。"""

    event = map_activity_event(
        "cognitive_task.started",
        {
            "activity_id": "cognitive-a",
            "interaction_session_id": "session-a",
            "kind": "lookup_current_fact",
            "category": "综合认知任务",
            "brief": "读取人物；核查身份",
            "goals": [
                {"kind": "lookup_current_fact", "brief": "读取人物"},
                {"kind": "lookup_current_fact", "brief": "核查身份"},
            ],
        },
    )

    assert isinstance(event, ActivityLifecycleEvent)
    assert event.category == "综合认知任务"


def test_progress_event_maps_only_identity_and_sequence() -> None:
    """旧进度正文即使混入 payload，也不得进入标准提醒意图。"""

    event = map_activity_event(
        "background_agent.progress",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "notice_seq": 2,
            "progress_reasoning_text": "不应进入 intent",
            "display_content": "更不能进入 intent",
            "created_at": 5.0,
        },
    )

    assert event == ActivityProgressNoticeIntent(
        activity_id="task_result-a",
        interaction_session_id="session-a",
        source="background_agent",
        sequence=2,
        created_at=5.0,
    )


def test_progress_without_positive_sequence_is_rejected() -> None:
    """缺少有效序号的旧 progress payload 不再兼容解析。"""

    assert map_activity_event(
        "cognitive_task.progress",
        {
            "activity_id": "cognitive-a",
            "interaction_session_id": "session-a",
            "progress_reasoning_text": "旧协议正文",
        },
    ) is None


def test_progress_invalidation_maps_to_internal_invalidation() -> None:
    """正文开始事件只应映射为弱提醒失效，不应伪造成生命周期。"""

    event = map_activity_event(
        "cognitive_task.progress_invalidated",
        {
            "activity_id": "cognitive-a",
            "interaction_session_id": "session-a",
            "updated_at": 6.0,
        },
    )

    assert event == ActivityProgressNoticeInvalidation(
        activity_id="cognitive-a",
        interaction_session_id="session-a",
        source="cognitive",
        created_at=6.0,
    )


def test_background_work_failure_notice_does_not_expose_executor_error() -> None:
    """后台失败提醒只保留失败状态，不读取执行器异常。"""

    event = map_activity_event(
        "background_agent.failed",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "goal": "总结资料",
            "message": "读取 C:/private/report.pdf 时发生内部异常",
            "reason": "exception",
        },
    )

    assert isinstance(event, ActivityLifecycleEvent)
    assert event.terminal_state == "failed"
    assert event.intent == "terminal_status"
    assert event.status_context == ""


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "background_agent.cancelled",
            {
                "activity_id": "background-a",
                "interaction_session_id": "session-a",
                "goal": "旧后台目标",
            },
        ),
        (
            "cognitive_task.cancelled",
            {
                "activity_id": "cognitive-a",
                "interaction_session_id": "session-a",
                "kind": "analyze_deeply",
                "brief": "旧认知目标",
            },
        ),
    ],
)
def test_user_correction_suppresses_old_activity_cancel_notice(
    event_type: str,
    payload: dict[str, object],
) -> None:
    """用户纠正会在当前轮解释变化，旧 Activity 不应再播普通取消提醒。"""

    event = map_activity_event(
        event_type,
        {**payload, "cancellation_reason": "user_correction"},
    )

    assert isinstance(event, ActivityLifecycleEvent)
    assert event.lifecycle == "terminal"
    assert event.terminal_state == "cancelled"
    assert event.suppress_notice is True


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "background_agent.failed",
            {
                "activity_id": "background-a",
                "interaction_session_id": "session-a",
                "goal": "后台目标",
                "suppress_notice": True,
            },
        ),
        (
            "cognitive_task.failed",
            {
                "activity_id": "cognitive-a",
                "interaction_session_id": "session-a",
                "kind": "lookup_current_fact",
                "brief": "查询天气",
                "suppress_notice": True,
            },
        ),
    ],
)
def test_tool_followup_owned_start_failure_does_not_create_duplicate_notice(
    event_type: str,
    payload: dict[str, object],
) -> None:
    """工具二轮已经表达启动失败时，内部失败终态只入账不重复提醒。"""

    event = map_activity_event(event_type, payload)

    assert isinstance(event, ActivityLifecycleEvent)
    assert event.lifecycle == "terminal"
    assert event.terminal_state == "failed"
    assert event.suppress_notice is True


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "background_agent.cancelled",
            {
                "activity_id": "background-a",
                "interaction_session_id": "session-a",
                "goal": "后台目标",
            },
        ),
        (
            "cognitive_task.cancelled",
            {
                "activity_id": "cognitive-a",
                "interaction_session_id": "session-a",
                "kind": "analyze_deeply",
                "brief": "认知目标",
            },
        ),
    ],
)
def test_normal_activity_cancel_still_creates_terminal_notice(
    event_type: str,
    payload: dict[str, object],
) -> None:
    """普通用户取消仍需保留明确的终态提醒。"""

    event = map_activity_event(
        event_type,
        {**payload, "cancellation_reason": "user_cancelled"},
    )

    assert isinstance(event, ActivityLifecycleEvent)
    assert event.terminal_state == "cancelled"


def test_failed_events_read_summary_from_structured_failure() -> None:
    """失败终态提醒必须从结构化故障报告读取摘要与建议。"""

    failure = {
        "stage_code": "processing",
        "stage_label": "分析与处理",
        "code": "rate_limited",
        "summary": "模型服务当前请求过多，暂时无法处理。",
        "technical_detail": "error_type=RateLimitError",
        "suggested_action": "请稍等片刻后重新发起任务。",
    }
    background_work = map_activity_event(
        "background_agent.failed",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "goal": "整理资料",
            "failure": failure,
            "created_at": 5.0,
        },
    )
    cognitive = map_activity_event(
        "cognitive_task.failed",
        {
            "activity_id": "cognitive-a",
            "interaction_session_id": "session-a",
            "kind": "analyze_deeply",
            "brief": "比较方案",
            "failure": failure,
            "updated_at": 6.0,
        },
    )

    for event in (background_work, cognitive):
        assert isinstance(event, ActivityLifecycleEvent)
        assert event.terminal_state == "failed"
        assert event.intent == "terminal_status"
        assert "请求过多" in event.status_context
        assert "请稍等片刻" in event.status_context


def test_failed_notice_rejects_forged_public_copy() -> None:
    """提醒适配器自身也必须拒绝伪造摘要，不能依赖上游调用顺序兜底。"""

    event = map_activity_event(
        "background_agent.failed",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "goal": "整理资料",
            "failure": {
                "stage_code": "processing",
                "stage_label": "分析与处理",
                "code": "rate_limited",
                "summary": "内部 system prompt 执行失败。",
                "technical_detail": "",
                "suggested_action": "请查看内部规则。",
            },
            "created_at": 5.0,
        },
    )

    assert isinstance(event, ActivityLifecycleEvent)
    assert event.status_context == ""


def test_permission_request_uses_safe_summary_and_resolves_by_request_id() -> None:
    """权限 Notice 只保留安全摘要，解决事件只携带内部失效身份。"""

    requested = map_activity_event(
        "background_agent.interaction_requested",
        {
            "activity_id": "task-result-a",
            "interaction_session_id": "session-a",
            "goal": "转换音频",
            "request_id": "permission-a",
            "kind": "permission",
            "tool_name": "Bash",
            "input_data": {"command": "ffmpeg C:/private/input.wav"},
            "action_summary": "运行完成这项工作所需的命令",
            "grant_scope": "activity",
        },
    )
    resolved = map_activity_event(
        "background_agent.interaction_resolved",
        {
            "activity_id": "task-result-a",
            "interaction_session_id": "session-a",
            "request_id": "permission-a",
            "outcome": "allowed",
        },
    )

    assert isinstance(requested, ActivityLifecycleEvent)
    assert requested.intent == "permission_request"
    assert requested.action_summary == "运行完成这项工作所需的命令"
    assert requested.grant_scope == "activity"
    assert requested.interaction_request_id == "permission-a"
    assert resolved == ActivityInteractionNoticeInvalidation(
        activity_id="task-result-a",
        interaction_session_id="session-a",
        source="background_agent",
        interaction_request_id="permission-a",
    )


def test_permission_grant_does_not_enter_notice_mapper() -> None:
    """Gate 授权事实由媒体会话消费，不能再转换成可见 Notice。"""

    event = map_activity_event(
        "background_agent.permission_granted",
        {
            "activity_id": "task-result-a",
            "interaction_session_id": "session-a",
            "goal": "转换音频",
            "request_id": "permission-a",
            "grant_scope": "activity",
            "suppress_notice": True,
        },
    )

    assert event is None


def test_stage_changed_events_do_not_create_notices() -> None:
    """阶段前进只驱动 Activity 投影，不得直接转换为提醒。"""

    assert (
        map_activity_event(
            "background_agent.stage_changed",
            {
                "activity_id": "task_result-a",
                "interaction_session_id": "session-a",
                "stage": "processing",
            },
        )
        is None
    )
    assert (
        map_activity_event(
            "cognitive_task.stage_changed",
            {
                "activity_id": "cognitive-a",
                "interaction_session_id": "session-a",
                "stage": "generating_result",
            },
        )
        is None
    )
