from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.background_agent.interaction_registry import (
    BackgroundAgentInteractionRegistry,
)
from companion.background_agent.interaction_service import BackgroundAgentInteractionService
from companion.core.api.dependencies import get_background_agent_dependencies
from companion.core.api.routers import background_agent


class StubRuntime:
    """记录权限决定实际送入助手控制队列的数据。"""

    def __init__(self, *, fail_submission: bool = False) -> None:
        """初始化可选失败的 Runtime 替身。

        Args:
            fail_submission: 是否在接收权限决定时模拟运行时不可用。
        """

        self.answers: list[dict[str, object]] = []
        self.fail_submission = fail_submission

    def answer_background_agent_interaction(self, **answer: object) -> None:
        """保存一条已经通过 Registry claim 的控制命令。"""

        if self.fail_submission:
            raise RuntimeError("runtime unavailable")
        self.answers.append(answer)


class RecordingMediaSessionController:
    """记录权限卡决定是否触发了精确播报早停。"""

    def __init__(self, *, fail_decision_handling: bool = False) -> None:
        """初始化空的权限卡决定记录。

        Args:
            fail_decision_handling: 是否模拟媒体早停内部异常。
        """

        self.permission_card_decisions: list[tuple[object, str]] = []
        self.fail_decision_handling = fail_decision_handling

    def handle_permission_card_decision(
        self,
        interaction: object,
        outcome: str,
    ) -> None:
        """记录一次已经成功进入 Runtime 的权限卡决定。

        Args:
            interaction: 已由 Registry 原子认领的交互快照。
            outcome: 用户点击的允许或拒绝结果。
        """

        self.permission_card_decisions.append((interaction, outcome))
        if self.fail_decision_handling:
            raise RuntimeError("media session unavailable")


def test_permission_decision_api_claims_and_broadcasts_processing() -> None:
    """鼠标允许必须先提交权限，再广播卡片处理状态。"""

    runtime = StubRuntime()
    registry = _registry_with_permission_request()
    client, broadcast_events, media_controller = _client(runtime, registry)

    response = client.post(
        "/api/background-agent/interactions/request-a/permission-decision",
        json={"outcome": "allow"},
    )

    assert response.status_code == 202
    assert runtime.answers == [
        {
            "request_id": "request-a",
            "outcome": "allow",
            "answers": {},
            "reason": "",
            "materials": (),
            "user_text": "",
            "submission_id": None,
            "runtime_epoch": 3,
        }
    ]
    assert broadcast_events == [
        {
            "type": "background_agent.interaction_submitted",
            "payload": {"request_id": "request-a", "activity_id": "activity-a"},
        }
    ]
    assert [
        (interaction.request_id, outcome)
        for interaction, outcome in media_controller.permission_card_decisions
    ] == [("request-a", "allow")]
    assert registry.pending("request-a").state == "answering"  # type: ignore[union-attr]


def test_permission_decision_api_rejects_unknown_request() -> None:
    """未知或已经收口的请求不能进入助手控制队列。"""

    runtime = StubRuntime()
    client, _, media_controller = _client(
        runtime,
        BackgroundAgentInteractionRegistry(),
    )

    response = client.post(
        "/api/background-agent/interactions/missing/permission-decision",
        json={"outcome": "deny"},
    )

    assert response.status_code == 404
    assert runtime.answers == []
    assert media_controller.permission_card_decisions == []


def test_permission_decision_api_rejects_duplicate_click() -> None:
    """第一份权限决定已经提交后，重复点击必须在 Registry claim 阶段被拒绝。"""

    runtime = StubRuntime()
    client, _, media_controller = _client(
        runtime,
        _registry_with_permission_request(),
    )

    first_response = client.post(
        "/api/background-agent/interactions/request-a/permission-decision",
        json={"outcome": "allow"},
    )
    second_response = client.post(
        "/api/background-agent/interactions/request-a/permission-decision",
        json={"outcome": "deny"},
    )

    assert first_response.status_code == 202
    assert second_response.status_code == 409
    assert len(runtime.answers) == 1
    assert len(media_controller.permission_card_decisions) == 1


def test_permission_decision_continues_when_processing_broadcast_fails() -> None:
    """权限已经送入后台后，状态广播故障不能伪装提交失败。"""

    runtime = StubRuntime()
    registry = _registry_with_permission_request()
    client, _, media_controller = _client(
        runtime,
        registry,
        fail_broadcast=True,
    )

    response = client.post(
        "/api/background-agent/interactions/request-a/permission-decision",
        json={"outcome": "allow"},
    )

    assert response.status_code == 202
    assert len(runtime.answers) == 1
    assert len(media_controller.permission_card_decisions) == 1
    assert registry.pending("request-a").state == "answering"  # type: ignore[union-attr]


