from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from companion.activities.progress_ledger import ActivityProgressLedger
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_output_commit_coordinator import (
    AssistantOutputCommitCoordinator,
)
from companion.realtime.assistant_output_settlement_service import (
    AssistantOutputSettlementService,
)
from companion.realtime.visible_output_journal import (
    VisibleOutputAdmission,
    VisibleOutputJournal,
    VisibleOutputSettlement,
)
from companion.realtime.runtime_identity import AssistantRequestIdentityConflict


class _FailFirstCommitSettlement:
    """第一次提交失败，用来验证同一 generation 的后续事件不会被吞掉。"""

    def __init__(self) -> None:
        self.commit_attempts = 0
        self.committed_event_types: list[str] = []

    def prepare_broadcast_text(self, event: RuntimeEvent) -> VisibleOutputAdmission:
        """为每条公开事件生成独立 admission。"""

        return VisibleOutputAdmission(
            token=f"admission-{event.type}-{id(event)}",
            event_type=event.type,
            payload=event.payload,
        )

    def commit_broadcast_text(
        self,
        event: RuntimeEvent,
        _admission: VisibleOutputAdmission,
    ) -> bool:
        """首轮抛错，后续按真实事件顺序记录提交。"""

        self.commit_attempts += 1
        if self.commit_attempts == 1:
            raise RuntimeError("synthetic journal failure")
        self.committed_event_types.append(event.type)
        return True

    def abort_broadcast_text(self, _admission: VisibleOutputAdmission) -> None:
        """测试广播均成功，因此不需要记录撤销。"""


class _IdentityConflictCommitSettlement:
    """模拟 Commit 阶段发现身份冲突的不可重试服务。"""

    def __init__(self) -> None:
        self.aborted_tokens: list[str] = []

    def prepare_broadcast_text(self, event: RuntimeEvent) -> VisibleOutputAdmission:
        """为已经广播的正文创建 admission。"""

        return VisibleOutputAdmission(
            token=f"identity-conflict-{event.type}",
            event_type=event.type,
            payload=event.payload,
        )

    def commit_broadcast_text(
        self,
        _event: RuntimeEvent,
        _admission: VisibleOutputAdmission,
    ) -> bool:
        """返回不可重试的身份冲突。"""

        raise AssistantRequestIdentityConflict(
            request_id="request-a",
            field="self_reflection_id",
            expected="reflection-a",
            actual=None,
        )

    def abort_broadcast_text(self, admission: VisibleOutputAdmission) -> None:
        """记录 Coordinator 是否撤销了悬挂 admission。"""

        self.aborted_tokens.append(admission.token)


class _RecordingCancelledSettlement:
    """记录被取消请求是否仍能完成输出结算。"""

    def __init__(self) -> None:
        self.validated_request_ids: list[str] = []
        self.settled_request_ids: list[str] = []

    def prepare_broadcast_text(
        self,
        _event: RuntimeEvent,
    ) -> VisibleOutputAdmission | None:
        """取消墓碑应在 Journal Prepare 前拦住迟到正文。"""

        raise AssertionError("取消后的正文不应进入 Journal Prepare")

    def validate_settlement(self, event: RuntimeEvent) -> None:
        """记录 settlement 仍然经过身份预检。"""

        self.validated_request_ids.append(cast(str, event.payload["request_id"]))

    def settle(self, event: RuntimeEvent) -> None:
        """记录 settlement 仍然完成提交。"""

        self.settled_request_ids.append(cast(str, event.payload["request_id"]))


class _RecordingMaterials:
    """记录 Final 成功后才允许发生的材料释放。"""

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline

    async def release_from_payload(self, _payload: dict[str, object]) -> None:
        """记录材料释放。"""

        self.timeline.append("materials")


class _RecordingActivity:
    """记录 Final 对 TaskResult 请求的确认。"""

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline

    async def acknowledge_task_result_request(
        self,
        _runtime_epoch: int,
        _request_id: str,
    ) -> None:
        """记录 TaskResult 确认。"""

        self.timeline.append("task_result")


