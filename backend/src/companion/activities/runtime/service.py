"""提供 Activity 查询、事件投影、广播和取消的唯一应用服务。

本模块位于 Activity Registry 与外部调用方之间：它把助手子进程发来的来源事件写入
Registry，把已提交的权威快照广播给 Desktop 和 Console，并把用户取消请求真正送回
助手子进程。它不执行 Activity，也不决定 Notice 的表达和交付顺序。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from companion.activities.runtime.adapters import adapt_activity_source_event
from companion.activities.runtime.identity import canonical_activity_uuid
from companion.activities.runtime.models import (
    ActivityApplyResult,
    ActivityCompletionResult,
    ActivityDetail,
    ActivityProjection,
    ActivitySnapshot,
)
from companion.activities.runtime.registry import (
    ActivityInvariantViolation,
    ActivityNotFound,
    ActivityRuntimeRegistry,
    ActivityStateConflict,
)
from companion.observability.logging_config import get_backend_logger
from companion.observability.structured_logging import log_event
from companion.realtime.assistant_events import CancelActivityCommand


logger = get_backend_logger(__name__)


# 状态代码供程序判断；日志使用对应中文，让人能直接看出 Activity 当前变成了什么状态。
_ACTIVITY_STATUS_LABELS: dict[str, str] = {
    "running": "运行中",
    "waiting_user": "等待用户",
    "cancelling": "正在取消",
    "completed": "已完成",
    "failed": "已失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


class ActivityRuntimeUnavailable(RuntimeError):
    """表示当前助手运行时无法接收 Activity 控制命令。

    这意味着取消命令没有进入执行链路，调用方不应把 Activity 显示为 ``cancelling``。
    """


class ActivityService:
    """协调 Activity 权威状态、公共查询、广播和真实取消。

    Registry 保存本次 Backend 运行中的唯一 Activity 事实；本服务只负责把来源事件安全地
    交给它、向前台发布已提交快照，并保证取消命令已被助手运行时接收后才标记
    ``cancelling``。
    
    ActivityService 只管“主进程任务中心里的 Activity 状态”。
    它要解决的问题是：助手子进程做任务时，Desktop 和 Console 需要知道：
    """

    def __init__(
        self,
        *,
        registry: ActivityRuntimeRegistry,
        broadcast_snapshot: Callable[[dict[str, object]], Awaitable[None]],
        submit_cancel_command: Callable[[CancelActivityCommand], object],
        current_runtime_epoch: Callable[[], int],
        interruption_terminal_handler: Callable[
            [
                tuple[ActivityDetail, ...],
                Literal["runtime_reconfigured", "runtime_interrupted"],
            ],
            object,
        ]
        | None = None,
        monotonic_now: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化 Activity 应用服务。

        Args:
            registry: 本次进程内的 Activity 权威状态机。
            broadcast_snapshot: 向控制 WebSocket 广播快照事件的异步端口。
            submit_cancel_command: 向助手子进程提交取消命令的同步端口。
            current_runtime_epoch: 读取当前已发布助手运行时世代的同步端口，用于拒绝
                热替换前迟到的事件和取消请求。
            interruption_terminal_handler: 在批量中断写入 Registry 后、广播和 Notice 前，
                结算对应 Progress 与任务记忆的同步端口。
            monotonic_now: 为取消命令生成时间戳的单调时钟，避免受系统校时影响。
        """

        self._registry = registry
        self._broadcast_snapshot = broadcast_snapshot
        self._submit_cancel_command = submit_cancel_command
        self._current_runtime_epoch = current_runtime_epoch
        self._interruption_terminal_handler = interruption_terminal_handler
        self._monotonic_now = monotonic_now
        # Registry 先关闭 Activity，终态记忆才可能暂时失败；保存这批不可变详情，
        # 让同一 runtime_unavailable 重试仍能完成原始结算。
        self._pending_interruption_settlements: dict[
            tuple[int, str], tuple[ActivityDetail, ...]
        ] = {}

    def apply_source_event(
        self,
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> ActivityApplyResult:
        """同步投影一条来源事件，并返回调用方下一步该做什么。

        事件先经过来源适配器，再由 Registry 校验生命周期和阶段不变量。无法识别、来自
        旧运行时或违反契约的事件都只会被吸收，不会影响事件排水线程；真正改变权威状态
        的事件才返回广播所需的 revision。

        Args:
            event_type: TaskResult 或 Cognitive 来源事件名称。
            payload: 来源事件的 JSON 友好载荷。
            runtime_epoch: 事件所属助手运行时世代。

        Returns:
            changed、duplicate 或 absorbed 结果；只有 changed 携带投影。
        """

        current_runtime_epoch = self._current_runtime_epoch()
        activity_id = canonical_activity_uuid(payload.get("activity_id")) or "unknown"
        if runtime_epoch != current_runtime_epoch:
            # 热替换后旧子进程仍可能吐出残留事件；绝不能让它改写当前运行时的快照。
            log_event(
                logger,
                logging.INFO,
                "activity_event_stale_epoch",
                action="忽略来源事件",
                reason="事件来自已失效的助手运行时，不能改写当前 Activity 状态",
                activity_id=activity_id,
                source_event_type=event_type,
                received_runtime_epoch=runtime_epoch,
                active_runtime_epoch=current_runtime_epoch,
            )
            return ActivityApplyResult("absorbed")
        try:
            change = adapt_activity_source_event(event_type, payload, runtime_epoch)
            if change is None:
                return ActivityApplyResult("absorbed")
            changed = self._registry.apply(change)
        except ValueError as exception:
            # 非法阶段代码或故障结构属于来源契约违规；隔离单条事件，不能拖垮事件排水线程。
            log_event(
                logger,
                logging.WARNING,
                "activity_event_rejected",
                action="拒绝并隔离来源事件",
                reason="事件包含非法阶段代码或故障报告，无法转换为 Activity 状态变化",
                activity_id=activity_id,
                source_event_type=event_type,
                runtime_epoch=runtime_epoch,
                exception_type=type(exception).__name__,
                exception_message=str(exception),
            )
            return ActivityApplyResult("absorbed")
        except ActivityInvariantViolation as exception:
            # 阶段倒退或同类 Activity 重复占用同样只是单条坏事件，不能影响其它任务。
            log_event(
                logger,
                logging.WARNING,
                "activity_event_rejected",
                action="拒绝并隔离来源事件",
                reason="事件会破坏 Activity 生命周期不变量，已保留当前权威状态",
                activity_id=activity_id,
                source_event_type=event_type,
                runtime_epoch=runtime_epoch,
                exception_type=type(exception).__name__,
                exception_message=str(exception),
            )
            return ActivityApplyResult("absorbed")
        if not changed:
            log_event(
                logger,
                logging.DEBUG,
                "activity_event_duplicate",
                action="忽略重复来源事件",
                reason="相同 Activity 状态已经写入，重复事件不再次广播",
                activity_id=change.activity_id,
                activity_kind=change.kind,
                activity_change=change.change,
                source_event_type=event_type,
                runtime_epoch=runtime_epoch,
            )
            return ActivityApplyResult("duplicate")
        snapshot = self._registry.desktop_snapshot()
        self._log_transition(
            activity_id=change.activity_id,
            runtime_epoch=runtime_epoch,
            revision=snapshot.revision,
            runtime_id=snapshot.runtime_id,
        )
        return ActivityApplyResult(
            "changed",
            ActivityProjection(revision=snapshot.revision),
        )

    async def broadcast_projection(self, projection: ActivityProjection) -> None:
        """广播一次已提交的 Activity 变化，并防止 revision 倒退。

        ``projection`` 只是“某次写入已经成功”的凭据，不携带完整快照。广播前重新读取
        Registry，因此前台始终拿到当前权威状态；若并发事件已推进到更高 revision，也允许
        一次广播覆盖多次变化。

        Args:
            projection: 同步应用来源事件得到的提交凭据。
        """

        snapshot = self._registry.desktop_snapshot()
        if snapshot.revision < projection.revision:
            raise RuntimeError("activity registry revision cannot move backwards")
        await self._broadcast_snapshot(
            {
                "type": "activities.snapshot_changed",
                "payload": snapshot.to_payload(),
            }
        )

    def desktop_snapshot(self) -> ActivitySnapshot:
        """返回 Desktop 面板使用的裁剪 Activity 快照。

        Returns:
            包含运行中 Activity 与最近三项终态的当前权威快照。
        """

        return self._registry.desktop_snapshot()

    def console_snapshot(self) -> ActivitySnapshot:
        """返回 Console 使用的本次运行完整 Activity 快照。

        Returns:
            包含本次运行全部可见 Activity 的当前权威快照。
        """

        return self._registry.console_snapshot()

    def detail(self, activity_id: str) -> ActivityDetail:
        """按编号返回一项 Activity 的公共详情。

        Args:
            activity_id: 标准 UUID 活动编号。

        Returns:
            当前运行中匹配的 Activity 详情；不存在时由 Registry 抛出 ``ActivityNotFound``。
        """

        return self._registry.detail(activity_id)

    def completion_result(self, activity_id: str) -> ActivityCompletionResult:
        """按编号返回成功 Activity 的结构化完成结果。

        Args:
            activity_id: 标准 UUID 活动编号。

        Returns:
            Notice 临时投影所需的完成结果。

        Raises:
            ActivityNotFound: 当前运行不存在该 Activity。
            ActivityStateConflict: Activity 尚未成功完成或没有完成结果。
        """

        return self._registry.completion_result(activity_id)

    async def cancel(self, activity_id: str) -> None:
        """提交真实取消命令，并在接收成功后广播 ``cancelling`` 快照。

        先让助手运行时接收命令，再修改 Registry。这样提交端口不可用时，任务中心不会
        错误地显示“正在取消”；真正的 ``cancelled`` 终态仍必须等待执行器退出后回报。

        Args:
            activity_id: 准备取消的标准 UUID 活动编号。

        Raises:
            ActivityRuntimeUnavailable: 助手运行时不能接收命令。
            ActivityNotFound: 命令入队前活动已经不存在。
            ActivityStateConflict: 命令入队前活动已不再允许取消。
        """

        target = self._registry.cancellation_target(activity_id)
        if target.runtime_epoch != self._current_runtime_epoch():
            raise ActivityStateConflict("activity belongs to a replaced runtime")
        command = CancelActivityCommand(
            activity_id=target.activity_id,
            source=target.control_source,
            reason="user_cancelled",
            issued_at=self._monotonic_now(),
        )
        try:
            self._submit_cancel_command(command)
        except RuntimeError as exception:
            raise ActivityRuntimeUnavailable("assistant runtime is unavailable") from exception

        mark_result = self._registry.mark_cancelling(activity_id)
        # 命令已被接收后，活动可能恰好进入终态并被淘汰；这不能把已接受的取消改判为失败。
        if mark_result != "marked":
            return
        snapshot = self._registry.desktop_snapshot()
        self._log_transition(
            activity_id=activity_id,
            runtime_epoch=target.runtime_epoch,
            revision=snapshot.revision,
            runtime_id=snapshot.runtime_id,
        )
        await self._broadcast_desktop_snapshot()

    async def interrupt_active(
        self,
        runtime_epoch: int,
        failure_code: Literal["runtime_reconfigured", "runtime_interrupted"],
    ) -> bool:
        """在助手运行时热替换或崩溃时中断指定世代的全部在途 Activity。

        ``runtime_reconfigured`` 表示计划内替换，``runtime_interrupted`` 表示意外中断；
        二者都会写入权威终态和故障报告，是否形成前台 Notice 由调用链上的 Notice 边界
        另行决定。

        Args:
            runtime_epoch: 已失效的助手运行时世代。
            failure_code: 计划内热替换或意外中断对应的错误码。

        Returns:
            至少关闭一项 Activity 并广播当前权威快照时返回真。
        """

        settlement_key = (runtime_epoch, failure_code)
        interrupted = self._registry.interrupt_active(runtime_epoch, failure_code)
        if not interrupted:
            interrupted = self._pending_interruption_settlements.get(settlement_key, ())
        if not interrupted:
            return False
        # Registry 已经给出本次真正进入终态的权威集合。先结算 Progress 和任务记忆，
        # 再广播 Activity、生成 Notice，避免任何消费者看见缺少记忆的半终态。
        if self._interruption_terminal_handler is not None:
            try:
                self._interruption_terminal_handler(interrupted, failure_code)
            except Exception:
                self._pending_interruption_settlements[settlement_key] = interrupted
                raise
        self._pending_interruption_settlements.pop(settlement_key, None)
        snapshot = self._registry.desktop_snapshot()
        for detail in interrupted:
            self._log_transition(
                activity_id=detail.compact.activity_id,
                runtime_epoch=runtime_epoch,
                revision=snapshot.revision,
                runtime_id=snapshot.runtime_id,
                detail=detail,
            )
        await self._broadcast_desktop_snapshot()
        return True

    def _log_transition(
        self,
        *,
        activity_id: str,
        runtime_epoch: int,
        revision: int,
        runtime_id: str,
        detail: ActivityDetail | None = None,
    ) -> None:
        """为一次已提交的公共 Activity 变化写结构化日志。

        日志使用 Activity 编号关联完整技术日志，但只记录已进入公共状态的安全字段，避免
        把 Prompt、原始异常或执行正文带入普通业务日志。

        Args:
            activity_id: 发生变化的活动编号。
            runtime_epoch: 产生该事实的助手运行时世代。
            revision: 变化提交后的 Registry 版本。
            runtime_id: 本次 Backend 进程运行编号。
            detail: 已知的 Activity 详情；为空时从 Registry 读取。
        """

        if detail is None:
            try:
                detail = self._registry.detail(activity_id)
            except ActivityNotFound:
                # 读取日志详情期间可能被最近终态淘汰；不补造记录，只留下可排查的痕迹。
                log_event(
                    logger,
                    logging.DEBUG,
                    "activity_transition_detail_missing",
                    action="跳过 Activity 变化日志",
                    reason="Activity 已被最近终态记录淘汰，无法读取当时的公开详情",
                    activity_id=activity_id,
                    revision=revision,
                )
                return
        status_label = _ACTIVITY_STATUS_LABELS.get(
            detail.compact.status,
            detail.compact.status,
        )
        stage_label = detail.compact.stage_label or "未进入执行阶段"
        action = (
            f"{detail.compact.type_label}状态已更新为“{status_label}”，"
            f"当前执行阶段为“{stage_label}”"
        )
        if detail.compact.failure_summary:
            action = f"{action}，故障原因：{detail.compact.failure_summary}"
        log_event(
            logger,
            logging.INFO,
            "activity_transition",
            action=action,
            activity_id=activity_id,
            activity_kind=detail.compact.kind,
            activity_type=detail.compact.type_label,
            goal_summary=detail.compact.goal_summary,
            status=detail.compact.status,
            stage_code=detail.compact.stage_code or "",
            stage_label=stage_label,
            failure_code=detail.failure.code if detail.failure is not None else "",
            failure_summary=detail.compact.failure_summary or "",
            runtime_epoch=runtime_epoch,
            runtime_id=runtime_id,
            revision=revision,
        )

    async def _broadcast_desktop_snapshot(self) -> None:
        """广播经过固定裁剪的 Desktop Activity 快照。

        取消和运行时中断没有预先持有投影凭据，因此直接读取 Registry 的最新快照；前端
        仍以其中的 ``runtime_id`` 与 revision 判断是否接受。
        """

        await self._broadcast_snapshot(
            {
                "type": "activities.snapshot_changed",
                "payload": self._registry.desktop_snapshot().to_payload(),
            }
        )
