from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from companion.foreground_tools.orchestration.contracts import (
    DeferredActionPrepared,
    ExecutedToolCall,
    ForegroundToolExecution,
    ToolFollowupFact,
    ToolReplyRequirement,
)
from companion.foreground_tools.timed_reminders.broker import (
    TimedReminderCommandBroker,
)
from companion.foreground_tools.timed_reminders.execution import (
    TimedReminderToolExecutor,
)
from companion.foreground_tools.timed_reminders.schemas import (
    timed_reminder_tool_schemas,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from companion.prompt.tool_followup import render_tool_followup_fallback
from companion.prompt.tool_followup_policy import (
    ToolFollowupMode,
    ToolFollowupPlan,
    VisibleTransitionKind,
)
from companion.realtime.assistant_child_runtime import AssistantChildRuntime
from companion.realtime.assistant_events import (
    RuntimeEvent,
    TimedReminderCommandResultCommand,
)
from companion.realtime.assistant_runtime_event_router import (
    classify_runtime_event_owner,
)
from companion.realtime.timed_reminder_runtime_event_handler import (
    TimedReminderRuntimeEventHandler,
)
from companion.timed_reminders.repository import TimedReminderRepository
from companion.timed_reminders.service import TimedReminderService


def _reminder(
    reminder_id: str = "reminder-a",
    *,
    state: str = "scheduled",
) -> dict[str, object]:
    """构造一条 Foreground 工具使用的最小提醒快照。

    Args:
        reminder_id: 当前运行期提醒编号。
        state: 等待中或提醒中的生命周期状态。

    Returns:
        带真实修改和取消能力标记的快照字典。
    """

    return {
        "reminder_id": reminder_id,
        "content": "参加面试",
        "state": state,
        "trigger_at": "2026-09-03T07:00:00+00:00",
        "can_modify": state == "scheduled",
        "can_cancel": state == "scheduled",
        "can_dismiss": state == "ringing",
    }


def test_schema_always_exposes_create_and_freezes_manageable_ids() -> None:
    """创建能力始终存在，管理分支只能枚举本轮真实可操作编号。"""

    assert [schema["function"]["name"] for schema in timed_reminder_tool_schemas(())] == [  # type: ignore[index]
        "create_timed_reminder"
    ]

    schemas = timed_reminder_tool_schemas(
        (_reminder("scheduled"), _reminder("ringing", state="ringing"))
    )
    manage_parameters = schemas[1]["function"]["parameters"]  # type: ignore[index]
    update_schema, cancel_schema, dismiss_schema = manage_parameters["oneOf"]  # type: ignore[index]

    assert update_schema["properties"]["reminder_id"]["enum"] == ["scheduled"]
    assert cancel_schema["properties"]["reminder_id"]["enum"] == ["scheduled"]
    assert dismiss_schema["properties"]["reminder_ids"]["items"]["enum"] == [
        "ringing"
    ]


def test_manage_schema_parameters_are_objects_for_every_manageable_state() -> None:
    """验证任何可管理提醒组合都生成对象类型的函数参数。

    Returns:
        无。
    """

    reminder_cases = (
        (_reminder("scheduled"),),
        (_reminder("ringing", state="ringing"),),
        (_reminder("scheduled"), _reminder("ringing", state="ringing")),
    )

    for timed_reminders in reminder_cases:
        schemas = timed_reminder_tool_schemas(timed_reminders)
        manage_schema = next(
            schema
            for schema in schemas
            if schema["function"]["name"] == "manage_timed_reminder"  # type: ignore[index]
        )

        assert manage_schema["function"]["parameters"]["type"] == "object"  # type: ignore[index]


class _RecordingReminderBroker:
    """记录 executor 是否在工具提交边界前误发提醒命令。"""

    def __init__(self) -> None:
        self.submissions: list[tuple[str, str, dict[str, object]]] = []

    def create_command_id(self) -> str:
        """返回固定命令编号。"""

        return "command-a"

    def submit_and_wait(
        self,
        *,
        command_id: str,
        operation: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        """记录正式提交，并返回主进程已经创建的 canonical 事实。"""

        self.submissions.append((command_id, operation, dict(arguments)))
        return {
            "state": "created",
            "reminder": {
                "reminder_id": "reminder-a",
                "content": "参加面试",
                "trigger_at": "2026-09-03T07:00:00+00:00",
            },
            "crosses_local_day": False,
            "runtime_only": True,
        }


def test_create_side_effect_runs_only_after_tool_event_commit() -> None:
    """prepare 和事件接收都不能提前创建提醒，只有 after_commit 可以。"""

    broker = _RecordingReminderBroker()
    executor = TimedReminderToolExecutor(broker)  # type: ignore[arg-type]
    result = executor.prepare(
        tool_call=ForegroundToolCall(
            tool_call_id="call-a",
            name="create_timed_reminder",
            arguments={"content": "参加面试", "delay_seconds": 1800},
        ),
        timed_reminders=(),
    )
    reservation = executor.take_reservation(str(result["resource_id"]))

    assert reservation is not None
    assert broker.submissions == []
    assert reservation.action.accept() is True
    assert reservation.action.commit_enqueued() is True
    assert broker.submissions == []
    assert reservation.action.run_after_commit() is True
    assert broker.submissions == [
        (
            "command-a",
            "create",
            {"content": "参加面试", "delay_seconds": 1800},
        )
    ]
    assert reservation.action.after_commit_fact == ToolFollowupFact(
        kind="timed_reminder_created",
        command_id="command-a",
        reminder_id="reminder-a",
        reminder_content="参加面试",
        trigger_at="2026-09-03T07:00:00+00:00",
        runtime_only=True,
    )


def test_manage_uses_frozen_state_and_rejects_ringing_update() -> None:
    """已经到点的提醒只能关闭，不能绕过冻结状态再修改。"""

    executor = TimedReminderToolExecutor(_RecordingReminderBroker())  # type: ignore[arg-type]

    result = executor.prepare(
        tool_call=ForegroundToolCall(
            tool_call_id="call-a",
            name="manage_timed_reminder",
            arguments={
                "action": "update",
                "reminder_id": "reminder-a",
                "delay_seconds": 60,
            },
        ),
        timed_reminders=(_reminder(state="ringing"),),
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "timed_reminder_not_manageable_in_current_turn"


def test_reminder_broker_waits_for_matching_main_process_receipt() -> None:
    """Broker 只有收到命令编号和动作都匹配的回执后才报告成功。"""

    broker = TimedReminderCommandBroker(
        timeout_seconds=1.0,
        command_id_factory=lambda: "unused",
    )
    result: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: result.append(
            broker.submit_and_wait(
                command_id="command-a",
                operation="create",
                arguments={"content": "喝水", "delay_seconds": 60},
            )
        ),
        daemon=True,
    )
    worker.start()

    events = []
    for _ in range(100):
        events = broker.drain_events()
        if events:
            break
        time.sleep(0.005)
    assert events[0].payload["command_id"] == "command-a"
    assert broker.apply_result(
        TimedReminderCommandResultCommand(
            command_id="command-a",
            operation="create",
            result={"state": "created", "reminder": {"reminder_id": "r-a"}},
        )
    ) is True

    worker.join(timeout=1.0)
    assert worker.is_alive() is False
    assert result == [{"state": "created", "reminder": {"reminder_id": "r-a"}}]


async def test_main_handler_creates_canonical_reminder_and_replies_to_origin_epoch() -> None:
    """主进程必须先写入权威仓储，再向原始助手世代返回创建事实。"""

    fixed_now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    published: list[dict[str, object]] = []
    receipts: list[tuple[TimedReminderCommandResultCommand, int]] = []

    async def publish(event: dict[str, object]) -> None:
        """记录提醒快照广播。"""

        published.append(event)

    service = TimedReminderService(
        repository=TimedReminderRepository(
            runtime_id="runtime-a",
            reminder_id_factory=lambda: "reminder-a",
        ),
        broadcast_snapshot=publish,
        now=lambda: fixed_now,
    )
    handler = TimedReminderRuntimeEventHandler(
        service=service,
        result_handler=lambda command, epoch: receipts.append((command, epoch)),
    )

    await handler.handle(
        RuntimeEvent(
            type="llm.foreground.timed_reminder_command_requested",
            source="assistant_process",
            payload={
                "command_id": "command-a",
                "operation": "create",
                "arguments": {"content": "参加面试", "delay_seconds": 1800},
            },
            runtime_epoch=7,
        )
    )

    assert service.snapshot().active[0].reminder_id == "reminder-a"
    assert published[-1]["type"] == "reminders.snapshot_changed"
    assert receipts[0][1] == 7
    assert receipts[0][0].result["state"] == "created"
    assert receipts[0][0].result["runtime_only"] is True


async def test_main_handler_dismisses_batch_without_single_reminder_id() -> None:
    """批次关闭只能读取 runtime_id 和 reminder_ids，不能误要单项编号。"""

    current_time = [datetime(2026, 9, 3, 6, 0, tzinfo=UTC)]
    receipts: list[tuple[TimedReminderCommandResultCommand, int]] = []

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        return None

    service = TimedReminderService(
        repository=TimedReminderRepository(
            runtime_id="runtime-a",
            reminder_id_factory=lambda: "reminder-a",
        ),
        broadcast_snapshot=ignore_broadcast,
        now=lambda: current_time[0],
    )
    created = await service.create(content="参加面试", delay_seconds=1)
    current_time[0] = datetime(2026, 9, 3, 6, 0, 1, tzinfo=UTC)
    await service.activate_due()
    handler = TimedReminderRuntimeEventHandler(
        service=service,
        result_handler=lambda command, epoch: receipts.append((command, epoch)),
    )

    await handler.handle(
        RuntimeEvent(
            type="llm.foreground.timed_reminder_command_requested",
            source="assistant_process",
            payload={
                "command_id": "command-dismiss",
                "operation": "dismiss",
                "arguments": {
                    "runtime_id": "runtime-a",
                    "reminder_ids": [created.reminder.reminder_id],
                },
            },
            runtime_epoch=8,
        )
    )

    assert receipts[0][1] == 8
    assert receipts[0][0].result == {
        "state": "dismissed",
        "dismissed_ids": ["reminder-a"],
        "dismissed_contents": ["参加面试"],
        "runtime_only": True,
    }
    assert service.snapshot().active == ()
    assert service.snapshot().recent[0].finish_reason == "user_dismissed"


def test_router_assigns_reminder_command_to_independent_owner() -> None:
    """提醒写命令必须由独立 owner 处理，不能混入 Activity。"""

    owner = classify_runtime_event_owner(
        RuntimeEvent(
            type="llm.foreground.timed_reminder_command_requested",
            source="assistant_process",
            payload={},
            runtime_epoch=1,
        )
    )

    assert owner == "timed_reminder_command"


def test_child_runtime_applies_reminder_receipt_to_broker() -> None:
    """父进程控制命令必须精确唤醒子进程内的提醒 Broker。"""

    commands: list[TimedReminderCommandResultCommand] = []

    class RecordingBroker:
        """记录子进程交给 Broker 的提醒回执。"""

        def apply_result(self, command: TimedReminderCommandResultCommand) -> bool:
            """保存命令并报告已接收。"""

            commands.append(command)
            return True

    runtime = object.__new__(AssistantChildRuntime)
    runtime._timed_reminder_command_broker = RecordingBroker()
    command = TimedReminderCommandResultCommand(
        command_id="command-a",
        operation="cancel",
        result={"state": "cancelled"},
    )

    runtime.handle_control_command(command)

    assert commands == [command]


def test_foreground_pairs_after_commit_fact_by_command_id() -> None:
    """提醒最终事实必须按命令编号回到原工具调用，而不是借用 Activity 编号。"""

    broker = _RecordingReminderBroker()
    executor = TimedReminderToolExecutor(broker)  # type: ignore[arg-type]
    prepared = executor.prepare(
        tool_call=ForegroundToolCall(
            tool_call_id="call-a",
            name="create_timed_reminder",
            arguments={"content": "参加面试", "delay_seconds": 1800},
        ),
        timed_reminders=(),
    )
    call = ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="call-a",
            name="create_timed_reminder",
            arguments={"content": "参加面试", "delay_seconds": 1800},
        ),
        outcome=DeferredActionPrepared(
            resource_id=str(prepared["resource_id"]),
            payload=dict(prepared),
        ),
        reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
        channel="deferred",
    )
    processor = object.__new__(ForegroundTurnProcessor)
    processor._timed_reminder_tool_executor = executor
    reservation = processor._timed_reminder_reservation(
        executed_call=call,
        existing_keys=set(),
    )

    assert reservation is not None
    reservation.action.accept()
    reservation.action.commit_enqueued()
    reservation.action.run_after_commit()
    facts = processor._tool_followup_facts(
        execution=ForegroundToolExecution(calls=(call,)),
        reservations=[reservation],
    )

    assert facts[0].kind == "timed_reminder_created"
    assert facts[0].command_id == "command-a"


