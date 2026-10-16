from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion.activities.notice.models import NoticeDeliveryCommand
from companion.runtime_memory.store import RuntimeMemoryStore
from backend.tests.runtime_public_event_support import runtime_event_router


RUNTIME_EPOCH = 1
RUNTIME_ID = "00000000-0000-4000-8000-0000000000bb"

from backend.tests.activities.runtime_event_router_test_support import (
    _event,
)

@pytest.mark.asyncio
async def test_final_asr_enqueue_failure_finishes_exact_material_context() -> None:
    """没有 generation 终态的 final ASR 失败也必须释放明确绑定的材料上下文。"""

    broadcasts: list[dict[str, object]] = []
    finished_contexts: list[str] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录瞬时材料事件。"""

        broadcasts.append(event)

    def finish(context_id: str) -> list[dict[str, object]]:
        """记录清理身份并返回权威材料快照事件。"""

        finished_contexts.append(context_id)
        return [{"type": "materials.contexts_changed", "payload": {"revision": 2}}]

    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(
            apply_assistant_output_finished=lambda **_kwargs: [
                {"type": "turn.state_changed", "payload": {"turn_state": "idle"}}
            ]
        ),
        turn_material_final_handler=finish,
    )

    await event_router.dispatch(
        _event(
            "assistant.request_enqueue_failed",
            {
                "request_id": "request-a",
                "source": "final_asr",
                "turn_material_context_id": "context-a",
            },
        )
    )

    assert finished_contexts == ["context-a"]
    assert broadcasts == [
        {"type": "materials.contexts_changed", "payload": {"revision": 2}},
        {"type": "turn.state_changed", "payload": {"turn_state": "idle"}},
        {
            "type": "assistant.request_failed",
            "payload": {
                    "request_id": "request-a",
                    "code": "request_start_failed",
                    "message": "消息已经发送，但我现在没能开始回复，请稍后再试。",
            },
        },
    ]


@pytest.mark.asyncio
async def test_bridge_runtime_submit_failure_restores_idle_and_reports_chat_failure() -> None:
    """异步 bridge 启动失败必须清理同一材料、恢复 idle 并显示助手失败提示。"""

    broadcasts: list[dict[str, object]] = []
    ephemeral: list[dict[str, object]] = []
    finished_contexts: list[str] = []
    notice_commands: list[NoticeDeliveryCommand] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        ephemeral.append(event)

    def finish(context_id: str) -> list[dict[str, object]]:
        finished_contexts.append(context_id)
        return [{"type": "materials.contexts_changed", "payload": {"revision": 3}}]

    realtime_input_runtime = SimpleNamespace(
        apply_assistant_output_finished=lambda **_kwargs: [
            {"type": "turn.state_changed", "payload": {"turn_state": "idle"}}
        ]
    )
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast_ephemeral,
        realtime_input_runtime=realtime_input_runtime,
        turn_material_final_handler=finish,
        notice_delivery_handler=notice_commands.append,
    )

    await event_router.dispatch(
        _event(
            "assistant.generation_interrupted",
            {
                "request_id": "text-input-1",
                "source": "text_input",
                "source_turn_id": None,
                "turn_material_context_id": "context-a",
                "reason": "runtime_submit_failed",
            },
        )
    )

    assert finished_contexts == ["context-a"]
    assert notice_commands == [
        NoticeDeliveryCommand(
            request_id="text-input-1",
            outcome="delivery_cancelled",
        )
    ]
    assert [event["type"] for event in ephemeral] == ["materials.contexts_changed"]
    assert [event["type"] for event in broadcasts] == [
        "turn.state_changed",
        "assistant.request_failed",
    ]


@pytest.mark.asyncio
async def test_text_final_broadcast_failure_compensates_without_memory_commit() -> None:
    """正文广播失败时不写正文账本，但必须释放资源并产生稳定失败事件。"""

    finished_contexts: list[str] = []
    notice_commands: list[NoticeDeliveryCommand] = []

    broadcasts: list[dict[str, object]] = []
    broadcast_attempts = 0

    async def fail_first_broadcast(event: dict[str, object]) -> None:
        nonlocal broadcast_attempts
        broadcast_attempts += 1
        if broadcast_attempts == 1:
            raise RuntimeError("control broadcast failed")
        broadcasts.append(event)

    def finish(context_id: str) -> list[dict[str, object]]:
        finished_contexts.append(context_id)
        return []

    memory_store = RuntimeMemoryStore()
    event_router = runtime_event_router(
        publish=fail_first_broadcast,
        publish_ephemeral=fail_first_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        turn_material_final_handler=finish,
        notice_delivery_handler=notice_commands.append,
        runtime_memory_store=memory_store,
    )

    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {
                "request_id": "request-a",
                "text": "已经完成。",
                "turn_material_context_id": "context-a",
                "memory_commit": {
                    "commit_id": "commit-a",
                    "request_id": "request-a",
                    "base_memory_epoch": 0,
                    "messages": [
                        {"role": "assistant", "content": "已经完成。"}
                    ],
                },
            },
        )
    )

    assert notice_commands == []
    assert finished_contexts == ["context-a"]
    assert memory_store.snapshot().recent_messages == ()
    assert broadcasts == [
        {
            "type": "assistant.request_failed",
            "payload": {
                "request_id": "request-a",
                "code": "output_unavailable",
                "message": "消息已经发送，但助手输出暂时不可用，请重新连接后再试。",
            },
        }
    ]


@pytest.mark.asyncio
async def test_stream_whitespace_does_not_settle_before_later_final() -> None:
    """Provider 空白 delta 不能把请求提前结算为输出不可用。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event: dict[str, object]) -> None:
        broadcasts.append(event)

    memory_store = RuntimeMemoryStore()
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(
            apply_assistant_output_finished=lambda **_kwargs: []
        ),
        runtime_memory_store=memory_store,
    )
    identity = {
        "request_id": "request-stream-whitespace",
        "session_id": "session-stream-whitespace",
        "generation_id": "generation-stream-whitespace",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
    }
    full_text = "[Shh] 正文"

    for text in ["[Shh]", " ", "正文"]:
        await event_router.dispatch(
            _event("assistant.text_chunk", {**identity, "text": text})
        )
    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {
                **identity,
                "text": full_text,
                "memory_commit": {
                    "commit_id": "commit-stream-whitespace",
                    "request_id": identity["request_id"],
                    "base_memory_epoch": 0,
                    "messages": [
                        {
                            "role": "assistant",
                            "content": full_text,
                            "metadata": {
                                "origin_key": "request-stream-whitespace:1"
                            },
                        }
                    ],
                },
            },
        )
    )
    await event_router.dispatch(
        _event(
            "assistant.output_settled",
            {**identity, "outcome": "completed"},
        )
    )

    assert "assistant.request_failed" not in [event["type"] for event in broadcasts]
    final_events = [
        event for event in broadcasts if event["type"] == "assistant.text_final"
    ]
    assert final_events[-1]["payload"]["text"] == full_text
    assert [
        message.content for message in memory_store.snapshot().recent_messages
    ] == [full_text]

