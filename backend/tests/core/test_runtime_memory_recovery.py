from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from types import SimpleNamespace

import pytest

from companion.runtime_memory import store as runtime_memory_store_module
from companion.runtime_memory.models import MemoryCommit, TaskRecoveryOutcome
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.runtime_memory.summary_prompt import (
    build_runtime_memory_summary_prompt,
    format_runtime_messages_for_summary,
)
from companion.runtime_memory.unattended_episode import (
    UnattendedEpisodeIdentity,
    UnattendedInputItem,
    UnattendedInterruptionReply,
    UnattendedMaterialReference,
)
from companion.session.context import SessionContext


_OPENED_AT = datetime.fromisoformat("2026-08-14T10:00:00+08:00")
_CLOSED_AT = datetime.fromisoformat("2026-08-14T10:01:00+08:00")


def test_unattended_episode_orders_mixed_inputs_and_deduplicates_retries() -> None:
    """Text 与 Final ASR 按权威序号投影，重复 append/close 不得重复历史。"""

    store = RuntimeMemoryStore()
    identity = _episode_identity()
    later_text = _episode_input(
        input_identity="text-submission-1",
        source="text",
        text="换成 b.wav",
        episode_sequence=2,
        materials=(
            UnattendedMaterialReference(
                file_name="b.wav",
                media_type="audio/wav",
            ),
        ),
    )
    earlier_asr = _episode_input(
        input_identity="asr-turn-1",
        source="final_asr",
        text="算了",
        episode_sequence=1,
    )

    assert store.open_unattended_input_episode(
        identity,
        opened_at=_OPENED_AT,
    ) is True
    assert store.open_unattended_input_episode(
        identity,
        opened_at=_OPENED_AT,
    ) is False
    assert store.append_unattended_input(identity, later_text) == "appended"
    assert store.append_unattended_input(identity, earlier_asr) == "appended"
    assert store.append_unattended_input(identity, earlier_asr) == "duplicate"
    episode = store.close_unattended_input_episode(
        identity,
        closed_at=_CLOSED_AT,
    )
    epoch_after_first_close = store.memory_epoch
    duplicate_close = store.close_unattended_input_episode(
        identity,
        closed_at=datetime.fromisoformat("2026-08-14T10:02:00+08:00"),
    )

    assert duplicate_close == episode
    assert store.memory_epoch == epoch_after_first_close
    assert [item.input_identity for item in episode.inputs] == [
        "asr-turn-1",
        "text-submission-1",
    ]
    assert episode.actionable is False
    assert store.append_unattended_input(identity, earlier_asr) == "duplicate"
    snapshot = store.snapshot()
    assert [message.role for message in snapshot.recent_messages] == [
        "user",
        "user",
        "assistant",
    ]
    assert snapshot.recent_messages[0].content == "算了"
    assert "换成 b.wav" in snapshot.recent_messages[1].content
    assert "b.wav（audio/wav）" in snapshot.recent_messages[1].content
    assert "不是待办" in snapshot.recent_messages[2].content
    assert "不会因为这段历史自动执行" in snapshot.recent_messages[2].content
    assert all(
        message.metadata["actionable"] is False
        for message in snapshot.recent_messages
    )


def test_unattended_episode_projects_inputs_and_reply_settlements_in_one_order() -> None:
    """输入和打扰回应按同一 episode 序号提交，重复回应不能生成第二份历史。"""

    store = RuntimeMemoryStore()
    identity = _episode_identity()
    first_input = _episode_input(
        input_identity="asr-first",
        source="final_asr",
        text="你听见了吗",
        episode_sequence=1,
    )
    reply = UnattendedInterruptionReply(
        request_id="interruption-reply-1",
        generation_id="generation-reply-1",
        trigger_input_identity="asr-first",
        content="我听到了，我还在重新梳理。",
        settlement="completed",
        episode_sequence=2,
        settled_at=datetime.fromisoformat("2026-08-14T10:00:20+08:00"),
    )
    second_input = _episode_input(
        input_identity="text-second",
        source="text",
        text="那你慢慢想",
        episode_sequence=3,
    )

    store.open_unattended_input_episode(identity, opened_at=_OPENED_AT)
    assert store.append_unattended_input(identity, first_input) == "appended"
    assert store.append_unattended_interruption_reply(identity, reply) == "appended"
    assert store.append_unattended_interruption_reply(identity, reply) == "duplicate"
    assert store.append_unattended_input(identity, second_input) == "appended"
    episode = store.close_unattended_input_episode(identity, closed_at=_CLOSED_AT)

    assert [item.episode_sequence for item in episode.ordered_items] == [1, 2, 3]
    messages = store.snapshot().recent_messages
    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [message.content for message in messages[:3]] == [
        "你听见了吗",
        "我听到了，我还在重新梳理。",
        "那你慢慢想",
    ]
    assert messages[1].metadata["reply_settlement"] == "completed"


