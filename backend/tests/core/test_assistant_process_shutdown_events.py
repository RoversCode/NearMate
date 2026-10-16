from __future__ import annotations

import time
from queue import Queue
from threading import RLock
from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.background_agent.cua_integration import CUA_SESSION_CLEANUP_TIMEOUT_SECONDS
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.foreground_self_reflection.contracts import (
    SelfReflectionToolCommitDecisionCommand,
)
from companion.realtime import assistant_process as assistant_process_module
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_process import (
    AssistantProcessCommand,
    AssistantProcessConfig,
)
from companion.realtime.assistant_process_client import (
    AssistantProcessNotReadyError,
    MultiprocessingAssistantProcessClient,
    _drain_shutdown_event_queue,
)
from companion.realtime.assistant_process_handle import SwappableAssistantProcessClient


def test_shutdown_event_queue_drain_keeps_runtime_events_in_order() -> None:
    """子进程退出后必须按原顺序取回最后事件，并忽略非法队列值。"""

    event_queue: Queue[object] = Queue()
    first = _event("background_agent.cancelled")
    second = _event("background_agent.completed")
    event_queue.put(first)
    event_queue.put("invalid")
    event_queue.put(second)

    assert _drain_shutdown_event_queue(event_queue) == (first, second)


def test_swappable_shutdown_preserves_old_runtime_epoch() -> None:
    """稳定句柄关闭后返回的最后事件仍应携带关闭前的运行时世代。"""

    client = SwappableAssistantProcessClient(
        initial_runtime=ShutdownEventRuntime((_event("background_agent.cancelled"),))
    )

    events = client.shutdown()

    assert len(events) == 1
    assert events[0].runtime_epoch == 1


def test_supervisor_forwards_shutdown_events_to_private_handler() -> None:
    """Supervisor 关闭任何运行时时都必须把最后事件交给私有回收端口。"""

    recorded: list[tuple[str, dict[str, object]]] = []
    supervisor = object.__new__(AssistantProcessSupervisor)
    supervisor._shutdown_lock = RLock()
    supervisor._pending_shutdown_clients = {}
    supervisor.shutdown_event_handler = lambda event_type, payload: recorded.append(
        (event_type, payload)
    )

    supervisor.shutdown_process(
        ShutdownEventRuntime((_event("background_agent.cancelled"),))
    )

    assert recorded[0][0] == "background_agent.cancelled"
    assert recorded[0][1]["internal_usage"] == {
        "is_complete": False,
        "reference_cost_usd": None,
        "by_model": [],
    }


def test_supervisor_routes_retired_epoch_events_without_duplicate_usage_write() -> None:
    """有旧世代身份时，退出 backlog 必须交给共享事件队列且不能双写用量。"""

    routed: list[RuntimeEvent] = []
    usage_fallback: list[tuple[str, dict[str, object]]] = []
    supervisor = object.__new__(AssistantProcessSupervisor)
    supervisor._shutdown_lock = RLock()
    supervisor._pending_shutdown_clients = {}
    supervisor.shutdown_event_handler = lambda event_type, payload: usage_fallback.append(
        (event_type, payload)
    )
    supervisor.retired_event_handler = lambda event: routed.append(event)

    supervisor.shutdown_process(
        ShutdownEventRuntime((_event("background_agent.completed"),)),
        runtime_epoch=7,
    )

    assert len(routed) == 1
    assert routed[0].runtime_epoch == 7
    assert routed[0].retired_runtime_event is True
    assert usage_fallback == []


