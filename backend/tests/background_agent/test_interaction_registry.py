from __future__ import annotations

import pytest

from companion.background_agent.interaction_registry import (
    AgentInteractionEventApplyResult,
    BackgroundAgentInteractionRegistry,
    InteractionRequestConflictError,
    InteractionRequestNotFoundError,
)


def test_registry_claims_once_and_waits_for_runtime_resolution() -> None:
    """HTTP claim 只占回答权，子进程 resolved 到达后才删除请求。"""

    registry = BackgroundAgentInteractionRegistry()
    assert _register(registry, runtime_epoch=4)

    claimed, normalized_answers = registry.claim_answer(
        request_id="request-a",
        kind="permission",
        outcome="allow",
        answers={},
        current_runtime_epoch=4,
    )

    assert claimed.state == "answering"
    assert normalized_answers == {}
    with pytest.raises(InteractionRequestConflictError):
        registry.claim_answer(
            request_id="request-a",
            kind="permission",
            outcome="allow",
            answers={},
            current_runtime_epoch=4,
        )
    assert registry.apply_runtime_event(
        "background_agent.interaction_resolved",
        {
            "request_id": "request-a",
            "activity_id": "activity-a",
            "outcome": "allowed",
        },
        4,
    )
    assert registry.pending("request-a") is None


def test_registry_releases_claim_when_command_submission_fails() -> None:
    """控制队列失败后同一张卡片仍可重新提交回答。"""

    registry = BackgroundAgentInteractionRegistry()
    assert _register(registry, runtime_epoch=2)
    claimed, _normalized_answers = registry.claim_answer(
        request_id="request-a",
        kind="permission",
        outcome="allow",
        answers={},
        current_runtime_epoch=2,
    )

    assert registry.release_answer_claim(claimed)
    assert registry.pending("request-a").state == "pending"  # type: ignore[union-attr]


def test_registry_rejects_kind_epoch_and_unknown_request() -> None:
    """请求类型、当前世代和 request id 任一不匹配都不能 claim。"""

    registry = BackgroundAgentInteractionRegistry()
    assert _register(registry, runtime_epoch=7)

    with pytest.raises(InteractionRequestConflictError):
        registry.claim_answer(
            request_id="request-a",
            kind="question",
            outcome="answer",
            answers={"问题": "答案"},
            current_runtime_epoch=7,
        )
    with pytest.raises(InteractionRequestConflictError):
        registry.claim_answer(
            request_id="request-a",
            kind="permission",
            outcome="allow",
            answers={},
            current_runtime_epoch=8,
        )
    with pytest.raises(InteractionRequestNotFoundError):
        registry.claim_answer(
            request_id="missing",
            kind="permission",
            outcome="allow",
            answers={},
            current_runtime_epoch=7,
        )


def test_registry_invalidates_old_epoch_and_closes_cards() -> None:
    """热替换会删除旧世代请求并生成 cancelled 卡片收口事件。"""

    registry = BackgroundAgentInteractionRegistry()
    assert _register(registry, runtime_epoch=5)

    events = registry.invalidate_runtime_epoch(5)

    assert events == [
        {
            "type": "background_agent.interaction_resolved",
            "payload": {
                "request_id": "request-a",
                "activity_id": "activity-a",
                "outcome": "cancelled",
            },
        }
    ]
    assert registry.pending("request-a") is None