class _FailFirstVisibleOutputJournal(VisibleOutputJournal):
    """第一次 admission 提交失败，其余操作使用真实 Journal 逻辑。"""

    def __init__(self, timeline: list[str]) -> None:
        super().__init__()
        self.timeline = timeline
        self.commit_attempts = 0

    def commit_public_text_event(self, admission: VisibleOutputAdmission) -> bool:
        """首轮模拟临时存储异常，后续记录实际提交顺序。"""

        self.commit_attempts += 1
        if self.commit_attempts == 1:
            raise RuntimeError("synthetic journal failure")
        recorded = super().commit_public_text_event(admission)
        if recorded:
            self.timeline.append(f"journal:{admission.event_type}")
        return recorded

    def settle(
        self,
        payload: dict[str, object],
        *,
        settled_at: float,
        commit_settlement: Any,
    ) -> VisibleOutputSettlement | None:
        """记录 settlement 调用位置，再执行真实归档。"""

        self.timeline.append("settlement")
        return super().settle(
            payload,
            settled_at=settled_at,
            commit_settlement=commit_settlement,
        )


def _text_event(event_type: str, text: str) -> RuntimeEvent:
    """构造同一个 generation 内的文本事件。"""

    return RuntimeEvent(
        type=event_type,
        source="assistant_runtime",
        runtime_epoch=1,
        payload={
            "request_id": "request-a",
            "session_id": "session-a",
            "generation_id": "generation-a",
            "text": text,
        },
    )


def _self_reflection_identity() -> dict[str, object]:
    """构造 Self-Reflection 正文和 settlement 共用的完整身份。"""

    return {
        "request_id": "reflection-request",
        "session_id": "session-a",
        "generation_id": "reflection-generation",
        "source": "self_reflection_entry",
        "history_policy": "unattended_episode",
        "activity_progress_references": [],
        "derived_from_activity_ids": [],
        "self_reflection_id": "reflection-a",
        "episode_id": "episode-a",
        "runtime_epoch": 1,
    }


@pytest.mark.asyncio
async def test_failed_chunk_commit_does_not_swallow_following_final() -> None:
    """前一 chunk 提交失败时，Final 仍公开且所有提交保持原始顺序。"""

    published: list[str] = []
    timeline: list[str] = []
    settlement = _FailFirstCommitSettlement()

    async def publish(event: dict[str, Any]) -> None:
        """记录每条公共正文，确保重试不重复广播。"""

        published.append(cast(str, event["type"]))

    coordinator = AssistantOutputCommitCoordinator(
        publish_assistant_output=publish,
        output_settlement_service=cast(Any, settlement),
        turn_material_release_service=cast(Any, _RecordingMaterials(timeline)),
        activity_event_handler=cast(Any, _RecordingActivity(timeline)),
        notice_delivery_handler=lambda _command: timeline.append("notice"),
    )

    await coordinator.handle_text(_text_event("assistant.text_chunk", "第一段"))
    await coordinator.handle_text(_text_event("assistant.text_final", "第一段完成"))

    assert published == ["assistant.text_chunk", "assistant.text_final"]
    assert settlement.committed_event_types == [
        "assistant.text_chunk",
        "assistant.text_final",
    ]
    assert timeline == ["notice", "materials", "task_result"]


@pytest.mark.asyncio
async def test_cancelled_request_drops_late_text_but_still_commits_settlement() -> None:
    """显式取消后不再公开迟到正文，但必须放行 settlement 释放请求资源。"""

    published: list[str] = []
    settlement = _RecordingCancelledSettlement()

    async def publish(event: dict[str, Any]) -> None:
        """记录任何错误穿过取消墓碑的公共正文。"""

        published.append(cast(str, event["type"]))

    coordinator = AssistantOutputCommitCoordinator(
        publish_assistant_output=publish,
        output_settlement_service=cast(Any, settlement),
        turn_material_release_service=cast(Any, _RecordingMaterials([])),
        activity_event_handler=cast(Any, _RecordingActivity([])),
        notice_delivery_handler=None,
    )

    coordinator.cancel_request("request-a")
    for event_type in (
        "assistant.text_chunk",
        "assistant.text_segment_final",
        "assistant.text_final",
    ):
        await coordinator.handle_text(_text_event(event_type, "迟到正文"))
    await coordinator.handle_settlement(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={
                "request_id": "request-a",
                "generation_id": "generation-a",
                "outcome": "interrupted",
            },
        )
    )

    assert published == []
    assert settlement.validated_request_ids == ["request-a"]
    assert settlement.settled_request_ids == ["request-a"]
    assert coordinator._lanes == {}