def test_shutdown_signal_does_not_skip_earlier_tool_commit_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止信号已设置时，子进程仍必须先消费排在 shutdown 前的批准。

    Args:
        monkeypatch: 用最小测试 Runtime 替换真实模型和音频装配。
    """

    child_runtime = RecordingChildRuntime()
    command_queue: Queue[AssistantProcessCommand] = Queue()
    command_queue.put(
        AssistantProcessCommand(
            kind="assistant_control",
            payload=_tool_commit_approval(),
        )
    )
    command_queue.put(AssistantProcessCommand(kind="shutdown"))
    monkeypatch.setattr(
        assistant_process_module,
        "_bootstrap_assistant_process_runtime",
        lambda: None,
    )
    monkeypatch.setattr(
        assistant_process_module,
        "_build_assistant_child_runtime",
        lambda *_args, **_kwargs: child_runtime,
    )
    monkeypatch.setattr(
        assistant_process_module,
        "shutdown_backend_logging",
        lambda: None,
    )

    assistant_process_module.run_assistant_process(
        SimpleNamespace(is_set=lambda: True),
        SimpleNamespace(set=lambda: None),
        cast(Any, command_queue),
        cast(Any, Queue()),
        cast(Any, Queue()),
        cast(Any, SimpleNamespace(set=lambda: None)),
        cast(AssistantProcessConfig, None),
    )

    assert child_runtime.controls == [_tool_commit_approval()]
    assert child_runtime.shutdown_called is True


def test_process_client_allows_browser_cleanup_to_finish_before_force_terminate() -> None:
    """父进程必须给 Browser 清理及后续收口留出超过 CLI 单步超时的时间。

    Returns:
        无返回值；真实子进程自然返回的终态证明清理没有被父进程提前终止。
    """

    cleanup_delay_seconds = CUA_SESSION_CLEANUP_TIMEOUT_SECONDS + 0.15
    client = MultiprocessingAssistantProcessClient(
        now=time.monotonic,
        process_target=_run_delayed_cleanup_assistant_process,
        config=cast(AssistantProcessConfig, cleanup_delay_seconds),
    )
    client.start()

    # 清理期限从运行时已就绪开始测试，不能把 Windows 模块导入耗时算进 Browser 关闭时间。
    assert client.bridge_work_event.wait(timeout=10)
    client.bridge_work_event.clear()
    shutdown_events = client.shutdown()

    assert [event.type for event in shutdown_events] == ["background_agent.cancelled"]


def test_process_client_publishes_ready_only_after_child_signals_readiness() -> None:
    """父进程必须等待子进程完成装配，不能把 Process.start 当作可服务。"""

    client = MultiprocessingAssistantProcessClient(
        now=time.monotonic,
        process_target=_run_ready_then_wait_assistant_process,
        config=None,
    )
    client.start()

    assert client.is_ready is False
    client.wait_until_ready(timeout=5.0)
    assert client.is_ready is True
    client.shutdown()


def test_process_client_readiness_timeout_uses_monotonic_clock() -> None:
    """业务 now 被固定时，readiness 仍必须按真实单调时间超时。"""

    client = MultiprocessingAssistantProcessClient(
        now=lambda: 0.0,
        process_target=_run_not_ready_then_wait_assistant_process,
        config=None,
    )
    client.start()

    with pytest.raises(AssistantProcessNotReadyError, match="startup timed out"):
        client.wait_until_ready(timeout=0.1)
    client.shutdown()


def test_process_client_rejects_command_before_readiness() -> None:
    """子进程装配期间不能把业务控制命令排进无人服务的队列。"""

    client = MultiprocessingAssistantProcessClient(
        now=time.monotonic,
        process_target=_run_not_ready_then_wait_assistant_process,
        config=None,
    )
    client.start()

    with pytest.raises(AssistantProcessNotReadyError, match="command submission"):
        client.cancel_current_generation(reason="test")
    client.shutdown()


def test_process_client_rejects_management_before_readiness() -> None:
    """运行时尚未就绪时，管理 API 必须立即报告可恢复状态。"""

    client = MultiprocessingAssistantProcessClient(now=time.monotonic, config=None)

    with pytest.raises(AssistantProcessNotReadyError, match="not ready"):
        client.get_streamvox_runtime_state()


def _run_ready_then_wait_assistant_process(
    _stop_event: object,
    _bridge_work_event: object,
    command_queue: object,
    _event_queue: object,
    _streamvox_management_response_queue: object,
    runtime_ready_event: object,
    _config: object,
) -> None:
    """测试用子进程：先报告就绪，再等待父进程发来关闭命令。"""

    runtime_ready_event.set()  # type: ignore[attr-defined]
    while True:
        command = command_queue.get()  # type: ignore[attr-defined]
        if isinstance(command, AssistantProcessCommand) and command.kind == "shutdown":
            return


def _run_not_ready_then_wait_assistant_process(
    _stop_event: object,
    _bridge_work_event: object,
    command_queue: object,
    _event_queue: object,
    _streamvox_management_response_queue: object,
    _runtime_ready_event: object,
    _config: object,
) -> None:
    """测试用子进程：故意不报告 readiness，等待关闭命令收口。"""

    while True:
        command = command_queue.get()  # type: ignore[attr-defined]
        if isinstance(command, AssistantProcessCommand) and command.kind == "shutdown":
            return


def _run_delayed_cleanup_assistant_process(
    _stop_event: object,
    bridge_work_event: object,
    command_queue: object,
    event_queue: object,
    _streamvox_management_response_queue: object,
    _runtime_ready_event: object,
    cleanup_delay_seconds: object,
) -> None:
    """模拟 Browser Session 与 MCP transport 按顺序完成的慢关闭子进程。

    Args:
        _stop_event: 与生产子进程签名一致但本探针不读取的停止信号。
        bridge_work_event: 通知父进程存在退出阶段事件的跨进程信号。
        command_queue: 父进程发送 shutdown 命令的队列。
        event_queue: 子进程写回最终 Activity 事件的队列。
        _streamvox_management_response_queue: 本探针不使用的管理响应队列。
        cleanup_delay_seconds: 模拟有序清理实际消耗的秒数。

    Returns:
        无返回值；清理完成后写入一条去内容化终态。
    """

    bridge_work_event.set()  # type: ignore[attr-defined]
    while True:
        command = command_queue.get()  # type: ignore[attr-defined]
        if isinstance(command, AssistantProcessCommand) and command.kind == "shutdown":
            break
    time.sleep(float(cleanup_delay_seconds))
    event_queue.put(_event("background_agent.cancelled"))  # type: ignore[attr-defined]
    bridge_work_event.set()  # type: ignore[attr-defined]


def test_failed_start_leaves_client_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start 失败留下未启动进程对象时，shutdown 与再次 start 都不得抛出 AssertionError。"""

    client = MultiprocessingAssistantProcessClient.for_test_noop(now=time.monotonic)

    class FailingProcess:
        """模拟 OS 级启动失败：进程对象存在但从未进入运行态。"""

        _popen = None

        def start(self) -> None:
            raise OSError("resource temporarily unavailable")

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(
        client._context,
        "Process",
        lambda **_kwargs: FailingProcess(),
    )

    with pytest.raises(OSError, match="resource temporarily unavailable"):
        client.start()
    # 再次 start 会先对旧进程对象做句柄回收判断；从未启动的对象不能被 join。
    with pytest.raises(OSError, match="resource temporarily unavailable"):
        client.start()

    assert client.shutdown() == ()
    assert client._queues_closed is True


