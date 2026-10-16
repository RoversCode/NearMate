from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from companion.activities.progress_ledger import ActivityProgressLedger
from companion.activities.runtime.failure_reporting import failure_from_reason
from companion.activities.runtime.models import ActivityApplyResult
from companion.realtime.assistant_events import (
    RuntimeMemoryCompactionPlanCommand,
    TaskMemoryLookupResultCommand,
)
from companion.runtime_memory.models import TaskMemoryRecord
from companion.runtime_memory.store import RuntimeMemoryStore
from backend.tests.runtime_public_event_support import runtime_event_router


RUNTIME_EPOCH = 1
RUNTIME_ID = "00000000-0000-4000-8000-0000000000bb"

from backend.tests.activities.runtime_event_router_test_support import (
    _append_runtime_memory_messages,
    _event,
    _noop_broadcast,
)

@pytest.mark.asyncio
async def test_task_memory_lookup_uses_main_store_and_stays_internal() -> None:
    """主进程应按用途返回任务详情，召回事件和图片编码都不能公开广播。"""

    broadcasts: list[dict[str, object]] = []
    commands: list[TaskMemoryLookupResultCommand] = []

    async def broadcast(event: dict[str, object]) -> None:
        """记录任何意外进入公共协议的事件。"""

        broadcasts.append(event)

    store = RuntimeMemoryStore()
    store.record_task_terminal(
        "cognitive_task.completed",
        {
            "activity_id": "activity-a",
            "brief": "识别截图",
            "summary": "截图中有一个按钮。",
            "result_text": "按钮文字是继续。",
            "task_input_snapshot": {
                "encoded_images": ["data:image/png;base64,private-image"]
            },
        },
    )
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=store,
        task_memory_lookup_result_handler=commands.append,
    )

    await event_router.dispatch(
        _event(
            "llm.foreground.task_memory_lookup_requested",
            {
                "lookup_id": "lookup-a",
                "activity_id": "activity-a",
                "purpose": "foreground_recall",
            },
        )
    )

    assert broadcasts == []
    assert len(commands) == 1
    assert commands[0].purpose == "foreground_recall"
    assert commands[0].result["input_snapshot"] == {"encoded_image_count": 1}


@pytest.mark.asyncio
async def test_memory_commit_request_identity_mismatch_is_rejected() -> None:
    """外层事件与内嵌提交编号冲突时，主进程不得写入历史。"""

    async def broadcast(_event_payload: dict[str, object]) -> None:
        """身份校验测试不需要公共广播。"""

    store = RuntimeMemoryStore()
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=store,
    )

    await event_router.dispatch(
        _event(
            "runtime.internal_error",
            {
                "request_id": "request-current",
                "memory_commit": {
                    "commit_id": "commit-stale",
                    "request_id": "request-stale",
                    "base_memory_epoch": 0,
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "不应写入另一请求的历史",
                        }
                    ],
                },
            },
        )
    )

    assert store.snapshot().recent_messages == ()


@pytest.mark.asyncio
async def test_memory_commit_writes_assistant_reply_to_main_store() -> None:
    """主进程保存回复时不得把历史策略和结算正文泄漏到公共事件。"""

    broadcasts: list[dict[str, object]] = []

    async def broadcast(event_payload: dict[str, object]) -> None:
        """记录公开事件并检查内部提交字段随后被剥离。"""

        broadcasts.append(event_payload)

    store = RuntimeMemoryStore()
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=store,
    )

    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {
                "request_id": "request-assistant",
                "text": "主进程应该记住这条回复。",
                "settlement_text": "主进程应该记住这条回复。",
                "history_policy": "stable_dialogue",
                "activity_progress_references": [],
                "memory_commit": {
                    "commit_id": "commit-assistant",
                    "request_id": "request-assistant",
                    "base_memory_epoch": 0,
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "主进程应该记住这条回复。",
                            "metadata": {"origin_key": "request-assistant:1"},
                        }
                    ],
                },
            },
        )
    )

    snapshot = store.snapshot()
    assert [message.role for message in snapshot.recent_messages] == ["assistant"]
    assert snapshot.recent_messages[0].content == "主进程应该记住这条回复。"
    assert broadcasts == [
        {
            "type": "assistant.text_final",
                "payload": {
                    "request_id": "request-assistant",
                    "session_id": "session-test",
                    "generation_id": "generation-test",
                    "text": "主进程应该记住这条回复。",
                },
        }
    ]


