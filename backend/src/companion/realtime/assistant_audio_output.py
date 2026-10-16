from __future__ import annotations

import asyncio
import base64
import inspect
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from queue import Empty, Queue
from typing import Protocol

from companion.observability.logging_config import get_backend_logger
from companion.observability.realtime_latency import RealtimeLatencyRecorder
from companion.realtime.audio_resampler import resample_pcm16_mono
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.media_tracks import AssistantAudioPublisher, create_assistant_audio_publisher
from companion.realtime.notify_queue import NotifyQueue
from companion.realtime.playout_delivery import PlayoutCompletion
from companion.realtime.system_notice_playout import SystemNoticePlayoutTracker


logger = get_backend_logger(__name__)

_MAX_CANCELLED_REQUEST_TOMBSTONES = 256


class AssistantAudioPublisherLike(Protocol):
    """助手音频发布器协议。

    定义真实 LiveKit 音频发布器与测试替身都需要满足的最小发布边界。
    """

    async def publish_pcm16(self, *, pcm16: bytes, sample_rate: int) -> None:
        """发布一段 PCM16 音频。

        Args:
            pcm16: PCM16 编码的原始音频字节。
            sample_rate: 音频采样率。
        """
        ...

    async def wait_for_playout(self) -> None:
        """等待已经进入底层音频源的内容播放完成。"""
        ...

    def clear_queue(self) -> None:
        """清空底层音频源中尚未播放的缓存。"""
        ...


AssistantAudioTrackFactory = Callable[..., tuple[AssistantAudioPublisherLike, object]]


@dataclass(frozen=True)
class QueuedAssistantAudioEvent:
    """助手音频输出队列项。

    将运行时音频事件与入队时的清空代际绑定，便于取消后识别过期完成事件。

    Args:
        event: 助手运行时产出的原始音频事件。
        clear_generation: 事件入队时的清空代际快照。
    """

    # event 是原始助手运行时事件，调度器只按类型分流和发布。
    event: RuntimeEvent
    # clear_generation 用于取消后抑制已在处理中的旧 audio_finished。
    clear_generation: int


@dataclass(frozen=True)
class ActiveAssistantPlayout:
    """保存当前已经进入真实播放链路的助手请求身份。

    Args:
        generation_id: 当前助手输出代际编号。
        request_id: 当前助手请求编号。
    """

    generation_id: str | None
    request_id: str | None
    delivery_id: str | None


@dataclass(slots=True)
class _CancelledAudioRequest:
    """保存一个近期取消请求的迟到音频过滤状态。

    Args:
        playout_cancelled_emitted: 是否已经发布过播放取消事实。
    """

    playout_cancelled_emitted: bool = False