def test_episode_close_is_retryable_when_history_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史构造失败时不能先关闭 ledger 或留下半块稳定聊天。"""

    store = RuntimeMemoryStore()
    identity = _episode_identity()
    store.open_unattended_input_episode(identity, opened_at=_OPENED_AT)
    store.append_unattended_input(
        identity,
        _episode_input(
            input_identity="retryable-input",
            source="text",
            text="这条输入必须原子提交",
            episode_sequence=1,
        ),
    )

    original_count_text_tokens = runtime_memory_store_module.count_text_tokens
    invocation_count = 0

    def fail_on_second_history_item(text: str) -> int:
        """让第一条历史已经构造后，第二条构造稳定失败。"""

        nonlocal invocation_count
        invocation_count += 1
        if invocation_count == 2:
            raise RuntimeError("injected history construction failure")
        return original_count_text_tokens(text)

    monkeypatch.setattr(
        runtime_memory_store_module,
        "count_text_tokens",
        fail_on_second_history_item,
    )
    with pytest.raises(RuntimeError, match="injected history construction failure"):
        store.close_unattended_input_episode(identity, closed_at=_CLOSED_AT)

    assert store.snapshot().recent_messages == ()

    monkeypatch.setattr(
        runtime_memory_store_module,
        "count_text_tokens",
        original_count_text_tokens,
    )
    episode = store.close_unattended_input_episode(identity, closed_at=_CLOSED_AT)
    messages = store.snapshot().recent_messages

    assert episode.identity == identity
    assert len(messages) == 2
    assert messages[0].content == "这条输入必须原子提交"
    assert messages[1].metadata["episode_role"] == "closure"


def test_duplicate_input_identity_cannot_change_content_or_sequence() -> None:
    """同一输入身份重试只能完全相同，不能悄悄改写用户原话。"""

    store = RuntimeMemoryStore()
    identity = _episode_identity()
    original = _episode_input(
        input_identity="text-stable",
        source="text",
        text="第一份原话",
        episode_sequence=1,
    )
    store.open_unattended_input_episode(identity, opened_at=_OPENED_AT)
    assert store.append_unattended_input(identity, original) == "appended"

    with pytest.raises(ValueError, match="不能改写正文或顺序"):
        store.append_unattended_input(
            identity,
            _episode_input(
                input_identity="text-stable",
                source="text",
                text="被篡改的原话",
                episode_sequence=2,
            ),
        )


def test_close_and_input_race_assigns_input_to_exactly_one_history_lane() -> None:
    """关闭与新输入并发时，正文只能进入 episode 或下一普通 Turn。"""

    store = RuntimeMemoryStore()
    identity = _episode_identity()
    raced_input = _episode_input(
        input_identity="raced-input",
        source="text",
        text="竞态中的唯一正文",
        episode_sequence=1,
    )
    store.open_unattended_input_episode(identity, opened_at=_OPENED_AT)
    barrier = Barrier(2)

    def close_episode() -> object:
        """与输入线程同时尝试取得 Store 的线性化锁。"""

        barrier.wait()
        return store.close_unattended_input_episode(
            identity,
            closed_at=_CLOSED_AT,
        )

    def append_input() -> str:
        """与关闭线程同时尝试把正文归入未处理片段。"""

        barrier.wait()
        return store.append_unattended_input(identity, raced_input)

    with ThreadPoolExecutor(max_workers=2) as executor:
        close_future = executor.submit(close_episode)
        append_future = executor.submit(append_input)
        episode = close_future.result()
        append_status = append_future.result()

    if append_status == "appended":
        assert len(episode.inputs) == 1
        ordinary_message = store.commit_request(_ordinary_request("raced-input"))
        assert ordinary_message is None
    else:
        assert append_status == "closed"
        assert len(episode.inputs) == 0
        ordinary_message = store.commit_request(_ordinary_request("raced-input"))
        assert ordinary_message is not None

    assert sum(
        message.content.count("竞态中的唯一正文")
        for message in store.snapshot().recent_messages
    ) == 1


def test_unattended_projection_has_same_non_actionable_meaning_everywhere() -> None:
    """普通 Prompt、可见对话和 Summary 都必须看到同一条非待办语义。"""

    store = RuntimeMemoryStore()
    identity = _episode_identity()
    store.open_unattended_input_episode(identity, opened_at=_OPENED_AT)
    store.append_unattended_input(
        identity,
        _episode_input(
            input_identity="asr-memory",
            source="final_asr",
            text="把刚才那个文件删掉",
            episode_sequence=1,
        ),
    )
    store.close_unattended_input_episode(identity, closed_at=_CLOSED_AT)
    snapshot = store.snapshot()

    context = SessionContext()
    context.load_runtime_snapshot(snapshot, interaction_session_id="session-next")
    visible_dialogue = context.visible_dialogue_messages()
    summary_lines = format_runtime_messages_for_summary(snapshot.recent_messages)
    summary_prompt = build_runtime_memory_summary_prompt(
        snapshot.recent_messages,
        covered_from=_OPENED_AT.date(),
        covered_to=_CLOSED_AT.date(),
    )

    assert [message.role for message in visible_dialogue] == ["user", "assistant"]
    assert "不是待办" in visible_dialogue[-1].content
    assert "不可自动执行" in "\n".join(summary_lines)
    assert "当时没有处理、不是待办、不会自动执行" in summary_prompt


def test_task_recovery_atomically_updates_both_records_and_is_idempotent() -> None:
    """恢复 outcome 与双向 Activity 引用只提交一次，并能从两端一致召回。"""

    store = RuntimeMemoryStore()
    _record_failed_cognitive(store, "cognitive-source")
    outcome = _started_recovery_outcome("reflection-1")

    assert store.retain_recovery_compaction_pin("reflection-1") is True
    source_record = store.commit_task_recovery(
        source_activity_id="cognitive-source",
        outcome=outcome,
        recovered_by_activity_id="background-recovery",
    )
    epoch_after_commit = store.memory_epoch
    duplicate = store.commit_task_recovery(
        source_activity_id="cognitive-source",
        outcome=outcome,
        recovered_by_activity_id="background-recovery",
    )
    store.record_task_terminal(
        "background_agent.completed",
        {
            "activity_id": "background-recovery",
            "goal": "真正转换文件",
            "delivery_brief": "文件已经转换完成。",
            "display_content": "转换后的文件已经生成。",
            "recovery_of_activity_id": "cognitive-source",
        },
    )

    assert duplicate == source_record
    assert store.memory_epoch == epoch_after_commit + 1
    source_recall = store.recall_task(
        "cognitive-source",
        purpose="foreground_recall",
    )
    recovered_recall = store.recall_task(
        "background-recovery",
        purpose="foreground_recall",
    )
    assert source_recall["recovered_by_activity_id"] == "background-recovery"
    assert source_recall["recovery_outcome"] == outcome.to_payload()
    assert recovered_recall["recovery_of_activity_id"] == "cognitive-source"
    assert store.task_record(
        "background-recovery"
    ).recovery_of_activity_id == "cognitive-source"
    recovery_messages = [
        message
        for message in store.snapshot().recent_messages
        if message.metadata.get("task_recovery") is True
    ]
    assert len(recovery_messages) == 1
    assert "新的恢复任务已经在后台开始处理" in recovery_messages[0].content


def test_recovery_rejects_missing_pin_and_illegal_references() -> None:
    """不存在、自引用、错误来源和冲突恢复关系都必须在写历史前失败。"""

    store = RuntimeMemoryStore()
    _record_failed_cognitive(store, "source-a")
    _record_failed_cognitive(store, "source-b")
    outcome_a = _started_recovery_outcome("reflection-a")

    with pytest.raises(ValueError, match="必须持有对应 compaction pin"):
        store.commit_task_recovery(
            source_activity_id="source-a",
            outcome=outcome_a,
            recovered_by_activity_id="recovery-task",
        )
    assert store.retain_recovery_compaction_pin("reflection-a") is True
    with pytest.raises(ValueError, match="任务不能恢复自己"):
        store.commit_task_recovery(
            source_activity_id="source-a",
            outcome=outcome_a,
            recovered_by_activity_id="source-a",
        )
    store.commit_task_recovery(
        source_activity_id="source-a",
        outcome=outcome_a,
        recovered_by_activity_id="recovery-task",
    )

    with pytest.raises(ValueError, match="指向不存在"):
        store.record_task_terminal(
            "background_agent.failed",
            {
                "activity_id": "orphan-recovery",
                "goal": "孤立恢复",
                "recovery_of_activity_id": "missing-source",
            },
        )
    assert store.retain_recovery_compaction_pin("reflection-b") is True
    with pytest.raises(ValueError, match="已经恢复其它失败任务"):
        store.commit_task_recovery(
            source_activity_id="source-b",
            outcome=_started_recovery_outcome("reflection-b"),
            recovered_by_activity_id="recovery-task",
        )


def test_recovery_pin_blocks_new_and_existing_compaction_plans() -> None:
    """短时恢复 pin 既阻止新计划，也拒绝 pin 之前冻结的旧摘要。"""

    store = RuntimeMemoryStore()
    _append_assistant_messages(store, count=12)
    existing_plan = store.create_compaction_plan(
        owner_runtime_epoch=1,
        max_context_tokens=1,
        keep_recent_messages=4,
    )
    assert existing_plan is not None
    assert store.retain_recovery_compaction_pin("reflection-pin") is True

    assert store.apply_compaction(
        memory_epoch=existing_plan.memory_epoch,
        plan_id=existing_plan.plan_id,
        source_message_id=existing_plan.source_message_id,
        owner_runtime_epoch=1,
        summary="不应写入的半状态摘要",
    ) is False
    assert store.create_compaction_plan(
        owner_runtime_epoch=1,
        max_context_tokens=1,
        keep_recent_messages=4,
    ) is None
    assert store.release_recovery_compaction_pin("reflection-pin") is True
    assert store.create_compaction_plan(
        owner_runtime_epoch=1,
        max_context_tokens=1,
        keep_recent_messages=4,
    ) is not None


def _episode_identity() -> UnattendedEpisodeIdentity:
    """构造每项测试独占的固定 episode 身份。"""

    return UnattendedEpisodeIdentity(
        self_reflection_id="reflection-episode",
        runtime_epoch=7,
        episode_id="episode-1",
    )


def _episode_input(
    *,
    input_identity: str,
    source: str,
    text: str,
    episode_sequence: int,
    materials: tuple[UnattendedMaterialReference, ...] = (),
) -> UnattendedInputItem:
    """构造带稳定时间的未处理输入。"""

    return UnattendedInputItem(
        input_identity=input_identity,
        source=source,  # type: ignore[arg-type]
        text=text,
        episode_sequence=episode_sequence,
        accepted_at=_OPENED_AT,
        materials=materials,
    )


def _ordinary_request(request_id: str) -> SimpleNamespace:
    """构造 close 线性化点之后的普通用户 Turn。"""

    return SimpleNamespace(
        request_id=request_id,
        source="text_input",
        user_text="竞态中的唯一正文",
        foreground_context={},
    )


def _record_failed_cognitive(store: RuntimeMemoryStore, activity_id: str) -> None:
    """为恢复关系测试写入一项真实失败的 Cognitive Task Memory。"""

    store.record_task_terminal(
        "cognitive_task.failed",
        {
            "activity_id": activity_id,
            "brief": "转换音频文件",
            "summary": "原处理方式没有完成文件转换。",
            "error_summary": "这项任务需要文件操作和成果交付。",
        },
    )


def _started_recovery_outcome(self_reflection_id: str) -> TaskRecoveryOutcome:
    """构造一项已由恢复通道真实启动的恢复结果。"""

    return TaskRecoveryOutcome(
        self_reflection_id=self_reflection_id,
        kind="activity_started",
        safe_summary="新的恢复任务已经在后台开始处理，原失败任务没有被改写为成功。",
        tool_name="start_background_work",
        tool_status="started",
    )


def _append_assistant_messages(
    store: RuntimeMemoryStore,
    *,
    count: int,
) -> None:
    """追加足以触发压缩计划的测试消息。"""

    for index in range(count):
        store.apply_memory_commit(
            MemoryCommit(
                commit_id=f"recovery-compaction-{index}",
                request_id=f"recovery-request-{index}",
                base_memory_epoch=store.memory_epoch,
                messages=(
                    {
                        "role": "assistant",
                        "content": f"用于压缩的较长消息 {index}",
                        "metadata": {
                            "origin_key": f"recovery-origin-{index}",
                        },
                    },
                ),
            )
        )
