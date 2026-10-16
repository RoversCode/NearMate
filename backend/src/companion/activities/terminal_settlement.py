"""统一结算 Activity 终态对应的 Progress 与任务记忆。"""

from __future__ import annotations

from typing import Literal

from companion.activities.progress_ledger import (
    ActivityProgressKey,
    ActivityProgressLedger,
)
from companion.activities.runtime.models import ActivityDetail
from companion.activities.runtime.adapters import validate_activity_terminal_contract
from companion.observability.logging_config import get_backend_logger
from companion.runtime_memory.store import RuntimeMemoryStore


logger = get_backend_logger(__name__)

ACTIVITY_TERMINAL_EVENT_TYPES = frozenset(
    {
        "cognitive_task.completed",
        "cognitive_task.failed",
        "cognitive_task.rejected",
        "cognitive_task.cancelled",
        "cognitive_task.interrupted",
        "background_agent.completed",
        "background_agent.failed",
        "background_agent.cancelled",
        "background_agent.interrupted",
    }
)


class ActivityTerminalSettlementRetryableError(RuntimeError):
    """表示 Activity 终态尚未完成 Progress 与任务记忆的必达结算。"""


class ActivityTerminalContractError(ValueError):
    """表示来源终态违反公开合同，不能通过退避重试修复。"""


class ActivityTerminalSettlementService:
    """保证 Activity 终态先冻结 Progress，再写入任务记忆。

    Args:
        activity_progress_ledger: Backend 主进程唯一的 Progress 临时账本。
        runtime_memory_store: Backend 主进程唯一的运行期记忆账本；为空时只处理 Progress。
    """

    def __init__(
        self,
        *,
        activity_progress_ledger: ActivityProgressLedger,
        runtime_memory_store: RuntimeMemoryStore | None,
        require_canonical_activity_id: bool = False,
    ) -> None:
        """保存两个主进程权威账本。

        Args:
            activity_progress_ledger: Backend 主进程唯一的 Progress 临时账本。
            runtime_memory_store: Backend 主进程唯一的运行期记忆账本；为空时只处理 Progress。
        """

        self._activity_progress_ledger = activity_progress_ledger
        self._runtime_memory_store = runtime_memory_store
        self._require_canonical_activity_id = require_canonical_activity_id

    def settle_source_terminal(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        """结算一条由执行器回流的 Activity 终态。

        Args:
            event_type: Cognitive 或后台工作的来源终态事件名。
            payload: 已通过当前运行时世代门禁的终态载荷。

        Returns:
            本次至少推进了一项 Progress 或任务记忆事实时返回真。
        """

        if event_type not in ACTIVITY_TERMINAL_EVENT_TYPES:
            return False
        # 来源终态必须先通过结果与故障报告合同，才能冻结进度或写任务记忆。
        # 否则后续 Activity Registry 即使拒绝坏事件，模型上下文也已经被污染。
        try:
            validate_activity_terminal_contract(
                event_type,
                payload,
                require_canonical_activity_id=self._require_canonical_activity_id,
            )
        except (TypeError, ValueError) as exception:
            raise ActivityTerminalContractError(
                f"Activity 终态合同非法: {event_type}"
            ) from exception
        try:
            # 任务记忆可能马上触发终态 Notice。必须先把进度移出 active 动态环境，
            # 后续用户轮才不会同时看到“任务仍在运行”和“任务已经结束”两套事实。
            progress_frozen = self._activity_progress_ledger.freeze_from_runtime_event(
                event_type,
                payload,
            )
            if self._runtime_memory_store is None:
                return progress_frozen
            task_record = self._runtime_memory_store.record_task_terminal(
                event_type,
                payload,
            )
        except Exception as exception:
            # Progress 冻结和任务记忆都是幂等操作。任何一步暂时失败都让来源终态留在
            # Drainer 的必达重试通道，不能继续创建一个缺少任务记忆的终态 Notice。
            raise ActivityTerminalSettlementRetryableError(
                f"Activity 终态结算失败: {event_type}"
            ) from exception
        return progress_frozen or task_record is not None

    def settle_runtime_interruption(
        self,
        interrupted_activities: tuple[ActivityDetail, ...],
        failure_code: Literal["runtime_reconfigured", "runtime_interrupted"],
    ) -> int:
        """结算主进程为失效运行时合成的 Activity 中断终态。

        Args:
            interrupted_activities: Activity Registry 本次真正关闭的权威详情。
            failure_code: 计划内热替换或意外运行时中断。

        Returns:
            本次识别并结算的 Activity 数量。
        """

        settled_activity_keys: list[ActivityProgressKey] = []
        for activity in interrupted_activities:
            source = activity.compact.kind
            if source == "cognitive":
                event_type = "cognitive_task.interrupted"
                goal_field = "brief"
            elif source == "background_agent":
                event_type = "background_agent.interrupted"
                goal_field = "goal"
            else:
                logger.warning(
                    "忽略未知来源的 Activity 中断终态 activity_id=%s source=%s",
                    activity.compact.activity_id,
                    source,
                )
                continue

            payload: dict[str, object] = {
                "activity_id": activity.compact.activity_id,
                goal_field: activity.goal,
            }
            if activity.failure is not None:
                payload["failure"] = activity.failure.to_payload()
            self.settle_source_terminal(event_type, payload)
            settled_activity_keys.append((source, activity.compact.activity_id))

        if failure_code == "runtime_reconfigured" and settled_activity_keys:
            # 计划内热替换明确不产生终态 Notice。此时冻结只服务于任务记忆提交顺序，
            # 写完任务记忆后就已经完成处置，不能留下永远等不到交付回执的 frozen 记录。
            self._activity_progress_ledger.release_frozen(
                tuple(settled_activity_keys)
            )
        return len(settled_activity_keys)