def test_permission_decision_continues_when_playout_stop_fails() -> None:
    """权限已经送入后台后，媒体早停故障不能把已提交决定误报为失败。"""

    runtime = StubRuntime()
    registry = _registry_with_permission_request()
    client, broadcast_events, media_controller = _client(
        runtime,
        registry,
        fail_media_decision_handling=True,
    )

    response = client.post(
        "/api/background-agent/interactions/request-a/permission-decision",
        json={"outcome": "allow"},
    )

    assert response.status_code == 202
    assert len(runtime.answers) == 1
    assert len(media_controller.permission_card_decisions) == 1
    assert broadcast_events == [
        {
            "type": "background_agent.interaction_submitted",
            "payload": {"request_id": "request-a", "activity_id": "activity-a"},
        }
    ]
    assert registry.pending("request-a").state == "answering"  # type: ignore[union-attr]


def test_permission_decision_returns_accepted_when_broadcast_times_out() -> None:
    """processing 广播超过半秒时，202 不能被拖住或回滚。"""

    runtime = StubRuntime()
    registry = _registry_with_permission_request()
    client, _, media_controller = _client(
        runtime,
        registry,
        broadcast_delay_seconds=1.0,
    )

    response = client.post(
        "/api/background-agent/interactions/request-a/permission-decision",
        json={"outcome": "allow"},
    )

    assert response.status_code == 202
    assert len(runtime.answers) == 1
    assert len(media_controller.permission_card_decisions) == 1
    assert registry.pending("request-a").state == "answering"  # type: ignore[union-attr]


def test_permission_decision_does_not_stop_playout_when_runtime_submission_fails() -> None:
    """权限决定未进入 Runtime 时，原权限请求仍须保留给用户重试。"""

    runtime = StubRuntime(fail_submission=True)
    registry = _registry_with_permission_request()
    client, _, media_controller = _client(runtime, registry)

    response = client.post(
        "/api/background-agent/interactions/request-a/permission-decision",
        json={"outcome": "allow"},
    )

    assert response.status_code == 503
    assert media_controller.permission_card_decisions == []
    assert registry.pending("request-a").state == "pending"  # type: ignore[union-attr]


def test_old_answer_route_and_question_payload_are_retired() -> None:
    """旧综合回答入口不留兼容层，资料回答只能回到正常用户轮。"""

    client, _, _ = _client(
        StubRuntime(),
        _registry_with_permission_request(),
    )

    response = client.post(
        "/api/background-agent/interactions/request-a/answer",
        json={"kind": "question", "outcome": "answer", "answers": {"格式？": "MP3"}},
    )

    assert response.status_code == 404


def _client(
    runtime: StubRuntime,
    registry: BackgroundAgentInteractionRegistry,
    *,
    fail_broadcast: bool = False,
    broadcast_delay_seconds: float = 0.0,
    fail_media_decision_handling: bool = False,
) -> tuple[
    TestClient,
    list[dict[str, object]],
    RecordingMediaSessionController,
]:
    """构造只挂载后台工作交互路由的测试客户端。"""

    app = FastAPI()
    app.include_router(background_agent.router)
    broadcast_events: list[dict[str, object]] = []
    media_controller = RecordingMediaSessionController(
        fail_decision_handling=fail_media_decision_handling,
    )

    class Hub:
        """实现权限决定路由真正依赖的 Activity Publisher 端口。"""

        def publisher(self, domain: str) -> "Hub":
            """返回测试使用的 Activity 瞬时事件发布端口。

            Args:
                domain: 路由申请的公共事件领域。

            Returns:
                当前 Hub；它同时实现 publish_ephemeral 方法。
            """

            assert domain == "activity"
            return self

        async def publish_ephemeral(self, event: dict[str, object]) -> None:
            """记录卡片进入 processing 的 Control 事件。

            Args:
                event: 准备公开的交互处理状态事件。
            """

            if broadcast_delay_seconds > 0:
                await asyncio.sleep(broadcast_delay_seconds)
            if fail_broadcast:
                raise RuntimeError("control websocket unavailable")
            broadcast_events.append(event)

    interaction_service = BackgroundAgentInteractionService(
        registry=registry,
        runtime_provider=lambda: runtime,
        current_runtime_epoch=lambda: 3,
    )
    app.dependency_overrides[get_background_agent_dependencies] = lambda: SimpleNamespace(
        assistant_process_supervisor=SimpleNamespace(
            client=SimpleNamespace(current_epoch=3),
            require_for_output=lambda: runtime,
        ),
        background_agent_interactions=registry,
        background_agent_interaction_service=interaction_service,
        livekit_media_session_controller=media_controller,
        hub=Hub(),
    )
    return TestClient(app), broadcast_events, media_controller


def _registry_with_permission_request() -> BackgroundAgentInteractionRegistry:
    """创建已经登记一条当前世代权限请求的 Registry。"""

    registry = BackgroundAgentInteractionRegistry()
    assert registry.apply_runtime_event(
        "background_agent.interaction_requested",
        {
            "request_id": "request-a",
            "activity_id": "activity-a",
            "kind": "permission",
            "action_summary": "执行继续这项工作所需的操作",
            "grant_scope": "activity",
            "interaction_session_id": "session-a",
            "questions": [],
        },
        3,
    )
    return registry
