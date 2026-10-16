"""准备 Foreground Activity 取消，并把副作用绑定到提交后事务。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from companion.foreground_tools.activity_control.broker import (
    ActivityCancellationBroker,
)
from companion.foreground_tools.activity_control.policy import (
    CancellableActivityContext,
)
from companion.foreground_tools.orchestration.contracts import ToolFollowupFact
from companion.foreground_tools.orchestration.transactions import CommitAction
from companion.llm.foreground_models import ForegroundToolCall


@dataclass(frozen=True)
class ActivityCancellationReservation:
    """保存一笔已准备、但尚未越过 Foreground 提交边界的取消动作。

    Args:
        cancellation_id: 本次取消请求的唯一编号。
        activity_id: 准备精确取消的 Activity 编号。
        source: 供提交日志识别的固定资源来源。
        action: 工具事件提交后才会向主进程发请求的一次性事务。
    """

    cancellation_id: str
    activity_id: str
    source: str
    action: CommitAction

    def rollback(self) -> bool:
        """放弃一笔尚未提交的取消动作。

        Returns:
            事务首次进入回滚态时返回真，已提交或已回滚时返回假。
        """

        return self.action.rollback()


class ActivityCancellationToolExecutor:
    """校验取消参数，并创建只能在提交后执行的取消事务。

    Args:
        broker: 负责把正式取消请求送到主进程并等待权威回执的 Broker。
    """

    def __init__(self, broker: ActivityCancellationBroker) -> None:
        """保存 Broker，并初始化尚未被编排器领取的事务表。

        Args:
            broker: 把已提交取消请求送到主进程并等待回执的 Broker。
        """

        self._broker = broker
        self._prepared: dict[str, ActivityCancellationReservation] = {}
        self._lock = RLock()

    def prepare(
        self,
        *,
        tool_call: ForegroundToolCall,
        cancellable_activities: tuple[CancellableActivityContext, ...],
    ) -> dict[str, object]:
        """根据本轮冻结候选准备一笔精确取消动作。

        prepare 只验证编号并创建事务，不会向主进程发送取消请求。这样工具完成事件
        在入队前被打断时，原 Activity 会继续运行。

        Args:
            tool_call: Foreground 模型产生的结构化取消调用。
            cancellable_activities: Prompt、schema 和执行共用的冻结允许列表。

        Returns:
            可交给 deferred 通道的准备结果或安全拒绝。
        """

        activity_id_value = tool_call.arguments.get("activity_id")
        activity_id = (
            activity_id_value.strip()
            if isinstance(activity_id_value, str)
            else ""
        )
        context = next(
            (
                candidate
                for candidate in cancellable_activities
                if candidate.activity_id == activity_id
            ),
            None,
        )
        if context is None:
            return {
                "state": "rejected",
                "reason": "activity_not_cancellable_in_current_turn",
                "safe_message": "这项任务不在当前可取消范围内，没有提交取消。",
            }

        cancellation_id = self._broker.create_cancellation_id()
        if not cancellation_id:
            return {
                "state": "failed",
                "reason": "activity_cancellation_unavailable",
                "safe_message": "当前无法提交取消请求，任务没有被取消。",
            }

        def cancel_after_commit() -> ToolFollowupFact:
            """在工具事件正式提交后请求主进程取消 Activity。

            Returns:
                主进程已接收取消请求或明确拒绝后的结构化事实。
            """

            result = self._broker.submit_and_wait(
                cancellation_id=cancellation_id,
                activity_id=context.activity_id,
            )
            if result.get("state") == "cancellation_requested":
                return ToolFollowupFact(
                    kind="activity_cancellation_requested",
                    activity_id=context.activity_id,
                    goal=context.goal,
                )
            safe_message = result.get("safe_message")
            return ToolFollowupFact(
                kind="activity_cancellation_failed",
                activity_id=context.activity_id,
                goal=context.goal,
                safe_message=(
                    safe_message.strip()
                    if isinstance(safe_message, str) and safe_message.strip()
                    else "这项任务没有确认进入停止流程。"
                ),
            )

        reservation = ActivityCancellationReservation(
            cancellation_id=cancellation_id,
            activity_id=context.activity_id,
            source="activity_control",
            action=CommitAction(
                on_accept=lambda: None,
                on_rollback=lambda: None,
                after_commit=cancel_after_commit,
                transaction_id=f"activity-cancellation-{cancellation_id}",
                resource_type="activity_cancellation",
                resource_id=context.activity_id,
            ),
        )
        with self._lock:
            if cancellation_id in self._prepared:
                return {
                    "state": "failed",
                    "reason": "duplicate_cancellation_request",
                    "safe_message": "这次取消请求没有成功准备。",
                }
            self._prepared[cancellation_id] = reservation
        return {
            "state": "accepted",
            "resource_id": cancellation_id,
            "activity_id": context.activity_id,
            "goal": context.goal,
            "safe_message": "取消动作已准备，等待当前工具事务提交。",
        }

    def take_reservation(
        self,
        cancellation_id: str,
    ) -> ActivityCancellationReservation | None:
        """按取消请求编号首次领取已准备的提交事务。

        Args:
            cancellation_id: deferred 准备结果返回的稳定资源编号。

        Returns:
            首次领取时返回事务，不存在或已领取时返回空。
        """

        with self._lock:
            return self._prepared.pop(cancellation_id.strip(), None)

    def rollback_prepared(self, cancellation_id: str) -> bool:
        """释放 generation 在提交前中断时尚未领取的取消事务。

        Args:
            cancellation_id: deferred 准备结果返回的稳定资源编号。

        Returns:
            找到事务并首次回滚时返回真，否则返回假。
        """

        reservation = self.take_reservation(cancellation_id)
        return reservation.rollback() if reservation is not None else False