def test_registry_projects_pending_permissions_and_questions_for_current_session() -> None:
    """Foreground 同时看到本会话权限与资料请求，但看不到其它会话或已 claim 请求。"""

    registry = BackgroundAgentInteractionRegistry()
    assert registry.apply_runtime_event(
        "background_agent.interaction_requested",
        {
            "request_id": "question-a",
            "activity_id": "activity-a",
            "interaction_session_id": "session-a",
            "kind": "question",
            "questions": [
                {
                    "question": "输出什么格式？",
                    "header": "输出格式",
                    "options": [
                        {"label": "Markdown", "description": "生成 md 文件"}
                    ],
                    "multiSelect": False,
                    "reasoning": "不能进入前台",
                }
            ],
        },
        3,
    )
    assert registry.apply_runtime_event(
        "background_agent.interaction_requested",
        {
            "request_id": "permission-a",
            "activity_id": "activity-a",
            "interaction_session_id": "session-a",
            "kind": "permission",
            "action_summary": "运行完成这项工作所需的命令",
            "grant_scope": "activity",
        },
        3,
    )

    assert registry.foreground_interactions("session-b") == []
    assert registry.foreground_interactions("session-a") == [
        {
            "request_id": "question-a",
            "activity_id": "activity-a",
            "kind": "question",
            "questions": [
                {
                    "question": "输出什么格式？",
                    "header": "输出格式",
                    "options": [
                        {"label": "Markdown", "description": "生成 md 文件"}
                    ],
                    "multi_select": False,
                }
            ],
        },
        {
            "request_id": "permission-a",
            "activity_id": "activity-a",
            "kind": "permission",
            "action_summary": "运行完成这项工作所需的命令",
            "grant_scope": "activity",
            "questions": [],
        },
    ]

    registry.claim_answer(
        request_id="question-a",
        kind="question",
        outcome="answer",
        answers={"输出什么格式？": "Markdown"},
        current_runtime_epoch=3,
    )
    assert [
        item["request_id"]
        for item in registry.foreground_interactions("session-a")
    ] == ["permission-a"]


def test_registry_snapshot_projects_pending_and_processing_cards() -> None:
    """重连首帧同时恢复可操作卡和等待运行时确认的 processing 卡。"""

    registry = BackgroundAgentInteractionRegistry()
    assert registry.apply_runtime_event(
        "background_agent.interaction_requested",
        {
            "request_id": "question-a",
            "activity_id": "activity-a",
            "interaction_session_id": "session-a",
            "kind": "question",
            "tool_name": "AskUserQuestion",
            "input_data": {"questions": [{"question": "选择版本"}]},
            "questions": [
                {
                    "question": "选择版本",
                    "header": "版本",
                    "options": [],
                    "multiSelect": True,
                }
            ],
        },
        3,
    )
    assert registry.apply_runtime_event(
        "background_agent.interaction_requested",
        {
            "request_id": "permission-a",
            "activity_id": "activity-a",
            "interaction_session_id": "session-a",
            "kind": "permission",
            "tool_name": "Bash",
            "input_data": {"command": "echo safe"},
            "action_summary": "运行完成这项工作所需的命令",
            "grant_scope": "activity",
            "questions": [],
        },
        3,
    )

    snapshot = registry.pending_control_snapshot_event()
    assert snapshot == {
        "type": "background_agent.interactions_snapshot",
        "payload": {
            "items": [
                {
                    "request_id": "question-a",
                    "activity_id": "activity-a",
                    "interaction_session_id": "session-a",
                    "status": "pending",
                    "kind": "question",
                    "tool_name": "AskUserQuestion",
                    "input_data": {"questions": [{"question": "选择版本"}]},
                    "questions": [
                        {
                            "question": "选择版本",
                            "header": "版本",
                            "options": [],
                            "multiSelect": True,
                        }
                    ],
                },
                {
                    "request_id": "permission-a",
                    "activity_id": "activity-a",
                    "interaction_session_id": "session-a",
                    "status": "pending",
                    "kind": "permission",
                    "tool_name": "Bash",
                    "input_data": {"command": "echo safe"},
                    "action_summary": "运行完成这项工作所需的命令",
                    "grant_scope": "activity",
                    "questions": [],
                },
            ]
        },
    }

    registry.claim_answer(
        request_id="question-a",
        kind="question",
        outcome="answer",
        answers={"选择版本": "第三版"},
        current_runtime_epoch=3,
    )
    assert [
        (item["request_id"], item["status"])
        for item in registry.pending_control_snapshot_event()["payload"]["items"]  # type: ignore[index,union-attr]
    ] == [("question-a", "processing"), ("permission-a", "pending")]


def _register(
    registry: BackgroundAgentInteractionRegistry,
    *,
    runtime_epoch: int,
) -> AgentInteractionEventApplyResult:
    """登记测试使用的权限请求。"""

    return registry.apply_runtime_event(
        "background_agent.interaction_requested",
        {
            "request_id": "request-a",
            "activity_id": "activity-a",
            "kind": "permission",
            "action_summary": "执行继续这项工作所需的操作",
            "grant_scope": "activity",
        },
        runtime_epoch,
    )
