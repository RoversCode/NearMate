from __future__ import annotations

from companion.background_agent.contracts import AgentMaterial
from companion.background_agent.interaction_registry import BackgroundAgentInteractionRegistry
from companion.background_agent.interaction_service import BackgroundAgentInteractionService


class StubRuntime:
    """记录统一交互服务真正送入助手运行时的命令。"""

    def __init__(self) -> None:
        self.answers: list[dict[str, object]] = []
        self.rejections: list[dict[str, object]] = []

    def answer_background_agent_interaction(self, **kwargs: object) -> object:
        """保存一次已经通过 Registry claim 的回答。"""

        self.answers.append(dict(kwargs))
        return object()

    def reject_background_agent_interaction_submission(
        self,
        **kwargs: object,
    ) -> object:
        """保存一次主进程 Registry 拒绝回执。"""

        self.rejections.append(dict(kwargs))
        return object()


def test_foreground_question_response_waits_for_background_receipt() -> None:
    """资料回答先 claim 并回送命令，后台 accepted 后才公开 processing。"""

    registry = BackgroundAgentInteractionRegistry()
    _register(registry, kind="question")
    runtime = StubRuntime()
    service = _service(registry, runtime)

    requested = service.apply_runtime_event(
        "llm.foreground.work_interaction_submission_requested",
        {
            "submission_id": "submission-a",
            "request_id": "request-a",
            "activity_id": "activity-a",
            "outcome": "answer",
            "answers": {"输出什么格式？": "MP3"},
            "user_text": "就用 MP3，这个文件也一起处理",
            "materials": [
                AgentMaterial(
                    material_id="material-a",
                    display_name="补充.wav",
                    material_kind="dragged_file",
                    media_class="audio",
                    local_path="C:/补充.wav",
                    node_kind="file",
                ).to_payload()
            ],
        },
        3,
    )

    assert requested
    assert requested.control_event is None
    assert registry.pending("request-a").state == "answering"  # type: ignore[union-attr]
    assert runtime.answers == [
        {
            "request_id": "request-a",
            "outcome": "answer",
            "answers": {"输出什么格式？": "MP3"},
            "reason": "",
            "materials": (
                AgentMaterial(
                    material_id="material-a",
                    display_name="补充.wav",
                    material_kind="dragged_file",
                    media_class="audio",
                    local_path="C:/补充.wav",
                    node_kind="file",
                ),
            ),
            "user_text": "就用 MP3，这个文件也一起处理",
            "submission_id": "submission-a",
            "runtime_epoch": 3,
        }
    ]

    completed = service.apply_runtime_event(
        "llm.foreground.work_interaction_submission_completed",
        {
            "submission_id": "submission-a",
            "request_id": "request-a",
            "activity_id": "activity-a",
            "receipt": "accepted",
        },
        3,
    )

    assert completed
    assert completed.control_event == {
        "type": "background_agent.interaction_submitted",
        "payload": {"request_id": "request-a", "activity_id": "activity-a"},
    }


def test_mouse_and_foreground_response_cannot_both_claim_same_request() -> None:
    """鼠标抢先后，迟到语音事件不能重复下发同一权限回答。"""

    registry = BackgroundAgentInteractionRegistry()
    _register(registry, kind="permission")
    runtime = StubRuntime()
    service = _service(registry, runtime)

    service.submit_permission_decision(
        request_id="request-a",
        outcome="allow",
    )
    late_result = service.apply_runtime_event(
        "llm.foreground.work_interaction_submission_requested",
        {
            "submission_id": "submission-late",
            "request_id": "request-a",
            "activity_id": "activity-a",
            "outcome": "allow",
            "answers": {},
            "materials": [],
            "user_text": "允许执行",
        },
        3,
    )

    assert late_result
    assert late_result.control_event is None
    assert len(runtime.answers) == 1
    assert runtime.rejections == [
        {
            "submission_id": "submission-late",
            "request_id": "request-a",
            "reason": "already_resolved",
            "runtime_epoch": 3,
        }
    ]


def test_failed_background_receipt_releases_only_its_registry_claim() -> None:
    """后台拒收后恢复该请求回答权，允许用户重新提交。"""

    registry = BackgroundAgentInteractionRegistry()
    _register(registry, kind="question")
    runtime = StubRuntime()
    service = _service(registry, runtime)
    service.apply_runtime_event(
        "llm.foreground.work_interaction_submission_requested",
        {
            "submission_id": "submission-a",
            "request_id": "request-a",
            "activity_id": "activity-a",
            "outcome": "answer",
            "answers": {"输出什么格式？": "MP3"},
            "materials": [],
        },
        3,
    )

    result = service.apply_runtime_event(
        "llm.foreground.work_interaction_submission_failed",
        {
            "submission_id": "submission-a",
            "request_id": "request-a",
            "activity_id": "activity-a",
            "receipt": "background_unavailable",
        },
        3,
    )

    assert result
    assert registry.pending("request-a").state == "pending"  # type: ignore[union-attr]


def _service(
    registry: BackgroundAgentInteractionRegistry,
    runtime: StubRuntime,
) -> BackgroundAgentInteractionService:
    """构造固定在第三世代的统一交互服务。"""

    return BackgroundAgentInteractionService(
        registry=registry,
        runtime_provider=lambda: runtime,
        current_runtime_epoch=lambda: 3,
    )


def _register(
    registry: BackgroundAgentInteractionRegistry,
    *,
    kind: str,
) -> None:
    """登记一条测试使用的权限或资料请求。"""

    payload: dict[str, object] = {
        "request_id": "request-a",
        "activity_id": "activity-a",
        "interaction_session_id": "session-a",
        "kind": kind,
    }
    if kind == "permission":
        payload.update(
            {
                "action_summary": "执行继续这项工作所需的操作",
                "grant_scope": "activity",
                "questions": [],
            }
        )
    else:
        payload["questions"] = [{"question": "输出什么格式？"}]

    assert registry.apply_runtime_event(
        "background_agent.interaction_requested",
        payload,
        3,
    )