@pytest.mark.asyncio
async def test_old_runtime_material_cleanup_preserves_new_runtime_lease() -> None:
    """旧世代定向清场只能释放旧 TaskResult，不能误清新任务材料。"""

    current_epoch = 1
    released: list[str] = []

    async def discard_broadcast(_event: dict[str, object]) -> None:
        return None

    def release(context_id: str) -> list[dict[str, object]]:
        released.append(context_id)
        return []

    event_router = runtime_event_router(
        publish=discard_broadcast,
        publish_ephemeral=discard_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        current_runtime_epoch=lambda: current_epoch,
        turn_material_retain_handler=lambda _context_id: True,
        turn_material_release_handler=release,
    )
    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": "task_result-old",
                "request_id": "request-old",
                "turn_material_context_id": "context-old",
            },
            runtime_epoch=1,
        )
    )
    current_epoch = 2
    await event_router.dispatch(
        _event(
            "llm.foreground.tool_call_completed",
            {
                "name": "start_background_work",
                "deliverable": True,
                "state": "accepted",
                "activity_id": "task_result-new",
                "request_id": "request-new",
                "turn_material_context_id": "context-new",
            },
            runtime_epoch=2,
        )
    )

    event_router.activity_event_handler.interrupt_task_result_deliveries(1)
    assert released == ["context-old"]

    event_router.activity_event_handler.interrupt_task_result_deliveries(2)
    assert released == ["context-old", "context-new"]
