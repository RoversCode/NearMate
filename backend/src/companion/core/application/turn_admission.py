"""维护文本 Turn 队列、语音优先级与可见输出顺序屏障。"""

from __future__ import annotations

import asyncio
import mimetypes
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from threading import RLock
from typing import Literal

from companion.core.application.material_lifecycle import MaterialLifecycleService
from companion.core.application.turn_submission import (
    TextTurnDispatch,
    TurnSubmissionOutcome,
)
from companion.core.broadcast import BroadcastHub
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_request_failure import (
    build_assistant_request_failed_event,
)
from companion.realtime.assistant_events import AssistantRequestSource
from companion.realtime.visible_output_journal import VisibleOutputSettlement
from companion.foreground_self_reflection.models import (
    DivertFinalInputCommand,
    InputDiversionResult,
    UnattendedMaterialReference,
)
from companion.materials.models import MaterialAttachment


logger = get_backend_logger(__name__)

TextSubmissionState = Literal["preparing", "queued", "paused", "failed"]


class TurnAdmissionError(RuntimeError):
    """表示文本提交当前无法执行请求的状态操作。"""


class TurnSubmissionNotFound(TurnAdmissionError):
    """表示提交编号不属于当前 Backend 运行。"""


class TurnSubmissionConflict(TurnAdmissionError):
    """表示提交存在，但当前状态不允许目标操作。"""


class TurnSubmissionQueueFull(TurnAdmissionError):
    """表示未派发文本已经达到产品容量上限。"""


@dataclass(frozen=True, slots=True)
class TextSubmissionSnapshot:
    """保存 Desktop 恢复一条待发文本需要的全部公开事实。

    Args:
        submission_id: Desktop 创建的稳定提交编号。
        message_text: 当前排队正文。
        state: 准备中、排队中、语音暂停或失败。
        created_order: Backend 分配的进程内稳定顺序。
        error_message: 失败时可直接展示的安全文案。
    """

    submission_id: str
    message_text: str
    state: TextSubmissionState
    created_order: int
    error_message: str | None

    def to_payload(self) -> dict[str, object]:
        """转换成控制事件使用的严格字典。

        Returns:
            不包含材料和环境内部身份的公开载荷。
        """

        return {
            "submission_id": self.submission_id,
            "message_text": self.message_text,
            "state": self.state,
            "created_order": self.created_order,
            "error_message": self.error_message,
        }


@dataclass(slots=True)
class _TextSubmissionRecord:
    """保存一条文本 submission 的主进程内部状态。

    Args:
        submission_id: Desktop 创建的稳定提交编号。
        message_text: 当前等待派发的正文。
        turn_material_context_id: reservation 阶段冻结的材料上下文编号。
        created_order: Backend 分配的进程内点击顺序。
        state: 当前公开队列状态。
        environment_snapshot_id: 环境准备完成后绑定的快照编号。
        context_ready: 现场环境准备是否已经收口。
        dispatching: 是否已经越过不可编辑的派发边界。
        error_message: 失败状态可直接展示的安全文案。
    """

    submission_id: str
    message_text: str
    turn_material_context_id: str
    created_order: int
    state: TextSubmissionState = "preparing"
    environment_snapshot_id: str | None = None
    context_ready: bool = False
    dispatching: bool = False
    error_message: str | None = None

    def snapshot(self) -> TextSubmissionSnapshot:
        """生成不泄漏内部上下文编号的公开快照。

        Returns:
            当前记录对应的 Desktop 投影。
        """

        return TextSubmissionSnapshot(
            submission_id=self.submission_id,
            message_text=self.message_text,
            state=self.state,
            created_order=self.created_order,
            error_message=self.error_message,
        )


@dataclass(slots=True)
class _PendingVoiceRequest:
    """保存已经完成 ASR、但还要等待上一轮结算的语音请求。"""

    request_id: str
    dispatch_request: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class _NoticeReservation:
    """保存尚未提交的独立 Notice 对 Turn 准入位置的占用。

    Args:
        request_id: 即将进入 Bridge 的助手请求编号。
        source: 区分普通 System Notice 与可抢占语音的 Reminder Notice。
    """

    request_id: str
    source: Literal["system_notice", "timed_reminder_notice"]


@dataclass(slots=True)
class _DivertedTextCommit:
    """保存已归入 Episode 的文本在稳定副作用完成前的补偿凭据。

    Args:
        submission_id: 文本提交的稳定身份。
        message_text: 已经写入 Episode 的原始正文。
        context_id: 需要最终释放的材料上下文。
        episode_id: 输入归属的 Episode 身份。
        runtime_epoch: 输入接纳时的 Assistant Runtime 世代。
        attachments: 输入正式成立后应登记的完整附件快照。
        initial_material_broadcast_pending: 激活材料上下文的广播是否待重试。
        transcript_pending: user.final_transcript 是否待重试。
        recent_materials_recorded: 近期材料是否已经完成幂等登记。
        material_finished: finish_turn 是否已经成功执行。
        release_broadcast_pending: 材料释放后的广播是否待重试。
    """

    submission_id: str
    message_text: str
    context_id: str
    episode_id: str
    runtime_epoch: int
    attachments: tuple[MaterialAttachment, ...]
    initial_material_broadcast_pending: bool = True
    transcript_pending: bool = True
    recent_materials_recorded: bool = False
    material_finished: bool = False
    release_broadcast_pending: bool = True


