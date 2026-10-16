from __future__ import annotations

import threading
import time

import pytest

from companion.activities.runtime.registry import ActivityNotFound, ActivityStateConflict
from companion.activities.runtime.service import ActivityRuntimeUnavailable
from companion.foreground_tools.activity_control.broker import (
    ActivityCancellationBroker,
)
from companion.foreground_tools.activity_control.execution import (
    ActivityCancellationToolExecutor,
)
from companion.foreground_tools.activity_control.policy import (
    CancellableActivityContext,
    select_cancellable_activity_contexts,
)
from companion.foreground_tools.activity_control.schemas import (
    activity_control_tool_schemas,
)
from companion.foreground_tools.orchestration.contracts import (
    DeferredActionPrepared,
    ExecutedToolCall,
    ForegroundToolExecution,
    ToolFollowupFact,
    ToolReplyRequirement,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from companion.prompt.activity_prompt import render_active_activities_block
from companion.prompt.capabilities import (
    compile_foreground_capabilities,
    render_foreground_protocol,
)
from companion.prompt.tool_followup import render_tool_followup_fallback
from companion.prompt.tool_followup_policy import (
    ToolFollowupMode,
    ToolFollowupPlan,
    VisibleTransitionKind,
)
from companion.realtime.activity_runtime_event_handler import (
    ActivityRuntimeEventHandler,
)
from companion.realtime.assistant_child_runtime import AssistantChildRuntime
from companion.realtime.assistant_events import (
    ActivityCancellationResultCommand,
    RuntimeEvent,
)


def _activity(
    activity_id: str,
    *,
    source: str = "background_agent",
    session_id: str = "session-a",
    state: str = "running",
    phase: str = "reasoning",
    goal: str = "整理报告",
) -> dict[str, object]:
    """构造一条用于候选筛选的最小 Activity 快照。

    Args:
        activity_id: 测试 Activity 编号。
        source: Cognitive 或后台工作来源。
        session_id: Activity 所属交互会话。
        state: 当前生命周期状态。
        phase: 当前执行阶段。
        goal: 当前任务目标。

    Returns:
        可直接交给取消候选策略的字典。
    """

    return {
        "activity_id": activity_id,
        "source": source,
        "interaction_session_id": session_id,
        "state": state,
        "phase": phase,
        "goal": goal,
        "category": "后台工作",
    }


def _context(
    activity_id: str = "activity-a",
    *,
    goal: str = "整理报告",
) -> CancellableActivityContext:
    """构造一项已冻结的可取消现场。"""

    return CancellableActivityContext(
        activity_id=activity_id,
        source="background_agent",
        interaction_session_id="session-a",
        state="running",
        phase="reasoning",
        goal=goal,
    )


def test_cancellable_policy_matches_registry_states_and_current_session() -> None:
    """候选必须与 Registry 的可取消状态一致，并严格限制在当前会话。"""

    contexts = select_cancellable_activity_contexts(
        [
            _activity("running"),
            _activity("waiting", state="waiting_user"),
            _activity("finalizing", phase="finalizing"),
            _activity("attention", state="attention"),
            _activity("cancelling", state="cancelling"),
            _activity("completed", state="completed"),
            _activity("cross-session", session_id="session-b"),
            _activity("unsupported", source="unknown"),
            _activity("running"),
        ],
        interaction_session_id="session-a",
    )

    assert [context.activity_id for context in contexts] == [
        "running",
        "waiting",
        "finalizing",
    ]


def test_cancel_tool_is_absent_without_candidates_and_uses_exact_enum() -> None:
    """动态 schema 只能枚举本轮已经冻结的候选编号。"""

    assert activity_control_tool_schemas(()) == []

    schemas = activity_control_tool_schemas(
        (_context("activity-a"), _context("activity-b"))
    )
    parameters = schemas[0]["function"]["parameters"]  # type: ignore[index]

    assert schemas[0]["function"]["name"] == "cancel_activity"  # type: ignore[index]
    assert parameters["properties"]["activity_id"]["enum"] == [  # type: ignore[index]
        "activity-a",
        "activity-b",
    ]


def test_prompt_requires_clarification_only_when_multiple_candidates_exist() -> None:
    """单候选可直接停止，多候选无法唯一指代时必须先澄清。"""

    single = render_active_activities_block(
        {"active_activities": [_activity("activity-a")]},
        cancellable_activities=(_context("activity-a"),),
    )
    multiple = render_active_activities_block(
        {
            "active_activities": [
                _activity("activity-a"),
                _activity("activity-b", goal="核对版本"),
            ]
        },
        cancellable_activities=(
            _context("activity-a"),
            _context("activity-b", goal="核对版本"),
        ),
    )
    protocol = render_foreground_protocol(
        compile_foreground_capabilities(["cancel_activity"])
    )

    assert "唯一可取消的 Activity" in single
    assert "多个可取消 Activity" in multiple
    assert "指代含糊时必须先澄清" in multiple
    assert "取消和纠正不是一回事" in protocol
    assert "绝不能说任务‘已经取消’" in protocol
    assert "带有 cancellation_policy 说明" in protocol
    assert "停止说话或正在播放的播报" in protocol


class _RecordingCancellationBroker:
    """记录 executor 是否在提交边界前错误发送取消请求。"""

    def __init__(self) -> None:
        self.submissions: list[tuple[str, str]] = []

    def create_cancellation_id(self) -> str:
        """返回固定取消请求编号。"""

        return "cancel-a"

    def submit_and_wait(
        self,
        *,
        cancellation_id: str,
        activity_id: str,
    ) -> dict[str, object]:
        """记录正式提交，并返回主进程已接收事实。"""

        self.submissions.append((cancellation_id, activity_id))
        return {
            "state": "cancellation_requested",
            "activity_id": activity_id,
            "reason": "accepted",
            "safe_message": "取消请求已接收，任务正在停止。",
        }


def test_cancellation_side_effect_runs_only_after_tool_event_commit() -> None:
    """prepare、accept 和事件入队都不能提前停止任务，只有 after_commit 可以。"""

    broker = _RecordingCancellationBroker()
    executor = ActivityCancellationToolExecutor(broker)  # type: ignore[arg-type]
    result = executor.prepare(
        tool_call=ForegroundToolCall(
            tool_call_id="call-a",
            name="cancel_activity",
            arguments={"activity_id": "activity-a"},
        ),
        cancellable_activities=(_context(),),
    )
    reservation = executor.take_reservation(str(result["resource_id"]))

    assert reservation is not None
    assert broker.submissions == []
    assert reservation.action.accept() is True
    assert reservation.action.commit_enqueued() is True
    assert broker.submissions == []
    assert reservation.action.run_after_commit() is True
    assert broker.submissions == [("cancel-a", "activity-a")]
    assert reservation.action.after_commit_fact == ToolFollowupFact(
        kind="activity_cancellation_requested",
        activity_id="activity-a",
        goal="整理报告",
    )


def test_pre_commit_rollback_never_submits_cancellation() -> None:
    """Foreground generation 在提交前中断时只能回滚事务，不能误停 Activity。"""

    broker = _RecordingCancellationBroker()
    executor = ActivityCancellationToolExecutor(broker)  # type: ignore[arg-type]
    result = executor.prepare(
        tool_call=ForegroundToolCall(
            tool_call_id="call-a",
            name="cancel_activity",
            arguments={"activity_id": "activity-a"},
        ),
        cancellable_activities=(_context(),),
    )

    assert executor.rollback_prepared(str(result["resource_id"])) is True
    assert broker.submissions == []


def test_foreground_attaches_prepared_cancellation_to_tool_commit_event() -> None:
    """Foreground 编排器必须领取取消事务，并从 after_commit 事实生成工具二轮输入。"""

    broker = _RecordingCancellationBroker()
    executor = ActivityCancellationToolExecutor(broker)  # type: ignore[arg-type]
    prepared = executor.prepare(
        tool_call=ForegroundToolCall(
            tool_call_id="call-a",
            name="cancel_activity",
            arguments={"activity_id": "activity-a"},
        ),
        cancellable_activities=(_context(),),
    )
    executed_call = ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="call-a",
            name="cancel_activity",
            arguments={"activity_id": "activity-a"},
        ),
        outcome=DeferredActionPrepared(
            resource_id=str(prepared["resource_id"]),
            payload=dict(prepared),
        ),
        reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
        channel="deferred",
    )
    processor = object.__new__(ForegroundTurnProcessor)
    processor._activity_cancellation_tool_executor = executor

    reservation = processor._activity_cancellation_reservation(
        executed_call=executed_call,
        existing_keys=set(),
    )

    assert reservation is not None
    assert reservation.action.accept() is True
    assert reservation.action.commit_enqueued() is True
    assert reservation.action.run_after_commit() is True
    facts = processor._tool_followup_facts(
        execution=ForegroundToolExecution(calls=(executed_call,)),
        reservations=[reservation],
    )
    assert facts == (
        ToolFollowupFact(
            kind="activity_cancellation_requested",
            activity_id="activity-a",
            goal="整理报告",
        ),
    )


