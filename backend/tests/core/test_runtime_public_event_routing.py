from __future__ import annotations

from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import ValidationError

from companion.background_agent.contracts import AgentEventType
from companion.realtime.assistant_events import (
    RECENT_MATERIAL_OBSERVATION_FIELD,
    RecentMaterialObservation,
    RuntimeEvent,
)
from companion.realtime.assistant_runtime_event_router import (
    RuntimeEventRoutingError,
    classify_runtime_event_owner,
    validate_runtime_event_owner_sets,
)
from backend.tests.runtime_public_event_support import runtime_event_router


INPUT_RUNTIME_ID = "input-runtime-00000000-0000-4000-8000-000000000007"


@pytest.mark.asyncio
async def test_wake_detection_preserves_monotonic_time_for_shortcut_resume() -> None:
    """公共唤醒事件保留发生时间，让 Desktop 只恢复取消之后的新短答。"""

    from companion.realtime.realtime_input_public_event_handler import RealtimeInputPublicEventHandler

    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    event = _runtime_event("wake.detected", {
        "input_runtime_id": INPUT_RUNTIME_ID,
        "input_runtime_epoch": 7,
        "session_id": "room-a",
        "detected_at": 11.5,
    })
    await RealtimeInputPublicEventHandler(publish_interaction=publish).handle(event)
    assert published[0]["payload"]["detected_at"] == 11.5


def _runtime_event(event_type: str, payload: dict[str, object]) -> RuntimeEvent:
    """构造当前 Backend 运行期事件。

    Args:
        event_type: 需要验证归属的事件名称。
        payload: 事件进入 Router 前的完整载荷。

    Returns:
        不绑定助手子进程世代的运行期事件。
    """

    return RuntimeEvent(
        type=event_type,
        source="realtime_input",
        payload=payload,
        produced_at=10.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "user.partial_transcript",
            {
                "input_runtime_id": INPUT_RUNTIME_ID,
                "input_runtime_epoch": 7,
                "session_id": "room-a",
                "turn_id": "turn-7",
                "turn_sequence": 7,
                "text": "我正在说话",
            },
        ),
        (
            "user.final_transcript",
            {
                "input_runtime_id": INPUT_RUNTIME_ID,
                "input_runtime_epoch": 7,
                "session_id": "room-a",
                "turn_id": "turn-7",
                "turn_sequence": 7,
                "request_id": "final-asr-request-a",
                "history_disposition": "ordinary_turn",
                "text": "我说完了",
            },
        ),
    ],
)
async def test_realtime_input_public_event_keeps_complete_payload(
    event_type: str,
    payload: dict[str, object],
) -> None:
    """实时输入公共事件不能经过助手字段投影丢掉用户正文。

    Args:
        event_type: Partial 或 Final 公共事件名称。
        payload: 预期原样到达控制面的载荷。
    """

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录 Router 公开的事件。"""

        broadcasts.append(event)

    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(
            input_runtime_id=INPUT_RUNTIME_ID,
            input_runtime_epoch=7,
        ),
    )

    await router.dispatch(_runtime_event(event_type, payload))

    assert broadcasts == [{"type": event_type, "payload": payload}]


@pytest.mark.asyncio
async def test_old_input_runtime_transient_event_is_absorbed() -> None:
    """旧输入 Runtime 的瞬时事件不能污染当前输入现场。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录任何不应发生的公共广播。"""

        broadcasts.append(event)

    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        current_input_runtime_identity=lambda: (
            "input-runtime-00000000-0000-4000-8000-000000000008",
            8,
        ),
    )

    await router.dispatch(
        _runtime_event(
            "user.partial_transcript",
            {
                "input_runtime_id": INPUT_RUNTIME_ID,
                "input_runtime_epoch": 7,
                "session_id": "room-a",
                "turn_id": "turn-7",
                "turn_sequence": 7,
                "text": "旧现场的迟到识别",
            },
        )
    )

    assert broadcasts == []


@pytest.mark.asyncio
async def test_cancelled_reminder_text_is_absorbed_at_runtime_router_boundary() -> None:
    """Reminder 显式关闭后，已排队的迟到正文不能越过真实 Router 公共边界。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录任何错误穿过请求墓碑的公共事件。"""

        broadcasts.append(event)

    router = runtime_event_router(
        publish=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        current_runtime_epoch=lambda: 7,
    )
    router.cancel_request("timed-reminder-request-a")

    await router.dispatch(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            runtime_epoch=7,
            payload={
                "request_id": "timed-reminder-request-a",
                "session_id": "room-a",
                "generation_id": "generation-a",
                "source": "timed_reminder_notice",
                "history_policy": "stable_dialogue",
                "activity_progress_references": [],
                "derived_from_activity_ids": [],
                "text": "迟到的提醒正文",
            },
        )
    )

    assert broadcasts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_broadcast_count"),
    [("旧 Runtime 已经形成的真实消息", 1), ("  \t", 0)],
)
async def test_old_input_runtime_final_retains_only_nonblank_user_message(
    text: str,
    expected_broadcast_count: int,
) -> None:
    """旧 Runtime 只保留非空 Final，空白收口事实不进入 Chat。

    Args:
        text: 旧输入 Runtime 最终识别得到的正文。
        expected_broadcast_count: 当前正文应产生的公共事件数量。
    """

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录保留下来的旧 Runtime Final。"""

        broadcasts.append(event)

    payload = {
        "input_runtime_id": INPUT_RUNTIME_ID,
        "input_runtime_epoch": 7,
        "session_id": "room-a",
        "turn_id": "turn-7",
        "turn_sequence": 7,
        "request_id": "final-asr-request-a",
        "history_disposition": "ordinary_turn",
        "text": text,
    }
    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        current_input_runtime_identity=lambda: (
            "input-runtime-00000000-0000-4000-8000-000000000008",
            8,
        ),
    )

    await router.dispatch(_runtime_event("user.final_transcript", payload))

    assert len(broadcasts) == expected_broadcast_count


