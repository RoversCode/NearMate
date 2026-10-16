from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from companion.core.application.lifecycle import ApplicationLifecycle
from companion.realtime.assistant_events import RuntimeEvent


@pytest.mark.asyncio
async def test_lifecycle_shutdown_collects_final_usage_before_media_close() -> None:
    """关闭时必须先收口 Bridge 和助手终态，再关闭承载 Router 的媒体会话。"""

    calls: list[str] = []
    pending_event = _usage_event("11111111-1111-4111-8111-111111111111")
    child_event = _usage_event("22222222-2222-4222-8222-222222222222")
    usage_service = RecordingUsageService(calls)
    runtime_event_service = RecordingRuntimeEventService(calls, usage_service)
    runtime_event_service.enqueue(pending_event)
    lifecycle = ApplicationLifecycle(
        hub=RecordingHub(calls),
        realtime_input_runtime_owner=RecordingRuntimeOwner(calls),
        assistant_process_supervisor=RecordingAssistantSupervisor(
            calls,
            final_event=child_event,
            shutdown_event_handler=runtime_event_service.enqueue,
        ),
        livekit_session_service=RecordingLiveKitService(calls),
        background_agent_usage_service=usage_service,
        runtime_event_service=runtime_event_service,
        timed_reminder_delivery=RecordingTimedReminderResource(
            calls,
            "reminder-delivery",
        ),
        timed_reminder_scheduler=RecordingTimedReminderResource(
            calls,
            "reminder-scheduler",
        ),
    )

    await lifecycle.shutdown()

    assert usage_service.activity_ids == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    assert calls == [
        "reminder-scheduler:shutdown",
        "reminder-delivery:release",
        "hub:close",
        "runtime:drain",
        "usage:record",
        "assistant:shutdown",
        "runtime:drain",
        "usage:record",
        "livekit:shutdown",
        "runtime:stop",
        "reminder-delivery:shutdown",
        "input:shutdown",
        "usage:shutdown",
    ]


@pytest.mark.asyncio
async def test_lifecycle_collects_event_bridged_during_assistant_shutdown() -> None:
    """Bridge 首次停止超时后迟到主进程队列的终态必须在媒体关闭前再次回收。"""

    calls: list[str] = []
    late_event = _usage_event("33333333-3333-4333-8333-333333333333")
    usage_service = RecordingUsageService(calls)
    livekit_service = RecordingLiveKitService(
        calls,
    )
    runtime_event_service = RecordingRuntimeEventService(calls, usage_service)
    lifecycle = ApplicationLifecycle(
        hub=RecordingHub(calls),
        realtime_input_runtime_owner=RecordingRuntimeOwner(calls),
        assistant_process_supervisor=RecordingAssistantSupervisor(
            calls,
            final_event=late_event,
            shutdown_event_handler=runtime_event_service.enqueue,
        ),
        livekit_session_service=livekit_service,
        background_agent_usage_service=usage_service,
        runtime_event_service=runtime_event_service,
        timed_reminder_delivery=RecordingTimedReminderResource(
            calls,
            "reminder-delivery",
        ),
        timed_reminder_scheduler=RecordingTimedReminderResource(
            calls,
            "reminder-scheduler",
        ),
    )

    await lifecycle.shutdown()

    assert usage_service.activity_ids == [
        "33333333-3333-4333-8333-333333333333"
    ]
    assert calls.index("assistant:shutdown") < calls.index("usage:record")
    assert calls.index("usage:record") < calls.index("livekit:shutdown")


@pytest.mark.asyncio
async def test_lifecycle_cleans_usage_service_when_assistant_startup_fails() -> None:
    """助手启动失败时，已经启动的用量 worker 也必须进入关闭流程。"""

    calls: list[str] = []
    usage_service = RecordingUsageService(calls)
    runtime_event_service = RecordingRuntimeEventService(calls, usage_service)
    lifecycle = ApplicationLifecycle(
        hub=RecordingHub(calls),
        realtime_input_runtime_owner=RecordingRuntimeOwner(calls),
        assistant_process_supervisor=FailingAssistantSupervisor(calls),
        livekit_session_service=RecordingLiveKitService(calls),
        background_agent_usage_service=usage_service,
        runtime_event_service=runtime_event_service,
        timed_reminder_delivery=RecordingTimedReminderResource(
            calls,
            "reminder-delivery",
        ),
        timed_reminder_scheduler=RecordingTimedReminderResource(
            calls,
            "reminder-scheduler",
        ),
    )

    with pytest.raises(RuntimeError, match="assistant startup failed"):
        async with lifecycle(SimpleNamespace()):
            pytest.fail("启动失败后不应进入应用运行阶段")

    assert calls[0:3] == ["usage:startup", "runtime:start", "assistant:start"]
    assert calls[-1] == "usage:shutdown"


