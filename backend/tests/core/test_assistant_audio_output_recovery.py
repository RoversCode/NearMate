from __future__ import annotations

import asyncio
import base64
import threading
from queue import Queue
from types import SimpleNamespace

import pytest

from companion.realtime.assistant_audio_output import (
    ActiveAssistantPlayout,
    AssistantAudioOutputScheduler,
    QueuedAssistantAudioEvent,
)
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.media_tracks import AssistantAudioPublisher
from companion.realtime.interaction_state import InteractionState
from companion.realtime.playout_delivery import PlayoutDeliveryRegistry


class NeverDrainingPublisher:
    """模拟底层接受音频后永远不返回播放排空确认。"""

    def __init__(self) -> None:
        self.cleared = False

    async def publish_pcm16(self, *, pcm16: bytes, sample_rate: int) -> None:
        """本测试不发布音频。"""

    async def wait_for_playout(self) -> None:
        """永久等待，复现生产日志中的 Turn 卡死。"""

        await asyncio.Event().wait()

    def clear_queue(self) -> None:
        """记录 watchdog 是否清理了底层残留队列。"""

        self.cleared = True


class ImmediatePublisher(NeverDrainingPublisher):
    """模拟底层立即确认播放队列已经排空。"""

    async def wait_for_playout(self) -> None:
        """立即返回，不给正常播放增加超时等待。"""


class FailingPublisher(NeverDrainingPublisher):
    """模拟底层在等待排空时直接抛出异常。"""

    async def wait_for_playout(self) -> None:
        """抛出固定异常，验证调度器不会随单条音频事件退出。"""

        raise RuntimeError("audio source unavailable")


def test_idle_audio_thread_applies_clear_before_the_next_request() -> None:
    """空闲时也要执行清理，后续请求的首段不能成为旧清理回调的牺牲品。"""

    cleared = threading.Event()
    captured: list[bytes] = []

    class AudioSource:
        sample_rate = 24000

        async def capture_frame(self, frame) -> None:
            captured.append(bytes(frame.data))
            await asyncio.sleep(0)

        def clear_queue(self) -> None:
            cleared.set()

    scheduler = AssistantAudioOutputScheduler(
        event_queue=Queue(),
        assistant_audio_track_factory=lambda: (AssistantAudioPublisher(AudioSource()), object()),
    )
    scheduler.start()
    try:
        scheduler.clear_pending_audio(interrupt_before=10.0)
        assert cleared.wait(timeout=1.0), "空闲音频线程必须处理清理，不能等新音频到达才执行"
        pcm16 = b"\x01\x00" * 2400
        scheduler.enqueue_audio_event(RuntimeEvent(
            type="assistant.audio_chunk", source="test", produced_at=11.0,
            payload={"request_id": "new", "generation_id": "generation-new",
                     "pcm16_b64": base64.b64encode(pcm16).decode(), "sample_rate": 24000},
        ))
        assert scheduler.wait_until_idle(timeout=1.0)
        assert b"".join(captured) == pcm16
    finally:
        scheduler.stop()


@pytest.mark.asyncio
async def test_new_audio_applies_clear_even_when_its_callback_is_late() -> None:
    """新段即使先获得执行机会，也须先完成清理；迟到回调不能再次截断它。"""

    callbacks: list[object] = []
    captured: list[bytes] = []

    class AudioSource:
        sample_rate = 24000

        async def capture_frame(self, frame) -> None:
            captured.append(bytes(frame.data))
            if callbacks:
                callbacks.pop(0)()

        def clear_queue(self) -> None:
            pass

    scheduler = AssistantAudioOutputScheduler(event_queue=Queue())
    scheduler._publisher = AssistantAudioPublisher(AudioSource())
    scheduler._loop = SimpleNamespace(call_soon_threadsafe=callbacks.append)
    scheduler.clear_pending_audio(interrupt_before=10.0)
    pcm16 = b"\x01\x00" * 2400
    await scheduler._handle_audio_event(QueuedAssistantAudioEvent(
        RuntimeEvent(type="assistant.audio_chunk", source="test", produced_at=11.0, payload={
            "request_id": "new", "generation_id": "generation-new",
            "pcm16_b64": base64.b64encode(pcm16).decode(), "sample_rate": 24000,
        }), scheduler._current_clear_generation(),
    ))
    assert b"".join(captured) == pcm16


