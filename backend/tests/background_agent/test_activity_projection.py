from __future__ import annotations

from uuid import uuid4

from companion.activities.notice.event_mapper import map_activity_event
from companion.activities.runtime.adapters import adapt_activity_source_event
from companion.activities.runtime.registry import ActivityRuntimeRegistry


def test_background_agent_interaction_moves_activity_into_and_out_of_waiting_user() -> None:
    activity_id = str(uuid4())
    payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "转换录音",
        "created_at": 1.0,
    }
    registry = ActivityRuntimeRegistry(runtime_id="runtime-a")

    started = adapt_activity_source_event("background_agent.started", payload, 1)
    waiting = adapt_activity_source_event(
        "background_agent.interaction_requested",
        {**payload, "kind": "permission", "request_id": "request-a"},
        1,
    )
    resumed = adapt_activity_source_event(
        "background_agent.interaction_resolved",
        {**payload, "request_id": "request-a", "outcome": "allowed"},
        1,
    )

    assert started is not None and registry.apply(started) is True
    assert waiting is not None and registry.apply(waiting) is True
    assert registry.desktop_snapshot().active[0].status == "waiting_user"
    assert resumed is not None and registry.apply(resumed) is True
    assert registry.desktop_snapshot().active[0].status == "running"


def test_background_agent_permission_request_maps_to_waiting_user_notice() -> None:
    notice = map_activity_event(
        "background_agent.interaction_requested",
        {
            "activity_id": str(uuid4()),
            "interaction_session_id": "session-a",
            "goal": "转换录音",
            "kind": "permission",
            "request_id": "request-a",
            "action_summary": "运行完成这项工作所需的命令",
            "grant_scope": "activity",
            "created_at": 1.0,
        },
    )

    assert notice is not None
    assert notice.lifecycle == "waiting_user"
    assert notice.source == "background_agent"
    assert notice.intent == "permission_request"
    assert notice.action_summary == "运行完成这项工作所需的命令"
    assert notice.grant_scope == "activity"


def test_background_agent_stays_waiting_until_all_interactions_are_resolved() -> None:
    """并行权限请求只在最后一张卡片收口后恢复运行态。"""

    activity_id = str(uuid4())
    payload = {
        "activity_id": activity_id,
        "interaction_session_id": "session-a",
        "goal": "批量处理文件",
        "created_at": 1.0,
    }
    registry = ActivityRuntimeRegistry(runtime_id="runtime-a")
    changes = [
        adapt_activity_source_event("background_agent.started", payload, 1),
        adapt_activity_source_event(
            "background_agent.interaction_requested",
            {**payload, "request_id": "request-a", "kind": "permission"},
            1,
        ),
        adapt_activity_source_event(
            "background_agent.interaction_requested",
            {**payload, "request_id": "request-b", "kind": "permission"},
            1,
        ),
        adapt_activity_source_event(
            "background_agent.interaction_resolved",
            {**payload, "request_id": "request-a", "outcome": "allowed"},
            1,
        ),
    ]
    for change in changes:
        assert change is not None
        assert registry.apply(change) is True

    assert registry.desktop_snapshot().active[0].status == "waiting_user"
    last_resolved = adapt_activity_source_event(
        "background_agent.interaction_resolved",
        {**payload, "request_id": "request-b", "outcome": "allowed"},
        1,
    )
    assert last_resolved is not None and registry.apply(last_resolved) is True
    assert registry.desktop_snapshot().active[0].status == "running"
