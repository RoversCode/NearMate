from __future__ import annotations

import pytest

from companion.activities.progress_ledger import ActivityProgressLedger
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_output_settlement_service import (
    AssistantOutputSettlementService,
)
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.session.context import SessionContext


def _identity(*, policy: str = "stable_dialogue") -> dict[str, object]:
    """构造一组完整且稳定的助手输出身份。"""

    return {
        "request_id": "request-a",
        "generation_id": "generation-a",
        "history_policy": policy,
        "activity_progress_references": [
            {
                "activity_id": "activity-a",
                "source": "cognitive",
                "notice_sequence": 2,
            }
        ],
        "derived_from_activity_ids": ["cognitive-result-a"],
    }

def _record_broadcast_text(
    service: AssistantOutputSettlementService,
    event: RuntimeEvent,
) -> bool:
    """测试中显式执行广播后的 Prepare/Commit 端口。"""

    admission = service.prepare_broadcast_text(event)
    if admission is None:
        return False
    return service.commit_broadcast_text(event, admission)


def test_stable_memory_commit_is_applied_only_after_public_text_is_recorded() -> None:
    """主进程只有在调用方确认广播成功后才可以写稳定回复。"""

    memory_store = RuntimeMemoryStore()
    service = AssistantOutputSettlementService(runtime_memory_store=memory_store)
    event = RuntimeEvent(
        type="assistant.text_final",
        source="assistant_runtime",
        payload={
            **_identity(),
            "text": "已经公开的完整回复。",
            "memory_commit": {
                "commit_id": "commit-a",
                "request_id": "request-a",
                "base_memory_epoch": 0,
                "messages": [
                    {"role": "assistant", "content": "已经公开的完整回复。"}
                ],
            },
        },
    )

    assert memory_store.snapshot().recent_messages == ()
    assert _record_broadcast_text(service, event) is True
    assert [
        message.content for message in memory_store.snapshot().recent_messages
    ] == ["已经公开的完整回复。"]

    service.settle(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            payload={**_identity(), "outcome": "completed"},
        )
    )
    assert len(memory_store.snapshot().recent_messages) == 1


def test_progress_settlement_records_temporary_ledger_and_acknowledges_source() -> None:
    """Progress 正文同时进入运行期记忆和临时账本，再确认来源进度。"""

    memory_store = RuntimeMemoryStore()
    progress_ledger = ActivityProgressLedger()
    acknowledgements: list[object] = []
    service = AssistantOutputSettlementService(
        runtime_memory_store=memory_store,
        activity_progress_ledger=progress_ledger,
        progress_acknowledgement_handler=lambda command: (
            acknowledgements.append(command) or True
        ),
    )
    final_event = RuntimeEvent(
        type="assistant.text_final",
        source="assistant_runtime",
        payload={
            **_identity(policy="activity_progress"),
            "text": "我已经核对完第一批资料。",
        },
    )

    assert _record_broadcast_text(service, final_event) is True
    service.settle(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            payload={
                **_identity(policy="activity_progress"),
                "outcome": "completed",
            },
        )
    )

    memory_messages = memory_store.snapshot().recent_messages
    assert len(memory_messages) == 1
    assert memory_messages[0].content == (
        "<proactive_broadcast>\n"
        "  <![CDATA[我已经核对完第一批资料。]]>\n"
        "</proactive_broadcast>"
    )
    assert memory_messages[0].metadata["proactive_broadcast"] is True
    child_context = SessionContext()
    child_context.load_runtime_snapshot(
        memory_store.snapshot(),
        interaction_session_id="session-a",
    )
    assert child_context.recent_messages()[0].to_llm_message()["content"] == (
        memory_messages[0].content
    )
    assert child_context.visible_dialogue_messages()[0].content == (
        memory_messages[0].content
    )
    assert progress_ledger.reports_by_activity({("cognitive", "activity-a")}) == {
        ("cognitive", "activity-a"): ("我已经核对完第一批资料。",)
    }
    assert len(acknowledgements) == 1
    assert acknowledgements[0].request_id == "request-a"


def test_settlement_commits_successfully_broadcast_chunks_when_final_is_missing() -> None:
    """子进程在 final 前退出时，已广播 chunk 仍必须成为稳定 assistant 历史。"""

    memory_store = RuntimeMemoryStore()
    service = AssistantOutputSettlementService(runtime_memory_store=memory_store)
    _record_broadcast_text(
        service,
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            payload={**_identity(), "text": "只公开了这一部分。"},
        )
    )

    service.settle(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_process",
            payload={**_identity(), "outcome": "output_unavailable"},
        )
    )

    messages = memory_store.snapshot().recent_messages
    assert [message.content for message in messages] == ["只公开了这一部分。"]
    assert messages[0].metadata["output_outcome"] == "output_unavailable"
    assert messages[0].metadata["derived_from_activity_ids"] == [
        "cognitive-result-a"
    ]


def test_turn_release_runs_after_visible_output_has_been_archived() -> None:
    """下一轮释放回调观察到的必须是已经完成归档的主进程事实。"""

    memory_store = RuntimeMemoryStore()
    observed_message_counts: list[int] = []
    service = AssistantOutputSettlementService(
        runtime_memory_store=memory_store,
        output_settled_handler=lambda _settlement: observed_message_counts.append(
            len(memory_store.snapshot().recent_messages)
        ),
    )
    _record_broadcast_text(
        service,
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            payload={**_identity(), "text": "已经公开。"},
        )
    )

    service.settle(
        RuntimeEvent(
            type="assistant.output_settled",
            source="assistant_runtime",
            payload={**_identity(), "outcome": "interrupted"},
        )
    )

    assert observed_message_counts == [1]