@pytest.mark.asyncio
async def test_settlement_waits_for_failed_visible_text_commit_lane() -> None:
    """结算不能越过已公开但首次提交失败的正文，也不能重复广播或提交副作用。"""

    published: list[str] = []
    timeline: list[str] = []
    progress_ledger = ActivityProgressLedger()
    journal = _FailFirstVisibleOutputJournal(timeline)
    settlement_service = AssistantOutputSettlementService(
        activity_progress_ledger=progress_ledger,
        visible_output_journal=journal,
    )

    async def publish(event: dict[str, Any]) -> None:
        """记录公共广播次数。"""

        published.append(cast(str, event["type"]))

    coordinator = AssistantOutputCommitCoordinator(
        publish_assistant_output=publish,
        output_settlement_service=settlement_service,
        turn_material_release_service=cast(Any, _RecordingMaterials(timeline)),
        activity_event_handler=cast(Any, _RecordingActivity(timeline)),
        notice_delivery_handler=lambda _command: timeline.append("notice"),
    )
    progress_identity = {
        "request_id": "request-progress",
        "session_id": "session-a",
        "generation_id": "generation-progress",
        "history_policy": "activity_progress",
        "activity_progress_references": [
            {
                "activity_id": "activity-a",
                "source": "cognitive",
                "notice_sequence": 2,
            }
        ],
    }

    await coordinator.handle_text(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**progress_identity, "text": "初步结果。"},
        )
    )
    await coordinator.handle_text(
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**progress_identity, "text": "最终结果。"},
        )
    )
    await coordinator.handle_settlement(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**progress_identity, "outcome": "completed"},
        )
    )
    await asyncio.sleep(0)

    assert published == ["assistant.text_chunk", "assistant.text_final"]
    assert timeline == [
        "journal:assistant.text_chunk",
        "journal:assistant.text_final",
        "notice",
        "materials",
        "task_result",
        "settlement",
    ]
    assert progress_ledger.reports_by_activity({("cognitive", "activity-a")}) == {
        ("cognitive", "activity-a"): ("最终结果。",)
    }
    assert coordinator._lanes == {}


@pytest.mark.asyncio
async def test_identity_conflict_aborts_admission_without_retrying_lane() -> None:
    """身份冲突必须撤销 admission，不能留下可重试的半提交 lane。"""

    settlement = _IdentityConflictCommitSettlement()

    async def publish(_event: dict[str, Any]) -> None:
        """模拟公共正文已经成功广播。"""

    coordinator = AssistantOutputCommitCoordinator(
        publish_assistant_output=publish,
        output_settlement_service=cast(Any, settlement),
        turn_material_release_service=cast(Any, _RecordingMaterials([])),
        activity_event_handler=cast(Any, _RecordingActivity([])),
        notice_delivery_handler=None,
    )

    with pytest.raises(AssistantRequestIdentityConflict, match="self_reflection_id"):
        await coordinator.handle_text(_text_event("assistant.text_final", "错误 final"))

    assert settlement.aborted_tokens == ["identity-conflict-assistant.text_final"]
    lane = next(iter(coordinator._lanes.values()))
    assert list(lane.pending) == []
    assert lane.retry_task is None


