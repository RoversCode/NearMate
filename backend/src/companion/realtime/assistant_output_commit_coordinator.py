"""协调助手正文的 Prepare、Publish 和 Commit 三阶段。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from companion.activities.notice.delivery_event_mapper import map_notice_delivery_event
from companion.activities.notice.models import NoticeDeliveryCommand
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_output_settlement_service import (
    AssistantOutputSettlementService,
)
from companion.realtime.assistant_public_event_projection import (
    project_public_assistant_event,
)
from companion.realtime.activity_runtime_event_handler import ActivityRuntimeEventHandler
from companion.realtime.turn_material_release_service import TurnMaterialReleaseService
from companion.realtime.visible_output_journal import (
    VisibleOutputAdmission,
    VisibleOutputSettlement,
)
from companion.realtime.runtime_identity import AssistantRequestIdentityConflict


logger = get_backend_logger(__name__)


@dataclass(slots=True)
class _PendingCommit:
    """保存已经广播正文但尚未完成的提交步骤。"""

    event: RuntimeEvent
    admission: VisibleOutputAdmission
    notice_command: NoticeDeliveryCommand | None
    journal_committed: bool = False
    notice_committed: bool = False
    materials_released: bool = False
    task_result_acknowledged: bool = False


@dataclass(slots=True)
class _CommitLane:
    """按同一 request/generation 的公开顺序保存待提交事件。"""

    pending: deque[_PendingCommit] = field(default_factory=deque)
    commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    settlement_event: RuntimeEvent | None = None
    settled: bool = False
    retry_task: asyncio.Task[None] | None = None
    retry_attempts: int = 0


class AssistantOutputCommitCoordinator:
    """保证助手正文不会出现“先广播后重复广播”的半提交。

    Args:
        publish_assistant_output: 已通过严格投影的 Assistant Output 发布端口。
        output_settlement_service: Journal、MemoryCommit 和 Progress 结算服务。
        turn_material_release_service: 请求材料租约释放端口。
        activity_event_handler: TaskResult 请求确认端口。
        notice_delivery_handler: Notice 交付账本端口。
        settlement_committed_handler: 结算真正提交后的通知端口。
    """

    def __init__(
        self,
        *,
        publish_assistant_output: Callable[[dict[str, Any]], Awaitable[None]],
        output_settlement_service: AssistantOutputSettlementService,
        turn_material_release_service: TurnMaterialReleaseService,
        activity_event_handler: ActivityRuntimeEventHandler,
        notice_delivery_handler: Callable[[NoticeDeliveryCommand], object] | None,
        settlement_committed_handler: Callable[[VisibleOutputSettlement], object]
        | None = None,
    ) -> None:
        self._publish_assistant_output = publish_assistant_output
        self._output_settlement_service = output_settlement_service
        self._turn_material_release_service = turn_material_release_service
        self._activity_event_handler = activity_event_handler
        self._notice_delivery_handler = notice_delivery_handler
        self._settlement_committed_handler = settlement_committed_handler
        self._lanes: dict[tuple[str, str], _CommitLane] = {}
        # request_id 在 Backend 进程内永久唯一。保留完整取消集合，才能拦住任意晚到的
        # 子进程正文；集合不承担异步资源，因此无需像活跃 lane 一样主动淘汰。
        self._cancelled_request_ids: set[str] = set()
        self._cancellation_lock = RLock()

    def cancel_request(self, request_id: str) -> None:
        """禁止指定请求此后再公开任何正文事件。

        Args:
            request_id: 已由上游确认需要停止的助手请求编号。

        Returns:
            None: 取消事实会保留到当前 Backend 进程结束。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return
        with self._cancellation_lock:
            self._cancelled_request_ids.add(normalized_request_id)

    async def handle_text(self, event: RuntimeEvent) -> None:
        """公开一条文本事件，并把它加入同 generation 的有序提交 lane。"""

        key = _generation_key(event)
        with self._cancellation_lock:
            if key[0] in self._cancelled_request_ids:
                return
        lane = self._lanes.setdefault(key, _CommitLane())
        async with lane.commit_lock:
            # output_settled 一旦进入本 lane，后续正文就是同一 generation 的迟到事件。
            # 这里必须在广播前吸收，否则 UI 已看到的内容无法再由 Journal 墓碑追回。
            if lane.settled or lane.settlement_event is not None:
                return

            admission = self._output_settlement_service.prepare_broadcast_text(event)
            if admission is None:
                return

            notice_command = (
                map_notice_delivery_event(event.type, event.payload)
                if event.type == "assistant.text_final"
                else None
            )
            try:
                public_event = project_public_assistant_event(event)
                await self._publish_assistant_output(public_event)
            except Exception:
                self._output_settlement_service.abort_broadcast_text(admission)
                raise

            lane.pending.append(
                _PendingCommit(
                    event=event,
                    admission=admission,
                    notice_command=notice_command,
                    notice_committed=notice_command is None,
                    materials_released=event.type != "assistant.text_final",
                    task_result_acknowledged=event.type != "assistant.text_final",
                )
            )
            await self._drain_lane_locked(key, lane)

    async def handle_settlement(self, event: RuntimeEvent) -> None:
        """把结算排在同一 generation 已公开正文的提交步骤之后。"""

        key = self._settlement_key(event)
        lane = self._lanes.setdefault(key, _CommitLane())
        async with lane.commit_lock:
            # 先做只读身份校验。重复 settlement 可以幂等吸收，但不能借重复事件改写
            # generation、历史策略或 Progress 引用。
            try:
                self._output_settlement_service.validate_settlement(event)
            except AssistantRequestIdentityConflict as exception:
                self._abort_identity_conflict_lane(lane, exception)
                raise
            if lane.settled or lane.settlement_event is not None:
                return
            lane.settlement_event = event
            await self._drain_lane_locked(key, lane)

    async def _drain_lane_locked(
        self,
        key: tuple[str, str],
        lane: _CommitLane,
    ) -> None:
        """在已持有 lane 锁时，按正文顺序提交并最后处理 settlement。"""

        while lane.pending:
            try:
                committed = await self._commit_pending(key, lane.pending[0])
            except AssistantRequestIdentityConflict as exception:
                self._abort_identity_conflict_lane(lane, exception)
                raise
            if not committed:
                self._schedule_retry(key, lane)
                return
            lane.pending.popleft()
            lane.retry_attempts = 0

        if lane.settlement_event is not None:
            try:
                settlement = self._output_settlement_service.settle(
                    lane.settlement_event
                )
            except AssistantRequestIdentityConflict:
                raise
            except Exception:
                logger.exception(
                    "助手输出结算提交失败，保留同一执行 lane request_id=%s",
                    key[0],
                )
                self._schedule_retry(key, lane)
                return
            lane.settlement_event = None
            lane.settled = True
            if settlement is not None and self._settlement_committed_handler is not None:
                try:
                    self._settlement_committed_handler(settlement)
                except Exception:
                    # settlement 和 Turn 放行已经提交，后置压缩通知失败不能把同一
                    # settlement 伪装成未完成事务，也不能阻止 lane 清理。
                    logger.exception(
                        "助手输出结算后置通知失败 request_id=%s",
                        key[0],
                    )

        self._cancel_retry_task(lane)
        if lane.settled and self._lanes.get(key) is lane:
            # 永久迟到门禁属于 Journal；Coordinator 只保存尚未结算的活跃 lane，
            # 避免为进程内每个历史请求永久保留异步锁和队列对象。
            self._lanes.pop(key, None)

    def _abort_identity_conflict_lane(
        self,
        lane: _CommitLane,
        exception: AssistantRequestIdentityConflict,
    ) -> None:
        """清掉身份冲突请求留下的全部 admission，禁止它再次重试。

        Args:
            lane: 保存该请求正文和 settlement 的提交车道。
            exception: 需要记录的身份冲突诊断。

        Returns:
            None: 所有待提交 admission 都会被撤销。
        """

        # 同一 lane 可能在临时异常退避期间已经广播多条正文；冲突后必须全部撤销，
        # 否则任意一个残留 token 都会继续挡住 settlement 的永久墓碑。
        while lane.pending:
            pending = lane.pending.popleft()
            self._output_settlement_service.abort_broadcast_text(pending.admission)
        lane.settlement_event = None
        self._cancel_retry_task(lane)
        logger.error(
            "助手输出正文身份冲突，已撤销 admission request_id=%s field=%s "
            "expected=%r actual=%r",
            exception.request_id,
            exception.field,
            exception.expected,
            exception.actual,
        )

    async def _commit_pending(
        self,
        key: tuple[str, str],
        pending: _PendingCommit,
    ) -> bool:
        """按固定顺序完成所有提交步骤；失败步骤留在账本中重试。"""

        try:
            if not pending.journal_committed:
                recorded = self._output_settlement_service.commit_broadcast_text(
                    pending.event,
                    pending.admission,
                )
                if not recorded:
                    raise AssistantRequestIdentityConflict(
                        request_id=key[0],
                        field="visible_output_admission",
                        expected="pending",
                        actual="missing_or_rejected",
                    )
                pending.journal_committed = True

            if (
                pending.notice_command is not None
                and not pending.notice_committed
            ):
                if self._notice_delivery_handler is not None:
                    self._notice_delivery_handler(pending.notice_command)
                pending.notice_committed = True

            if not pending.materials_released:
                await self._turn_material_release_service.release_from_payload(
                    pending.event.payload
                )
                pending.materials_released = True

            if not pending.task_result_acknowledged:
                request_id = _clean_text(pending.event.payload.get("request_id"))
                if pending.event.runtime_epoch is None or not request_id:
                    pending.task_result_acknowledged = True
                else:
                    await self._activity_event_handler.acknowledge_task_result_request(
                        pending.event.runtime_epoch,
                        request_id,
                    )
                    pending.task_result_acknowledged = True
        except AssistantRequestIdentityConflict:
            raise
        except Exception:
            logger.exception(
                "助手正文广播后提交步骤失败，保留未完成步骤 request_id=%s",
                key[0],
            )
            return False

        return _is_complete(pending)

    def _schedule_retry(
        self,
        key: tuple[str, str],
        lane: _CommitLane,
    ) -> None:
        """只调度提交重试，不重新调用公共正文 Publisher。

        正文已经成功广播后，后续账本写入属于 Backend 自己的必达收口。这里不能
        只尝试几次就把状态丢掉，否则 Notice、材料或 TaskResult 会永久卡在半完成状态。
        重试次数只用于退避，不改变提交步骤，也不会再次发送正文。
        """

        if lane.retry_task is not None and not lane.retry_task.done():
            return

        delay_seconds = min(
            0.05 * (2 ** min(lane.retry_attempts, 6)),
            2.0,
        )
        lane.retry_attempts += 1

        async def retry() -> None:
            await asyncio.sleep(delay_seconds)
            lane.retry_task = None
            if self._lanes.get(key) is not lane:
                return
            async with lane.commit_lock:
                await self._drain_lane_locked(key, lane)

        lane.retry_task = asyncio.create_task(
            retry(),
            name=f"assistant-output-commit-retry:{key[0]}",
        )

    def _cancel_retry_task(self, lane: _CommitLane) -> None:
        """lane 已经排空时取消旧退避任务，避免无意义唤醒。"""

        retry_task = lane.retry_task
        if (
            retry_task is not None
            and retry_task is not asyncio.current_task()
            and not retry_task.done()
        ):
            retry_task.cancel()
        lane.retry_task = None

    def _settlement_key(self, event: RuntimeEvent) -> tuple[str, str]:
        """让缺少 generation 的早期失败结算仍复用同一请求的活跃 lane。"""

        request_id = _clean_text(event.payload.get("request_id"))
        generation_id = _clean_text(event.payload.get("generation_id"))
        if not request_id:
            raise ValueError("assistant_output_settlement_missing_request_identity")
        if generation_id:
            return request_id, generation_id

        # generation 尚未建立的启动失败没有正文 lane；若请求已经公开过正文，则 request_id
        # 的进程级唯一性保证最多只有一个活跃执行 lane，可以安全沿用它的 generation。
        active_keys = [key for key in self._lanes if key[0] == request_id]
        if len(active_keys) == 1:
            return active_keys[0]
        return request_id, ""


def _generation_key(event: RuntimeEvent) -> tuple[str, str]:
    """生成用于保持正文提交顺序的 request/generation 身份。"""

    request_id = _clean_text(event.payload.get("request_id"))
    generation_id = _clean_text(event.payload.get("generation_id"))
    if not request_id or not generation_id:
        raise ValueError("assistant_output_commit_missing_identity")
    return request_id, generation_id


def _is_complete(pending: _PendingCommit) -> bool:
    """判断正文的所有副作用是否都已完成。"""

    return (
        pending.journal_committed
        and pending.notice_committed
        and pending.materials_released
        and pending.task_result_acknowledged
    )


def _clean_text(value: object) -> str:
    """清洗跨进程身份字段。"""

    return value.strip() if isinstance(value, str) else ""