@pytest.mark.asyncio
async def test_memory_commit_failure_does_not_skip_final_material_release(monkeypatch) -> None:
    """稳定记忆暂时不可写时，final 事件仍必须释放本轮材料。"""

    ephemeral_events: list[dict[str, object]] = []

    async def broadcast(_event_payload: dict[str, object]) -> None:
        return None

    async def broadcast_ephemeral(event_payload: dict[str, object]) -> None:
        ephemeral_events.append(event_payload)

    store = RuntimeMemoryStore()
    original_apply_memory_commit = store.apply_memory_commit
    apply_attempt_count = 0

    def fail_first_apply(value: object) -> tuple:
        nonlocal apply_attempt_count
        apply_attempt_count += 1
        if apply_attempt_count == 1:
            raise RuntimeError("temporary archive failure")
        return original_apply_memory_commit(value)

    monkeypatch.setattr(store, "apply_memory_commit", fail_first_apply)
    released_contexts: list[str] = []
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast_ephemeral,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=store,
        turn_material_final_handler=lambda context_id: (
            released_contexts.append(context_id)
            or [{"type": "materials.assets_released", "payload": {}}]
        ),
    )
    identity = {
        "request_id": "request-final-release",
        "generation_id": "generation-a",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
        "turn_material_context_id": "context-final-release",
    }

    await event_router.dispatch(_event(
        "assistant.text_final",
        {
            **identity,
            "text": "已经公开但记忆暂时写不进去。",
            "memory_commit": {
                "commit_id": "commit-final-release",
                "request_id": "request-final-release",
                "base_memory_epoch": 0,
                "messages": [{
                    "role": "assistant",
                    "content": "已经公开但记忆暂时写不进去。",
                }],
            },
        },
    ))

    assert released_contexts == ["context-final-release"]
    assert ephemeral_events == [
        {"type": "materials.assets_released", "payload": {}}
    ]
    assert store.snapshot().recent_messages == ()

    await event_router.dispatch(_event(
        "assistant.output_settled",
        {
            **{key: value for key, value in identity.items() if key != "turn_material_context_id"},
            "outcome": "completed",
        },
    ))

    assert [message.content for message in store.snapshot().recent_messages] == [
        "已经公开但记忆暂时写不进去。"
    ]


@pytest.mark.asyncio
async def test_old_runtime_compaction_result_releases_plan_without_applying() -> None:
    """旧助手世代迟到的摘要只能释放旧计划，不能改写当前记忆。"""

    async def broadcast(_event_payload: dict[str, object]) -> None:
        return None

    store = RuntimeMemoryStore()
    _append_runtime_memory_messages(store, count=10)
    plan = store.create_compaction_plan(
        owner_runtime_epoch=1,
        max_context_tokens=1,
        keep_recent_messages=8,
    )
    assert plan is not None
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=store,
        current_runtime_epoch=lambda: 2,
    )

    await event_router.dispatch(_event(
        "llm.foreground.runtime_memory_compaction_completed",
        {
            "memory_epoch": plan.memory_epoch,
            "plan_id": plan.plan_id,
            "source_message_id": plan.source_message_id,
            "summary": "不应生效的旧世代摘要",
        },
        runtime_epoch=1,
    ))

    assert store.snapshot().rolling_summary == ""
    assert store.create_compaction_plan(
        owner_runtime_epoch=2,
        max_context_tokens=1,
        keep_recent_messages=8,
    ) is not None