@pytest.mark.asyncio
async def test_output_failure_compensation_preserves_self_reflection_identity() -> None:
    """公共输出失败补偿必须保留主业务结算所需的完整反思身份。"""

    compensated_failures: list[RuntimeEvent] = []
    settlements: list[RuntimeEvent] = []

    async def ignore_publish(_event: dict[str, object]) -> None:
        """本测试直接调用补偿入口，不需要公共广播。"""

    async def record_failure(event: RuntimeEvent, *, code: str) -> None:
        """记录请求失败补偿及其稳定错误码。"""

        assert code == "output_unavailable"
        compensated_failures.append(event)

    async def record_settlement(event: RuntimeEvent) -> None:
        """保存 Router 生成的最终无正文结算事件。"""

        settlements.append(event)

    router = runtime_event_router(
        publish=ignore_publish,
        realtime_input_runtime=SimpleNamespace(),
    )
    router._request_failure_handler = SimpleNamespace(compensate=record_failure)
    router._output_event_handler = SimpleNamespace(handle_settlement=record_settlement)
    original_payload = {
        "request_id": "self-reflection-interruption-a",
        "generation_id": "generation-a",
        "source": "self_reflection_interruption_reply",
        "history_policy": "unattended_episode",
        "self_reflection_id": "reflection-a",
        "episode_id": "episode-a",
        "runtime_epoch": 7,
        "trigger_input_identity": "input-a",
    }

    await router._compensate_assistant_output_failure(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_process",
            payload=original_payload,
            produced_at=10.0,
            runtime_epoch=7,
        )
    )

    assert len(compensated_failures) == 1
    assert len(settlements) == 1
    settlement_payload = settlements[0].payload
    for field_name in (
        "source",
        "self_reflection_id",
        "episode_id",
        "runtime_epoch",
        "trigger_input_identity",
    ):
        assert settlement_payload[field_name] == original_payload[field_name]