def test_manual_clear_settles_completed_request_that_has_not_started_playout() -> None:
    """生成已结束、音频仍排队时，清理必须给产品输出留下唯一取消终态。"""

    events: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(event_queue=events)
    scheduler._thread = SimpleNamespace(is_alive=lambda: True)
    interaction = InteractionState()
    interaction.register_assistant_request(1.0, request_id="request-a")
    scheduler.enqueue_audio_event(_audio_finished_event())
    interrupted = scheduler.clear_pending_audio(interrupt_before=10.0)
    for request_id in interrupted:
        scheduler.clear_pending_audio_for_request(request_id)
    assert interrupted == ("request-a",)
    assert events.qsize() == 1
    cancelled = events.get_nowait()
    assert cancelled.type == "assistant.audio_publishing_cancelled"
    assert cancelled.payload == {
        "request_id": "request-a", "generation_id": "generation-a", "delivery_id": "delivery-a",
    }
    interaction.on_assistant_audio_publishing_cancelled(
        10.0, request_id=cancelled.payload["request_id"], generation_id=cancelled.payload["generation_id"],
    )
    assert interaction.turn_state == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["all", "request", "wake"])
async def test_real_publisher_stops_remaining_frames_after_clear_and_keeps_next_request(interruption: str) -> None:
    """用真实逐帧发布器锁定清队列期间的旧尾音，并验证新请求仍能完整播放。"""

    first_frame_started = asyncio.Event()
    release_first_frame = asyncio.Event()
    captured: list[bytes] = []

    class AudioSource:
        sample_rate = 24000

        async def capture_frame(self, frame) -> None:
            captured.append(bytes(frame.data))
            if len(captured) == 1:
                first_frame_started.set()
                await release_first_frame.wait()

        def clear_queue(self) -> None:
            release_first_frame.set()

    scheduler = AssistantAudioOutputScheduler(event_queue=Queue())
    scheduler._publisher = AssistantAudioPublisher(AudioSource())
    scheduler._loop = asyncio.get_running_loop()
    old_event = RuntimeEvent(
        type="wake_response.audio_chunk" if interruption == "wake" else "assistant.audio_chunk",
        source="assistant_runtime", produced_at=9.0,
        payload={
            "pcm16_b64": base64.b64encode(bytes(4800)).decode(), "sample_rate": 24000,
            **({} if interruption == "wake" else {
                "request_id": "old", "generation_id": "generation-old", "delivery_id": "delivery-old",
            }),
        },
    )
    publishing = asyncio.create_task(scheduler._handle_audio_event(QueuedAssistantAudioEvent(old_event, 0)))
    await asyncio.wait_for(first_frame_started.wait(), timeout=1)
    if interruption == "request":
        scheduler.clear_pending_audio_for_request("old")
    else:
        scheduler.clear_pending_audio(interrupt_before=10.0)
    await asyncio.wait_for(publishing, timeout=1)
    assert len(captured) == 1, "清队列后不应继续写入同一旧段的剩余九帧"

    new_pcm = b"\x01\x00" * 2400
    await scheduler._handle_audio_event(QueuedAssistantAudioEvent(
        RuntimeEvent(type="assistant.audio_chunk", source="assistant_runtime", produced_at=11.0, payload={
            "pcm16_b64": base64.b64encode(new_pcm).decode(), "sample_rate": 24000,
            "request_id": "new", "generation_id": "generation-new", "delivery_id": "delivery-new",
        }),
        scheduler._current_clear_generation(),
    ))
    assert b"".join(captured[1:]) == new_pcm