@pytest.mark.asyncio
async def test_rejected_compaction_command_immediately_releases_plan() -> None:
    """Bridge 明确拒绝控制命令时，主进程不能留下悬空压缩计划。"""

    async def broadcast(_event_payload: dict[str, object]) -> None:
        return None

    commands: list[RuntimeMemoryCompactionPlanCommand] = []

    def reject(command: RuntimeMemoryCompactionPlanCommand) -> bool:
        commands.append(command)
        return False

    store = RuntimeMemoryStore()
    _append_runtime_memory_messages(store, count=10)
    event_router = runtime_event_router(
        publish=broadcast,
        publish_ephemeral=broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=store,
        current_runtime_epoch=lambda: 3,
        runtime_memory_compaction_plan_handler=reject,
    )

    await event_router.dispatch(_event("runtime.internal_error", {"message": "trigger"}, 3))

    assert len(commands) == 1
    assert commands[0].plan.owner_runtime_epoch == 3
    assert store.create_compaction_plan(
        owner_runtime_epoch=3,
        max_context_tokens=1,
        keep_recent_messages=8,
    ) is not None

@pytest.mark.asyncio
async def test_old_runtime_output_settlement_is_applied_before_epoch_filter() -> None:
    """热替换前已公开的正文必须由旧世代 settlement 完成主进程归档。"""

    current_epoch = 1
    memory_store = RuntimeMemoryStore()
    event_router = runtime_event_router(
        publish=_noop_broadcast,
        publish_ephemeral=_noop_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=memory_store,
        current_runtime_epoch=lambda: current_epoch,
    )
    identity = {
        "request_id": "request-a",
        "generation_id": "generation-a",
        "history_policy": "stable_dialogue",
        "activity_progress_references": [],
    }

    await event_router.dispatch(
        _event("assistant.text_chunk", {**identity, "text": "已经公开的半句。"})
    )
    current_epoch = 2
    await event_router.dispatch(
        _event(
            "assistant.output_settled",
            {**identity, "outcome": "output_unavailable"},
            runtime_epoch=1,
        )
    )

    assert [
        message.content for message in memory_store.snapshot().recent_messages
    ] == ["已经公开的半句。"]


@pytest.mark.asyncio
async def test_progress_output_settlement_writes_self_awareness_runtime_memory() -> None:
    """实际公开的 Progress 同时进入自我感知历史与临时差分账本。"""

    memory_store = RuntimeMemoryStore()
    progress_ledger = ActivityProgressLedger()
    event_router = runtime_event_router(
        publish=_noop_broadcast,
        publish_ephemeral=_noop_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=memory_store,
        activity_progress_ledger=progress_ledger,
    )
    identity = {
        "request_id": "progress-a",
        "generation_id": "generation-a",
        "history_policy": "activity_progress",
        "activity_progress_references": [
            {
                "activity_id": "activity-a",
                "source": "cognitive",
                "notice_sequence": 3,
            }
        ],
    }

    await event_router.dispatch(
        _event(
            "assistant.text_final",
            {**identity, "text": "我已经核对了第一部分。"},
        )
    )
    await event_router.dispatch(
        _event(
            "assistant.output_settled",
            {**identity, "outcome": "interrupted"},
        )
    )

    memory_messages = memory_store.snapshot().recent_messages
    assert len(memory_messages) == 1
    assert memory_messages[0].content == (
        "<proactive_broadcast>\n"
        "  <![CDATA[我已经核对了第一部分。]]>\n"
        "</proactive_broadcast>"
    )
    assert memory_messages[0].metadata["proactive_broadcast"] is True
    assert progress_ledger.reports_by_activity({("cognitive", "activity-a")}) == {
        ("cognitive", "activity-a"): ("我已经核对了第一部分。",)
    }