class TurnAdmissionSequencer:
    """统一裁决文本、语音和助手可见输出的先后关系。

    Args:
        hub: 材料上下文与控制广播中枢。
        material_lifecycle: queued 文本删除或启动失败时的材料清理端口。
        dispatch_text_turn: 真正冻结上下文并提交一条已获准文本的异步端口。
        cancel_system_notice: 按请求编号取消独立 System Notice 的同步端口。
        try_divert_reserved_text: 文本 Reservation 阶段尝试归入活跃 Episode 的端口。
        current_runtime_epoch: 返回当前 Assistant Runtime 世代的函数。
        max_pending_submissions: 同时允许保留的未派发文本数量。
    """

    def __init__(
        self,
        *,
        hub: BroadcastHub,
        material_lifecycle: MaterialLifecycleService,
        dispatch_text_turn: Callable[
            [TextTurnDispatch], Awaitable[TurnSubmissionOutcome]
        ],
        cancel_system_notice: Callable[[str], object],
        try_divert_reserved_text: Callable[
            [DivertFinalInputCommand], InputDiversionResult
        ]
        | None = None,
        current_runtime_epoch: Callable[[], int] | None = None,
        max_pending_submissions: int = 10,
    ) -> None:
        """初始化空的 Turn 准入队列。

        Args:
            hub: 材料上下文与控制广播中枢。
            material_lifecycle: queued 文本删除或启动失败时的材料清理端口。
            dispatch_text_turn: 真正冻结上下文并提交一条已获准文本的异步端口。
            cancel_system_notice: 按请求编号取消独立 System Notice 的同步端口。
            try_divert_reserved_text: 文本 Reservation 阶段尝试归入活跃 Episode 的端口。
            current_runtime_epoch: 返回当前 Assistant Runtime 世代的函数。
            max_pending_submissions: 同时允许保留的未派发文本数量。

        Raises:
            ValueError: 队列容量不是正整数。
        """

        if max_pending_submissions <= 0:
            raise ValueError("max_pending_submissions 必须大于零")
        self._hub = hub
        self._material_lifecycle = material_lifecycle
        self._dispatch_text_turn = dispatch_text_turn
        self._cancel_system_notice = cancel_system_notice
        self._try_divert_reserved_text_port = try_divert_reserved_text
        self._current_runtime_epoch = current_runtime_epoch
        self._max_pending_submissions = max_pending_submissions
        self._records: OrderedDict[str, _TextSubmissionRecord] = OrderedDict()
        self._unsettled_requests: OrderedDict[str, str] = OrderedDict()
        self._pending_voice_requests: OrderedDict[str, _PendingVoiceRequest] = OrderedDict()
        self._voice_hold_turn_id: str | None = None
        self._revision = 0
        self._next_created_order = 1
        self._state_lock = RLock()
        self._dispatch_lock = asyncio.Lock()
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._diverted_text_commits: dict[str, _DivertedTextCommit] = {}
        self._active_diverted_text_commits: set[str] = set()
        self._system_notice_cancellation_requested: set[str] = set()
        self._notice_reservation: _NoticeReservation | None = None
        self._transition_cleanup: dict[str, tuple[bool, list[str]]] = {}
        self._transition_cleanup_pending = False
        self._runtime_transition_blocked = False
        self._transition_cleanup_error: Exception | None = None

    async def clear_pending_for_runtime_transition(self) -> None:
        """运行时替换时丢弃待发任务及材料，新运行时不执行旧队列。

        Returns:
            无返回值；失败的材料释放保留供下次清理重试。
        """
        async with self._dispatch_lock:
            with self._state_lock:
                if not self._transition_cleanup:
                    self._transition_cleanup = {
                        record.turn_material_context_id: (False, [])
                        for record in self._records.values()
                    }
                self._transition_cleanup_pending = True
                self._runtime_transition_blocked = True
                self._records.clear()
                self._pending_voice_requests.clear()
                self._voice_hold_turn_id = None
                self._unsettled_requests.clear()
                self._system_notice_cancellation_requested.clear()
                self._notice_reservation = None
                self._revision += 1
                snapshot_event = self._snapshot_event_locked()
            first_error: Exception | None = None
            for context_id, (finished, released) in tuple(self._transition_cleanup.items()):
                try:
                    if not finished:
                        released = self._hub.turn_materials.finish_turn(context_id)
                        with self._state_lock:
                            self._transition_cleanup[context_id] = (True, released)
                    await self._material_lifecycle.broadcast_changed(released)
                except Exception as exception:
                    first_error = first_error or exception
                    continue
                else:
                    with self._state_lock:
                        self._transition_cleanup.pop(context_id, None)
            try:
                await self._hub.publisher("turn_admission").publish(snapshot_event)
            except Exception as exception:
                first_error = first_error or exception
            with self._state_lock:
                # 清理完成后仍保持阻断，必须等新运行时就绪才显式打开准入。
                self._transition_cleanup_pending = bool(self._transition_cleanup)
                self._transition_cleanup_error = first_error
            if first_error is not None:
                raise first_error

    def resume_after_runtime_transition(self) -> None:
        """清理账本为空后显式重新开放输入和 Notice 准入。"""
        with self._state_lock:
            if self._transition_cleanup:
                raise TurnSubmissionConflict("运行时切换清理尚未完成")
            self._transition_cleanup_error = None
            self._transition_cleanup_pending = False
            self._runtime_transition_blocked = False

    async def reserve_text(
        self,
        *,
        submission_id: str,
        message_text: str,
    ) -> dict[str, object]:
        """立即接纳一条用户文本并冻结它当时的 staged 材料。

        Args:
            submission_id: Desktop 生成的标准 UUID 文本。
            message_text: 用户点击发送时的正文。

        Returns:
            当前权威队列快照，供 HTTP 调用方和 WebSocket 采用同一事实。

        Raises:
            ValueError: 提交编号或正文为空。
            TurnSubmissionConflict: 同一编号已经绑定不同正文。
            TurnSubmissionQueueFull: 未派发文本达到十条上限。
        """

        self._remember_running_loop()
        normalized_submission_id = submission_id.strip()
        normalized_text = message_text.strip()
        if not normalized_submission_id:
            raise ValueError("submission_id 不能为空")
        if not normalized_text:
            raise ValueError("message_text 不能为空")

        created_record: _TextSubmissionRecord | None = None
        with self._state_lock:
            if self._runtime_transition_blocked:
                raise TurnSubmissionConflict("运行时清理尚未完成")
            existing = self._records.get(normalized_submission_id)
            if existing is not None:
                if existing.message_text != normalized_text:
                    raise TurnSubmissionConflict("同一 submission_id 已绑定不同正文")
                snapshot_event = self._snapshot_event_locked()
                material_context_created = False
                system_notice_to_cancel = None
            else:
                if len(self._records) >= self._max_pending_submissions:
                    raise TurnSubmissionQueueFull("待发文本已达到 10 条上限")
                turn_context = self._hub.turn_materials.activate_turn(
                    turn_id=f"text_submission_{normalized_submission_id}",
                    created_at=self._local_iso_datetime_text(),
                )
                created_record = _TextSubmissionRecord(
                    submission_id=normalized_submission_id,
                    message_text=normalized_text,
                    turn_material_context_id=turn_context.context_id,
                    created_order=self._next_created_order,
                )
                self._records[normalized_submission_id] = created_record
                self._next_created_order += 1
                self._revision += 1
                snapshot_event = self._snapshot_event_locked()
                material_context_created = True
                self._invalidate_system_notice_reservation_locked()
                system_notice_to_cancel = self._should_cancel_system_notice_locked()

        diversion_result: InputDiversionResult | None = None
        diverted_commit_id: str | None = None
        if material_context_created:
            assert created_record is not None
            # Episode 归属必须发生在首个异步等待之前。否则材料广播暂停时，编辑或
            # 删除请求会改写同一个 record，最终写入 Episode 的就不是用户提交时正文。
            diversion_result = self._try_divert_reserved_text(created_record)
            with self._state_lock:
                current = self._records.get(normalized_submission_id)
                if diversion_result is not None and diversion_result.diverted:
                    if current is created_record:
                        self._records.pop(normalized_submission_id, None)
                        self._revision += 1
                    if not diversion_result.episode_id:
                        raise TurnAdmissionError(
                            "Episode 改道成功但缺少 episode_id"
                        )
                    diverted_commit_id = normalized_submission_id
                    self._diverted_text_commits[normalized_submission_id] = (
                        _DivertedTextCommit(
                            submission_id=normalized_submission_id,
                            message_text=created_record.message_text,
                            context_id=created_record.turn_material_context_id,
                            episode_id=diversion_result.episode_id,
                            runtime_epoch=(
                                diversion_result.runtime_epoch
                                if diversion_result.runtime_epoch is not None
                                else self._current_runtime_epoch()
                            ),
                            attachments=tuple(turn_context.attachments),
                        )
                    )
                elif (
                    diversion_result is not None
                    and diversion_result.reason != "self_reflection_not_active"
                    and current is created_record
                ):
                    current.state = "failed"
                    current.error_message = "输入暂时未能记录，请重试。"
                    self._revision += 1

            # 材料变化可以触发 WebSocket 背压，所以归属和 record 冻结不能放在它后面。
            if diverted_commit_id is None:
                await self._material_lifecycle.broadcast_changed()
            else:
                if not await self._process_diverted_text_commit(diverted_commit_id):
                    self._schedule_diverted_text_commit_retry(diverted_commit_id)

        # 异步广播期间普通 reservation 仍可合法编辑或删除，返回前必须重新读取当前
        # 快照，不能把进入等待前的旧 revision 伪装成权威结果。
        with self._state_lock:
            snapshot_event = self._snapshot_event_locked()
        await self._hub.publisher("turn_admission").publish(snapshot_event)
        if system_notice_to_cancel is not None:
            self._cancel_waiting_system_notice(system_notice_to_cancel)
        return snapshot_event["payload"]

    async def commit_context(
        self,
        *,
        submission_id: str,
        environment_snapshot_id: str | None,
    ) -> dict[str, object]:
        """完成 reservation 的环境准备并尝试派发下一条文本。

        Args:
            submission_id: 需要提交环境上下文的文本编号。
            environment_snapshot_id: 捕获成功时的现场环境快照编号；无截图时可为空。

        Returns:
            应用本次上下文后的权威队列快照。
        """

        self._remember_running_loop()
        normalized_environment_id = (
            environment_snapshot_id.strip()
            if isinstance(environment_snapshot_id, str)
            and environment_snapshot_id.strip()
            else None
        )
        with self._state_lock:
            record = self._require_record_locked(submission_id)
            self._ensure_not_dispatching(record)
            if record.state == "failed":
                raise TurnSubmissionConflict("失败提交必须先执行 retry")
            record.environment_snapshot_id = normalized_environment_id
            record.context_ready = True
            if record.state != "paused":
                record.state = "queued"
            record.error_message = None
            self._revision += 1
            snapshot_event = self._snapshot_event_locked()

        await self._hub.publisher("turn_admission").publish(snapshot_event)
        await self._drain_ready_text()
        return self.snapshot_payload()

    async def edit_text(
        self,
        *,
        submission_id: str,
        message_text: str,
    ) -> dict[str, object]:
        """原位修改一条尚未派发的文本。

        Args:
            submission_id: 目标文本提交编号。
            message_text: 替换后的非空正文。

        Returns:
            修改后的权威队列快照。
        """

        self._remember_running_loop()
        normalized_text = message_text.strip()
        if not normalized_text:
            raise ValueError("message_text 不能为空")
        with self._state_lock:
            record = self._require_record_locked(submission_id)
            self._ensure_not_dispatching(record)
            record.message_text = normalized_text
            self._revision += 1
            snapshot_event = self._snapshot_event_locked()
        await self._hub.publisher("turn_admission").publish(snapshot_event)
        return snapshot_event["payload"]

    async def delete_text(self, submission_id: str) -> dict[str, object]:
        """删除一条尚未派发的文本并释放它冻结的材料。

        Args:
            submission_id: 目标文本提交编号。

        Returns:
            删除后的权威队列快照。
        """

        self._remember_running_loop()
        with self._state_lock:
            record = self._require_record_locked(submission_id)
            self._ensure_not_dispatching(record)
            self._records.pop(record.submission_id)
            self._revision += 1
            snapshot_event = self._snapshot_event_locked()
        await self._finish_material_context(record.turn_material_context_id)
        await self._hub.publisher("turn_admission").publish(snapshot_event)
        await self._drain_ready_text()
        return snapshot_event["payload"]

    async def resume_text(self, submission_id: str) -> dict[str, object]:
        """把用户明确恢复的 paused 文本重新放回可派发队列。

        Args:
            submission_id: 目标文本提交编号。

        Returns:
            恢复后的权威队列快照。
        """

        self._remember_running_loop()
        with self._state_lock:
            record = self._require_record_locked(submission_id)
            self._ensure_not_dispatching(record)
            if record.state != "paused":
                raise TurnSubmissionConflict("只有 paused 文本可以恢复")
            record.state = "queued" if record.context_ready else "preparing"
            self._revision += 1
            snapshot_event = self._snapshot_event_locked()
        await self._hub.publisher("turn_admission").publish(snapshot_event)
        await self._drain_ready_text()
        return self.snapshot_payload()

    async def retry_text(self, submission_id: str) -> dict[str, object]:
        """把失败文本恢复为等待 Desktop 重新准备环境的状态。

        Args:
            submission_id: 目标文本提交编号。

        Returns:
            重试后的权威队列快照。
        """

        self._remember_running_loop()
        with self._state_lock:
            record = self._require_record_locked(submission_id)
            self._ensure_not_dispatching(record)
            if record.state != "failed":
                raise TurnSubmissionConflict("只有 failed 文本可以重试")
            record.state = "preparing"
            record.context_ready = False
            record.environment_snapshot_id = None
            record.error_message = None
            self._revision += 1
            snapshot_event = self._snapshot_event_locked()
        await self._hub.publisher("turn_admission").publish(snapshot_event)
        return snapshot_event["payload"]

    async def fail_preparation(
        self,
        *,
        submission_id: str,
        error_message: str = "环境准备失败，请重试。",
    ) -> dict[str, object]:
        """把 Desktop 环境准备失败记录成可恢复状态。

        Args:
            submission_id: 目标文本提交编号。
            error_message: 可直接展示的安全失败文案。

        Returns:
            失败后的权威队列快照。
        """

        self._remember_running_loop()
        with self._state_lock:
            record = self._require_record_locked(submission_id)
            self._ensure_not_dispatching(record)
            record.state = "failed"
            record.context_ready = False
            record.error_message = error_message.strip() or "环境准备失败，请重试。"
            self._revision += 1
            snapshot_event = self._snapshot_event_locked()
        await self._hub.publisher("turn_admission").publish(snapshot_event)
        # 失败项已经明确不会派发，必须马上重新检查后续 queued 文本，避免它们永远躺在队列里。
        await self._drain_ready_text()
        return snapshot_event["payload"]

    def hold_for_voice(self, turn_id: str) -> None:
        """在 VAD 确认用户开口后阻止新文本越过派发边界。

        Args:
            turn_id: 当前已确认开口的语音 Turn 编号。
        """

        normalized_turn_id = turn_id.strip()
        if not normalized_turn_id:
            return
        with self._state_lock:
            self._voice_hold_turn_id = normalized_turn_id

    def resolve_voice_turn(
        self,
        *,
        turn_id: str,
        assistant_request_id: str | None,
        dispatch_request: Callable[[], bool] | None = None,
    ) -> None:
        """按 Final ASR 是否有效暂停旧文本或解除临时语音 hold。

        Args:
            turn_id: 本次 Final ASR 对应的语音 Turn 编号。
            assistant_request_id: 有效 Final ASR 的助手请求编号；无效语音为空。
            dispatch_request: 只有在顺序屏障清空后才执行的语音请求提交回调。
        """

        normalized_turn_id = turn_id.strip()
        normalized_request_id = (
            assistant_request_id.strip()
            if isinstance(assistant_request_id, str) and assistant_request_id.strip()
            else None
        )
        snapshot_event: dict[str, object] | None = None
        with self._state_lock:
            if self._voice_hold_turn_id == normalized_turn_id:
                self._voice_hold_turn_id = None
            system_notice_to_cancel = self._invalidate_system_notice_reservation_locked()
            if normalized_request_id is None:
                should_drain = True
            elif dispatch_request is None:
                # 兼容只需要登记屏障的输入侧调用；生产 Final ASR 会提供延迟提交回调。
                self._unsettled_requests[normalized_request_id] = "final_asr"
                should_drain = False
            else:
                self._pending_voice_requests.setdefault(
                    normalized_request_id,
                    _PendingVoiceRequest(
                        request_id=normalized_request_id,
                        dispatch_request=dispatch_request,
                    ),
                )
                should_drain = False
            if normalized_request_id is not None:
                paused_any = False
                for record in self._records.values():
                    if record.dispatching or record.state not in {"preparing", "queued"}:
                        continue
                    record.state = "paused"
                    paused_any = True
                if paused_any:
                    self._revision += 1
                    snapshot_event = self._snapshot_event_locked()
                system_notice_to_cancel = (
                    self._should_cancel_system_notice_locked()
                    or system_notice_to_cancel
                )

        if snapshot_event is not None:
            self._schedule_snapshot_broadcast(snapshot_event)
        if system_notice_to_cancel is not None:
            self._cancel_waiting_system_notice(system_notice_to_cancel)
        if normalized_request_id is not None and dispatch_request is not None:
            self._schedule_voice_dispatch()
        elif should_drain:
            self._schedule_drain()

    def register_assistant_request(
        self,
        request_id: str,
        source: AssistantRequestSource,
    ) -> None:
        """登记所有来源共用的未结算助手请求屏障。

        Args:
            request_id: 已经进入 Bridge 的助手请求编号。
            source: text_input、final_asr 或 system_notice。
        """

        normalized_request_id = request_id.strip()
        normalized_source = source.strip()
        if not normalized_request_id or not normalized_source:
            return
        with self._state_lock:
            self._unsettled_requests[normalized_request_id] = normalized_source
            system_notice_to_cancel = (
                normalized_request_id
                if normalized_source == "system_notice"
                and bool(self._records)
                and normalized_request_id
                not in self._system_notice_cancellation_requested
                else None
            )
            if system_notice_to_cancel:
                self._system_notice_cancellation_requested.add(
                    normalized_request_id
                )
        if system_notice_to_cancel:
            self._cancel_waiting_system_notice(normalized_request_id)

    def is_request_unsettled(self, request_id: str) -> bool:
        """判断指定助手请求是否仍占用输出结算屏障。

        Args:
            request_id: 需要查询的助手请求编号。

        Returns:
            请求已登记且尚未收到可见输出 settlement 时返回真。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return False
        with self._state_lock:
            return normalized_request_id in self._unsettled_requests

    def handle_output_settled(self, settlement: VisibleOutputSettlement) -> None:
        """在公开正文完成归档后释放对应请求的顺序屏障。

        Args:
            settlement: 已经过 journal 幂等校验的可见输出结算事实。
        """

        with self._state_lock:
            source = self._unsettled_requests.pop(settlement.request_id, None)
            if source in {"system_notice", "timed_reminder_notice"}:
                self._system_notice_cancellation_requested.discard(
                    settlement.request_id
                )
            should_drain = not self._unsettled_requests
        if should_drain:
            self._schedule_voice_dispatch()
            self._schedule_drain()

    def handle_request_submission_failed(self, request_id: str) -> None:
        """释放尚未进入 Bridge 的预登记语音请求。

        Args:
            request_id: 构造或提交失败的 Final ASR 请求编号。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return
        with self._state_lock:
            self._unsettled_requests.pop(normalized_request_id, None)
            self._pending_voice_requests.pop(normalized_request_id, None)
            should_drain = not self._unsettled_requests
        if should_drain:
            self._schedule_voice_dispatch()
            self._schedule_drain()

    def reserve_system_notice(self, request_id: str) -> bool:
        """原子预留一次 System Notice 的最终提交位置。

        Args:
            request_id: 即将构造的独立 System Notice 请求编号。

        Returns:
            当前没有语音、文本或未结算输出占用时返回真。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return False
        with self._state_lock:
            if not self._allows_new_system_notice_locked():
                return False
            self._notice_reservation = _NoticeReservation(
                request_id=normalized_request_id,
                source="system_notice",
            )
            return True

    def reserve_timed_reminder(self, request_id: str) -> bool:
        """在清理未完成语音前预留定时提醒的唯一提交位置。

        Args:
            request_id: 即将构造的定时提醒请求编号。

        Returns:
            没有待发文本、待派发语音、另一条 Notice 或 Self-Reflection 时返回真。

        业务意图：已确认但未完成的用户语音正是 Reminder 可以抢占的对象，因此
        reservation 阶段允许 voice hold；真正提交时仍会重新确认输入已由 owner 清理。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return False
        with self._state_lock:
            if not self._allows_new_timed_reminder_locked():
                return False
            self._notice_reservation = _NoticeReservation(
                request_id=normalized_request_id,
                source="timed_reminder_notice",
            )
            return True

    def submit_reserved_system_notice(
        self,
        request_id: str,
        submit_request: Callable[[], bool],
    ) -> bool:
        """在同一锁边界内确认并提交已预留的 System Notice。

        Args:
            request_id: 已由 ``reserve_system_notice`` 预留的请求编号。
            submit_request: 只执行 Bridge 原子提交的回调。

        Returns:
            Bridge 接受请求时返回真；reservation 已被用户输入抢先取消时返回假。
        """

        normalized_request_id = request_id.strip()
        with self._state_lock:
            if (
                self._notice_reservation
                != _NoticeReservation(
                    request_id=normalized_request_id,
                    source="system_notice",
                )
                or self._voice_hold_turn_id is not None
                or bool(self._records)
                or bool(self._unsettled_requests)
                or bool(self._pending_voice_requests)
            ):
                if (
                    self._notice_reservation is not None
                    and normalized_request_id
                    == self._notice_reservation.request_id
                ):
                    self._notice_reservation = None
                return False
            try:
                accepted = submit_request()
            except Exception:
                self._notice_reservation = None
                raise
            self._notice_reservation = None
            return accepted

    def submit_reserved_timed_reminder(
        self,
        request_id: str,
        submit_request: Callable[[], bool],
    ) -> bool:
        """确认定时提醒预留仍有效并提交抢占请求。

        Args:
            request_id: 已由 ``reserve_timed_reminder`` 预留的请求编号。
            submit_request: 只执行 Bridge 原子提交的回调。

        Returns:
            预留仍有效且 Bridge 接受请求时返回真。
        """

        normalized_request_id = request_id.strip()
        with self._state_lock:
            if (
                self._notice_reservation
                != _NoticeReservation(
                    request_id=normalized_request_id,
                    source="timed_reminder_notice",
                )
                or not self._allows_timed_reminder_submission_locked(
                    normalized_request_id
                )
            ):
                if (
                    self._notice_reservation is not None
                    and normalized_request_id
                    == self._notice_reservation.request_id
                ):
                    self._notice_reservation = None
                return False
            try:
                accepted = submit_request()
            except Exception:
                self._notice_reservation = None
                raise
            self._notice_reservation = None
            return accepted

    def release_notice_reservation(self, request_id: str) -> None:
        """释放尚未进入 Bridge 的独立 Notice reservation。

        Args:
            request_id: 需要释放的 System Notice 或 Reminder Notice 请求编号。
        """

        normalized_request_id = request_id.strip()
        with self._state_lock:
            if (
                self._notice_reservation is not None
                and normalized_request_id == self._notice_reservation.request_id
            ):
                self._notice_reservation = None
                released = True
            else:
                released = False
        if released:
            self._schedule_voice_dispatch()
            self._schedule_drain()

    def allows_system_notice(self) -> bool:
        """判断独立 System Notice 是否可以抢占当前输入空档。

        Returns:
            没有待发文本、语音 hold 或未结算助手请求时返回真。
        """

        with self._state_lock:
            return (
                self._allows_new_system_notice_locked()
            )

    def snapshot_event(self) -> dict[str, object]:
        """读取当前控制面完整快照事件。

        Returns:
            可直接放入 WebSocket 首帧的权威事件。
        """

        with self._state_lock:
            return self._snapshot_event_locked()

    def snapshot_payload(self) -> dict[str, object]:
        """读取当前队列公开载荷。

        Returns:
            revision 与全部未派发文本。
        """

        return self.snapshot_event()["payload"]

    async def _drain_ready_text(self) -> None:
        """串行派发当前最早一条 queued 文本。

        Returns:
            无。存在语音 hold、未结算请求或没有 queued 文本时直接返回。
        """

        self._remember_running_loop()
        async with self._dispatch_lock:
            while True:
                with self._state_lock:
                    record = self._claim_next_ready_locked()
                    if record is None:
                        return
                    request_id = f"text-input-{record.submission_id}"
                    # 文本直连助手运行时时，极快的 settlement 可能在 dispatch 返回前到达。
                    # 因此必须先登记屏障，再跨越任何广播或运行时提交的异步边界。
                    self._unsettled_requests.setdefault(request_id, "text_input")
                    attachments = self._material_attachments_for_context(
                        record.turn_material_context_id
                    )
                    dispatch = TextTurnDispatch(
                        submission_id=record.submission_id,
                        request_id=request_id,
                        message_text=record.message_text,
                        turn_material_context_id=record.turn_material_context_id,
                        environment_snapshot_id=record.environment_snapshot_id,
                        attachments=attachments,
                        materials=self._material_references(attachments),
                    )

                try:
                    outcome = await self._dispatch_text_turn(dispatch)
                except Exception as exception:
                    logger.exception(
                        "queued 文本派发失败 submission_id=%s",
                        record.submission_id,
                        exc_info=exception,
                    )
                    with self._state_lock:
                        self._unsettled_requests.pop(dispatch.request_id, None)
                        current = self._records.get(record.submission_id)
                        if current is not None:
                            current.dispatching = False
                            current.state = "failed"
                            current.error_message = "文本发送失败，请重试。"
                            self._revision += 1
                            snapshot_event = self._snapshot_event_locked()
                    await self._hub.publisher("turn_admission").publish(snapshot_event)
                    return

                if outcome.request_id != dispatch.request_id:
                    with self._state_lock:
                        self._unsettled_requests.pop(dispatch.request_id, None)
                        current = self._records.get(record.submission_id)
                        if current is not None:
                            current.dispatching = False
                            current.state = "failed"
                            current.error_message = "文本发送失败，请重试。"
                            self._revision += 1
                            snapshot_event = self._snapshot_event_locked()
                    logger.error(
                        "文本派发返回了不一致的 request_id submission_id=%s expected=%s actual=%s",
                        record.submission_id,
                        dispatch.request_id,
                        outcome.request_id,
                    )
                    await self._hub.publisher("turn_admission").publish(snapshot_event)
                    return

                with self._state_lock:
                    self._records.pop(record.submission_id, None)
                    # 被 Attention 接纳的文本没有普通助手输出结算，必须在这里
                    # 释放顺序屏障；否则后一条文本会永久等待一条不存在的回复。
                    if (
                        outcome.diverted_to_episode
                        or not outcome.assistant_request_started
                    ):
                        self._unsettled_requests.pop(dispatch.request_id, None)
                    self._revision += 1
                    snapshot_event = self._snapshot_event_locked()
                await self._hub.publisher("turn_admission").publish(snapshot_event)

                if outcome.diverted_to_episode:
                    # diversion 只保留 episode 的安全投影，reservation 中的正式材料
                    # 不得跟着输入长期存活；改道服务本身不拥有这份 reservation。
                    await self._finish_material_context(
                        record.turn_material_context_id
                    )
                    continue

                if not outcome.assistant_request_started:
                    await self._finish_material_context(
                        record.turn_material_context_id
                    )
                    await self._hub.publisher("assistant_output").publish(
                        build_assistant_request_failed_event(
                            outcome.request_id,
                            code="request_start_failed",
                        )
                    )
                    continue
                return

    def _material_attachments_for_context(
        self,
        context_id: str,
    ) -> tuple[MaterialAttachment, ...]:
        """冻结文本 reservation 当前绑定的完整附件。

        Args:
            context_id: 已由文本 Turn reservation 取得的材料上下文编号。

        Returns:
            当前上下文的附件快照；上下文不存在时返回空。
        """

        active_context_reader = getattr(self._hub.turn_materials, "active_context", None)
        if not callable(active_context_reader):
            return ()
        active_context = active_context_reader(context_id)
        if active_context is None:
            return ()
        return tuple(active_context.attachments)

    def _material_references(
        self,
        attachments: tuple[MaterialAttachment, ...],
    ) -> tuple[UnattendedMaterialReference, ...]:
        """把完整附件投影成 Episode 可安全回忆的文件名和类型。

        Attention 只需要知道用户当时提交过哪些材料，不应看到本机路径、资产编号或
        其它材料内部字段。

        Args:
            attachments: 已在输入归属边界冻结的完整附件。

        Returns:
            只包含安全文件名和媒体类型的不可执行材料引用。
        """

        references: list[UnattendedMaterialReference] = []
        for attachment in attachments:
            display_name = str(attachment.display_name or "material").strip()
            safe_name = PurePath(display_name.replace("\\", "/")).name or "material"
            media_type = mimetypes.guess_type(safe_name)[0]
            if not media_type:
                media_type = {
                    "audio": "audio/*",
                    "video": "video/*",
                    "text": "text/plain",
                    "mixed_or_document": "application/octet-stream",
                }.get(attachment.media_class, "application/octet-stream")
            try:
                references.append(
                    UnattendedMaterialReference(
                        file_name=safe_name,
                        media_type=media_type,
                    )
                )
            except ValueError:
                # 异常名称只影响过去材料的安全语义投影，不能阻塞正文的正常派发。
                logger.warning(
                    "跳过无法安全投影的文本材料 material_id=%s",
                    attachment.material_id,
                )
        return tuple(references)

    def _claim_next_ready_locked(self) -> _TextSubmissionRecord | None:
        """在状态锁内领取最早一条当前可派发文本。

        Returns:
            已标记 dispatching 的记录；当前受阻时返回空。
        """

        if (
            self._runtime_transition_blocked
            or self._voice_hold_turn_id is not None
            or any(
                not source.startswith("self_reflection_")
                for source in self._unsettled_requests.values()
            )
            or self._pending_voice_requests
            or self._notice_reservation is not None
        ):
            return None
        for record in self._records.values():
            # paused/failed 是用户明确停放的记录，不阻塞后续新输入；仍在 preparing 的更早
            # reservation 则必须挡住后来文本，避免环境捕获快慢改变用户点击顺序。
            if record.state in {"paused", "failed"}:
                continue
            if record.state == "preparing" or record.dispatching:
                return None
            if record.state != "queued" or not record.context_ready:
                return None
            record.dispatching = True
            return record
        return None

    async def _finish_material_context(self, context_id: str) -> None:
        """结束一条不再可能派发的文本材料上下文。

        Args:
            context_id: reservation 阶段冻结的材料上下文编号。
        """

        released = self._hub.turn_materials.finish_turn(context_id)
        await self._material_lifecycle.broadcast_changed(released)

    async def _process_diverted_text_commit(self, submission_id: str) -> bool:
        """完成一项 Episode 文本的广播、历史通知和材料释放副作用。

        Args:
            submission_id: 已经从普通待发队列移除的文本身份。

        Returns:
            所有副作用都完成时返回真；任一步失败时保留补偿凭据并返回假。
        """

        normalized_submission_id = submission_id.strip()
        with self._state_lock:
            commit = self._diverted_text_commits.get(normalized_submission_id)
            if commit is None:
                return True
            if normalized_submission_id in self._active_diverted_text_commits:
                return False
            self._active_diverted_text_commits.add(normalized_submission_id)
        try:
            with self._state_lock:
                commit = self._diverted_text_commits.get(normalized_submission_id)
            if commit is None:
                return True
            if commit.initial_material_broadcast_pending:
                try:
                    await self._material_lifecycle.broadcast_changed()
                except Exception:
                    logger.exception(
                        "Episode 文本材料广播失败 submission_id=%s",
                        normalized_submission_id,
                    )
                    return False
                with self._state_lock:
                    current = self._diverted_text_commits.get(normalized_submission_id)
                    if current is not None:
                        current.initial_material_broadcast_pending = False

            if commit.transcript_pending:
                try:
                    await self._hub.publisher("interaction").publish(
                        {
                            "type": "user.final_transcript",
                            "payload": {
                                "session_id": "desktop-text",
                                "turn_id": None,
                                "request_id": f"text-input-{commit.submission_id}",
                                "submission_id": commit.submission_id,
                                "text": commit.message_text,
                                "history_disposition": "unattended_episode",
                                "episode_id": commit.episode_id,
                            },
                        }
                    )
                except Exception:
                    logger.exception(
                        "Episode 文本 transcript 广播失败 submission_id=%s",
                        normalized_submission_id,
                    )
                    return False
                with self._state_lock:
                    current = self._diverted_text_commits.get(normalized_submission_id)
                    if current is not None:
                        current.transcript_pending = False

            with self._state_lock:
                commit = self._diverted_text_commits.get(normalized_submission_id)
            if commit is None:
                return True
            if not commit.recent_materials_recorded:
                # Episode 和 transcript 都已正式成立后才登记；补偿重试只登记一次，
                # 否则每次广播失败都会错误刷新 LRU 顺序。
                self._material_lifecycle.remember_recent_materials(
                    commit.attachments
                )
                with self._state_lock:
                    current = self._diverted_text_commits.get(normalized_submission_id)
                    if current is not None:
                        current.recent_materials_recorded = True

            if commit.release_broadcast_pending:
                released_assets: list[str] = []
                if not commit.material_finished:
                    try:
                        released_assets = self._hub.turn_materials.finish_turn(
                            commit.context_id
                        )
                    except Exception:
                        logger.exception(
                            "Episode 文本材料释放失败 submission_id=%s",
                            normalized_submission_id,
                        )
                        return False
                    with self._state_lock:
                        current = self._diverted_text_commits.get(normalized_submission_id)
                        if current is not None:
                            current.material_finished = True
                try:
                    await self._material_lifecycle.broadcast_changed(released_assets)
                except Exception:
                    logger.exception(
                        "Episode 文本材料释放广播失败 submission_id=%s",
                        normalized_submission_id,
                    )
                    return False
                with self._state_lock:
                    current = self._diverted_text_commits.get(normalized_submission_id)
                    if current is not None:
                        current.release_broadcast_pending = False

            with self._state_lock:
                self._diverted_text_commits.pop(normalized_submission_id, None)
            return True
        finally:
            with self._state_lock:
                self._active_diverted_text_commits.discard(normalized_submission_id)

    def _schedule_diverted_text_commit_retry(self, submission_id: str) -> None:
        """为失败的 Episode 文本副作用启动后台补偿任务。

        Args:
            submission_id: 需要继续补偿的稳定文本身份。
        """

        async def retry() -> None:
            """按短退避重试，直到所有副作用形成稳定事实。"""

            delay_seconds = 0.01
            for _attempt in range(32):
                await asyncio.sleep(delay_seconds)
                if await self._process_diverted_text_commit(submission_id):
                    return
                delay_seconds = min(delay_seconds * 2, 1.0)

        try:
            task = asyncio.create_task(retry())
        except RuntimeError:
            logger.warning(
                "Episode 文本补偿任务无法启动 submission_id=%s",
                submission_id,
            )
            return
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _try_divert_reserved_text(
        self,
        record: _TextSubmissionRecord,
    ) -> InputDiversionResult | None:
        """在环境准备前尝试把刚接纳的文本归入活跃 Episode。

        Args:
            record: 已经冻结正文和材料上下文的文本 Reservation。

        Returns:
            归属端口的完整裁决；没有接线时返回空。端口或世代读取异常会返回明确
            失败，调用方保留该 Reservation 供用户重试。
        """

        diversion_port = self._try_divert_reserved_text_port
        if diversion_port is None:
            return None
        epoch_reader = self._current_runtime_epoch
        if epoch_reader is None:
            return InputDiversionResult(
                status="not_diverted",
                reason="runtime_epoch_unavailable",
            )
        try:
            runtime_epoch = epoch_reader()
        except Exception:
            logger.exception(
                "Reservation 阶段读取 Assistant Runtime 世代失败 submission_id=%s",
                record.submission_id,
            )
            return InputDiversionResult(
                status="not_diverted",
                reason="runtime_epoch_unavailable",
            )
        if type(runtime_epoch) is not int or runtime_epoch <= 0:
            return InputDiversionResult(
                status="not_diverted",
                reason="runtime_epoch_unavailable",
            )
        try:
            return diversion_port(
                DivertFinalInputCommand(
                    runtime_epoch=runtime_epoch,
                    input_identity=record.submission_id,
                    assistant_request_id=f"text-input-{record.submission_id}",
                    source="text",
                    text=record.message_text,
                    materials=self._material_references(
                        self._material_attachments_for_context(
                            record.turn_material_context_id
                        )
                    ),
                )
            )
        except Exception:
            logger.exception(
                "Reservation 阶段 Self-Reflection 输入归属失败 submission_id=%s",
                record.submission_id,
            )
            return InputDiversionResult(
                status="not_diverted",
                reason="reservation_diversion_failed",
                runtime_epoch=runtime_epoch,
            )

    def _should_cancel_system_notice_locked(self) -> str | None:
        """判断当前待发文本是否需要取消在途独立 Notice。

        Returns:
            返回尚未提交取消命令的最早 System Notice 请求编号；没有时返回空。
        """

        for request_id, source in self._unsettled_requests.items():
            if source not in {"system_notice", "timed_reminder_notice"}:
                continue
            if request_id in self._system_notice_cancellation_requested:
                continue
            self._system_notice_cancellation_requested.add(request_id)
            return request_id
        return None

    def _invalidate_system_notice_reservation_locked(self) -> str | None:
        """让用户输入取消尚未进入 Bridge 的普通 System Notice reservation。"""

        reservation = self._notice_reservation
        if reservation is None or reservation.source != "system_notice":
            return None
        self._notice_reservation = None
        return None

    def _allows_new_system_notice_locked(self) -> bool:
        """在状态锁内判断 Notice 是否可以获得唯一提交位置。"""

        return (
            not self._runtime_transition_blocked
            and
            self._voice_hold_turn_id is None
            and not self._records
            and not self._unsettled_requests
            and not self._pending_voice_requests
            and self._notice_reservation is None
        )

    def _allows_new_timed_reminder_locked(self) -> bool:
        """判断定时提醒是否可以预留一次普通 Foreground takeover。"""

        if self._runtime_transition_blocked or self._records or self._pending_voice_requests:
            return False
        if self._has_protected_timed_reminder_request_locked():
            return False
        return self._notice_reservation is None

    def _allows_timed_reminder_submission_locked(
        self,
        reservation_id: str | None,
    ) -> bool:
        """判断定时提醒在给定预留身份下是否仍可提交。

        Args:
            reservation_id: 已持有的系统输出预留编号；首次预留时为空。

        Returns:
            原 reservation 仍有效且没有受保护输出抢先到达时返回真。
        """

        # reservation 成立后到达的普通文本或语音排在 Reminder 后面，不得反向让本次
        # 已执行的输入 takeover 失去提交资格；特殊输出仍保留更高优先级。
        if self._has_protected_timed_reminder_request_locked():
            return False
        return self._notice_reservation == _NoticeReservation(
            request_id=reservation_id or "",
            source="timed_reminder_notice",
        )

    def _has_protected_timed_reminder_request_locked(self) -> bool:
        """判断是否已有 Reminder 不得抢占的独立输出。

        Returns:
            System Notice、Reminder Notice 或任一 Self-Reflection 未结算时返回真。
        """

        return any(
            source in {"system_notice", "timed_reminder_notice"}
            or source.startswith("self_reflection_")
            for source in self._unsettled_requests.values()
        )

    def _cancel_waiting_system_notice(self, request_id: str) -> None:
        """尽力提交一次 System Notice 取消命令。

        Returns:
            无。失败只记录诊断，队列仍等待 settlement 或运行时故障补偿。
        """

        try:
            self._cancel_system_notice(request_id)
        except Exception:
            logger.exception(
                "待发用户文本取消 System Notice 失败 request_id=%s",
                request_id,
            )

    def _require_record_locked(self, submission_id: str) -> _TextSubmissionRecord:
        """读取一条存在的 submission 记录。

        Args:
            submission_id: 未清洗的目标提交编号。

        Returns:
            当前主进程权威记录。

        Raises:
            TurnSubmissionNotFound: 编号不存在。
        """

        record = self._records.get(submission_id.strip())
        if record is None:
            raise TurnSubmissionNotFound("文本提交不存在")
        return record

    @staticmethod
    def _ensure_not_dispatching(record: _TextSubmissionRecord) -> None:
        """拒绝修改已经越过派发边界的记录。

        Args:
            record: 需要检查的内部记录。

        Raises:
            TurnSubmissionConflict: 文本正在正式提交。
        """

        if record.dispatching:
            raise TurnSubmissionConflict("文本已经开始派发")

    def _snapshot_event_locked(self) -> dict[str, object]:
        """在状态锁内构造完整控制事件。

        Returns:
            revision 与按创建顺序排列的未派发文本。
        """

        return {
            "type": "turn_submissions.snapshot_changed",
            "payload": {
                "revision": self._revision,
                "items": [
                    record.snapshot().to_payload()
                    for record in self._records.values()
                ],
            },
        }

    def _remember_running_loop(self) -> None:
        """记住当前应用事件循环，供同步媒体回调安全唤醒派发。

        Returns:
            无。
        """

        self._event_loop = asyncio.get_running_loop()

    def _schedule_drain(self) -> None:
        """从同步 settlement 或语音回调调度一次异步派发。

        Returns:
            无。尚未有文本 HTTP 请求绑定事件循环时无需调度。
        """

        event_loop = self._event_loop
        if event_loop is None or event_loop.is_closed():
            return
        event_loop.call_soon_threadsafe(
            self._start_background_task,
            self._drain_ready_text,
        )

    def _schedule_voice_dispatch(self) -> None:
        """调度一项待结算语音请求，保证不在输出结算锁内执行上下文准备。"""

        event_loop = self._event_loop
        if event_loop is not None and not event_loop.is_closed():
            event_loop.call_soon_threadsafe(self._dispatch_next_voice_request)
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._dispatch_next_voice_request()
        else:
            asyncio.get_running_loop().call_soon(self._dispatch_next_voice_request)

    def _dispatch_next_voice_request(self) -> None:
        """在顺序屏障清空后领取并执行最早的语音提交回调。"""

        with self._state_lock:
            if (
                self._unsettled_requests
                or self._notice_reservation is not None
                or not self._pending_voice_requests
            ):
                return
            request_id, pending = self._pending_voice_requests.popitem(last=False)
            self._unsettled_requests[request_id] = "final_asr"

        try:
            accepted = pending.dispatch_request()
        except Exception:
            logger.exception("Final ASR 延迟派发失败 request_id=%s", request_id)
            accepted = False
        if accepted:
            return
        self.handle_request_submission_failed(request_id)

    def _schedule_snapshot_broadcast(self, event: dict[str, object]) -> None:
        """从同步语音回调调度一次队列快照广播。

        Args:
            event: 已在状态锁内冻结的权威快照事件。
        """

        event_loop = self._event_loop
        if event_loop is None or event_loop.is_closed():
            return
        event_loop.call_soon_threadsafe(
            self._start_background_task,
            lambda: self._hub.publisher("turn_admission").publish(event),
        )

    def _start_background_task(
        self,
        coroutine_factory: Callable[[], Awaitable[None]],
    ) -> None:
        """在主事件循环创建并保活一个短异步任务。

        Args:
            coroutine_factory: 到事件循环线程后才创建 coroutine 的工厂。
        """

        task = asyncio.create_task(coroutine_factory())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    def _local_iso_datetime_text() -> str:
        """生成材料 reservation 使用的本地时间文本。

        Returns:
            带时区的 ISO 时间字符串。
        """

        return datetime.now().astimezone().isoformat()
