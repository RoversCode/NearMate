from __future__ import annotations

from queue import Queue
import threading
from types import SimpleNamespace

import httpx
import pytest

from companion.foreground_tools.orchestration.transactions import (
    BackgroundActivityReservation,
    CommitAction,
)
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.foreground_tools.activity_correction.schemas import (
    CORRECT_BACKGROUND_WORK_TOOL,
)
from companion.foreground_tools.orchestration.contracts import (
    BackgroundActivityPrepared,
    ExecutedToolCall,
    ToolFollowupFact,
    ToolReplyRequirement,
)
from companion.llm.foreground_models import ForegroundEvent, ForegroundToolCall
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from companion.llm.stream_lifecycle import iter_cancellable_stream
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.cancellation import CancellationToken
from companion.realtime.generation_supervisor import GenerationSupervisor
from companion.session.context import SessionContext


def _supervisor() -> tuple[GenerationSupervisor, Queue[RuntimeEvent], SimpleNamespace]:
    """构造可接收内部事务事件的当前 generation。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    supervisor = GenerationSupervisor(
        request_queue=Queue(),
        auxiliary_request_queue=Queue(),
        tts_queue=Queue(),
        event_queue=event_queue,
        now=lambda: 1.0,
    )
    context = SimpleNamespace(
        cancellation_event=CancellationToken(),
        request=SimpleNamespace(
            request_id="request-a",
            source="text_input",
            source_turn_id=None,
            turn_material_context_id=None,
            history_policy="stable_dialogue",
            activity_progress_references=(),
            derived_from_activity_ids=(),
        ),
        source_session_id="session-a",
        generation_id="generation-a",
        lane="primary",
    )
    supervisor._current_by_lane["primary"] = context
    return supervisor, event_queue, context


def test_current_event_commits_action_before_becoming_public() -> None:
    """当前 generation 接受事件时应提交副作用且不公开事务对象。"""

    supervisor, event_queue, context = _supervisor()
    transitions: list[str] = []
    action = CommitAction(on_accept=lambda: transitions.append("committed"))

    accepted = supervisor.put_foreground_event_if_current(
        context,
        ForegroundEvent(
            type="llm.foreground.tool_call_completed",
            payload={"state": "accepted"},
            commit_action=action,
        ),
    )

    assert accepted is True
    assert transitions == ["committed"]
    assert action.state == "committed"
    assert event_queue.get_nowait().payload == {
        "state": "accepted",
        "request_id": "request-a",
    }


def test_stale_event_rolls_back_action_exactly_once() -> None:
    """过期 generation 的事务必须回滚且不能进入事件队列。"""

    supervisor, event_queue, context = _supervisor()
    transitions: list[str] = []
    action = CommitAction(
        on_accept=lambda: transitions.append("committed"),
        on_rollback=lambda: transitions.append("rolled_back"),
    )
    context.cancellation_event.set("user_barge_in")

    for _ in range(2):
        accepted = supervisor.put_foreground_event_if_current(
            context,
            ForegroundEvent(type="tool", payload={}, commit_action=action),
        )
        assert accepted is False

    assert transitions == ["rolled_back"]
    assert action.state == "rolled_back"
    assert event_queue.empty()


def test_cancellation_token_closes_registered_resource_once() -> None:
    """取消 token 应主动关闭阻塞资源，并允许完成后撤销登记。"""

    token = CancellationToken()
    closed: list[str] = []
    unregister = token.register(lambda: closed.append("closed"))

    assert token.set("shutdown") is True
    assert token.set("ignored") is False
    unregister()

    assert token.reason == "shutdown"
    assert closed == ["closed"]


def test_stream_cancellation_actively_closes_sdk_stream_exactly_once() -> None:
    """取消与 finally 同时收口时，底层 SDK 流只能真正 close 一次。"""

    class RecordingStream:
        """记录 close 次数的最小同步流。"""

        def __init__(self) -> None:
            """初始化两个片段和关闭计数。"""

            self.items = iter(("first", "late"))
            self.close_count = 0

        def __iter__(self) -> RecordingStream:
            """返回自身作为迭代器。"""

            return self

        def __next__(self) -> str:
            """返回下一个模型片段。"""

            return next(self.items)

        def close(self) -> None:
            """记录一次底层关闭。"""

            self.close_count += 1

    token = CancellationToken()
    stream = RecordingStream()
    chunks = iter_cancellable_stream(stream, token)

    assert next(chunks) == "first"
    token.cancel("user_barge_in")
    assert stream.close_count == 1
    assert list(chunks) == []
    assert stream.close_count == 1


def test_cancelled_stream_suppresses_close_induced_transport_error() -> None:
    """取消后底层流的残余传输异常应按正常取消收口。"""

    class IncompleteChunkedStream:
        """模拟关闭后继续读取时抛出 chunked 响应不完整。"""

        def __init__(self) -> None:
            self.first_item_pending = True
            self.close_count = 0

        def __iter__(self) -> IncompleteChunkedStream:
            return self

        def __next__(self) -> str:
            if self.first_item_pending:
                self.first_item_pending = False
                return "first"
            raise httpx.RemoteProtocolError("incomplete chunked read")

        def close(self) -> None:
            self.close_count += 1

    token = CancellationToken()
    stream = IncompleteChunkedStream()
    chunks = iter_cancellable_stream(stream, token)

    assert next(chunks) == "first"
    token.cancel("activity_notice_superseded")

    assert list(chunks) == []
    assert stream.close_count == 1


def test_active_stream_still_propagates_transport_error() -> None:
    """未取消 generation 的真实传输异常必须继续向上传播。"""

    class BrokenStream:
        """模拟没有取消信号时发生的远端断流。"""

        def __iter__(self) -> BrokenStream:
            return self

        def __next__(self) -> str:
            raise httpx.RemoteProtocolError("incomplete chunked read")

        def close(self) -> None:
            return None

    chunks = iter_cancellable_stream(BrokenStream(), CancellationToken())

    with pytest.raises(httpx.RemoteProtocolError, match="incomplete chunked read"):
        next(chunks)


def test_accepted_final_commits_history_and_event_exactly_once() -> None:
    """同一 generation 的 final 即使被重复提交也只能写一次历史和事件。"""

    supervisor, event_queue, context = _supervisor()
    history: list[str] = []
    action = CommitAction(on_accept=lambda: history.append("final"))
    event = ForegroundEvent(
        type="assistant.text_final",
        payload={"text": "完成"},
        commit_action=action,
    )

    assert supervisor.put_foreground_event_if_current(context, event) is True
    assert supervisor.put_foreground_event_if_current(context, event) is False
    assert history == ["final"]
    assert event_queue.qsize() == 1


def test_direct_output_commits_plain_history_without_fallback_metadata() -> None:
    """确定性权限短句必须作为普通角色回复写入历史，不能伪装成错误兜底。"""

    session_context = SessionContext()
    processor = object.__new__(ForegroundTurnProcessor)
    processor._session_context = session_context

    event = processor.create_direct_output_event(
        "收到，这次操作已经允许。",
        request_id="permission-ack-a",
        history_policy="stable_dialogue",
    )

    assert event.commit_action is not None
    assert event.commit_action.accept() is True
    assert event.commit_action.commit_enqueued() is True
    messages = session_context.recent_messages()
    assert len(messages) == 1
    assert messages[0].content == "收到，这次操作已经允许。"
    assert "runtime_fallback" not in messages[0].metadata
    memory_commit = event.payload.get("memory_commit")
    assert isinstance(memory_commit, dict)
    assert memory_commit["request_id"] == "permission-ack-a"


def test_commit_failure_rolls_back_prepared_action() -> None:
    """提交副作用异常时事务必须进入 rolled_back，不能泄漏 prepared。"""

    transitions: list[str] = []

    def fail_commit() -> None:
        """模拟 Activity Executor 在提交边界拒绝启动。"""

        raise RuntimeError("start failed")

    action = CommitAction(
        on_accept=fail_commit,
        on_rollback=lambda: transitions.append("rolled_back"),
    )

    try:
        action.accept()
    except RuntimeError:
        pass

    assert action.state == "rolled_back"
    assert transitions == ["rolled_back"]


def test_event_enqueue_failure_rolls_back_before_after_commit_side_effect() -> None:
    """事件队列拒绝接受时必须回滚占位，且不能启动提交后 Activity。"""

    class FailingEventQueue(Queue[RuntimeEvent]):
        """模拟接受事件时发生不可恢复的队列故障。"""

        def put(self, item: RuntimeEvent, block: bool = True, timeout: float | None = None) -> None:
            _ = item, block, timeout
            raise RuntimeError("event enqueue failed")

    transitions: list[str] = []
    supervisor = GenerationSupervisor(
        request_queue=Queue(),
        auxiliary_request_queue=Queue(),
        tts_queue=Queue(),
        event_queue=FailingEventQueue(),
        now=lambda: 1.0,
    )
    context = SimpleNamespace(
        cancellation_event=CancellationToken(),
        request=SimpleNamespace(
            request_id="request-a",
            source="text_input",
            source_turn_id=None,
            turn_material_context_id=None,
            history_policy="stable_dialogue",
            activity_progress_references=(),
        ),
        source_session_id="session-a",
        generation_id="generation-a",
        lane="primary",
    )
    supervisor._current_by_lane["primary"] = context
    action = CommitAction(
        on_accept=lambda: transitions.append("reservation_confirmed"),
        on_rollback=lambda: transitions.append("rolled_back"),
        after_commit=lambda: transitions.append("activity_started"),
    )

    with pytest.raises(RuntimeError, match="event enqueue failed"):
        supervisor.put_foreground_event_if_current(
            context,
            ForegroundEvent(
                type="llm.foreground.tool_call_completed",
                payload={"state": "accepted"},
                commit_action=action,
            ),
        )

    assert transitions == ["reservation_confirmed", "rolled_back"]
    assert action.state == "rolled_back"


def test_background_activity_starts_only_after_acceptance_event_commit() -> None:
    """后台 Activity 在 accepted 与事件入队之间只能保留占位，不能开始执行。"""

    transitions: list[str] = []
    reservation = BackgroundActivityReservation.create(
        source="task_result",
        activity_id="task_result-a",
        start_after_commit=lambda: transitions.append("activity_started"),
        on_rollback=lambda: transitions.append("rolled_back"),
    )

    assert reservation.action.accept() is True
    assert transitions == []
    assert reservation.action.commit_enqueued() is True
    assert transitions == []
    assert reservation.action.run_after_commit() is True
    assert transitions == ["activity_started"]


def test_after_commit_fact_waits_until_the_running_callback_publishes_it() -> None:
    """并发读取不能观察到回调已被占用但最终事实尚未发布的中间状态。"""

    callback_entered = threading.Event()
    release_callback = threading.Event()
    expected_fact = ToolFollowupFact(
        kind="activity_started",
        activity_id="activity-a",
        goal="查询天气",
    )

    def publish_fact() -> ToolFollowupFact:
        """暂停回调，让测试稳定进入 fact 尚未写入的并发窗口。"""

        callback_entered.set()
        assert release_callback.wait(timeout=1.0)
        return expected_fact

    action = CommitAction(on_accept=lambda: None, after_commit=publish_fact)
    assert action.accept() is True
    assert action.commit_enqueued() is True
    runner = threading.Thread(target=action.run_after_commit, daemon=True)
    runner.start()
    assert callback_entered.wait(timeout=1.0) is True

    observed: list[ToolFollowupFact | None] = []
    reader = threading.Thread(
        target=lambda: observed.append(action.after_commit_fact),
        daemon=True,
    )
    repeated_run_results: list[bool] = []
    repeated_runner = threading.Thread(
        target=lambda: repeated_run_results.append(action.run_after_commit()),
        daemon=True,
    )
    reader.start()
    repeated_runner.start()
    reader.join(timeout=0.02)
    repeated_runner.join(timeout=0.02)
    assert reader.is_alive() is True
    assert repeated_runner.is_alive() is True

    release_callback.set()
    runner.join(timeout=1.0)
    reader.join(timeout=1.0)
    repeated_runner.join(timeout=1.0)
    assert runner.is_alive() is False
    assert reader.is_alive() is False
    assert repeated_runner.is_alive() is False
    assert observed == [expected_fact]
    assert repeated_run_results == [False]


def test_background_commit_barrier_survives_shutdown_timeout_until_after_commit() -> None:
    """提交已被接收后，关闭超时只能等待而不能释放它的资源所有权。"""

    barrier = BackgroundCommitDrainBarrier()
    callback_entered = threading.Event()
    release_callback = threading.Event()

    def start_activity() -> ToolFollowupFact:
        """阻塞启动动作，模拟 Executor 尚未完成 prepared 接管。

        Returns:
            测试放行后的固定启动事实。
        """

        callback_entered.set()
        assert release_callback.wait(timeout=1.0)
        return ToolFollowupFact(
            kind="activity_started",
            activity_id="activity-a",
            goal="等待提交排水",
        )

    action = CommitAction(
        on_accept=lambda: None,
        after_commit=start_activity,
        transaction_id="background-commit-a",
        commit_barrier=barrier,
    )
    assert action.accept() is True
    assert barrier.begin_shutdown(timeout=0.0).drained is False
    assert action.commit_enqueued() is True

    worker = threading.Thread(target=action.run_after_commit, daemon=True)
    worker.start()
    assert callback_entered.wait(timeout=1.0) is True
    assert barrier.pending_owner_ids() == ("background-commit-a",)

    release_callback.set()
    worker.join(timeout=1.0)
    assert worker.is_alive() is False
    assert barrier.wait_for_drain(timeout=0.1).drained is True


def test_background_commit_barrier_releases_when_rollback_callback_raises() -> None:
    """资源回滚自身异常时也不能让关闭屏障永久占用。"""

    barrier = BackgroundCommitDrainBarrier()

    def fail_rollback() -> None:
        """模拟工作区清理失败。

        Raises:
            RuntimeError: 每次调用都模拟回滚失败。
        """

        raise RuntimeError("rollback_failed")

    action = CommitAction(
        on_accept=lambda: None,
        on_rollback=fail_rollback,
        transaction_id="background-rollback-a",
        commit_barrier=barrier,
    )
    assert action.accept() is True
    assert barrier.begin_shutdown(timeout=0.0).drained is False

    with pytest.raises(RuntimeError, match="rollback_failed"):
        action.rollback()

    assert barrier.wait_for_drain(timeout=0.1).drained is True


def test_correction_uses_prepared_replacement_transaction_after_commit() -> None:
    """纠正工具必须提交共享替换事务，不能绕过它直接启动新 Activity。"""

    transitions: list[str] = []
    correction_reservation = BackgroundActivityReservation.create(
        source="background_agent",
        activity_id="replacement-activity",
        start_after_commit=lambda: transitions.append("replacement_committed"),
        on_rollback=lambda: transitions.append("replacement_rolled_back"),
    )

    class RecordingCorrectionExecutor:
        """记录 Foreground 是否按新 Activity 编号领取纠正事务。"""

        def take_reservation(
            self,
            activity_id: str,
        ) -> BackgroundActivityReservation | None:
            """返回预先准备的替换事务。

            Args:
                activity_id: Foreground 工具结果中的新 Activity 编号。

            Returns:
                编号匹配时返回替换事务，否则返回空。
            """

            transitions.append(f"reservation_taken:{activity_id}")
            if activity_id != "replacement-activity":
                return None
            return correction_reservation

    processor = object.__new__(ForegroundTurnProcessor)
    processor._activity_correction_tool_executor = RecordingCorrectionExecutor()
    processor._background_agent_activity_executor = SimpleNamespace(
        start=lambda _activity_id: transitions.append("direct_start")
    )
    processor._cognitive_activity_executor = None
    executed_call = ExecutedToolCall(
        call=ForegroundToolCall(
            tool_call_id="call-correction",
            name=CORRECT_BACKGROUND_WORK_TOOL,
            arguments={
                "activity_id": "old-activity",
                "goal": "执行正确目标",
                "deliverable": True,
            },
        ),
        outcome=BackgroundActivityPrepared(
            activity_id="replacement-activity",
            disposition="accepted",
            payload={
                "state": "accepted",
                "activity_id": "replacement-activity",
            },
        ),
        reply_requirement=ToolReplyRequirement.ACKNOWLEDGE,
        channel="background",
    )

    reservation = processor._background_activity_reservation(
        executed_call=executed_call,
        existing_keys=set(),
    )

    assert reservation is correction_reservation
    assert reservation.action.accept() is True
    assert reservation.action.commit_enqueued() is True
    assert reservation.action.run_after_commit() is True
    assert transitions == [
        "reservation_taken:replacement-activity",
        "replacement_committed",
    ]