@pytest.mark.asyncio
async def test_self_reflection_episode_commit_is_published_as_one_history_block() -> None:
    """闭合 episode 只能以一条完整 Interaction 事件公开。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录 Router 发布的原子历史事件。"""

        broadcasts.append(event)

    payload: dict[str, object] = {
        "self_reflection_id": "self-reflection-1",
        "episode_id": "episode-1",
        "runtime_epoch": 7,
        "revision": 4,
        "actionable": False,
        "history_block": [
            {
                "role": "user",
                "content": "先别急",
                "occurred_at": "2026-08-16T15:00:00+08:00",
                "episode_role": "input",
                "source_identity": "text-1",
                "reply_settlement": None,
            },
            {
                "role": "assistant",
                "content": "我听到了。",
                "occurred_at": "2026-08-16T15:00:01+08:00",
                "episode_role": "interruption_reply",
                "source_identity": "reply-1",
                "reply_settlement": "completed",
            },
        ],
    }
    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
    )

    await router.dispatch(
        RuntimeEvent(
            type="self_reflection.episode_committed",
            source="main_process",
            payload=payload,
            produced_at=10.0,
            runtime_epoch=None,
        )
    )

    assert broadcasts == [
        {"type": "self_reflection.episode_committed", "payload": payload}
    ]


