"""提供 Notice 交付测试共用的记录器与失败替身。"""

from __future__ import annotations

import threading
from queue import Queue
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any, cast

from companion.activities.notice.delivery_event_mapper import map_notice_delivery_event
from companion.activities.notice.models import (
    NoticeDeliveryCommand,
    NoticeDeliveryResult,
)
from companion.config.realtime_audio import load_realtime_audio_config
from companion.core.application.assistant_request_context_preparer import AssistantRequestContextPreparer
from companion.realtime.assistant_events import AssistantControlCommand, AssistantRequest, RuntimeEvent
from companion.realtime.realtime_input_worker import RealtimeInputWorker
from companion.realtime.livekit_media_session import LiveKitMediaSession
from companion.realtime.notify_queue import NotifyQueue
from backend.tests.runtime_public_event_support import (
    runtime_public_event_publishers,
)


class RecordingCoordinator:
    """记录助手交付闭环调用的测试协调器。"""

    def __init__(self) -> None:
        """初始化各类交付结果的请求编号记录。"""

        self.llm_completed: list[str] = []
        self.cancelled: list[str] = []
        self.output_unavailable: list[str] = []
        self.protocol_failed: list[str] = []
        self.superseded: list[str] = []

    def apply_delivery(self, command: NoticeDeliveryCommand) -> NoticeDeliveryResult:
        """按 typed command 记录被选择的交付状态转换。

        Args:
            command: 已完成事件映射的 Notice 交付命令。

        Returns:
            表示测试协调器已接受命令的交付结果。
        """

        transitions = {
            "llm_output_completed": self.llm_completed,
            "delivery_cancelled": self.cancelled,
            "output_unavailable": self.output_unavailable,
            "provider_protocol_failed": self.protocol_failed,
            "superseded": self.superseded,
        }
        target = transitions.get(command.outcome)
        if target is not None:
            target.append(command.request_id)
        return NoticeDeliveryResult(
            accepted=True,
            request_id=command.request_id,
            outcome=command.outcome,
        )


class FailingRequestQueue:
    """模拟 queue.put 在入队边界前失败。"""

    def remove_matching(self, _predicate: object) -> list[object]:
        """返回没有待抢占的旧 system notice。

        Args:
            _predicate: 生产队列传入的匹配条件，本替身不会执行它。

        Returns:
            固定为空的待移除请求列表。
        """

        return []

    def put(self, _request: AssistantRequest) -> None:
        """在请求真正入队前抛出异常。

        Args:
            _request: 尝试提交的助手请求。

        Raises:
            RuntimeError: 固定模拟队列不可用。
        """

        raise RuntimeError("queue unavailable")


class RecordingAssistantBridge:
    """实现 LiveKit 媒体会话所需的 Bridge 提交与移除端口。"""

    def __init__(
        self,
        *,
        request_queue: Any | None = None,
        request_committed_handler: Callable[[AssistantRequest], None] | None = None,
        output_available: bool = True,
    ) -> None:
        """初始化测试队列、提交回调和健康状态。

        Args:
            request_queue: 接收助手请求的测试队列；省略时创建内存队列。
            request_committed_handler: 请求成功入队后调用的确认回调。
            output_available: 助手输出边界当前是否健康。
        """

        self.request_queue = request_queue or NotifyQueue()
        self.control_queue: NotifyQueue[AssistantControlCommand] = NotifyQueue()
        self.request_committed_handler = request_committed_handler
        self.output_available = output_available

    def is_output_available(self) -> bool:
        """返回测试指定的输出健康状态。

        Returns:
            助手输出边界当前是否允许提交。
        """

        return self.output_available

    def submit_request(self, request: AssistantRequest) -> bool:
        """按真实边界顺序提交请求、确认身份，再返回结果。

        Args:
            request: 准备提交给助手运行时的请求。

        Returns:
            请求是否成功进入测试队列。
        """

        if not self.output_available:
            return False
        self.request_queue.put(request)
        if self.request_committed_handler is not None:
            self.request_committed_handler(request)
        return True

    def submit_control(self, command: AssistantControlCommand) -> bool:
        """记录健康状态允许的控制命令。

        Args:
            command: 准备提交给助手运行时的控制命令。

        Returns:
            控制命令是否成功进入测试队列。
        """

        if not self.output_available:
            return False
        self.control_queue.put(command)
        return True

    def remove_pending_requests(
        self,
        predicate: Callable[[AssistantRequest], bool],
    ) -> list[AssistantRequest]:
        """移除仍停留在测试请求队列中的匹配项。

        Args:
            predicate: 判断请求是否需要移除的条件。

        Returns:
            已从测试队列移除的助手请求。
        """

        remove_matching = getattr(self.request_queue, "remove_matching", None)
        if not callable(remove_matching):
            return []
        return list(remove_matching(predicate))


class ImmediateBridgeAudioOutput:
    """提供 bridge 请求路径需要的最小音频调度接口。"""

    def clear_pending_audio(self) -> None:
        """本测试没有待清理音频。"""

    def enqueue_audio_event(self, event: RuntimeEvent) -> None:
        """拒绝本测试不应出现的音频事件。

        Args:
            event: 意外到达的助手音频事件。

        Raises:
            AssertionError: 本测试产生了音频事件。
        """

        raise AssertionError(f"不应产生音频事件：{event.type}")