def test_manual_interruption_reports_audio_identity_and_drops_late_wake() -> None:
    """生成结束但仍在播的语音也要进入回执，旧唤醒短答不能迟到复活。"""

    scheduler = AssistantAudioOutputScheduler(event_queue=Queue())
    scheduler._thread = SimpleNamespace(is_alive=lambda: True)
    scheduler._active_playout = ActiveAssistantPlayout("generation-a", "request-a", "delivery-a")
    interrupted = scheduler.clear_pending_audio(interrupt_before=10.0)
    assert interrupted == ("request-a",)
    assert "request-a" in scheduler._cancelled_audio_requests
    for produced_at in (9.0, 10.0, 11.0):
        scheduler.enqueue_audio_event(RuntimeEvent(
            type="wake_response.audio_chunk",
            source="realtime_input",
            produced_at=produced_at,
        ))
    assert scheduler._work_queue.qsize() == 1
    assert scheduler._work_queue.get_nowait().event.produced_at == 11.0


def test_old_global_audio_clear_callback_cannot_clear_a_new_generation() -> None:
    """旧清理回调晚执行时，不能清空已经换到下一代的音轨。"""

    callbacks: list[object] = []
    publisher = ImmediatePublisher()
    scheduler = AssistantAudioOutputScheduler(event_queue=Queue())
    scheduler._loop = SimpleNamespace(call_soon_threadsafe=callbacks.append)
    scheduler._publisher = publisher
    scheduler.clear_pending_audio(interrupt_before=10.0)
    scheduler._advance_clear_generation()
    callbacks[0]()
    assert publisher.cleared is False


@pytest.mark.asyncio
async def test_interruption_during_wake_delay_prevents_audio_publication() -> None:
    """短答正在等播放延迟时按快捷键，也不能在延迟结束后重新响起。"""

    published: list[bytes] = []
    scheduler = AssistantAudioOutputScheduler(event_queue=Queue())

    async def publish_pcm16(*, pcm16: bytes, sample_rate: int) -> None:
        published.append(pcm16)

    async def interrupt_during_delay(_delay: float) -> None:
        scheduler.clear_pending_audio(interrupt_before=10.0)

    scheduler._publisher = SimpleNamespace(publish_pcm16=publish_pcm16)
    scheduler.sleep = interrupt_during_delay
    await scheduler._handle_audio_event(QueuedAssistantAudioEvent(
        event=RuntimeEvent(
            type="wake_response.audio_chunk", source="realtime_input", produced_at=9.0,
            payload={"pcm16_b64": "AAA=", "sample_rate": 24000, "playback_delay_s": 0.2},
        ),
        clear_generation=0,
    ))
    assert published == []


@pytest.mark.asyncio
async def test_playout_watchdog_releases_finished_event_as_unconfirmed() -> None:
    """底层永不返回时必须有界释放 Turn，并标记为未确认交付。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    publisher = NeverDrainingPublisher()
    scheduler = AssistantAudioOutputScheduler(
        event_queue=event_queue,
        playout_wait_timeout_sec=0.01,
    )
    scheduler._publisher = publisher  # type: ignore[attr-defined]
    scheduler._active_playout = ActiveAssistantPlayout(  # type: ignore[attr-defined]
        generation_id="generation-a",
        request_id="request-a",
        delivery_id="delivery-a",
    )

    await asyncio.wait_for(
        scheduler._handle_audio_event(  # type: ignore[attr-defined]
            QueuedAssistantAudioEvent(
                event=_audio_finished_event(),
                clear_generation=0,
            )
        ),
        timeout=0.2,
    )

    finished = event_queue.get_nowait()
    assert finished.type == "assistant.audio_finished"
    assert finished.payload["playout_completion"] == "unconfirmed_timeout"
    assert publisher.cleared is True


@pytest.mark.asyncio
async def test_normal_playout_completion_remains_drained_without_delay() -> None:
    """正常排空应立即完成，并保留已确认交付语义。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(
        event_queue=event_queue,
        playout_wait_timeout_sec=5.0,
    )
    scheduler._publisher = ImmediatePublisher()  # type: ignore[attr-defined]
    scheduler._active_playout = ActiveAssistantPlayout(  # type: ignore[attr-defined]
        generation_id="generation-a",
        request_id="request-a",
        delivery_id="delivery-a",
    )

    await asyncio.wait_for(
        scheduler._handle_audio_event(  # type: ignore[attr-defined]
            QueuedAssistantAudioEvent(
                event=_audio_finished_event(),
                clear_generation=0,
            )
        ),
        timeout=0.05,
    )

    assert event_queue.get_nowait().payload["playout_completion"] == "drained"


