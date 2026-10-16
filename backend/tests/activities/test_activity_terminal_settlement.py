from __future__ import annotations

from typing import Literal

import pytest

from companion.activities.progress_ledger import ActivityProgressLedger
from companion.activities.runtime.models import ActivityDetail
from companion.activities.runtime.registry import ActivityRuntimeRegistry
from companion.activities.runtime.service import ActivityService
from companion.activities.terminal_settlement import ActivityTerminalSettlementService
from companion.realtime.assistant_output_contracts import ActivityProgressReference
from companion.runtime_memory.store import RuntimeMemoryStore


RUNTIME_EPOCH = 5
ACTIVITY_ID = "00000000-0000-4000-8000-0000000000c1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_code", "expected_progress_lifecycle"),
    [
        ("runtime_interrupted", "frozen"),
        ("runtime_reconfigured", None),
    ],
)
async def test_runtime_interruption_settles_memory_before_broadcast_and_handles_progress(
    failure_code: Literal["runtime_interrupted", "runtime_reconfigured"],
    expected_progress_lifecycle: str | None,
) -> None:
    """运行时合成终态也必须先结算记忆，并按是否有 Notice 决定保留 Progress。"""

    timeline: list[str] = []
    progress_ledger = ActivityProgressLedger()
    progress_ledger.record_visible_progress(
        visible_text="我已经核对了一部分。",
        references=(
            ActivityProgressReference(ACTIVITY_ID, "cognitive", 1),
        ),
    )
    memory_store = RuntimeMemoryStore()
    terminal_settlement = ActivityTerminalSettlementService(
        activity_progress_ledger=progress_ledger,
        runtime_memory_store=memory_store,
    )

    def settle_interruption(
        interrupted_activities: tuple[ActivityDetail, ...],
        interruption_code: Literal["runtime_reconfigured", "runtime_interrupted"],
    ) -> int:
        """记录中断结算时机并调用真实终态服务。

        Args:
            interrupted_activities: Registry 本次真正关闭的 Activity 详情。
            interruption_code: 计划内热替换或意外中断代码。

        Returns:
            真实服务结算的 Activity 数量。
        """

        timeline.append("terminal_settled")
        return terminal_settlement.settle_runtime_interruption(
            interrupted_activities,
            interruption_code,
        )

    async def broadcast_snapshot(_event: dict[str, object]) -> None:
        """记录 Activity 快照广播时机。

        Args:
            _event: 已提交的 Activity 快照事件。
        """

        timeline.append("activity_broadcasted")

    service = ActivityService(
        registry=ActivityRuntimeRegistry(runtime_id="runtime-terminal-test"),
        broadcast_snapshot=broadcast_snapshot,
        submit_cancel_command=lambda _command: None,
        current_runtime_epoch=lambda: RUNTIME_EPOCH,
        interruption_terminal_handler=settle_interruption,
    )
    applied = service.apply_source_event(
        "cognitive_task.started",
        {
            "activity_id": ACTIVITY_ID,
            "kind": "lookup_current_fact",
            "brief": "核对天气结论",
        },
        RUNTIME_EPOCH,
    )
    assert applied.disposition == "changed"

    interrupted = await service.interrupt_active(
        RUNTIME_EPOCH,
        failure_code,
    )

    assert interrupted is True
    assert timeline == ["terminal_settled", "activity_broadcasted"]
    recalled = memory_store.recall_task(ACTIVITY_ID, purpose="foreground_recall")
    assert recalled["terminal_state"] == "interrupted"
    snapshots = progress_ledger.snapshot()
    if expected_progress_lifecycle is None:
        assert snapshots == ()
    else:
        assert len(snapshots) == 1
        assert snapshots[0].lifecycle == expected_progress_lifecycle