@pytest.mark.asyncio
async def test_settlement_identity_conflict_aborts_all_pending_admissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settlement 预检冲突也必须清掉退避期间积压的 admission。"""

    journal = VisibleOutputJournal()
    settlement_service = AssistantOutputSettlementService(
        activity_progress_ledger=ActivityProgressLedger(),
        visible_output_journal=journal,
    )

    async def publish(_event: dict[str, Any]) -> None:
        """模拟公共正文成功广播。"""

    coordinator = AssistantOutputCommitCoordinator(
        publish_assistant_output=publish,
        output_settlement_service=settlement_service,
        turn_material_release_service=cast(Any, _RecordingMaterials([])),
        activity_event_handler=cast(Any, _RecordingActivity([])),
        notice_delivery_handler=None,
    )
    identity = _self_reflection_identity()
    await coordinator.handle_text(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**identity, "text": "已经公开。"},
        )
    )

    original_record = journal._record_public_text_event_locked

    def fail_final_commit_once(
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        """让 final 暂时留在退避 lane，便于验证冲突清理。"""

        if event_type == "assistant.text_final":
            raise RuntimeError("temporary journal failure")
        return original_record(event_type, payload)

    monkeypatch.setattr(
        journal,
        "_record_public_text_event_locked",
        fail_final_commit_once,
    )
    await coordinator.handle_text(
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**identity, "text": "完整正文。"},
        )
    )
    lane = next(iter(coordinator._lanes.values()))
    assert len(lane.pending) == 1
    assert journal._pending_admissions  # type: ignore[attr-defined]

    malformed_identity = dict(identity)
    malformed_identity.pop("self_reflection_id")
    with pytest.raises(AssistantRequestIdentityConflict, match="self_reflection_id"):
        await coordinator.handle_settlement(
            RuntimeEvent(
                type="assistant.output_settled",
                source="assistant_runtime",
                runtime_epoch=1,
                payload={**malformed_identity, "outcome": "completed"},
            )
        )

    await asyncio.sleep(0)
    assert list(lane.pending) == []
    assert lane.retry_task is None
    assert journal._pending_admissions == {}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_self_reflection_chunk_final_settlement_completes_without_pending_admission() -> None:
    """Self-Reflection 正常正文链必须只结算一次且不遗留 admission。"""

    journal = VisibleOutputJournal()
    settlement_service = AssistantOutputSettlementService(
        activity_progress_ledger=ActivityProgressLedger(),
        visible_output_journal=journal,
    )
    published: list[str] = []

    async def publish(event: dict[str, Any]) -> None:
        """记录公共正文事件。"""

        published.append(cast(str, event["type"]))

    coordinator = AssistantOutputCommitCoordinator(
        publish_assistant_output=publish,
        output_settlement_service=settlement_service,
        turn_material_release_service=cast(Any, _RecordingMaterials([])),
        activity_event_handler=cast(Any, _RecordingActivity([])),
        notice_delivery_handler=None,
    )
    identity = _self_reflection_identity()

    await coordinator.handle_text(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**identity, "text": "正文片段。"},
        )
    )
    await coordinator.handle_text(
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**identity, "text": "完整正文。"},
        )
    )
    await coordinator.handle_settlement(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**identity, "outcome": "completed"},
        )
    )

    assert published == ["assistant.text_chunk", "assistant.text_final"]
    assert journal._pending_admissions == {}  # type: ignore[attr-defined]
    assert journal._records == {}  # type: ignore[attr-defined]
    assert journal._settled_request_ids == {"reflection-request"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_self_reflection_identity_conflict_never_publishes_or_blocks_settlement() -> None:
    """缺失反思身份的 final 必须在广播前失败，且不能阻塞结算。"""

    journal = VisibleOutputJournal()
    settlement_service = AssistantOutputSettlementService(
        activity_progress_ledger=ActivityProgressLedger(),
        visible_output_journal=journal,
    )
    published: list[str] = []

    async def publish(event: dict[str, Any]) -> None:
        """记录真正越过 Prepare 门禁的公共正文。"""

        published.append(cast(str, event["type"]))

    coordinator = AssistantOutputCommitCoordinator(
        publish_assistant_output=publish,
        output_settlement_service=settlement_service,
        turn_material_release_service=cast(Any, _RecordingMaterials([])),
        activity_event_handler=cast(Any, _RecordingActivity([])),
        notice_delivery_handler=None,
    )
    identity = _self_reflection_identity()
    malformed_final_payload = {**identity, "text": "错误 final"}
    for field_name in (
        "self_reflection_id",
        "episode_id",
        "runtime_epoch",
    ):
        malformed_final_payload.pop(field_name)

    await coordinator.handle_text(
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**identity, "text": "已经公开。"},
        )
    )
    with pytest.raises(AssistantRequestIdentityConflict, match="self_reflection_id"):
        await coordinator.handle_text(
            RuntimeEvent(
                type="assistant.text_final",
                source="assistant_runtime",
                runtime_epoch=1,
                payload=malformed_final_payload,
            )
        )

    assert published == ["assistant.text_chunk"]
    assert journal._pending_admissions == {}  # type: ignore[attr-defined]

    await coordinator.handle_settlement(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            runtime_epoch=1,
            payload={**identity, "outcome": "completed"},
        )
    )
    assert journal._settled_request_ids == {"reflection-request"}  # type: ignore[attr-defined]