@pytest.mark.asyncio
async def test_wake_response_completion_is_not_requeued_into_scheduler() -> None:
    """唤醒短答完成后不能再次回到 Drainer，否则会无限重复等待和提交。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._publisher = ImmediatePublisher()  # type: ignore[attr-defined]

    await asyncio.wait_for(
        scheduler._handle_audio_event(  # type: ignore[attr-defined]
            QueuedAssistantAudioEvent(
                event=RuntimeEvent(
                    type="wake_response.audio_finished",
                    source="realtime_input",
                    payload={"input_runtime_id": "runtime-a", "input_runtime_epoch": 1},
                ),
                clear_generation=0,
            )
        ),
        timeout=0.05,
    )

    assert event_queue.empty()


@pytest.mark.asyncio
async def test_playout_error_releases_finished_event_as_unconfirmed() -> None:
    """底层等待异常也必须释放 Turn，并清理可能残留的音频。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    publisher = FailingPublisher()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._publisher = publisher  # type: ignore[attr-defined]
    scheduler._active_playout = ActiveAssistantPlayout(  # type: ignore[attr-defined]
        generation_id="generation-a",
        request_id="request-a",
        delivery_id="delivery-a",
    )

    await scheduler._handle_audio_event(  # type: ignore[attr-defined]
        QueuedAssistantAudioEvent(
            event=_audio_finished_event(),
            clear_generation=0,
        )
    )

    assert event_queue.get_nowait().payload["playout_completion"] == "unconfirmed_error"
    assert publisher.cleared is True


def test_unconfirmed_scheduler_completion_cannot_be_recorded_as_delivered() -> None:
    """Desktop 已开始播放也不能把 watchdog 超时伪装成确认交付。"""

    registry = PlayoutDeliveryRegistry()
    registry.start_publishing(
        delivery_id="delivery-a",
        request_id="request-a",
        generation_id="generation-a",
        occurred_at=1.0,
    )
    registry.acknowledge(
        delivery_id="delivery-a",
        request_id="request-a",
        generation_id="generation-a",
        client_instance_id="desktop-a",
        outcome="playing",
        occurred_at=2.0,
    )

    registry.finish(
        delivery_id="delivery-a",
        request_id="request-a",
        generation_id="generation-a",
        completion="unconfirmed_timeout",
        occurred_at=3.0,
    )

    assert registry.get("delivery-a").status == "unconfirmed"


@pytest.mark.asyncio
async def test_request_scoped_clear_keeps_unrelated_audio_queued() -> None:
    """权限语音失效时只能移除目标请求，后续用户回复必须继续保留。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    publisher = ImmediatePublisher()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._publisher = publisher  # type: ignore[attr-defined]
    scheduler._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    scheduler._active_playout = ActiveAssistantPlayout(  # type: ignore[attr-defined]
        generation_id="generation-notice",
        request_id="system-notice-a",
        delivery_id="delivery-notice",
    )
    target = QueuedAssistantAudioEvent(
        event=_audio_finished_event(
            request_id="system-notice-a",
            generation_id="generation-notice",
            delivery_id="delivery-notice",
        ),
        clear_generation=0,
    )
    unrelated = QueuedAssistantAudioEvent(
        event=_audio_finished_event(
            request_id="user-request-a",
            generation_id="generation-user",
            delivery_id="delivery-user",
        ),
        clear_generation=0,
    )
    scheduler._increment_pending_count()  # type: ignore[attr-defined]
    scheduler._work_queue.put(target)  # type: ignore[attr-defined]
    scheduler._increment_pending_count()  # type: ignore[attr-defined]
    scheduler._work_queue.put(unrelated)  # type: ignore[attr-defined]

    assert scheduler.clear_pending_audio_for_request("system-notice-a") is True
    await asyncio.sleep(0)

    assert scheduler._work_queue.get_nowait() is unrelated  # type: ignore[attr-defined]
    cancelled = event_queue.get_nowait()
    assert cancelled.type == "assistant.audio_publishing_cancelled"
    assert cancelled.payload["request_id"] == "system-notice-a"
    assert publisher.cleared is True


@pytest.mark.asyncio
async def test_request_cancellation_before_finished_commit_has_one_terminal_event() -> None:
    """请求取消先提交时，随后到达的完成事件不能生成第二个终态。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._publisher = ImmediatePublisher()  # type: ignore[attr-defined]
    scheduler._active_playout = ActiveAssistantPlayout(  # type: ignore[attr-defined]
        generation_id="generation-a",
        request_id="request-a",
        delivery_id="delivery-a",
    )
    scheduler._set_inflight_playout(  # type: ignore[attr-defined]
        ActiveAssistantPlayout("generation-a", "request-a", "delivery-a")
    )

    assert scheduler.clear_pending_audio_for_request("request-a") is True

    await scheduler._handle_audio_event(  # type: ignore[attr-defined]
        QueuedAssistantAudioEvent(
            event=_audio_finished_event(),
            clear_generation=0,
        )
    )

    terminal_types = [event_queue.get_nowait().type for _ in range(event_queue.qsize())]
    assert terminal_types == ["assistant.audio_publishing_cancelled"]