class RecordingHub:
    """记录生命周期使用的控制连接和初始化状态。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.bootstrap = SimpleNamespace(status_payload=lambda: {"is_initialized": True})

    async def close_control_connections(self) -> None:
        """记录控制连接关闭。"""

        self.calls.append("hub:close")


class RecordingRuntimeOwner:
    """记录实时输入运行时关闭。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def shutdown(self) -> None:
        """记录输入运行时关闭。"""

        self.calls.append("input:shutdown")


class RecordingAssistantSupervisor:
    """记录助手启动关闭，并在关闭时交出子进程最后事件。"""

    def __init__(
        self,
        calls: list[str],
        final_event: RuntimeEvent | None = None,
        shutdown_event_handler: Callable[[RuntimeEvent], None] | None = None,
    ) -> None:
        self.calls = calls
        self.final_event = final_event
        self.shutdown_event_handler = shutdown_event_handler

    def start_if_initialized(self) -> None:
        """记录助手启动。"""

        self.calls.append("assistant:start")

    def shutdown(self) -> None:
        """记录助手关闭并把最后事件交给私有回收端口。"""

        self.calls.append("assistant:shutdown")
        if self.final_event is None or not callable(self.shutdown_event_handler):
            return
        self.shutdown_event_handler(self.final_event)


class FailingAssistantSupervisor(RecordingAssistantSupervisor):
    """模拟助手启动阶段直接失败。"""

    def start_if_initialized(self) -> None:
        """记录启动后抛出固定异常。"""

        self.calls.append("assistant:start")
        raise RuntimeError("assistant startup failed")


class RecordingLiveKitService:
    """记录 Bridge 收口和媒体会话关闭。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def shutdown(self) -> None:
        """记录媒体会话关闭。"""

        self.calls.append("livekit:shutdown")


class RecordingTimedReminderResource:
    """记录定时提醒调度与交付资源的启停顺序。"""

    def __init__(self, calls: list[str], name: str) -> None:
        self.calls = calls
        self.name = name

    def start(self) -> None:
        """记录资源启动。"""

        self.calls.append(f"{self.name}:start")

    async def shutdown(self) -> None:
        """记录资源关闭。"""

        self.calls.append(f"{self.name}:shutdown")

    async def release_active_requests(self) -> None:
        """记录助手输出关闭前的 Reminder 请求释放。"""

        self.calls.append(f"{self.name}:release")


class RecordingRuntimeEventService:
    """记录应用级事件服务的启动、排水和停止顺序。"""

    def __init__(
        self,
        calls: list[str],
        usage_service: "RecordingUsageService",
    ) -> None:
        self.calls = calls
        self.usage_service = usage_service
        self.pending_events: list[RuntimeEvent] = []

    def enqueue(self, event: RuntimeEvent) -> None:
        """模拟 Bridge 把关闭期终态送入进程级队列。"""

        self.pending_events.append(event)

    async def start(self) -> None:
        """记录唯一消费者启动。"""

        self.calls.append("runtime:start")

    async def drain_once(self) -> None:
        """按进入顺序把待处理终态交给私有用量端口。"""

        self.calls.append("runtime:drain")
        while self.pending_events:
            event = self.pending_events.pop(0)
            self.usage_service.record_terminal_event(event.type, event.payload)

    async def stop(self) -> None:
        """记录应用退出时停止唯一消费者。"""

        self.calls.append("runtime:stop")


class RecordingUsageService:
    """记录私有用量旁路的启动、写入和关闭。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.activity_ids: list[str] = []

    async def startup(self) -> None:
        """记录用量服务启动。"""

        self.calls.append("usage:startup")

    def record_terminal_event(
        self,
        _event_type: str,
        payload: dict[str, object],
    ) -> None:
        """记录关闭路径交出的终态用量。"""

        self.calls.append("usage:record")
        self.activity_ids.append(str(payload["activity_id"]))

    async def shutdown(self) -> None:
        """记录用量服务关闭。"""

        self.calls.append("usage:shutdown")


def _usage_event(activity_id: str) -> RuntimeEvent:
    """构造关闭路径使用的去内容化终态。"""

    return RuntimeEvent(
        type="background_agent.cancelled",
        source="assistant_runtime",
        runtime_epoch=1,
        payload={
            "activity_id": activity_id,
            "created_at": 1_000,
            "internal_usage": {
                "is_complete": False,
                "reference_cost_usd": None,
                "by_model": [],
            },
        },
    )
