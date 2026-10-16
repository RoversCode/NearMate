"""串行冻结 Reminder Notice，并按请求身份收口响铃尝试。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from threading import RLock
from typing import Literal

from companion.activities.notice.models import NoticeDeliveryCommand
from companion.observability.logging_config import get_backend_logger
from companion.timed_reminders.models import TimedReminder
from companion.timed_reminders.repository import MAX_TIMED_REMINDER_RING_COUNT
from companion.timed_reminders.service import TimedReminderService


logger = get_backend_logger(__name__)


_DEFAULT_RETRY_DELAY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class TimedReminderNoticeBatch:
    """保存一条独立 Reminder Notice 已冻结的全部事实。

    Args:
        request_id: 本次独立助手请求编号。
        runtime_id: 冻结批次所属的 Backend 运行编号。
        reminders: 开始消费前一次性冻结的可响任务。
        interrupted_user: 本次接管是否真实清除了未完成用户语音轮。
        interrupted_assistant: 本次接管是否真实终止了普通助手输出。
    """

    request_id: str
    runtime_id: str
    reminders: tuple[TimedReminder, ...]
    interrupted_user: bool
    interrupted_assistant: bool

    def prompt_payload(self) -> list[dict[str, object]]:
        """生成 Reminder Notice Prompt 使用的最小事项列表。

        Returns:
            只包含事项和原定触发时刻的有序列表。
        """

        return [
            {
                "content": reminder.content,
                "trigger_at": reminder.trigger_at.isoformat(),
            }
            for reminder in self.reminders
        ]


@dataclass(slots=True)
class _ReminderRequestState:
    """记录一条 Reminder Notice 从冻结到结算的全部边界。

    Args:
        batch: 当前请求冻结的提醒事实。
        stage: 请求尚未入队、正在写入响铃次数或已经完成计数。
        text_finished: Foreground 是否已经结束正文生成。
        audio_finished: TTS 是否已经播完或确认没有音频。
        finish_requested: 取消、失败或替代是否要求立即结束本次尝试。
        unsettled_attempts: 成功计数后尚未结算的提醒，保留各自本次响铃次数。
        cancellation_issued: 是否已经向媒体端口发送请求级取消命令。
        finishing: 是否已经安排唯一的领域结算任务。
    """

    batch: TimedReminderNoticeBatch
    stage: Literal["prepared", "enqueuing", "enqueued"] = "prepared"
    text_finished: bool = False
    audio_finished: bool = False
    finish_requested: bool = False
    unsettled_attempts: dict[str, TimedReminder] = field(default_factory=dict)
    cancellation_issued: bool = False
    finishing: bool = False


class TimedReminderDeliveryCoordinator:
    """独占待响集合、当前请求和请求到提醒的短期账本。

    外部线程只能通过公开通知入口投递事实；实际 ``asyncio.Event``、待响集合和
    请求账本都只在 ``start`` 所在线程的事件循环内修改。

    Args:
        service: 提供权威提醒快照和响铃状态转换的应用服务。
        retry_delay_seconds: 请求或领域结算暂时失败后，最迟多久重新检查一次。
    """

    def __init__(
        self,
        *,
        service: TimedReminderService,
        retry_delay_seconds: float = _DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        if retry_delay_seconds <= 0:
            raise ValueError("Reminder Notice 重试间隔必须大于 0")
        self._service = service
        self._retry_delay_seconds = retry_delay_seconds
        self._pending: OrderedDict[str, TimedReminder] = OrderedDict()
        self._pending_attempt_settlements: OrderedDict[tuple[str, ...], None] = (
            OrderedDict()
        )
        self._requests: dict[str, _ReminderRequestState] = {}
        self._current_request_id: str | None = None
        self._wake_event: asyncio.Event | None = None
        self._worker: asyncio.Task[None] | None = None
        self._state_tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_lock = RLock()
        self._request_notice: Callable[[], bool] | None = None
        self._cancel_requests: Callable[[tuple[str, ...]], None] | None = None
        self._requests_paused = False
        self._stopping = False

    def bind_request_notice(self, callback: Callable[[], bool]) -> None:
        """绑定媒体会话发起下一条 Reminder Notice 的同步端口。

        Args:
            callback: 在 owner 事件循环内尝试冻结并提交请求的函数。
        """

        self._request_notice = callback

    def bind_cancel_requests(
        self,
        callback: Callable[[tuple[str, ...]], None],
    ) -> None:
        """绑定按请求编号撤销生成和音频的媒体端口。

        Args:
            callback: 接收需要立即失效的 Reminder Notice 请求编号。
        """

        self._cancel_requests = callback

    def start(self) -> None:
        """在当前事件循环启动唯一串行 owner。"""

        with self._lifecycle_lock:
            if self._worker is not None and not self._worker.done():
                return
            self._loop = asyncio.get_running_loop()
            self._wake_event = asyncio.Event()
            self._requests_paused = False
            self._stopping = False
            self._worker = asyncio.create_task(
                self._run(),
                name="timed-reminder-delivery",
            )
        self.wake()

    async def shutdown(self) -> None:
        """释放所有请求占用并停止串行 owner。"""

        if not self._is_owner_loop():
            await self._run_on_owner(self._shutdown_on_owner())
            return
        await self._shutdown_on_owner()

    async def _shutdown_on_owner(self) -> None:
        """在 Coordinator 所属事件循环释放账本并停止 worker。"""

        self._require_owner_loop()
        self._requests_paused = True
        self._stopping = True
        self._set_wake_event()
        await self._drain_state_tasks()
        await self._release_all_requests()
        with self._lifecycle_lock:
            worker = self._worker
            self._worker = None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        with self._lifecycle_lock:
            self._loop = None
            self._wake_event = None

    async def drain(self) -> None:
        """让已经投递到 owner 的通知和领域写入完成。"""

        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def wake(self) -> None:
        """从任意线程请求重新检查权威可响集合。"""

        self._post_to_owner(self._set_wake_event)

    def resume_requests(self) -> None:
        """从任意线程通知 Coordinator 新媒体会话已经可以接单。"""

        self._post_to_owner(self._resume_requests_on_owner)

    def has_pending(self) -> bool:
        """判断 owner 当前是否保存尚未冻结的可响任务。

        Returns:
            至少有一条等待下一次 Notice 的任务时返回真。
        """

        return bool(self._pending)

    def freeze_ready_batch(
        self,
        *,
        request_id: str,
        interrupted_user: bool,
        interrupted_assistant: bool,
    ) -> TimedReminderNoticeBatch | None:
        """在开始消费前冻结当时全部可响任务。

        Args:
            request_id: 即将提交的独立助手请求编号。
            interrupted_user: 输入 takeover 是否真的丢弃了未完成用户语音轮。
            interrupted_assistant: 是否真的终止了普通助手正文或 TTS。

        Returns:
            冻结批次；当前已有 Reminder Notice 或没有可响任务时返回空。
        """

        self._require_owner_loop()
        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ValueError("Reminder Notice 必须绑定非空 request_id")
        if self._requests_paused or self._stopping:
            return None
        if self._current_request_id is not None:
            return None

        self._refresh_pending()
        if not self._pending:
            return None
        snapshot = self._service.snapshot()
        reminders = tuple(self._pending.values())
        self._pending.clear()
        batch = TimedReminderNoticeBatch(
            request_id=normalized_request_id,
            runtime_id=snapshot.runtime_id,
            reminders=reminders,
            interrupted_user=interrupted_user,
            interrupted_assistant=interrupted_assistant,
        )
        self._requests[normalized_request_id] = _ReminderRequestState(batch=batch)
        self._current_request_id = normalized_request_id
        return batch

    def commit_request_enqueued(self, request_id: str) -> bool:
        """确认冻结请求已经进入 Bridge，并异步消耗一次响铃额度。

        Args:
            request_id: 已冻结并成功入队的 Reminder Notice 请求编号。

        Returns:
            首次提交有效请求时返回真。
        """

        self._require_owner_loop()
        state = self._requests.get(request_id.strip())
        if state is None or state.stage != "prepared":
            return False
        state.stage = "enqueuing"
        self._start_state_task(self._mark_enqueued(request_id.strip()))
        return True

    def rollback_prepared_request(self, request_id: str) -> bool:
        """释放一条尚未入队的冻结请求，不消耗响铃次数。

        Args:
            request_id: 构造或入队失败的 Reminder Notice 请求编号。

        Returns:
            当前确实释放了一条 prepared 请求时返回真。
        """

        self._require_owner_loop()
        normalized_request_id = request_id.strip()
        state = self._requests.get(normalized_request_id)
        if state is None or state.stage != "prepared":
            return False
        self._requests.pop(normalized_request_id, None)
        if self._current_request_id == normalized_request_id:
            self._current_request_id = None
        self.wake()
        return True

    def apply_delivery(self, command: NoticeDeliveryCommand) -> None:
        """从任意线程接收 Reminder Notice 正文或失败终态。

        Args:
            command: 公共输出链路归一化后的请求级交付结果。

        回执只负责向 owner 投递命令，不跨线程读取请求账本。迟到或不属于
        Reminder 的请求会由 owner 按 ``request_id`` 安静忽略。
        """

        request_id = command.request_id.strip()
        if not request_id:
            return
        self._post_to_owner(lambda: self._apply_delivery_on_owner(command))

    def observe_audio_finished(self, request_id: str) -> None:
        """从任意线程接收底层 TTS 真正排空事实。

        Args:
            request_id: 音频完成事件所属请求编号。

        回执只投递到 owner；是否属于当前 Reminder Notice 由 owner 账本判断。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return
        self._post_to_owner(
            lambda: self._finish_after_audio_on_owner(normalized_request_id)
        )

    def dismiss_reminders(self, reminder_ids: tuple[str, ...]) -> None:
        """移除已关闭任务，并撤销任何包含这些任务的 Notice 请求。

        Args:
            reminder_ids: 服务已经原子关闭的提醒编号。

        关闭回调只提交事实；实际请求取消由 owner 账本统一执行。
        """

        normalized_ids = tuple(
            dict.fromkeys(
                reminder_id.strip()
                for reminder_id in reminder_ids
                if reminder_id.strip()
            )
        )
        if not normalized_ids:
            return
        self._post_to_owner(lambda: self._dismiss_on_owner(normalized_ids))

    async def release_active_requests(self) -> None:
        """在媒体重建或助手热替换前释放所有请求占用。"""

        if not self._is_owner_loop():
            await self._run_on_owner(self._release_active_requests_on_owner())
            return
        await self._release_active_requests_on_owner()

    async def _release_active_requests_on_owner(self) -> None:
        """在 Coordinator 所属事件循环排空状态任务并释放请求。"""

        self._require_owner_loop()
        # release 返回后旧媒体会话仍在继续关闭。门禁必须保持到新会话显式恢复，
        # 否则结算中的 await 会让 worker 把新批次重新交给正在销毁的 Bridge。
        self._requests_paused = True
        self._set_wake_event()
        await self._drain_state_tasks()
        await self._release_all_requests()

    async def _run(self) -> None:
        """用电平触发循环串行检查状态并发起下一条 Notice。"""

        while not self._stopping:
            wake_event = self._wake_event
            if wake_event is None:
                return
            await wake_event.wait()
            while not self._stopping:
                # 先清除旧电平，再读取全部权威状态；检查期间到达的新通知会重新置位，
                # 因此不会落入“刚检查完、尚未等待”之间的丢唤醒窗口。
                wake_event.clear()
                self._refresh_pending()
                retry_settlement = await self._settle_pending_attempts()
                if wake_event.is_set():
                    continue
                if retry_settlement:
                    await self._wait_for_wake_or_retry(wake_event)
                    continue
                if self._requests_paused:
                    break
                retry_request = False
                if self._current_request_id is None and self._pending:
                    requester = self._request_notice
                    if requester is not None:
                        try:
                            accepted = requester()
                        except Exception:
                            logger.exception("提交 Reminder Notice 请求失败")
                            accepted = False
                        retry_request = (
                            not accepted
                            and self._current_request_id is None
                            and bool(self._pending)
                        )
                if wake_event.is_set():
                    continue
                if retry_request:
                    # 正常恢复事件可以立刻推进；即使媒体层漏掉恢复通知，超时后也会
                    # 重新读取权威状态，避免任务永久停在 ringing。
                    await self._wait_for_wake_or_retry(wake_event)
                    continue
                if not wake_event.is_set():
                    break

    def _refresh_pending(self) -> None:
        """从权威服务刷新尚未被请求占用的可响集合。"""

        reserved_ids = {
            reminder.reminder_id
            for request in self._requests.values()
            for reminder in request.batch.reminders
        }
        ready = self._service.ready_to_ring()
        ready_ids = {reminder.reminder_id for reminder in ready}
        for reminder_id in tuple(self._pending):
            if reminder_id not in ready_ids:
                self._pending.pop(reminder_id, None)
        for reminder in ready:
            if reminder.reminder_id not in reserved_ids:
                self._pending[reminder.reminder_id] = reminder

    async def _mark_enqueued(self, request_id: str) -> None:
        """写入响铃次数，再处理计数期间提前到达的输出回执。

        Args:
            request_id: 已经进入 Bridge、正在消耗响铃次数的请求编号。
        """

        state = self._requests.get(request_id)
        if state is None or state.stage != "enqueuing":
            return
        reminder_ids = tuple(
            reminder.reminder_id for reminder in state.batch.reminders
        )

        try:
            marked = await self._service.mark_ring_enqueued(reminder_ids)
        except Exception:
            logger.exception("Reminder Notice 入队计数失败")
            current = self._requests.get(request_id)
            if current is state:
                self._requests.pop(request_id, None)
                if self._current_request_id == request_id:
                    self._current_request_id = None
                if not state.cancellation_issued:
                    state.cancellation_issued = True
                    self._cancel_requests_safely((request_id,))
            self.wake()
            return

        marked_ids = tuple(reminder.reminder_id for reminder in marked)
        current = self._requests.get(request_id)
        if current is not state:
            # 计数期间发生关闭时，请求已经被撤销；仍成功计数的剩余项目必须完成
            # 本次尝试，否则会永久停在 next_ring_at=None。
            if marked_ids:
                await self._finish_or_queue_attempt(marked_ids)
            self.wake()
            return
        if marked_ids != reminder_ids:
            # 冻结后若任一项目失效，原 Prompt 已不再是权威批次。整条请求必须停播；
            # 已经计数的交集仍按一次真实尝试结算。
            self._requests.pop(request_id, None)
            if self._current_request_id == request_id:
                self._current_request_id = None
            if not state.cancellation_issued:
                state.cancellation_issued = True
                self._cancel_requests_safely((request_id,))
            if marked_ids:
                await self._finish_or_queue_attempt(marked_ids)
            self.wake()
            return

        state.stage = "enqueued"
        state.unsettled_attempts = {reminder.reminder_id: reminder for reminder in marked}
        self._maybe_finish_request(request_id, state)

    def _apply_delivery_on_owner(self, command: NoticeDeliveryCommand) -> None:
        """在 owner 事件循环推进一条正文交付结果。"""

        state = self._requests.get(command.request_id.strip())
        if state is None:
            # 受保护的 Activity Notice 或 Self-Reflection 结束后也必须重新尝试当前电平。
            # 它们不在 Reminder 请求账本里，但可能正是上一轮提交被拒绝的原因。
            if command.outcome in {
                "llm_output_completed",
                "delivery_cancelled",
                "delivery_failed",
                "superseded",
            }:
                self.wake()
            return
        if command.outcome == "request_enqueued":
            self.commit_request_enqueued(command.request_id)
            return
        if command.outcome == "request_enqueue_failed":
            self.rollback_prepared_request(command.request_id)
            return
        if state.stage == "prepared":
            return
        if command.outcome == "llm_output_completed":
            state.text_finished = True
        else:
            state.finish_requested = True
        self._maybe_finish_request(command.request_id.strip(), state)

    def _finish_after_audio_on_owner(self, request_id: str) -> None:
        """保存匹配请求的播放完成事实，正文尚未结束时继续等待。"""

        state = self._requests.get(request_id)
        if state is None:
            # 普通或受保护输出释放音频占用后，等待的 Reminder 应立即再尝试。
            self.wake()
            return
        if state.stage == "prepared":
            return
        state.audio_finished = True
        self._maybe_finish_request(request_id, state)

    def _maybe_finish_request(
        self,
        request_id: str,
        state: _ReminderRequestState,
    ) -> None:
        """分别推进领域响铃结算和音频串行占用释放。

        Args:
            request_id: 当前 Reminder Notice 请求编号。
            state: 请求账本中的同一个可变状态对象。
        """

        if state.stage != "enqueued" or state.finishing:
            return

        # 一条播报可能混合不同次数的提醒。前两次文字结束即可重排，第三次要保留
        # “知道了”直到音频结束；失败或打断则同时终止文字和音频。
        output_finished = state.finish_requested or (state.text_finished and state.audio_finished)
        reminder_ids = tuple(
            reminder_id
            for reminder_id, reminder in state.unsettled_attempts.items()
            if output_finished
            or (state.text_finished and reminder.ring_count < MAX_TIMED_REMINDER_RING_COUNT)
        )
        if reminder_ids:
            state.finishing = True
            self._start_state_task(self._finish_request_by_id(request_id, reminder_ids))
            return

        if not state.unsettled_attempts and output_finished:
            self._release_request(request_id, state)

    async def _finish_request_by_id(
        self,
        request_id: str,
        reminder_ids: tuple[str, ...],
    ) -> None:
        """只结算本次已经满足结束条件的提醒，再检查剩余项。

        Args:
            request_id: 仍持有串行占用的请求编号。
            reminder_ids: 本次固定的待结算子集，不能在等待期间扩大到第三次提醒。
        """

        state = self._requests.get(request_id)
        if state is None or state.stage != "enqueued":
            return
        finished = await self._finish_request(reminder_ids)
        current = self._requests.get(request_id)
        if current is not state:
            return
        state.finishing = False
        if not finished:
            # 仓储暂时失败时不能丢掉 request -> reminders 账本，否则这些任务会
            # 永久停在 next_ring_at=None。使用有界延迟重试，避免故障时忙循环。
            loop = asyncio.get_running_loop()
            loop.call_later(
                self._retry_delay_seconds,
                lambda: self._retry_finish_request(request_id, state),
            )
            return
        for reminder_id in reminder_ids:
            state.unsettled_attempts.pop(reminder_id, None)
        self._maybe_finish_request(request_id, state)

    def _retry_finish_request(
        self,
        request_id: str,
        state: _ReminderRequestState,
    ) -> None:
        """在 owner 循环重试一次仍未完成的领域响铃结算。

        Args:
            request_id: 需要重新结算的 Reminder Notice 请求编号。
            state: 安排重试时对应的请求状态对象。
        """

        if self._stopping or self._requests.get(request_id) is not state:
            return
        self._maybe_finish_request(request_id, state)

    def _release_request(
        self,
        request_id: str,
        state: _ReminderRequestState,
    ) -> None:
        """释放一条已完成领域结算的串行请求占用。

        Args:
            request_id: 当前 Reminder Notice 请求编号。
            state: 必须仍与账本中相同的请求状态对象。
        """

        if self._requests.get(request_id) is not state:
            return
        self._requests.pop(request_id, None)
        if self._current_request_id == request_id:
            self._current_request_id = None
        self.wake()

    async def _finish_request(self, reminder_ids: tuple[str, ...]) -> bool:
        """结束一次已计数响铃，并返回领域结算是否成功。"""

        try:
            await self._service.finish_ring_attempt(reminder_ids)
        except Exception:
            logger.exception(
                "Reminder Notice 响铃结算失败 reminder_count=%s",
                len(reminder_ids),
            )
            return False
        return True

    def _dismiss_on_owner(self, reminder_ids: tuple[str, ...]) -> tuple[str, ...]:
        """在 owner 循环删除关闭项并取消相交请求。"""

        dismissed = set(reminder_ids)
        for reminder_id in dismissed:
            self._pending.pop(reminder_id, None)
        cancelled_request_ids: list[str] = []
        for request_id, state in tuple(self._requests.items()):
            batch_ids = tuple(item.reminder_id for item in state.batch.reminders)
            if dismissed.isdisjoint(batch_ids):
                continue
            if not state.cancellation_issued:
                state.cancellation_issued = True
                cancelled_request_ids.append(request_id)
            if state.stage == "prepared":
                # 尚未进入 Bridge 的请求没有消耗响铃次数，直接释放冻结占用即可。
                self._requests.pop(request_id, None)
                if self._current_request_id == request_id:
                    self._current_request_id = None
                continue
            if state.stage == "enqueuing":
                # 计数协程可能正在跨过 await。保留账本直到它落账，避免已入队的
                # 未关闭事项丢失一次尝试结算；完成标记让落账后立即结束原 Notice。
                state.finish_requested = True
                continue
            # 保留正在结算的同一份账本，防止部分关闭与结算协程同时重排剩余项。
            for reminder_id in dismissed:
                state.unsettled_attempts.pop(reminder_id, None)
            state.finish_requested = True
            self._maybe_finish_request(request_id, state)
        result = tuple(cancelled_request_ids)
        self._cancel_requests_safely(result)
        self.wake()
        return result

    async def _release_all_requests(self) -> None:
        """释放 prepared reservation，并结束已经计数的在途尝试。"""

        requests = tuple(self._requests.values())
        request_ids = tuple(
            request_id
            for request_id, state in self._requests.items()
            if not state.cancellation_issued
        )
        for state in self._requests.values():
            state.cancellation_issued = True
        self._requests.clear()
        self._current_request_id = None
        self._cancel_requests_safely(request_ids)
        for state in requests:
            if state.stage != "enqueued" or not state.unsettled_attempts:
                continue
            await self._finish_or_queue_attempt(
                tuple(state.unsettled_attempts)
            )
        self._refresh_pending()

    async def _finish_or_queue_attempt(
        self,
        reminder_ids: tuple[str, ...],
    ) -> None:
        """结算已经脱离请求账本的尝试，失败时交给 owner 持续重试。

        Args:
            reminder_ids: 已经真实入队、但对应媒体请求现已释放的提醒编号。
        """

        if await self._finish_request(reminder_ids):
            return
        self._pending_attempt_settlements.setdefault(reminder_ids, None)
        self._set_wake_event()

    async def _settle_pending_attempts(self) -> bool:
        """重试所有脱离媒体请求后仍未完成的领域结算。

        Returns:
            至少还有一批结算失败、需要延迟重试时返回真。
        """

        for reminder_ids in tuple(self._pending_attempt_settlements):
            if await self._finish_request(reminder_ids):
                self._pending_attempt_settlements.pop(reminder_ids, None)
        return bool(self._pending_attempt_settlements)

    async def _wait_for_wake_or_retry(self, wake_event: asyncio.Event) -> None:
        """等待外部状态变化，最迟在重试间隔后继续检查。

        Args:
            wake_event: Coordinator owner 事件循环持有的唤醒电平。
        """

        try:
            await asyncio.wait_for(
                wake_event.wait(),
                timeout=self._retry_delay_seconds,
            )
        except TimeoutError:
            pass

    def _cancel_requests_safely(self, request_ids: tuple[str, ...]) -> None:
        """尽力撤销请求生成与音频，但不让媒体故障阻断领域结算。

        Args:
            request_ids: 已由请求账本去重且首次发出取消的请求编号。
        """

        if not request_ids or self._cancel_requests is None:
            return
        try:
            self._cancel_requests(request_ids)
        except Exception:
            logger.exception(
                "撤销 Reminder Notice 请求失败 request_count=%s",
                len(request_ids),
            )

    def _start_state_task(
        self,
        coroutine: Coroutine[object, object, None],
    ) -> None:
        """启动并跟踪会改变请求或响铃状态的 owner 任务。

        Args:
            coroutine: 必须在 Coordinator 所属事件循环执行的状态协程。
        """

        task = asyncio.create_task(coroutine)
        self._state_tasks.add(task)
        task.add_done_callback(self._state_tasks.discard)

    async def _drain_state_tasks(self) -> None:
        """等待当前及其连带创建的状态任务全部结束。"""

        while self._state_tasks:
            tasks = tuple(self._state_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            # gather 已经证明这批任务全部结束，主动移除可以避免紧循环一直抢占
            # done callback 的调度机会；等待期间新建的任务仍留给下一轮处理。
            self._state_tasks.difference_update(tasks)

    def _set_wake_event(self) -> None:
        """只在 owner 事件循环内修改 asyncio Event。"""

        wake_event = self._wake_event
        if wake_event is not None:
            wake_event.set()

    def _resume_requests_on_owner(self) -> None:
        """在 owner 循环重新开放媒体请求并触发一次权威状态检查。"""

        self._require_owner_loop()
        if self._stopping:
            return
        self._requests_paused = False
        self._set_wake_event()

    def _post_to_owner(self, callback: Callable[[], None]) -> None:
        """把任意线程回调安全投递到 Coordinator 所属事件循环。"""

        with self._lifecycle_lock:
            loop = self._loop
        if loop is None or loop.is_closed():
            return
        if self._is_owner_loop():
            callback()
            return
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            logger.info("Reminder Coordinator 已关闭，忽略迟到通知")

    async def _run_on_owner(self, coroutine: Coroutine[object, object, None]) -> None:
        """把生命周期协程投递到 owner loop，并等待其真实完成。

        Args:
            coroutine: 只修改 Coordinator owner 状态的协程。
        """

        with self._lifecycle_lock:
            loop = self._loop
        if loop is None or loop.is_closed():
            coroutine.close()
            return
        try:
            owner_future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()
            return
        await asyncio.wrap_future(owner_future)

    def _is_owner_loop(self) -> bool:
        """判断调用方是否位于 Coordinator 所属事件循环。"""

        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _require_owner_loop(self) -> None:
        """阻止调用方跨线程直接修改短期请求账本。"""

        if not self._is_owner_loop():
            raise RuntimeError("Reminder Coordinator 状态只能在所属事件循环修改")
