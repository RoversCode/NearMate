from __future__ import annotations

import pytest

from companion.realtime.visible_output_journal import VisibleOutputJournal
from companion.realtime.runtime_identity import AssistantRequestIdentityConflict


def _metadata(*, policy: str = "stable_dialogue") -> dict[str, object]:
    """构造测试使用的稳定输出身份。"""

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


def test_journal_settles_only_successfully_recorded_chunks() -> None:
    """只有调用方确认广播成功后写入的 chunk 才能进入结算正文。"""

    journal = VisibleOutputJournal()
    first = {**_metadata(), "text": "已经公开。"}
    assert journal.record_public_text_event("assistant.text_chunk", first) is True

    settlement = journal.settle(
        {
            **_metadata(),
            "settlement_id": "settlement-a",
            "outcome": "output_unavailable",
        },
        settled_at=3.0,
        commit_settlement=lambda _settlement: None,
    )

    assert settlement is not None
    assert settlement.visible_text == "已经公开。"
    assert settlement.outcome == "output_unavailable"
    assert journal.settle(
        _metadata(),
        settled_at=4.0,
        commit_settlement=lambda _settlement: None,
    ) is None


def test_interrupted_final_uses_model_text_without_ui_suffix() -> None:
    """中断 final 可以展示尾注，但账本必须只保留模型真正输出的正文。"""

    journal = VisibleOutputJournal()
    final_payload = {
        **_metadata(policy="activity_progress"),
        "text": "我正在核对。【用户打断了此次的回复】",
        "settlement_text": "我正在核对。",
        "interrupted": True,
    }
    assert journal.record_public_text_event("assistant.text_final", final_payload) is True

    settlement = journal.settle(
        {
            **_metadata(policy="activity_progress"),
            "outcome": "interrupted",
        },
        settled_at=2.0,
        commit_settlement=lambda _settlement: None,
    )

    assert settlement is not None
    assert settlement.visible_text == "我正在核对。"
    assert settlement.history_policy == "activity_progress"


@pytest.mark.parametrize(
    "history_policy",
    ["stable_dialogue", "activity_progress", "unattended_episode"],
)
def test_history_policy_round_trips_through_visible_output_settlement(
    history_policy: str,
) -> None:
    """三种合法历史策略从正文登记到结算都不能被改写。

    Args:
        history_policy: 本轮要验证的合法历史策略。
    """

    journal = VisibleOutputJournal()
    metadata = {
        **_metadata(policy=history_policy),
        "activity_progress_references": [],
    }
    assert journal.record_public_text_event(
        "assistant.text_final",
        {**metadata, "text": "已经公开。"},
    )

    settlement = journal.settle(
        {**metadata, "outcome": "completed"},
        settled_at=2.0,
        commit_settlement=lambda _settlement: None,
    )

    assert settlement is not None
    assert settlement.history_policy == history_policy


def test_unattended_episode_rejects_stable_memory_commit() -> None:
    """Episode 输出不能携带稳定历史提交，避免同一正文写进两套账本。"""

    journal = VisibleOutputJournal()
    payload = {
        **_metadata(policy="unattended_episode"),
        "activity_progress_references": [],
        "text": "暂时没空回应。",
        "memory_commit": {
            "commit_id": "memory-episode-request-1",
            "request_id": "request-a",
            "base_memory_epoch": 0,
            "messages": [{"role": "assistant", "content": "暂时没空回应。"}],
        },
    }

    with pytest.raises(ValueError, match="不能携带稳定 MemoryCommit"):
        journal.record_public_text_event("assistant.text_final", payload)


def test_committed_stable_final_does_not_need_fallback_history() -> None:
    """正常 MemoryCommit 已覆盖正文时，settlement 不应再重复补写同一回复。"""

    journal = VisibleOutputJournal()
    assert journal.record_public_text_event(
        "assistant.text_final",
        {**_metadata(), "text": "完整回复。"},
    )
    assert journal.mark_stable_history_committed("request-a") is True

    settlement = journal.settle(
        {**_metadata(), "outcome": "completed"},
        settled_at=2.0,
        commit_settlement=lambda _settlement: None,
    )

    assert settlement is not None
    assert settlement.visible_text == ""


def test_same_request_cannot_mix_generation_identity() -> None:
    """迟到 generation 不得把正文混入同 request_id 的当前记录。"""

    journal = VisibleOutputJournal()
    assert journal.record_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), "text": "当前正文"},
    )

    with pytest.raises(AssistantRequestIdentityConflict, match="generation_id"):
        journal.record_public_text_event(
            "assistant.text_chunk",
            {
                **_metadata(),
                "generation_id": "generation-old",
                "text": "迟到正文",
            },
        )


def test_same_request_cannot_mix_archive_identity() -> None:
    """同一请求的历史策略和 Progress 引用被改写时必须 fail closed。"""

    journal = VisibleOutputJournal()
    assert journal.record_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), "text": "当前正文"},
    )

    with pytest.raises(AssistantRequestIdentityConflict, match="history_policy"):
        journal.record_public_text_event(
            "assistant.text_chunk",
            {
                **_metadata(policy="activity_progress"),
                "text": "错误归档正文",
            },
        )


def test_self_reflection_identity_conflict_is_rejected_before_admission() -> None:
    """Self-Reflection final 身份错误时，公共广播前就必须拒绝。"""

    journal = VisibleOutputJournal()
    reflection_identity = {
        "self_reflection_id": "reflection-a",
        "episode_id": "episode-a",
        "runtime_epoch": 1,
    }
    assert journal.record_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), **reflection_identity, "text": "已经公开"},
    )

    with pytest.raises(AssistantRequestIdentityConflict, match="self_reflection_id"):
        journal.prepare_public_text_event(
            "assistant.text_final",
            {**_metadata(), "text": "错误 final"},
        )

    assert journal._pending_admissions == {}  # type: ignore[attr-defined]