class AssistantAudioOutputScheduler:
    """音频输出调度器。

    在专用线程和专用 asyncio loop 内驱动助手音频输出，避免语音发布依赖主事件循环实时性。
    """

    def __init__(
        self,
        *,
        event_queue: Queue[RuntimeEvent],
        assistant_audio_track_factory: AssistantAudioTrackFactory = create_assistant_audio_publisher,
        output_sample_rate: int = 24000,
        idle_sleep_sec: float = 0.002,
        start_timeout_sec: float = 5.0,
        playout_wait_timeout_sec: float = 5.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
        latency_recorder: RealtimeLatencyRecorder | None = None,
        system_notice_playout_tracker: SystemNoticePlayoutTracker | None = None,
        audio_finished_handler: Callable[[str], object] | None = None,
    ) -> None:
        """初始化调度器生命周期和跨线程队列。

        Args:
            event_queue: 控制面事件输出队列，用于接收播放完成后的事件。
            assistant_audio_track_factory: 创建音频发布器和本地音频轨的工厂函数。
            output_sample_rate: 助手下行 LiveKit 音轨固定输出采样率。
            idle_sleep_sec: 保留旧调用方的构造参数；空闲等待现在由异步通知唤醒。
            start_timeout_sec: 等待输出线程启动完成的最大秒数。
            playout_wait_timeout_sec: 等待底层音频源排空的最大秒数。
            now: 单调时钟函数，用于记录真实播放链路事件时间。
            sleep: 异步等待函数，用于测试唤醒回复播放延迟。
            latency_recorder: 可选的实时链路延迟记录器。
            system_notice_playout_tracker: 权限 System Notice 的短期播放身份账本。
            audio_finished_handler: 底层播放真正排空后的请求级同步通知端口。

        Raises:
            ValueError: 播放排空等待秒数不是正数。
        """

        if playout_wait_timeout_sec <= 0:
            raise ValueError("playout_wait_timeout_sec 必须大于零")

        # 该队列由 LiveKitMediaSession 持有，播放完成事件会回到同一控制面。
        self.event_queue = event_queue
        # 工厂在线程内执行，确保 AudioSource 与发布逻辑绑定到同一个专用 loop。
        self.assistant_audio_track_factory = assistant_audio_track_factory
        # LiveKit AudioSource 生命周期内只能保持一种采样率，所有 StreamVox 输出在发布前统一适配到这里。
        self.output_sample_rate = output_sample_rate

        # 保留旧调用方的配置入口；音频线程现在按通知等待，不轮询同步队列。
        self.idle_sleep_sec = idle_sleep_sec
        # start_timeout_sec 防止 LiveKit FFI 创建卡住时主线程永久等待本地 track。
        self.start_timeout_sec = start_timeout_sec
        # 底层播放完成信号若丢失，必须有界释放 Turn，不能阻塞后续权限和终态 Notice。
        self.playout_wait_timeout_sec = playout_wait_timeout_sec
        # now 由调用方注入，测试可以推进时间，生产使用 time.monotonic。
        self.now = now
        # sleep 由调用方注入，生产使用 asyncio.sleep，测试可记录延迟而不真实等待。
        self.sleep = sleep
        # latency_recorder 是可选本地耗时记录器，用于音频输出侧 marker 打点。
        self.latency_recorder = latency_recorder
        self.system_notice_playout_tracker = system_notice_playout_tracker
        self.audio_finished_handler = audio_finished_handler

        # _work_queue 承载音频 chunk 和 audio_finished，按助手运行时生产顺序串行处理。
        self._work_queue: NotifyQueue[QueuedAssistantAudioEvent] = NotifyQueue()
        # _stop_event 是同步停止信号，便于 LiveKit 生命周期回调直接触发收口。
        self._stop_event = threading.Event()
        # _ready_event 表示调度线程已经创建 publisher 和 track，start 可以安全返回 track。
        self._ready_event = threading.Event()
        # _idle_event 在没有待处理和处理中事件时置位，供 audio_finished 测试和关闭路径等待。
        self._idle_event = threading.Event()
        self._idle_event.set()

        # _lock 串行化 start/stop，避免并发生命周期切换创建多个音频输出线程。
        self._lock = threading.Lock()
        # 播放代际、请求身份和取消账本必须共享一个原子边界，否则同一请求可能既取消又完成。
        self._state_lock = threading.Lock()
        # _pending_count 统计尚未完成 task_done 的事件数量，避免依赖 Queue 私有字段。
        self._pending_count = 0
        # _clear_generation 每次清空待播音频时递增，用于识别已经失效的旧完成事件。
        self._clear_generation = 0
        # 同一清理代际只应用到底层音轨一次；新段开始前主动追平，迟到回调随后只能空操作。
        self._publisher_cleared_generation = 0
        # 活跃播放身份必须原子保存，取消时才能同时收口代际状态和 system notice 输出租约。
        self._active_playout: ActiveAssistantPlayout | None = None
        # 当前正在 await 发布或排空的请求用于请求级取消，避免只能清空整条共享音轨。
        self._inflight_playout: ActiveAssistantPlayout | None = None
        # 已取消请求会在音频入口丢弃迟到事件；有界墓碑避免缺少 audio_finished 时长期增长。
        self._cancelled_audio_requests: OrderedDict[
            str, _CancelledAudioRequest
        ] = OrderedDict()
        self._wake_audio_interrupted_at = float("-inf")

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._work_available: asyncio.Event | None = None
        self._publisher: AssistantAudioPublisherLike | None = None
        self._audio_track: object | None = None
        self.last_error: BaseException | None = None

    def bind_audio_finished_handler(
        self,
        handler: Callable[[str], object] | None,
    ) -> None:
        """绑定播放排空后的请求级观察端口。

        Args:
            handler: 接收 ``request_id`` 的同步回调；为空时关闭通知。
        """

        self.audio_finished_handler = handler

    @property
    def is_running(self) -> bool:
        """返回调度器线程是否仍在运行。

        Returns:
            线程存在且存活时返回 True，否则返回 False。
        """

        return self._thread is not None and self._thread.is_alive()

    def start(self) -> object:
        """启动专用音频输出线程并返回本地音频轨。

        Returns:
            可交给 LiveKit local_participant.publish_track 的本地音频轨。

        Raises:
            RuntimeError: 输出线程启动超时、启动失败或未创建本地音频轨。
        """

        with self._lock:
            if self.is_running:
                if self._audio_track is None:
                    raise RuntimeError("助手音频输出线程已运行但本地音频轨尚未就绪")
                return self._audio_track

            # 新生命周期必须清理旧状态，避免 stop 后重启时复用旧异常或旧 ready 信号。
            self._stop_event.clear()
            self._ready_event.clear()
            self._idle_event.set()
            self.last_error = None
            with self._state_lock:
                self._cancelled_audio_requests.clear()
            self._thread = threading.Thread(
                target=self._run_thread,
                name="companion-assistant-audio-output",
                daemon=True,
            )
            self._thread.start()

        if not self._ready_event.wait(timeout=self.start_timeout_sec):
            self.stop(timeout=2.0)
            raise RuntimeError("助手音频输出线程启动超时")

        if self.last_error is not None:
            raise RuntimeError("助手音频输出线程启动失败") from self.last_error

        if self._audio_track is None:
            raise RuntimeError("助手音频输出线程没有创建本地音频轨")

        return self._audio_track

    def enqueue_audio_event(self, event: RuntimeEvent) -> None:
        """把助手音频事件放入专用输出队列。

        Args:
            event: 助手运行时产出的音频相关事件。

        Raises:
            RuntimeError: 调度器尚未启动。
        """

        if not self.is_running:
            raise RuntimeError("助手音频输出调度器尚未启动")

        if self._discard_cancelled_audio_event(event):
            return

        # 入队前递增待处理计数，确保 wait_until_idle 不会在 put 与 worker get 的间隙误判为空。
        self._increment_pending_count()
        self._work_queue.put(
            QueuedAssistantAudioEvent(
                event=event,
                clear_generation=self._current_clear_generation(),
            )
        )
        self._notify_audio_loop()

    def clear_pending_audio(self, *, interrupt_before: float | None = None) -> tuple[str, ...]:
        """清空尚未发布的助手音频并清理底层播放缓存。

        用于插话取消、断连和关闭。当前正在发布的单帧允许自然结束。

        Args:
            interrupt_before: 手动打断的单调时间；提供时拦截此前产生的迟到唤醒短答。

        Returns:
            此次清理覆盖的请求编号，包括生成结束后仍在播放的请求。
        """

        # 完成事实、清空代际和取消事实共用一把锁，避免同一请求既完成又被取消。
        with self._state_lock:
            self._clear_generation += 1
            clear_generation = self._clear_generation
            inflight_playout = self._inflight_playout
            active_playout = self._active_playout
            self._active_playout = None
            if interrupt_before is not None:
                self._wake_audio_interrupted_at = max(self._wake_audio_interrupted_at, interrupt_before)
            removed_events = self._work_queue.remove_matching(lambda _queued: True)
            self._pending_count = max(0, self._pending_count - len(removed_events))
            if self._pending_count == 0:
                self._idle_event.set()
            identities = {
                identity.request_id: identity
                for queued_event in removed_events
                if (identity := self._playout_identity(queued_event.event)) is not None
            }
            for identity in (inflight_playout, active_playout):
                if identity is not None and identity.request_id is not None:
                    identities[identity.request_id] = identity
            for request_id, identity in identities.items():
                cancelled = self._remember_cancelled_request_locked(request_id)
                if not cancelled.playout_cancelled_emitted:
                    cancelled.playout_cancelled_emitted = True
                    # 生成可能已结束，队列里的 audio_finished 就是唯一剩余终态；
                    # 丢弃它之前必须留下取消事实，否则产品 Turn 会一直停在 processing。
                    self._emit_request_playout_cancelled(identity)
        for request_id in identities:
            self._finish_system_notice_playout(request_id)

        # AudioSource 内部 future 属于专用 loop；用 call_soon_threadsafe 让 clear_queue 在正确线程执行。
        loop = self._loop
        publisher = self._publisher
        if loop is None or publisher is None:
            return tuple(sorted(identities))
        loop.call_soon_threadsafe(lambda: self._apply_pending_publisher_clear(clear_generation))
        return tuple(sorted(identities))

    def clear_pending_audio_for_request(self, request_id: str) -> bool:
        """只取消指定助手请求的待播音频，不触碰其他请求。

        Args:
            request_id: 需要停止发声的助手请求编号。

        Returns:
            请求有效且已登记取消时返回真；空编号返回假。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return False

        with self._state_lock:
            already_cancelled = normalized_request_id in self._cancelled_audio_requests
            cancelled_request = self._remember_cancelled_request_locked(
                normalized_request_id
            )
            inflight_playout = (
                self._inflight_playout
                if self._inflight_playout is not None
                and self._inflight_playout.request_id == normalized_request_id
                else None
            )
            active_playout = (
                self._active_playout
                if self._active_playout is not None
                and self._active_playout.request_id == normalized_request_id
                else None
            )
            cancelled_playout = active_playout or inflight_playout
            should_emit_cancelled = (
                cancelled_playout is not None
                and not cancelled_request.playout_cancelled_emitted
            )
            if should_emit_cancelled:
                cancelled_request.playout_cancelled_emitted = True

        removed_events = self._work_queue.remove_matching(
            lambda queued: (
                self._event_request_id(queued.event) == normalized_request_id
            )
        )
        for _ in removed_events:
            self._decrement_pending_count()

        if cancelled_playout is None:
            cancelled_playout = self._first_playout_identity(removed_events)
        removed_finished = any(
            queued_event.event.type == "assistant.audio_finished"
            for queued_event in removed_events
        )
        with self._state_lock:
            cancelled_request = self._cancelled_audio_requests.get(
                normalized_request_id
            )
            if (
                cancelled_playout is not None
                and cancelled_request is not None
                and not cancelled_request.playout_cancelled_emitted
            ):
                cancelled_request.playout_cancelled_emitted = True
                should_emit_cancelled = True
            if removed_finished:
                self._cancelled_audio_requests.pop(normalized_request_id, None)

        if should_emit_cancelled and cancelled_playout is not None:
            self._emit_request_playout_cancelled(cancelled_playout)
        self._finish_system_notice_playout(normalized_request_id)

        # 底层 AudioSource 只包含当前串行处理的请求。仅当目标正处于发布或播放中时清理，
        # 回调执行时再次核对在途身份，避免 A 的迟到回调清掉已经开始处理的 B。
        if active_playout is not None or inflight_playout is not None:
            self._schedule_publisher_clear_for_request(normalized_request_id)

        return not already_cancelled or bool(removed_events) or cancelled_playout is not None

    def wait_until_idle(self, *, timeout: float | None = None) -> bool:
        """等待调度器处理完当前已入队音频事件。

        Args:
            timeout: 最大等待秒数，None 表示无限等待。

        Returns:
            全部待处理事件完成时返回 True，超时返回 False。
        """

        return self._idle_event.wait(timeout=timeout)

    def stop(self, *, timeout: float | None = 2.0) -> None:
        """停止助手音频输出线程并释放 publisher 资源。

        Args:
            timeout: 等待线程退出的最大秒数。
        """

        with self._lock:
            thread = self._thread
            if thread is None:
                return

            self.clear_pending_audio()
            self._stop_event.set()
            self._work_queue.notify()
            self._notify_audio_loop()

        thread.join(timeout=timeout)

        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                # 线程已经彻底退出，不会再有旧音频事件到达，可以安全丢弃全部墓碑。
                with self._state_lock:
                    self._cancelled_audio_requests.clear()

    def _run_thread(self) -> None:
        """调度器后台线程入口。

        创建专用事件循环并运行音频输出主循环。
        """

        loop = asyncio.new_event_loop()
        self._loop = loop
        self._work_available = asyncio.Event()
        asyncio.set_event_loop(loop)
        try:
            self._publisher, self._audio_track = self._create_audio_track(loop)
            self._ready_event.set()
            loop.run_until_complete(self._run_loop())
        except BaseException as exception:
            self.last_error = exception
            logger.exception("Assistant audio output scheduler failed", exc_info=exception)
            self._ready_event.set()
        finally:
            try:
                # 无论是正常 stop 还是事件循环异常退出，都要尽量释放底层 AudioSource 资源。
                loop.run_until_complete(self._close_publisher())
            except BaseException as close_exception:
                # 如果主循环已经失败，保留原始异常作为 last_error；关闭失败单独记日志即可。
                if self.last_error is None:
                    self.last_error = close_exception
                logger.exception(
                    "Assistant audio output scheduler failed to close publisher",
                    exc_info=close_exception,
                )
                self._ready_event.set()
            finally:
                loop.close()
                self._loop = None
                self._work_available = None

    def _create_audio_track(self, loop: asyncio.AbstractEventLoop) -> tuple[AssistantAudioPublisherLike, object]:
        """在专用 loop 内创建 publisher 和本地音频轨。

        Args:
            loop: 当前调度线程创建的事件循环。

        Returns:
            绑定到该 loop 的 publisher，以及可发布的本地音频轨。
        """

        # 兼容注入 loop 的真实工厂和无参测试工厂，避免测试替身被迫感知事件循环。
        signature = inspect.signature(self.assistant_audio_track_factory)
        can_accept_loop = "loop" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )

        if can_accept_loop:
            return self.assistant_audio_track_factory(loop=loop)

        return self.assistant_audio_track_factory()

    async def _run_loop(self) -> None:
        """持续处理助手音频输出事件。

        按入队顺序发布 audio_chunk，并在 audio_finished 时等待底层播放完成后投递控制事件。
        """

        while not self._stop_event.is_set():
            # 同步等待会堵住整个 asyncio loop，使空闲时的音轨清理永远得不到执行。
            # 先清通知再读队列，生产者的后续通知就不会在检查空队列的间隙丢失。
            work_available = self._work_available
            if work_available is None:
                return
            work_available.clear()
            try:
                queued_event = self._work_queue.get_nowait()
            except Empty:
                await work_available.wait()
                continue

            try:
                await self._handle_audio_event(queued_event)
            finally:
                self._work_queue.task_done()
                self._decrement_pending_count()

    async def _handle_audio_event(self, queued_event: QueuedAssistantAudioEvent) -> None:
        """处理单条助手音频事件。

        Args:
            queued_event: 带清空代际的助手运行时音频事件。


        Raises:
            RuntimeError: publisher 尚未初始化。
            ValueError: 音频 chunk 的 payload 字段非法。
        """

        if self._publisher is None:
            raise RuntimeError("助手音频 publisher 尚未初始化")

        event = queued_event.event
        inflight_playout = self._playout_identity(event)
        self._set_inflight_playout(inflight_playout)
        try:
            if queued_event.clear_generation != self._current_clear_generation():
                # 清理时本事件可能刚出队、还没登记在途身份。仍须用本事件补齐取消终态。
                request_id = self._event_request_id(event)
                if request_id is not None:
                    self.clear_pending_audio_for_request(request_id)
                    self._discard_cancelled_audio_event(event)
                return
            if self._discard_cancelled_audio_event(event):
                return
            # event 是原始运行时事实；queued_event 额外提供取消代际用于判断完成事件是否过期。
            if event.type in {"assistant.audio_chunk", "wake_response.audio_chunk"}:
                if event.type == "wake_response.audio_chunk":
                    await self._wait_before_wake_response_playback(event)
                await self._publish_audio_chunk(queued_event)
                return

            if event.type == "wake_response.audio_finished":
                completion = await self._wait_for_playout_completion(event)
                self._commit_audio_finished_event(
                    event,
                    expected_clear_generation=queued_event.clear_generation,
                    completion=completion,
                    clear_active_playout=False,
                    enqueue_completion_event=False,
                )
                return

            if event.type == "assistant.audio_finished":
                completion = await self._wait_for_playout_completion(event)
                committed = self._commit_audio_finished_event(
                    event,
                    expected_clear_generation=queued_event.clear_generation,
                    completion=completion,
                    clear_active_playout=True,
                )
                request_id = self._event_request_id(event)
                if committed and request_id is not None:
                    self._finish_system_notice_playout(request_id)
                    if self.audio_finished_handler is not None:
                        self.audio_finished_handler(request_id)
                return

            # 理论上 bridge 只会投递音频相关事件；未知类型回到控制面比静默丢弃更容易暴露接线错误。
            self.event_queue.put(event)
        finally:
            self._clear_inflight_playout(inflight_playout)

    async def _wait_for_playout_completion(
        self,
        event: RuntimeEvent,
    ) -> PlayoutCompletion:
        """有界等待底层播放排空，并把故障转换成可继续运行的完成事实。

        Args:
            event: 正在等待播放完成的音频结束事件。

        Returns:
            正常排空、等待超时或底层异常对应的稳定完成分类。

        Raises:
            RuntimeError: publisher 尚未初始化。
        """

        publisher = self._publisher
        if publisher is None:
            raise RuntimeError("助手音频 publisher 尚未初始化")
        try:
            await asyncio.wait_for(
                publisher.wait_for_playout(),
                timeout=self.playout_wait_timeout_sec,
            )
            return "drained"
        except TimeoutError:
            logger.warning(
                "等待助手音频播放完成超时，按未确认状态释放 Turn "
                "request_id=%s generation_id=%s timeout_sec=%.3f",
                event.payload.get("request_id"),
                event.payload.get("generation_id"),
                self.playout_wait_timeout_sec,
            )
            self._clear_publisher_queue_after_unconfirmed()
            return "unconfirmed_timeout"
        except Exception:
            logger.exception(
                "等待助手音频播放完成失败，按未确认状态释放 Turn "
                "request_id=%s generation_id=%s",
                event.payload.get("request_id"),
                event.payload.get("generation_id"),
            )
            self._clear_publisher_queue_after_unconfirmed()
            return "unconfirmed_error"

    def _clear_publisher_queue_after_unconfirmed(self) -> None:
        """清理无法确认状态的底层缓存，同时保证清理异常不会杀死调度线程。"""

        publisher = self._publisher
        if publisher is None:
            return
        try:
            publisher.clear_queue()
        except Exception:
            logger.exception("清理未确认的助手音频缓存失败")

    async def _publish_audio_chunk(self, queued_event: QueuedAssistantAudioEvent) -> None:
        """解码并发布单条 assistant.audio_chunk。

        Args:
            queued_event: 携带入队清空代际和原始音频载荷的队列项。


        Raises:
            RuntimeError: publisher 尚未初始化。
            ValueError: payload 缺少合法的 pcm16_b64 字符串。
        """

        if self._publisher is None:
            raise RuntimeError("助手音频 publisher 尚未初始化")

        # AssistantOutputLanes 使用 pcm16_b64 承载二进制音频，控制信令不直接承载原始 bytes。
        event = queued_event.event
        raw_pcm16_b64 = event.payload.get("pcm16_b64")
        if not isinstance(raw_pcm16_b64, str):
            raise ValueError("assistant.audio_chunk 缺少 pcm16_b64 字符串")

        # sample_rate 来自 TTS provider；缺省时按固定输出采样率处理，兼容旧协议事件。
        source_sample_rate = int(event.payload.get("sample_rate", self.output_sample_rate))
        pcm16 = base64.b64decode(raw_pcm16_b64)
        output_pcm16 = resample_pcm16_mono(
            pcm16=pcm16,
            source_sample_rate=source_sample_rate,
            target_sample_rate=self.output_sample_rate,
        )

        # 唤醒短答可能刚结束延迟等待；打断后不能把这一段旧音频重新送进底层缓存。
        if (
            queued_event.clear_generation != self._current_clear_generation()
            or self._discard_cancelled_audio_event(event)
        ):
            return
        # 新段不能依赖 call_soon 的执行顺序；清理回调晚到时在首帧前主动完成同一代际的清理。
        self._apply_pending_publisher_clear(queued_event.clear_generation)
        await self._publisher.publish_pcm16(pcm16=output_pcm16, sample_rate=self.output_sample_rate)
        # publish 的 await 期间可能发生清空或请求级取消；两项检查与播放身份登记
        # 必须在同一临界区完成，禁止取消后重新发布 started。
        if event.type == "assistant.audio_chunk":
            self._emit_playout_started_if_needed(
                event,
                expected_clear_generation=queued_event.clear_generation,
            )

    async def _wait_before_wake_response_playback(self, event: RuntimeEvent) -> None:
        """按唤醒回复事件声明的延迟等待后再发布音频。

        Args:
            event: 唤醒回复音频 chunk 事件。
        """

        raw_delay = event.payload.get("playback_delay_s", 0.0)
        delay_s = raw_delay if isinstance(raw_delay, (int, float)) and not isinstance(raw_delay, bool) else 0.0
        if delay_s <= 0:
            return
        result = self.sleep(float(delay_s))
        if asyncio.iscoroutine(result):
            await result

    async def _close_publisher(self) -> None:
        """关闭底层 publisher 资源。

        如果 publisher 暴露 aclose，则在线程所属 loop 内释放 LiveKit AudioSource。
        """

        publisher = self._publisher
        if publisher is None:
            return

        close_publisher = getattr(publisher, "aclose", None)
        if not callable(close_publisher):
            return

        result = close_publisher()
        if asyncio.iscoroutine(result):
            await result

    def _increment_pending_count(self) -> None:
        """递增待处理事件计数。

        入队前标记调度器非空闲，防止 wait_until_idle 竞态返回。
        """

        with self._state_lock:
            self._pending_count += 1
            self._idle_event.clear()

    def _decrement_pending_count(self) -> None:
        """递减待处理事件计数。

        事件处理或清队列完成后更新空闲状态。
        """

        with self._state_lock:
            self._pending_count = max(0, self._pending_count - 1)
            if self._pending_count == 0:
                self._idle_event.set()

    def _notify_audio_loop(self) -> None:
        """唤醒异步空闲等待，让音频入队和停止都不依赖同步轮询。"""

        loop = self._loop
        work_available = self._work_available
        if loop is not None and work_available is not None:
            loop.call_soon_threadsafe(work_available.set)

    def _apply_pending_publisher_clear(self, expected_generation: int) -> None:
        """在音频线程中只应用一次仍有效的全局清理代际。

        Args:
            expected_generation: 回调或即将发布的音频绑定的清理代际。
        """

        with self._state_lock:
            if (
                self._clear_generation != expected_generation
                or self._publisher_cleared_generation >= expected_generation
                or self._publisher is None
            ):
                return
            self._publisher.clear_queue()
            self._publisher_cleared_generation = expected_generation

    def _current_clear_generation(self) -> int:
        """读取当前清空代际。

        Returns:
            当前 clear_generation 整数。
        """

        with self._state_lock:
            return self._clear_generation

    def _advance_clear_generation(self) -> None:
        """推进清空代际。

        取消或关闭时让已经在处理中的旧 audio_finished 失效。
        """

        with self._state_lock:
            self._clear_generation += 1

    def _emit_playout_started_if_needed(
        self,
        event: RuntimeEvent,
        *,
        expected_clear_generation: int | None = None,
    ) -> None:
        """首次真实发布音频后投递播放开始事件。

        Args:
            event: 已成功发布的助手音频块事件。
            expected_clear_generation: 音频入队时的清空代际；为空时跳过代际校验。
        """

        generation_id = event.payload.get("generation_id")
        generation_id_value = generation_id if isinstance(generation_id, str) else None
        request_id = event.payload.get("request_id")
        request_id_value = request_id if isinstance(request_id, str) else None
        delivery_id = event.payload.get("delivery_id")
        delivery_id_value = delivery_id if isinstance(delivery_id, str) else None
        with self._state_lock:
            if (
                expected_clear_generation is not None
                and expected_clear_generation != self._clear_generation
            ):
                return
            if (
                request_id_value is not None
                and request_id_value in self._cancelled_audio_requests
            ):
                return
            if self._active_playout is not None:
                return
            self._active_playout = ActiveAssistantPlayout(
                generation_id=generation_id_value,
                request_id=request_id_value,
                delivery_id=delivery_id_value,
            )

        # 首帧写入 LiveKit，落盘首帧发布 marker；端到端汇总交给离线分析按 turn_id 聚合
        if self.latency_recorder is not None:
            source_turn_id = event.payload.get("source_turn_id")
            if isinstance(source_turn_id, str) and source_turn_id:
                self.latency_recorder.record_marker(
                    trace_id=source_turn_id,
                    marker="livekit_first_frame_published",
                    at_wall=self.now(),
                )

        payload: dict[str, object] = {}
        if generation_id_value is not None:
            payload["generation_id"] = generation_id_value
        if request_id_value is not None:
            payload["request_id"] = request_id_value
        if delivery_id_value is not None:
            payload["delivery_id"] = delivery_id_value
        self.event_queue.put(
            RuntimeEvent(
                type="assistant.audio_publishing_started",
                payload=payload,
                produced_at=self.now(),
                source="assistant_runtime",
            )
        )

    def _emit_playout_cancelled_if_needed(self) -> ActiveAssistantPlayout | None:
        """播放队列被清空时投递播放取消事件并返回被取消身份。"""

        active_playout = self._clear_active_playout()
        if active_playout is None:
            return None

        self._emit_request_playout_cancelled(active_playout)
        return active_playout

    def _emit_request_playout_cancelled(
        self,
        active_playout: ActiveAssistantPlayout,
    ) -> None:
        """按已捕获的请求身份投递播放取消事实。

        Args:
            active_playout: 被精确取消的请求、generation 与交付身份。
        """

        payload: dict[str, object] = {}
        if active_playout.generation_id:
            payload["generation_id"] = active_playout.generation_id
        if active_playout.request_id:
            payload["request_id"] = active_playout.request_id
        if active_playout.delivery_id:
            payload["delivery_id"] = active_playout.delivery_id
        self.event_queue.put(
            RuntimeEvent(
                type="assistant.audio_publishing_cancelled",
                payload=payload,
                produced_at=self.now(),
                source="assistant_runtime",
            )
        )

    def _discard_cancelled_audio_event(self, event: RuntimeEvent) -> bool:
        """在入队前丢弃已经取消请求的迟到音频事件。"""

        with self._state_lock:
            if event.type.startswith("wake_response.") and event.produced_at <= self._wake_audio_interrupted_at:
                return True
        request_id = self._event_request_id(event)
        if request_id is None:
            return False
        identity = self._playout_identity(event)
        with self._state_lock:
            cancelled_request = self._cancelled_audio_requests.get(request_id)
            if cancelled_request is None:
                return False
            should_emit_cancelled = not cancelled_request.playout_cancelled_emitted
            if should_emit_cancelled:
                cancelled_request.playout_cancelled_emitted = True
            if event.type == "assistant.audio_finished":
                self._cancelled_audio_requests.pop(request_id, None)
                if (
                    self._active_playout is not None
                    and self._active_playout.request_id == request_id
                ):
                    self._active_playout = None
                if self._inflight_playout == identity:
                    self._inflight_playout = None
            else:
                self._cancelled_audio_requests.move_to_end(request_id)
        if should_emit_cancelled and identity is not None:
            self._emit_request_playout_cancelled(identity)
        return True

    def _remember_cancelled_request_locked(
        self,
        request_id: str,
    ) -> _CancelledAudioRequest:
        """在持锁状态下登记取消墓碑，并淘汰最旧的过期身份。

        Args:
            request_id: 需要过滤迟到音频的助手请求编号。

        Returns:
            当前请求对应的取消状态。
        """

        cancelled_request = self._cancelled_audio_requests.get(request_id)
        if cancelled_request is None:
            cancelled_request = _CancelledAudioRequest()
            self._cancelled_audio_requests[request_id] = cancelled_request
        self._cancelled_audio_requests.move_to_end(request_id)
        while len(self._cancelled_audio_requests) > _MAX_CANCELLED_REQUEST_TOMBSTONES:
            self._cancelled_audio_requests.popitem(last=False)
        return cancelled_request

    def _set_inflight_playout(
        self,
        playout: ActiveAssistantPlayout | None,
    ) -> None:
        """登记当前正在处理的音频请求身份。"""

        with self._state_lock:
            self._inflight_playout = playout

    def _clear_inflight_playout(
        self,
        playout: ActiveAssistantPlayout | None,
    ) -> None:
        """只清理仍与本次处理相同的在途音频身份。"""

        with self._state_lock:
            if self._inflight_playout == playout:
                self._inflight_playout = None

    def _schedule_publisher_clear_for_request(self, request_id: str) -> None:
        """在音频线程中核对身份后清理指定请求的底层缓存。

        Args:
            request_id: 已经登记取消的助手请求编号。

        Returns:
            无。
        """

        loop = self._loop
        publisher = self._publisher

        def clear_if_request_is_still_current() -> None:
            """只在目标请求仍占用共享音轨时执行同步清理。"""

            with self._state_lock:
                active_matches = (
                    self._active_playout is not None
                    and self._active_playout.request_id == request_id
                )
                inflight_matches = (
                    self._inflight_playout is not None
                    and self._inflight_playout.request_id == request_id
                )
                if not active_matches and not inflight_matches:
                    return
                if active_matches:
                    self._active_playout = None
            if publisher is None:
                return
            try:
                publisher.clear_queue()
            except Exception:
                logger.exception(
                    "清理已取消请求的助手音频缓存失败 request_id=%s",
                    request_id,
                )

        if loop is None or publisher is None:
            # publisher 只能在所属音频 loop 使用。loop 不存在时只收口本地身份，
            # 不能从调用线程直接碰底层 AudioSource。
            with self._state_lock:
                if (
                    self._active_playout is not None
                    and self._active_playout.request_id == request_id
                ):
                    self._active_playout = None
            return
        try:
            loop.call_soon_threadsafe(clear_if_request_is_still_current)
        except RuntimeError:
            # 音频 loop 已关闭时不会再有真实播放；仍要清掉本地活跃身份，避免污染下一生命周期。
            logger.exception(
                "已取消请求的音频清理无法进入调度线程 request_id=%s",
                request_id,
            )
            with self._state_lock:
                if (
                    self._active_playout is not None
                    and self._active_playout.request_id == request_id
                ):
                    self._active_playout = None

    def _commit_audio_finished_event(
        self,
        event: RuntimeEvent,
        *,
        expected_clear_generation: int,
        completion: PlayoutCompletion,
        clear_active_playout: bool,
        enqueue_completion_event: bool = True,
    ) -> bool:
        """原子提交一条仍有效的音频完成事实。

        Args:
            event: 等待底层排空后的完成事件。
            expected_clear_generation: 事件入队时绑定的清空代际。
            completion: 底层排空得到的稳定完成分类。
            clear_active_playout: 是否同时结束助手请求的活跃播放身份。
            enqueue_completion_event: 是否把完成事实重新交给共享事件队列；唤醒短答
                已经在此调度器内完成播放，不能再次回到 Drainer，否则会被重新送回本方法。

        Returns:
            完成事实仍属于当前播放代际时返回真；请求已取消或代际失效时返回假。
        """

        request_id = self._event_request_id(event)
        identity = self._playout_identity(event)
        with self._state_lock:
            if request_id is not None and request_id in self._cancelled_audio_requests:
                self._cancelled_audio_requests.pop(request_id, None)
                if (
                    self._active_playout is not None
                    and self._active_playout.request_id == request_id
                ):
                    self._active_playout = None
                if self._inflight_playout == identity:
                    self._inflight_playout = None
                return False
            if expected_clear_generation != self._clear_generation:
                return False
            if clear_active_playout:
                self._active_playout = None
            if self._inflight_playout == identity:
                self._inflight_playout = None
            if enqueue_completion_event:
                # 完成事件与身份清理必须在同一把锁内提交；否则请求级取消可以在两者之间
                # 插入，再为同一 request_id 发布一条 cancelled。
                self.event_queue.put(
                    replace(
                        event,
                        payload={**event.payload, "playout_completion": completion},
                        produced_at=self.now(),
                    )
                )
            return True

    @classmethod
    def _first_playout_identity(
        cls,
        queued_events: list[QueuedAssistantAudioEvent],
    ) -> ActiveAssistantPlayout | None:
        """从被移除的队列项中读取第一份完整请求身份。"""

        for queued_event in queued_events:
            identity = cls._playout_identity(queued_event.event)
            if identity is not None:
                return identity
        return None

    @staticmethod
    def _playout_identity(event: RuntimeEvent) -> ActiveAssistantPlayout | None:
        """从音频事件读取请求级播放身份。"""

        request_id = AssistantAudioOutputScheduler._event_request_id(event)
        if request_id is None:
            return None
        generation_id = event.payload.get("generation_id")
        delivery_id = event.payload.get("delivery_id")
        return ActiveAssistantPlayout(
            generation_id=generation_id if isinstance(generation_id, str) else None,
            request_id=request_id,
            delivery_id=delivery_id if isinstance(delivery_id, str) else None,
        )

    @staticmethod
    def _event_request_id(event: RuntimeEvent) -> str | None:
        """读取音频事件中非空的助手请求编号。"""

        request_id = event.payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return None
        return request_id.strip()

    def _finish_system_notice_playout(self, request_id: str) -> None:
        """在真实播放完成或取消后释放权限 System Notice 映射。"""

        if self.system_notice_playout_tracker is None:
            return
        self.system_notice_playout_tracker.finish_request(request_id)

    def _clear_active_playout(self) -> ActiveAssistantPlayout | None:
        """清理当前真实播放中的助手请求身份。

        Returns:
            ActiveAssistantPlayout | None: 清理前的播放身份；没有活跃播放时返回 None。
        """

        with self._state_lock:
            active_playout = self._active_playout
            self._active_playout = None
            return active_playout