def test_request_clear_rechecks_inflight_identity_on_audio_loop() -> None:
    """A 的迟到清理回调不能在调度器已处理 B 时清掉 B 的底层音频。"""

    class DeferredLoop:
        """保存请求级清理回调，允许测试先切换在途请求。"""

        def __init__(self) -> None:
            self.callbacks: list[object] = []

        def call_soon_threadsafe(self, callback: object) -> None:
            """暂存需要在音频线程执行的同步回调。"""

            self.callbacks.append(callback)

    event_queue: Queue[RuntimeEvent] = Queue()
    publisher = ImmediatePublisher()
    deferred_loop = DeferredLoop()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    scheduler._publisher = publisher  # type: ignore[attr-defined]
    scheduler._loop = deferred_loop  # type: ignore[assignment]
    request_a = ActiveAssistantPlayout("generation-a", "request-a", "delivery-a")
    request_b = ActiveAssistantPlayout("generation-b", "request-b", "delivery-b")
    scheduler._set_inflight_playout(request_a)  # type: ignore[attr-defined]

    assert scheduler.clear_pending_audio_for_request("request-a") is True
    scheduler._set_inflight_playout(request_b)  # type: ignore[attr-defined]
    callback = deferred_loop.callbacks.pop()
    assert callable(callback)
    callback()

    assert publisher.cleared is False


def test_removed_audio_finished_releases_cancelled_request_tombstone() -> None:
    """清除队列里的完成事件后必须释放同一请求的迟到事件过滤状态。"""

    scheduler = AssistantAudioOutputScheduler(event_queue=Queue())
    request_id = "system-notice-a"
    scheduler._increment_pending_count()  # type: ignore[attr-defined]
    scheduler._work_queue.put(  # type: ignore[attr-defined]
        QueuedAssistantAudioEvent(
            event=_audio_finished_event(request_id=request_id),
            clear_generation=0,
        )
    )

    assert scheduler.clear_pending_audio_for_request(request_id) is True

    assert request_id not in scheduler._cancelled_audio_requests  # type: ignore[attr-defined]


