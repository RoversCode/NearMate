from __future__ import annotations

import asyncio

import pytest

from companion.background_agent.interaction_bridge import (
    AgentInteractionAnswer,
    AgentInteractionRequest,
    InteractionBridge,
)
from companion.background_agent.permission_gate import ActivityPermissionGate


@pytest.mark.asyncio
async def test_permission_gate_merges_concurrent_requests_and_reuses_grant() -> None:
    """同一 Activity 的并发和后续权限回调只能产生一张授权卡。"""

    requests: list[AgentInteractionRequest] = []
    applied_outcomes: list[tuple[str, str, bool]] = []
    bridge = InteractionBridge(
        activity_id="activity-a",
        request_notifier=requests.append,
    )
    gate = ActivityPermissionGate(
        interaction_bridge=bridge,
        resolution_notifier=lambda request, outcome, suppress_notice: applied_outcomes.append(
            (request.request_id, outcome, suppress_notice)
        ),
    )

    first = asyncio.create_task(
        gate.request_permission(
            tool_name="Bash",
            input_data={"command": "first"},
            action_summary="运行命令",
        )
    )
    second = asyncio.create_task(
        gate.request_permission(
            tool_name="WebFetch",
            input_data={"url": "https://example.com"},
            action_summary="访问网络",
        )
    )
    for _ in range(10):
        if requests:
            break
        await asyncio.sleep(0)

    assert len(requests) == 1
    assert requests[0].grant_scope == "activity"
    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="allow",
            answers={},
        )
    ) is True

    first_answer, second_answer = await asyncio.gather(first, second)
    later_answer = await gate.request_permission(
        tool_name="Bash",
        input_data={"command": "unknown-command"},
        action_summary="运行命令",
    )

    assert first_answer.outcome == "allow"
    assert second_answer.outcome == "allow"
    assert later_answer.outcome == "allow"
    assert gate.state == "granted"
    assert len(requests) == 1
    assert applied_outcomes == [(requests[0].request_id, "allowed", False)]


@pytest.mark.asyncio
async def test_permission_gate_marks_natural_language_grant_as_already_confirmed() -> None:
    """自然语言授权已有工具二轮确认，Gate 事实应携带抑制重复提醒的标记。"""

    requests: list[AgentInteractionRequest] = []
    applied_outcomes: list[tuple[str, str, bool]] = []
    bridge = InteractionBridge(
        activity_id="activity-a",
        request_notifier=requests.append,
    )
    gate = ActivityPermissionGate(
        interaction_bridge=bridge,
        resolution_notifier=lambda request, outcome, suppress_notice: applied_outcomes.append(
            (request.request_id, outcome, suppress_notice)
        ),
    )
    pending = asyncio.create_task(
        gate.request_permission(
            tool_name="Bash",
            input_data={"command": "first"},
            action_summary="运行命令",
        )
    )
    for _ in range(10):
        if requests:
            break
        await asyncio.sleep(0)

    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="allow",
            answers={},
            submission_id="submission-a",
        )
    ) is True
    await pending

    assert applied_outcomes == [(requests[0].request_id, "allowed", True)]


@pytest.mark.asyncio
async def test_permission_gate_freezes_denial_before_sdk_callback_resumes() -> None:
    """拒绝必须先冻结 Activity 取消事实，再让 SDK 权限回调继续。"""

    requests: list[AgentInteractionRequest] = []
    timeline: list[str] = []
    bridge = InteractionBridge(
        activity_id="activity-a",
        request_notifier=requests.append,
    )
    gate = ActivityPermissionGate(
        interaction_bridge=bridge,
        resolution_notifier=lambda _request, _outcome, _suppress_notice: timeline.append(
            "resolved"
        ),
        denial_notifier=lambda: timeline.append("cancel_frozen"),
    )
    pending = asyncio.create_task(
        gate.request_permission(
            tool_name="Bash",
            input_data={"command": "first"},
            action_summary="运行命令",
        )
    )
    for _ in range(10):
        if requests:
            break
        await asyncio.sleep(0)

    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="deny",
            answers={},
        )
    ) is True
    answer = await pending
    timeline.append("sdk_resumed")

    assert answer.outcome == "cancel"
    assert answer.reason == "permission_denied"
    assert gate.state == "denied"
    assert timeline == ["cancel_frozen", "resolved", "sdk_resumed"]