def test_self_reflection_settlement_requires_the_recorded_identity() -> None:
    """Self-Reflection settlement 也不能删除已经记录的反思身份。"""

    journal = VisibleOutputJournal()
    reflection_identity = {
        "self_reflection_id": "reflection-a",
        "episode_id": "episode-a",
        "runtime_epoch": 1,
    }
    assert journal.record_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), **reflection_identity, "text": "已经公开"},
    )

    with pytest.raises(AssistantRequestIdentityConflict, match="self_reflection_id"):
        journal.validate_settlement({**_metadata(), "outcome": "completed"})


def test_settled_self_reflection_tombstone_rejects_changed_identity() -> None:
    """永久墓碑必须继续拒绝冒用同一 request_id 的迟到反思正文。"""

    journal = VisibleOutputJournal()
    reflection_identity = {
        "self_reflection_id": "reflection-a",
        "episode_id": "episode-a",
        "runtime_epoch": 1,
    }
    assert journal.record_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), **reflection_identity, "text": "已经公开"},
    )
    assert journal.settle(
        {**_metadata(), **reflection_identity, "outcome": "completed"},
        settled_at=2.0,
        commit_settlement=lambda _settlement: None,
    ) is not None

    with pytest.raises(AssistantRequestIdentityConflict, match="episode_id"):
        journal.prepare_public_text_event(
            "assistant.text_chunk",
            {
                **_metadata(),
                **reflection_identity,
                "episode_id": "episode-b",
                "text": "迟到正文",
            },
        )


def test_same_request_cannot_mix_cognitive_result_derivation() -> None:
    """同一请求不能在输出中途改写 Cognitive 成果来源。"""

    journal = VisibleOutputJournal()
    assert journal.record_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), "text": "当前正文"},
    )

    with pytest.raises(
        AssistantRequestIdentityConflict,
        match="derived_from_activity_ids",
    ):
        journal.record_public_text_event(
            "assistant.text_chunk",
            {
                **_metadata(),
                "derived_from_activity_ids": ["cognitive-result-b"],
                "text": "错误来源正文",
            },
        )


def test_settled_request_is_rejected_before_public_text_commit() -> None:
    """结算后的迟到正文必须在广播前被 Journal 门禁吸收。"""

    journal = VisibleOutputJournal()
    assert journal.record_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), "text": "已经公开"},
    )
    assert journal.settle(
        {**_metadata(), "outcome": "completed"},
        settled_at=2.0,
        commit_settlement=lambda _settlement: None,
    ) is not None

    admission = journal.prepare_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), "text": "迟到正文"},
    )

    assert admission is None

    with pytest.raises(AssistantRequestIdentityConflict, match="activity_progress_references"):
        journal.record_public_text_event(
            "assistant.text_chunk",
            {
                **_metadata(),
                "activity_progress_references": [
                    {
                        "activity_id": "activity-b",
                        "source": "cognitive",
                        "notice_sequence": 2,
                    }
                ],
                "text": "错误进度正文",
            },
        )


def test_failed_public_text_commit_consumes_one_time_admission_token() -> None:
    """正文 admission 即使最终无效，也不能在 Journal 中留下悬挂 token。"""

    journal = VisibleOutputJournal()
    admission = journal.prepare_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), "text": ""},
    )

    assert admission is not None
    assert journal.commit_public_text_event(admission) is False
    assert journal._pending_admissions == {}  # type: ignore[attr-defined]


def test_transient_commit_failure_preserves_admission_for_retry(monkeypatch) -> None:
    """Journal 内部临时异常不能消费已经成功广播正文的唯一 admission。"""

    journal = VisibleOutputJournal()
    admission = journal.prepare_public_text_event(
        "assistant.text_chunk",
        {**_metadata(), "text": "已经公开。"},
    )
    assert admission is not None
    original_record = journal._record_public_text_event_locked
    commit_attempts = 0

    def fail_once(event_type: str, payload: dict[str, object]) -> bool:
        """首轮抛出临时异常，第二轮恢复真实写入。"""

        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise RuntimeError("temporary journal failure")
        return original_record(event_type, payload)

    monkeypatch.setattr(journal, "_record_public_text_event_locked", fail_once)

    with pytest.raises(RuntimeError, match="temporary journal failure"):
        journal.commit_public_text_event(admission)

    assert admission.token in journal._pending_admissions
    assert journal.commit_public_text_event(admission) is True


def test_settlement_cannot_overtake_pending_public_text_admission() -> None:
    """永久墓碑必须等待已经通过预检的公开正文完成提交。"""

    journal = VisibleOutputJournal()
    admission = journal.prepare_public_text_event(
        "assistant.text_final",
        {**_metadata(), "text": "已经公开。"},
    )
    assert admission is not None

    with pytest.raises(RuntimeError, match="visible_output_commit_pending"):
        journal.settle(
            {**_metadata(), "outcome": "completed"},
            settled_at=2.0,
            commit_settlement=lambda _settlement: None,
        )

    assert journal.commit_public_text_event(admission) is True
    settlement = journal.settle(
        {**_metadata(), "outcome": "completed"},
        settled_at=2.0,
        commit_settlement=lambda _settlement: None,
    )
    assert settlement is not None
    assert settlement.visible_text == "已经公开。"