def test_repeated_request_clear_emits_cancelled_once() -> None:
    """迟到播放身份再次出现时，同一取消请求仍只能发一个取消终态。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    request_event = RuntimeEvent(
        type="assistant.audio_chunk",
        source="assistant_runtime",
        payload={
            "request_id": "request-a",
            "generation_id": "generation-a",
            "delivery_id": "delivery-a",
        },
    )
    scheduler._active_playout = ActiveAssistantPlayout(  # type: ignore[attr-defined]
        "generation-a", "request-a", "delivery-a"
    )

    assert scheduler.clear_pending_audio_for_request("request-a") is True
    scheduler._emit_playout_started_if_needed(request_event)  # type: ignore[attr-defined]
    scheduler.clear_pending_audio_for_request("request-a")

    emitted_types = [event_queue.get_nowait().type for _ in range(event_queue.qsize())]
    assert emitted_types == ["assistant.audio_publishing_cancelled"]


def test_cancelled_request_tombstones_are_bounded_without_audio_finished() -> None:
    """没有音频完成事件的快速取消也只能保留最近 256 个请求。"""

    scheduler = AssistantAudioOutputScheduler(event_queue=Queue())

    for index in range(300):
        assert scheduler.clear_pending_audio_for_request(f"system-notice-{index}") is True

    assert len(scheduler._cancelled_audio_requests) == 256  # type: ignore[attr-defined]
    assert "system-notice-0" not in scheduler._cancelled_audio_requests  # type: ignore[attr-defined]
    assert "system-notice-299" in scheduler._cancelled_audio_requests  # type: ignore[attr-defined]


def test_cancelled_request_finishes_once_and_releases_tombstone() -> None:
    """迟到音频只发布一次取消事实，完成事件到达后立即释放墓碑。"""

    event_queue: Queue[RuntimeEvent] = Queue()
    scheduler = AssistantAudioOutputScheduler(event_queue=event_queue)
    request_id = "system-notice-a"
    late_chunk = RuntimeEvent(
        type="assistant.audio_chunk",
        source="assistant_runtime",
        payload={
            "request_id": request_id,
            "generation_id": "generation-a",
            "delivery_id": "delivery-a",
        },
    )

    assert scheduler.clear_pending_audio_for_request(request_id) is True
    assert scheduler._discard_cancelled_audio_event(late_chunk) is True  # type: ignore[attr-defined]
    assert scheduler._discard_cancelled_audio_event(late_chunk) is True  # type: ignore[attr-defined]
    cancelled = event_queue.get_nowait()
    assert cancelled.type == "assistant.audio_publishing_cancelled"
    assert event_queue.empty()

    assert scheduler._discard_cancelled_audio_event(  # type: ignore[attr-defined]
        _audio_finished_event(request_id=request_id)
    ) is True
    assert request_id not in scheduler._cancelled_audio_requests  # type: ignore[attr-defined]


def test_successful_scheduler_restart_clears_cancelled_request_tombstones() -> None:
    """输出线程彻底停止或重新启动后，不得继承上一生命周期的取消身份。"""

    scheduler = AssistantAudioOutputScheduler(
        event_queue=Queue(),
        assistant_audio_track_factory=lambda: (ImmediatePublisher(), object()),
    )
    scheduler.start()
    scheduler.clear_pending_audio_for_request("system-notice-a")

    scheduler.stop()

    assert scheduler._cancelled_audio_requests == {}  # type: ignore[attr-defined]
    scheduler._cancelled_audio_requests["stale-request"] = object()  # type: ignore[attr-defined]
    scheduler.start()
    try:
        assert scheduler._cancelled_audio_requests == {}  # type: ignore[attr-defined]
    finally:
        scheduler.stop()


def test_scheduler_stop_timeout_preserves_cancelled_request_tombstones() -> None:
    """输出线程尚未退出时必须保留取消身份，继续拦截旧线程的迟到音频。"""

    scheduler = AssistantAudioOutputScheduler(event_queue=Queue())
    request_id = "system-notice-a"
    scheduler.clear_pending_audio_for_request(request_id)
    scheduler._thread = SimpleNamespace(  # type: ignore[assignment]
        join=lambda timeout: None,
        is_alive=lambda: True,
    )

    scheduler.stop(timeout=0.0)

    assert request_id in scheduler._cancelled_audio_requests  # type: ignore[attr-defined]


def _audio_finished_event(
    *,
    request_id: str = "request-a",
    generation_id: str = "generation-a",
    delivery_id: str = "delivery-a",
) -> RuntimeEvent:
    """构造身份完整的助手音频完成事件。"""

    return RuntimeEvent(
        type="assistant.audio_finished",
        source="assistant_runtime",
        payload={
            "generation_id": generation_id,
            "request_id": request_id,
            "delivery_id": delivery_id,
        },
    )