def test_cancellation_broker_waits_for_matching_main_process_receipt() -> None:
    """Broker 只有收到编号和 Activity 都匹配的回执后才能报告成功。"""

    broker = ActivityCancellationBroker(
        timeout_seconds=1.0,
        cancellation_id_factory=lambda: "unused",
    )
    result: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: result.append(
            broker.submit_and_wait(
                cancellation_id="cancel-a",
                activity_id="activity-a",
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
    assert events[0].payload == {
        "cancellation_id": "cancel-a",
        "activity_id": "activity-a",
    }
    assert broker.apply_result(
        ActivityCancellationResultCommand(
            cancellation_id="cancel-a",
            activity_id="activity-a",
            state="cancellation_requested",
            reason="accepted",
            safe_message="取消请求已接收，任务正在停止。",
        )
    ) is True

    worker.join(timeout=1.0)
    assert worker.is_alive() is False
    assert result == [
        {
            "state": "cancellation_requested",
            "activity_id": "activity-a",
            "reason": "accepted",
            "safe_message": "取消请求已接收，任务正在停止。",
        }
    ]


def test_cancellation_broker_timeout_never_claims_task_was_cancelled() -> None:
    """回执超时只能报告未确认，不能用口头成功承诺补偿。"""

    broker = ActivityCancellationBroker(timeout_seconds=0.01)

    result = broker.submit_and_wait(
        cancellation_id="cancel-a",
        activity_id="activity-a",
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "cancellation_receipt_timeout"
    assert "已经取消" not in str(result["safe_message"])


def test_tool_followup_says_stopping_until_terminal_notice_arrives() -> None:
    """取消工具二轮只能说正在停止，不能抢跑 cancelled 终态提醒。"""

    call = ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="call-a",
            name="cancel_activity",
            arguments={"activity_id": "activity-a"},
        ),
        outcome=DeferredActionPrepared(
            resource_id="cancel-a",
            payload={
                "state": "accepted",
                "resource_id": "cancel-a",
                "activity_id": "activity-a",
            },
        ),
        reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
        channel="deferred",
    )
    plan = ToolFollowupPlan(
        mode=ToolFollowupMode.RESULT_ONLY,
        visible_transition_kind=VisibleTransitionKind.NONE,
        tool_followup_facts=(
            ToolFollowupFact(
                kind="activity_cancellation_requested",
                activity_id="activity-a",
                goal="整理报告",
            ),
        ),
    )

    rendered = render_tool_followup_fallback(plan, (call,))

    assert "取消请求已接收，正在停止" in rendered
    assert "已经取消" not in rendered


async def test_main_handler_returns_authoritative_cancellation_receipt() -> None:
    """主进程必须先执行权威取消，再向原始运行时世代返回接收事实。"""

    cancelled: list[str] = []
    receipts: list[tuple[ActivityCancellationResultCommand, int]] = []

    async def publish(_event: dict[str, object]) -> None:
        """吸收本测试不关心的公开事件。"""

    async def cancel(activity_id: str) -> None:
        """记录主进程权威取消端口收到的 Activity。"""

        cancelled.append(activity_id)

    handler = ActivityRuntimeEventHandler(
        publish_activity=publish,
        publish_activity_ephemeral=publish,
        publish_materials_ephemeral=publish,
        activity_cancellation_handler=cancel,
        activity_cancellation_result_handler=lambda command, epoch: receipts.append(
            (command, epoch)
        ),
    )

    assert await handler.try_handle(_cancellation_event()) is True

    assert cancelled == ["activity-a"]
    assert receipts[0][1] == 7
    assert receipts[0][0].state == "cancellation_requested"
    assert receipts[0][0].safe_message == "取消请求已接收，任务正在停止。"


@pytest.mark.parametrize(
    ("exception", "expected_reason"),
    [
        (ActivityNotFound("missing"), "activity_not_found"),
        (ActivityStateConflict("terminal"), "activity_state_conflict"),
        (ActivityRuntimeUnavailable("offline"), "activity_runtime_unavailable"),
    ],
)
async def test_main_handler_maps_cancellation_races_to_safe_receipts(
    exception: Exception,
    expected_reason: str,
) -> None:
    """任务已结束、不可取消或运行时不可用时都必须返回稳定拒绝事实。"""

    receipts: list[ActivityCancellationResultCommand] = []

    async def publish(_event: dict[str, object]) -> None:
        """吸收本测试不关心的公开事件。"""

    async def cancel(_activity_id: str) -> None:
        """抛出当前参数指定的权威取消竞态。"""

        raise exception

    handler = ActivityRuntimeEventHandler(
        publish_activity=publish,
        publish_activity_ephemeral=publish,
        publish_materials_ephemeral=publish,
        activity_cancellation_handler=cancel,
        activity_cancellation_result_handler=lambda command, _epoch: receipts.append(
            command
        ),
    )

    assert await handler.try_handle(_cancellation_event()) is True

    assert receipts[0].state == "rejected"
    assert receipts[0].reason == expected_reason
    assert "已经取消" not in receipts[0].safe_message


def test_child_runtime_applies_cancellation_receipt_to_broker() -> None:
    """父进程控制命令必须精确唤醒子进程内的取消 Broker。"""

    commands: list[ActivityCancellationResultCommand] = []

    class RecordingBroker:
        """记录子进程交给 Broker 的取消回执。"""

        def apply_result(self, command: ActivityCancellationResultCommand) -> bool:
            """保存命令并报告已接收。"""

            commands.append(command)
            return True

    runtime = object.__new__(AssistantChildRuntime)
    runtime._activity_cancellation_broker = RecordingBroker()
    command = ActivityCancellationResultCommand(
        cancellation_id="cancel-a",
        activity_id="activity-a",
        state="rejected",
        reason="activity_state_conflict",
        safe_message="这项任务已经结束、正在取消或当前不能取消。",
    )

    runtime.handle_control_command(command)

    assert commands == [command]


def _cancellation_event() -> RuntimeEvent:
    """构造一条已经越过 Foreground 工具提交边界的取消请求。"""

    return RuntimeEvent(
        type="llm.foreground.activity_cancellation_requested",
        source="assistant_process",
        payload={
            "cancellation_id": "cancel-a",
            "activity_id": "activity-a",
        },
        runtime_epoch=7,
    )