def test_fallback_confirms_cross_day_runtime_limit_without_internal_id() -> None:
    """跨日提醒兜底必须确认时间并说明重启失效，但不能读出内部编号。"""

    call = ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="call-a",
            name="create_timed_reminder",
            arguments={"content": "参加面试", "delay_seconds": 1800},
        ),
        outcome=DeferredActionPrepared(
            resource_id="command-a",
            payload={"state": "accepted", "resource_id": "command-a"},
        ),
        reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
        channel="deferred",
    )
    plan = ToolFollowupPlan(
        mode=ToolFollowupMode.RESULT_ONLY,
        visible_transition_kind=VisibleTransitionKind.NONE,
        tool_followup_facts=(
            ToolFollowupFact(
                kind="timed_reminder_created",
                command_id="command-a",
                reminder_id="reminder-secret",
                reminder_content="参加面试",
                trigger_at="2026-09-04T07:00:00+00:00",
                crosses_local_day=True,
                runtime_only=True,
            ),
        ),
    )

    rendered = render_tool_followup_fallback(plan, (call,))

    assert "参加面试" in rendered
    assert "程序重启后会消失" in rendered
    assert "reminder-secret" not in rendered


def test_followup_prompt_confirms_dismissed_batch_without_internal_ids() -> None:
    """工具二轮的批次关闭事实必须使用实际事项而不是内部编号。"""

    call = ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="call-dismiss",
            name="manage_timed_reminder",
            arguments={"action": "dismiss", "reminder_ids": ["secret"]},
        ),
        outcome=DeferredActionPrepared(
            resource_id="command-dismiss",
            payload={"state": "accepted", "resource_id": "command-dismiss"},
        ),
        reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
        channel="deferred",
    )
    plan = ToolFollowupPlan(
        mode=ToolFollowupMode.RESULT_ONLY,
        visible_transition_kind=VisibleTransitionKind.NONE,
        tool_followup_facts=(
            ToolFollowupFact(
                kind="timed_reminders_dismissed",
                command_id="command-dismiss",
                reminder_contents=("参加面试",),
            ),
        ),
    )

    rendered = render_tool_followup_fallback(plan, (call,))

    assert "参加面试" in rendered
    assert "已经关闭" in rendered
    assert "secret" not in rendered


def test_fallback_confirms_canonical_dismissed_contents() -> None:
    """批次关闭兜底必须表达实际关闭事项，不能误判为结果缺失。"""

    call = ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="call-dismiss",
            name="manage_timed_reminder",
            arguments={
                "action": "dismiss",
                "reminder_ids": ["reminder-secret"],
            },
        ),
        outcome=DeferredActionPrepared(
            resource_id="command-dismiss",
            payload={"state": "accepted", "resource_id": "command-dismiss"},
        ),
        reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
        channel="deferred",
    )
    plan = ToolFollowupPlan(
        mode=ToolFollowupMode.RESULT_ONLY,
        visible_transition_kind=VisibleTransitionKind.NONE,
        tool_followup_facts=(
            ToolFollowupFact(
                kind="timed_reminders_dismissed",
                command_id="command-dismiss",
                reminder_contents=("参加面试", "关掉烤箱"),
                runtime_only=True,
            ),
        ),
    )

    rendered = render_tool_followup_fallback(plan, (call,))

    assert "参加面试" in rendered
    assert "关掉烤箱" in rendered
    assert "已经关闭" in rendered
    assert "reminder-secret" not in rendered