@pytest.mark.asyncio
async def test_retired_runtime_output_settlement_uses_settlement_only_path() -> None:
    """旧世代 backlog 只允许结算既有输出，不能被退役事件门禁直接拒绝。"""

    handled: list[RuntimeEvent] = []

    async def handle_settlement(event: RuntimeEvent) -> None:
        """记录退役结算进入了唯一的输出 settlement 入口。"""

        handled.append(event)

    async def ignore_publish(_event: dict[str, object]) -> None:
        """本测试不产生任何公共事件。"""

    router = runtime_event_router(
        publish=ignore_publish,
        publish_ephemeral=ignore_publish,
        realtime_input_runtime=SimpleNamespace(),
    )
    router._output_event_handler = SimpleNamespace(  # type: ignore[assignment]
        handle_settlement=handle_settlement,
    )
    event = RuntimeEvent(
        type="assistant.output_settled",
        source="assistant_process",
        payload={
            "request_id": "old-self-reflection-reply",
            "settlement_id": "settlement-old-self-reflection-reply",
            "outcome": "interrupted",
        },
        runtime_epoch=7,
        retired_runtime_event=True,
    )

    await router.dispatch(event)

    assert handled == [event]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "turn.state_changed",
            {
                "input_runtime_id": INPUT_RUNTIME_ID,
                "input_runtime_epoch": 7,
                "session_id": "room-a",
                "turn_state": "listening",
                "turn_id": "turn-7",
            },
        ),
        (
            "wake.detected",
            {
                "input_runtime_id": INPUT_RUNTIME_ID,
                "input_runtime_epoch": 7,
                "session_id": "   ",
            },
        ),
        (
            "wake.detected",
            {
                "input_runtime_id": INPUT_RUNTIME_ID,
                "input_runtime_epoch": 7,
                "session_id": "room-a",
                "text": None,
            },
        ),
        (
            "input.smart_turn_waiting_user_started",
            {
                "input_runtime_id": INPUT_RUNTIME_ID,
                "input_runtime_epoch": 7,
                "session_id": "room-a",
                "turn_id": "turn-7",
                "turn_sequence": 7,
                "probability": float("inf"),
                "hint_text": "请继续说",
            },
        ),
    ],
)
async def test_realtime_input_public_event_rejects_cross_contract_invalid_payload(
    event_type: str,
    payload: dict[str, object],
) -> None:
    """Backend 出口必须拒绝 Desktop Parser 同样不会接收的载荷。

    Args:
        event_type: 待验证的实时输入公共事件名称。
        payload: 类型正确但字段关系或语义值非法的载荷。
    """

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录任何不应发生的公共广播。"""

        broadcasts.append(event)

    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(
            input_runtime_id=INPUT_RUNTIME_ID,
            input_runtime_epoch=7,
        ),
    )

    with pytest.raises(ValidationError):
        await router.dispatch(_runtime_event(event_type, payload))

    assert broadcasts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        "assistant.generation_started",
        "assistant.state_changed",
        "llm.foreground.authentication_failed",
        "llm.foreground.tool_call_rejected",
    ],
)
async def test_internal_assistant_event_never_enters_public_control_plane(
    event_type: str,
) -> None:
    """助手内部生命周期和诊断事实不能伪装成公共事件。

    Args:
        event_type: 明确归为内部事实的事件名称。
    """

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
    )

    await router.dispatch(
        RuntimeEvent(
            type=event_type,
            source="assistant_output_lanes",
            payload={"request_id": "request-a", "private": "不能公开"},
            produced_at=10.0,
        )
    )

    assert broadcasts == []


@pytest.mark.asyncio
async def test_recent_material_observation_is_consumed_before_canonical_event() -> None:
    """近期路径只能进入主进程材料端口，不能进入 canonical 工具事实。"""

    observations: list[RecentMaterialObservation] = []
    canonical_events: list[RuntimeEvent] = []

    async def ignore_publish(_event: dict[str, object]) -> None:
        """本测试不产生公共广播。"""

    async def observe_recent_materials(
        observation: RecentMaterialObservation,
    ) -> None:
        """记录 Router 已解析的私有路径事实。"""

        observations.append(observation)

    async def register_canonical(event: RuntimeEvent) -> None:
        """记录即将进入记忆和 Activity 的脱敏工具事实。"""

        canonical_events.append(event)

    router = runtime_event_router(
        publish=ignore_publish,
        publish_ephemeral=ignore_publish,
        realtime_input_runtime=SimpleNamespace(),
        current_runtime_epoch=lambda: 7,
        recent_material_observation_handler=observe_recent_materials,
    )
    router._register_canonical_tool_event = register_canonical  # type: ignore[method-assign]
    event = RuntimeEvent(
        type="llm.foreground.tool_call_completed",
        source="assistant_runtime",
        payload={
            "request_id": "request-a",
            "name": "start_background_work",
            "state": "accepted",
            RECENT_MATERIAL_OBSERVATION_FIELD: {
                "outcome": "accepted",
                "paths": [r"C:\project\app.py"],
            },
        },
        runtime_epoch=7,
    )

    await router.dispatch(event)

    assert observations == [
        RecentMaterialObservation(
            outcome="accepted",
            paths=(r"C:\project\app.py",),
        )
    ]
    assert len(canonical_events) == 1
    assert RECENT_MATERIAL_OBSERVATION_FIELD not in canonical_events[0].payload


@pytest.mark.asyncio
async def test_old_runtime_cannot_refresh_recent_materials() -> None:
    """热替换前的迟到工具事实不能刷新当前进程的近期材料顺序。"""

    observations: list[RecentMaterialObservation] = []

    async def ignore_publish(_event: dict[str, object]) -> None:
        """本测试不产生公共广播。"""

    async def observe_recent_materials(
        observation: RecentMaterialObservation,
    ) -> None:
        """记录任何不应发生的旧世代观察。"""

        observations.append(observation)

    router = runtime_event_router(
        publish=ignore_publish,
        publish_ephemeral=ignore_publish,
        realtime_input_runtime=SimpleNamespace(),
        current_runtime_epoch=lambda: 8,
        recent_material_observation_handler=observe_recent_materials,
    )
    await router.dispatch(
        RuntimeEvent(
            type="llm.foreground.tool_call_completed",
            source="assistant_runtime",
            payload={
                "request_id": "request-old",
                "name": "start_background_work",
                "state": "accepted",
                RECENT_MATERIAL_OBSERVATION_FIELD: {
                    "outcome": "accepted",
                    "paths": [r"C:\project\old.py"],
                },
            },
            runtime_epoch=7,
        )
    )

    assert observations == []


@pytest.mark.asyncio
async def test_unknown_runtime_event_fails_closed() -> None:
    """未登记事件必须显式失败，不能广播一个空载荷事件。"""

    router = runtime_event_router(
        publish=lambda _event: _noop_async(),
        publish_ephemeral=lambda _event: _noop_async(),
        realtime_input_runtime=SimpleNamespace(),
    )

    with pytest.raises(Exception, match="unregistered.runtime_event"):
        await router.dispatch(
            RuntimeEvent(
                type="unregistered.runtime_event",
                source="assistant_runtime",
                payload={"private": "不能公开"},
            )
        )


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_owner"),
    [
        ("user.partial_transcript", {}, "realtime_input"),
        ("assistant.request_enqueue_failed", {}, "request_failure"),
        (
            "assistant.generation_interrupted",
            {"reason": "runtime_unavailable"},
            "request_failure",
        ),
        (
            "assistant.generation_interrupted",
            {"reason": "user_interrupted"},
            "assistant_output",
        ),
        ("assistant.output_settled", {}, "output_settlement"),
        (
            "llm.foreground.runtime_memory_compaction_completed",
            {},
            "runtime_memory_compaction",
        ),
        (
            "llm.foreground.task_memory_lookup_requested",
            {},
            "task_memory_lookup",
        ),
        ("assistant.state_changed", {}, "internal_lifecycle"),
        ("assistant.generation_started", {}, "internal_diagnostic"),
        ("cognitive_task.started", {}, "activity"),
        ("assistant.text_chunk", {}, "assistant_output"),
    ],
)
def test_runtime_event_owner_is_explicit_and_unique(
    event_type: str,
    payload: dict[str, object],
    expected_owner: str,
) -> None:
    """每类运行时事件都必须命中一个明确所有者。

    Args:
        event_type: 当前表格覆盖的运行时事件名称。
        payload: 会影响请求失败分类的最小稳定字段。
        expected_owner: 事件必须命中的处理器名称。
    """

    assert classify_runtime_event_owner(_runtime_event(event_type, payload)) == expected_owner


@pytest.mark.parametrize("agent_event_type", get_args(AgentEventType))
def test_every_background_agent_event_has_activity_owner(
    agent_event_type: str,
) -> None:
    """Runner 能发出的每种事件都必须在主进程命中 Activity 所有者。

    Args:
        agent_event_type: 不带来源前缀的后台工作事件类型。

    Returns:
        无。
    """

    event_type = f"background_agent.{agent_event_type}"

    assert classify_runtime_event_owner(_runtime_event(event_type, {})) == "activity"


def test_agent_event_type_excludes_non_runtime_events() -> None:
    """AgentEventType 不能重新混入同步准备事件或 SDK 会话元事件。

    Returns:
        无。
    """

    event_types = set(get_args(AgentEventType))

    assert "prepared" not in event_types
    assert "session_started" not in event_types


def test_session_started_event_remains_fail_closed() -> None:
    """历史会话启动事件即使被外部误发，也必须继续拒绝路由。

    Returns:
        无。
    """

    with pytest.raises(
        RuntimeEventRoutingError,
        match=r"background_agent\.session_started",
    ):
        classify_runtime_event_owner(
            _runtime_event("background_agent.session_started", {})
        )


def test_runtime_event_owner_sets_reject_overlap() -> None:
    """新增事件不能同时登记到两个静态处理器。"""

    with pytest.raises(RuntimeError, match="runtime.event.*first,second"):
        validate_runtime_event_owner_sets(
            {
                "first": frozenset({"runtime.event"}),
                "second": frozenset({"runtime.event"}),
            }
        )


def test_runtime_event_owner_rejects_unknown_type() -> None:
    """分类阶段必须拒绝未登记事件，不能等待后续默认分支。"""

    with pytest.raises(RuntimeEventRoutingError, match="unregistered.runtime_event"):
        classify_runtime_event_owner(
            _runtime_event("unregistered.runtime_event", {})
        )


async def _noop_async() -> None:
    """提供无副作用的异步广播结果。"""