@pytest.mark.asyncio
async def test_activity_terminal_freezes_progress_before_writing_task_memory() -> None:
    """来源终态必须先移出 active Progress，再写入可触发 Notice 的任务记忆。"""

    timeline: list[str] = []

    class RecordingProgressLedger(ActivityProgressLedger):
        """记录 Router 调用 Progress 冻结的准确时机。"""

        def freeze_from_runtime_event(
            self,
            event_type: str,
            payload: dict[str, object],
        ) -> bool:
            """记录冻结动作后执行真实账本逻辑。

            Args:
                event_type: Activity 来源事件名。
                payload: Activity 来源事件载荷。

            Returns:
                真实账本是否推进了冻结状态。
            """

            timeline.append("progress_frozen")
            return super().freeze_from_runtime_event(event_type, payload)

    class RecordingRuntimeMemoryStore(RuntimeMemoryStore):
        """记录任务记忆写入的准确时机。"""

        def record_task_terminal(
            self,
            event_type: str,
            payload: dict[str, object],
        ) -> TaskMemoryRecord | None:
            """记录任务记忆动作后执行真实账本逻辑。

            Args:
                event_type: Activity 来源终态事件名。
                payload: Activity 来源终态载荷。

            Returns:
                真实账本首次创建的任务记录或空值。
            """

            timeline.append("task_memory_recorded")
            return super().record_task_terminal(event_type, payload)

    progress_ledger = RecordingProgressLedger()
    memory_store = RecordingRuntimeMemoryStore()
    event_router = runtime_event_router(
        publish=_noop_broadcast,
        publish_ephemeral=_noop_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=memory_store,
        activity_progress_ledger=progress_ledger,
    )

    await event_router.dispatch(
        _event(
            "cognitive_task.failed",
            {
                "activity_id": str(uuid4()),
                "brief": "核对终态顺序",
                "failure_reason": "现有方法无法完成。",
                "failure": failure_from_reason(
                    "cognitive_task_failed",
                    stage_code="generating_result",
                ).to_payload(),
            },
        )
    )

    assert timeline[:2] == ["progress_frozen", "task_memory_recorded"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_reason",
    [None, "api_key=secret-value"],
)
async def test_invalid_cognitive_failure_is_rejected_before_main_process_writes(
    monkeypatch: pytest.MonkeyPatch,
    failure_reason: str | None,
) -> None:
    """不合法的 Cognitive 原因不能先污染主进程账本再由 Activity 拒绝。

    Args:
        monkeypatch: 临时替换主进程账本写入方法的测试工具。
        failure_reason: 本次验证的缺失或不安全失败原因。
    """

    progress_ledger = ActivityProgressLedger()
    memory_store = RuntimeMemoryStore()
    progress_write = Mock(wraps=progress_ledger.freeze_from_runtime_event)
    memory_observation = Mock(wraps=memory_store.observe_activity_event)
    task_memory_write = Mock(wraps=memory_store.record_task_terminal)
    registry_write = Mock(
        return_value=ActivityApplyResult(disposition="absorbed")
    )

    monkeypatch.setattr(
        progress_ledger,
        "freeze_from_runtime_event",
        progress_write,
    )
    monkeypatch.setattr(
        memory_store,
        "observe_activity_event",
        memory_observation,
    )
    monkeypatch.setattr(
        memory_store,
        "record_task_terminal",
        task_memory_write,
    )
    event_router = runtime_event_router(
        publish=_noop_broadcast,
        publish_ephemeral=_noop_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        runtime_memory_store=memory_store,
        activity_progress_ledger=progress_ledger,
        activity_apply_handler=registry_write,
        activity_broadcast_handler=_noop_broadcast,
    )

    with pytest.raises(ValueError, match="Cognitive 自主失败缺少安全的具体原因"):
        await event_router.dispatch(
            _event(
                "cognitive_task.failed",
                {
                    "activity_id": str(uuid4()),
                    "brief": "核对敏感失败原因",
                    "failure_reason": failure_reason,
                    "failure": failure_from_reason(
                        "cognitive_task_failed",
                        stage_code="generating_result",
                    ).to_payload(),
                },
            )
        )

    memory_observation.assert_not_called()
    progress_write.assert_not_called()
    task_memory_write.assert_not_called()
    registry_write.assert_not_called()