def test_archive_failure_keeps_settlement_retriable_and_does_not_release_turn(
    monkeypatch,
) -> None:
    """归档暂时失败时不能吞掉 settlement，也不能提前释放下一轮。"""

    memory_store = RuntimeMemoryStore()
    original_apply_memory_commit = memory_store.apply_memory_commit
    apply_attempt_count = 0
    released_request_ids: list[str] = []

    def fail_first_apply(value: object) -> tuple:
        """第一次模拟主进程归档失败，之后恢复真实写入。

        Args:
            value: 待应用的稳定记忆提交。

        Returns:
            真实运行期记忆写入结果。
        """

        nonlocal apply_attempt_count
        apply_attempt_count += 1
        if apply_attempt_count == 1:
            raise RuntimeError("temporary archive failure")
        return original_apply_memory_commit(value)

    monkeypatch.setattr(memory_store, "apply_memory_commit", fail_first_apply)
    service = AssistantOutputSettlementService(
        runtime_memory_store=memory_store,
        output_settled_handler=lambda settlement: released_request_ids.append(
            settlement.request_id
        ),
    )
    _record_broadcast_text(
        service,
        RuntimeEvent(
            type="assistant.text_chunk",
            source="assistant_runtime",
            payload={**_identity(), "text": "已经公开。"},
        )
    )
    settlement_event = RuntimeEvent(
        type="assistant.output_settled",
        source="assistant_runtime",
        payload={**_identity(), "outcome": "interrupted"},
    )

    with pytest.raises(RuntimeError, match="temporary archive failure"):
        service.settle(settlement_event)

    assert released_request_ids == []
    assert memory_store.snapshot().recent_messages == ()

    settlement = service.settle(settlement_event)

    assert settlement is not None
    assert released_request_ids == ["request-a"]
    assert [
        message.content for message in memory_store.snapshot().recent_messages
    ] == ["已经公开。"]


def test_turn_release_retry_does_not_duplicate_archived_progress() -> None:
    """Progress 已归档但 Turn 放行失败时，重试不得重复追加同一份公开正文。

    Returns:
        无。
    """

    progress_ledger = ActivityProgressLedger()
    release_attempt_count = 0

    def fail_first_release(_settlement: object) -> None:
        """第一次模拟 Turn 放行失败，第二次正常返回。

        Args:
            _settlement: 已完成正文归档的结算事实。

        Returns:
            无。

        Raises:
            RuntimeError: 第一次调用时模拟调度边界异常。
        """

        nonlocal release_attempt_count
        release_attempt_count += 1
        if release_attempt_count == 1:
            raise RuntimeError("temporary turn release failure")

    service = AssistantOutputSettlementService(
        activity_progress_ledger=progress_ledger,
        output_settled_handler=fail_first_release,
    )
    _record_broadcast_text(
        service,
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            payload={
                **_identity(policy="activity_progress"),
                "text": "我已经核对完第一批资料。",
            },
        )
    )
    settlement_event = RuntimeEvent(
        type="assistant.output_settled",
        source="assistant_runtime",
        payload={
            **_identity(policy="activity_progress"),
            "outcome": "completed",
        },
    )

    with pytest.raises(RuntimeError, match="temporary turn release failure"):
        service.settle(settlement_event)

    assert service.settle(settlement_event) is not None
    assert release_attempt_count == 2
    assert progress_ledger.reports_by_activity({("cognitive", "activity-a")}) == {
        ("cognitive", "activity-a"): ("我已经核对完第一批资料。",)
    }


def test_progress_acknowledgement_retries_without_duplicate_archive() -> None:
    """来源回执未进入控制队列时，只重试回执，不重复写记忆和临时账本。"""

    memory_store = RuntimeMemoryStore()
    progress_ledger = ActivityProgressLedger()
    acknowledgement_attempt_count = 0
    released_request_ids: list[str] = []

    def reject_first_acknowledgement(_command: object) -> bool:
        """第一次拒绝回执入队，第二次模拟控制队列恢复。

        Args:
            _command: 待送回助手子进程的 Progress 回执。

        Returns:
            第二次及后续调用返回真。
        """

        nonlocal acknowledgement_attempt_count
        acknowledgement_attempt_count += 1
        return acknowledgement_attempt_count > 1

    service = AssistantOutputSettlementService(
        runtime_memory_store=memory_store,
        activity_progress_ledger=progress_ledger,
        progress_acknowledgement_handler=reject_first_acknowledgement,
        output_settled_handler=lambda settlement: released_request_ids.append(
            settlement.request_id
        ),
    )
    _record_broadcast_text(
        service,
        RuntimeEvent(
            type="assistant.text_final",
            source="assistant_runtime",
            payload={
                **_identity(policy="activity_progress"),
                "text": "我已经核对完第一批资料。",
            },
        ),
    )
    settlement_event = RuntimeEvent(
        type="assistant.output_settled",
        source="assistant_runtime",
        payload={
            **_identity(policy="activity_progress"),
            "outcome": "completed",
        },
    )

    with pytest.raises(
        RuntimeError,
        match="activity_progress_acknowledgement_rejected",
    ):
        service.settle(settlement_event)

    assert released_request_ids == []
    assert len(memory_store.snapshot().recent_messages) == 1
    assert progress_ledger.reports_by_activity({("cognitive", "activity-a")}) == {
        ("cognitive", "activity-a"): ("我已经核对完第一批资料。",)
    }

    assert service.settle(settlement_event) is not None
    assert acknowledgement_attempt_count == 2
    assert released_request_ids == ["request-a"]
    assert len(memory_store.snapshot().recent_messages) == 1
    assert progress_ledger.reports_by_activity({("cognitive", "activity-a")}) == {
        ("cognitive", "activity-a"): ("我已经核对完第一批资料。",)
    }