class OrderedBridgeRuntime:
    """记录 bridge 向子进程边界提交请求和控制命令的先后顺序。"""

    def __init__(self) -> None:
        """初始化提交时序、唤醒次数和工作信号。"""

        self.submissions: list[tuple[str, str]] = []
        self.bridge_notifications = 0
        self.work_available = threading.Event()

    def clear_bridge_work_available(self) -> None:
        """清除测试运行时的工作信号。"""

        self.work_available.clear()

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """等待请求或控制命令唤醒。

        Args:
            timeout: 最长等待秒数；省略时持续等待。

        Returns:
            工作信号是否在超时前被设置。
        """

        return self.work_available.wait(timeout=timeout)

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """记录一次请求提交。

        Args:
            request: bridge 已从主进程队列取出的助手请求。

        Returns:
            原样返回的助手请求。
        """

        self.submissions.append(("request", request.request_id))
        return request

    def submit_control_command(self, command: AssistantControlCommand) -> AssistantControlCommand:
        """记录一次控制命令提交。

        Args:
            command: bridge 即将转交子进程的控制命令。

        Returns:
            原样返回的控制命令。
        """

        self.submissions.append(("control", command.request_id or ""))
        return command

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """返回空的子进程事件批次。

        Args:
            limit: 本次允许读取的最大事件数。

        Returns:
            空事件列表。
        """

        _ = limit
        return []

    def notify_bridge_work_available(self) -> None:
        """记录一次 bridge 工作唤醒。"""

        self.bridge_notifications += 1
        self.work_available.set()


class FailingBridgeRuntime(OrderedBridgeRuntime):
    """模拟 bridge 已出队但 runtime 拒绝提交。"""

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """拒绝当前请求。

        Args:
            request: bridge 已从主进程队列取出的助手请求。

        Raises:
            RuntimeError: 固定模拟运行时拒绝请求。
        """

        raise RuntimeError(f"runtime rejected {request.request_id}")


class BlockingBridgeAudioOutput:
    """把 bridge 暂停在请求出队后、IPC 提交前。"""

    def __init__(self) -> None:
        """初始化出队通知和继续提交的同步信号。"""

        self.request_dequeued = threading.Event()
        self.release_request = threading.Event()

    def clear_pending_audio(self) -> None:
        """暴露请求已出队的竞态窗口，并等待测试继续。"""

        self.request_dequeued.set()
        assert self.release_request.wait(timeout=1.0)

    def enqueue_audio_event(self, event: RuntimeEvent) -> None:
        """拒绝本测试不应出现的音频事件。

        Args:
            event: 意外到达的助手音频事件。

        Raises:
            AssertionError: bridge 在本测试中产生了音频事件。
        """

        raise AssertionError(f"不应产生音频事件：{event.type}")


def _apply_delivery_event(
    coordinator: RecordingCoordinator,
    event_type: str,
    payload: dict[str, object],
) -> NoticeDeliveryResult:
    """把运行时事件映射为 typed command 后交给测试协调器。

    Args:
        coordinator: 记录交付状态变化的测试协调器。
        event_type: 助手运行时事件类型。
        payload: 助手运行时事件载荷。

    Returns:
        测试协调器返回的 Notice 交付结果。
    """

    command = map_notice_delivery_event(event_type, payload)
    assert command is not None
    return coordinator.apply_delivery(command)


def build_assistant_request_context_preparer_for_test() -> AssistantRequestContextPreparer:
    """构造字段完整且不访问外部资源的请求上下文准备器。

    Returns:
        使用内存状态所有者初始化的真实请求上下文准备器。
    """

    return AssistantRequestContextPreparer(
        hub=cast(Any, SimpleNamespace()),
        realtime_input_runtime_owner=cast(Any, SimpleNamespace()),
    )


def build_livekit_media_session_for_test() -> LiveKitMediaSession:
    """构造不会连接房间或启动助手输出的真实媒体会话。

    Returns:
        已执行完整构造函数、可按用例替换协作端口的媒体会话。
    """

    async def ignore_public_event(_event: dict[str, object]) -> None:
        """忽略构造阶段不会触发的公共事件。

        Args:
            _event: 测试媒体会话可能发布的公共事件。
        """

    realtime_input_runtime = SimpleNamespace(
        realtime_audio_config=load_realtime_audio_config(),
        asr_provider=object(),
        text_eou_provider=None,
        vad_provider=object(),
        smart_turn_provider=object(),
        speaker_verification_provider=None,
        current_audio_session_id="session-test",
        bind_realtime_input_worker=lambda _worker: None,
    )
    return LiveKitMediaSession(
        realtime_input_runtime=cast(Any, realtime_input_runtime),
        public_event_publishers=runtime_public_event_publishers(ignore_public_event),
        assistant_runtime=None,
        assistant_output_enabled=False,
        room=object(),
    )


def build_realtime_input_worker_for_test() -> RealtimeInputWorker:
    """构造不运行 ASR 或媒体线程的真实输入 Worker。

    Returns:
        所有内部状态均由生产构造函数初始化的输入 Worker。
    """

    realtime_input_runtime = SimpleNamespace(
        asr_provider=object(),
        text_eou_provider=None,
    )
    return RealtimeInputWorker(
        realtime_input_runtime=realtime_input_runtime,
        vad_provider=object(),
        smart_turn_provider=object(),
        realtime_audio_config=load_realtime_audio_config(),
        audio_queue=Queue(),
        control_queue=Queue(),
        event_queue=Queue(),
        assistant_request_submitter=None,
        assistant_control_submitter=None,
        assistant_output_enabled=False,
        asr_scheduler=SimpleNamespace(),
        now=lambda: 1.0,
    )