class ShutdownEventRuntime:
    """关闭时返回固定事件的最小助手运行时替身。"""

    def __init__(self, events: tuple[RuntimeEvent, ...]) -> None:
        self.events = events

    def shutdown(self) -> tuple[RuntimeEvent, ...]:
        """返回构造期冻结的退出事件。"""

        return self.events


class RecordingChildRuntime:
    """记录真实子进程命令循环在关闭前处理的控制命令。"""

    def __init__(self) -> None:
        """初始化空控制记录和关闭标记。"""

        self.controls: list[object] = []
        self.shutdown_called = False

    def handle_control_command(self, command: object) -> None:
        """保存命令循环实际交付的控制命令。

        Args:
            command: 排在 shutdown 命令之前的父进程控制命令。
        """

        self.controls.append(command)

    def shutdown(self, timeout: float = 1.0) -> None:
        """记录子进程 finally 已关闭测试 Runtime。

        Args:
            timeout: 与生产关闭接口一致的总预算。
        """

        del timeout
        self.shutdown_called = True

    def drain_events(self) -> list[RuntimeEvent]:
        """返回空事件列表，避免测试制造额外生命周期事实。

        Returns:
            始终为空的 Runtime Event 列表。
        """

        return []


def _event(event_type: str) -> RuntimeEvent:
    """构造不包含任务内容的退出阶段事件。"""

    return RuntimeEvent(
        type=event_type,
        source="assistant_runtime",
        payload={
            "activity_id": "11111111-1111-4111-8111-111111111111",
            "created_at": 1_000,
            "internal_usage": {
                "is_complete": False,
                "reference_cost_usd": None,
                "by_model": [],
            },
        },
    )


def _tool_commit_approval() -> SelfReflectionToolCommitDecisionCommand:
    """构造必须早于 shutdown 被消费的完整批准命令。

    Returns:
        身份固定且可安全重复比较的批准命令。
    """

    return SelfReflectionToolCommitDecisionCommand(
        commit_request_id="commit-request-1",
        self_reflection_id="self-reflection-1",
        source_activity_id="cognitive-1",
        runtime_epoch=7,
        tool_call_id="tool-call-1",
        tool_name="start_background_work",
        approved=True,
        reason="tool_commit_approved",
        issued_at=10.0,
    )
