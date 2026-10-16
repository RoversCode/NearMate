from __future__ import annotations

from types import SimpleNamespace
from datetime import date, datetime

from companion.runtime_memory.models import MemoryCommit
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.runtime_memory.token_accounting import protocol_message_token_text
from companion.realtime.text_tokenizer import count_text_tokens
from companion.session.context import SessionContext


def test_runtime_memory_survives_interaction_session_changes() -> None:
    """交互会话切换只能改请求归属，不能清空主进程记忆。"""

    store = RuntimeMemoryStore()
    store.commit_request(
        SimpleNamespace(
            request_id="request-a",
            source="text_input",
            user_text="记住这句话",
            foreground_context={},
        )
    )
    child_context = SessionContext()
    child_context.load_runtime_snapshot(
        store.snapshot(),
        interaction_session_id="session-a",
    )
    child_context.ensure_session("session-b")

    assert [message.content for message in child_context.recent_messages()] == [
        "记住这句话"
    ]


def test_task_recall_has_exact_unicode_character_boundary() -> None:
    """召回最多返回五十万 Unicode 字符，并明确说明是否截断。"""

    store = RuntimeMemoryStore()
    full_result = "结" * 500_001
    store.record_task_terminal(
        "cognitive_task.completed",
        {
            "activity_id": "activity-long",
            "brief": "生成超长结果",
            "summary": "结果很长。",
            "result_text": full_result,
        },
    )

    result = store.recall_task("activity-long", purpose="foreground_recall")

    assert result["state"] == "completed"
    assert result["original_character_count"] == 500_001
    assert result["returned_character_count"] == 500_000
    assert result["truncated"] is True
    assert result["result_text"] == "结" * 500_000


def test_task_memory_preserves_activity_correction_causality() -> None:
    """旧任务说明纠正取消，新任务说明替代关系并保留自己的结果摘要。"""

    store = RuntimeMemoryStore()
    store.observe_tool_event(
        {
            "activity_id": "activity-b",
            "tool_call_id": "call-correct-b",
            "result": {
                "based_on_activity_id": "activity-a",
                "based_on_relationship": "activity_correction",
            },
        }
    )
    store.record_task_terminal(
        "background_agent.cancelled",
        {
            "activity_id": "activity-a",
            "goal": "按错误方向整理报告",
            "cancellation_reason": "user_correction",
        },
    )
    store.record_task_terminal(
        "background_agent.completed",
        {
            "activity_id": "activity-b",
            "goal": "按纠正后的方向整理报告",
            "delivery_brief": "新版报告已经生成。",
            "display_content": "新版报告完整正文",
        },
    )

    cancelled = store.recall_task("activity-a", purpose="foreground_recall")
    replacement = store.recall_task("activity-b", purpose="foreground_recall")

    assert cancelled["terminal_state"] == "cancelled"
    assert cancelled["cancellation_reason"] == "user_correction"
    assert cancelled["cancellation_meaning"] == (
        "用户纠正了这项任务的目标，它因此被停止；接替它的新任务以本任务为父任务。"
    )
    assert cancelled["outcome_brief"] == "任务已取消，没有完成。"
    assert replacement["terminal_state"] == "completed"
    assert replacement["based_on_activity_id"] == "activity-a"
    assert replacement["based_on_relationship"] == "activity_correction"
    assert replacement["based_on_meaning"] == (
        "本任务是对父任务的纠正替代：用户纠正了父任务的目标，父任务因此停止，由本任务接替。"
    )
    assert replacement["outcome_brief"] == "新版报告已经生成。"


def test_plain_activity_cancellation_does_not_invent_correction_relation() -> None:
    """普通停止只保存真实取消原因，不能伪造不存在的替代任务。"""

    store = RuntimeMemoryStore()
    store.record_task_terminal(
        "cognitive_task.cancelled",
        {
            "activity_id": "activity-cancelled",
            "brief": "停止核查",
            "cancellation_reason": "user_cancelled",
        },
    )

    result = store.recall_task(
        "activity-cancelled",
        purpose="foreground_recall",
    )

    assert result["cancellation_reason"] == "user_cancelled"
    assert result["cancellation_meaning"] == "用户明确要求停止这项任务。"
    assert result["based_on_activity_id"] is None
    assert result["based_on_relationship"] is None
    assert result["based_on_meaning"] is None


def test_validated_compaction_deletes_covered_task_record() -> None:
    """摘要覆盖任务短记忆和最后引用后，主进程彻底删除任务。"""

    store = RuntimeMemoryStore()
    store.record_task_terminal(
        "cognitive_task.completed",
        {
            "activity_id": "activity-old",
            "brief": "旧任务",
            "summary": "旧任务已经完成。",
            "result_text": "完整旧结果",
            "task_input_snapshot": {
                "encoded_images": ["data:image/png;base64,old-image"]
            },
        },
    )
    _append_assistant_messages(store, count=10)
    plan = store.create_compaction_plan(
        owner_runtime_epoch=3,
        max_context_tokens=1,
        keep_recent_messages=8,
    )

    assert plan is not None
    assert store.apply_compaction(
        memory_epoch=plan.memory_epoch,
        plan_id=plan.plan_id,
        source_message_id=plan.source_message_id,
        owner_runtime_epoch=3,
        summary="用户此前完成过一项旧任务。",
    ) is True
    assert store.task_record("activity-old") is None
    assert store.recall_task(
        "activity-old", purpose="foreground_recall"
    )["state"] == "not_available"


def test_failed_or_mismatched_compaction_keeps_task_detail() -> None:
    """压缩身份不匹配或调用失败时不能释放任何任务详情。"""

    store = RuntimeMemoryStore()
    store.record_task_terminal(
        "background_agent.failed",
        {
            "activity_id": "activity-failed",
            "goal": "转换文件",
            "error_summary": "输入文件缺失。",
        },
    )
    _append_assistant_messages(store, count=10)
    plan = store.create_compaction_plan(
        owner_runtime_epoch=4,
        max_context_tokens=1,
        keep_recent_messages=8,
    )

    assert plan is not None
    assert store.apply_compaction(
        memory_epoch=plan.memory_epoch,
        plan_id="wrong-plan",
        source_message_id=plan.source_message_id,
        owner_runtime_epoch=4,
        summary="错误摘要",
    ) is False
    record = store.task_record("activity-failed")
    assert record is not None
    assert store.release_compaction_plan(plan_id=plan.plan_id) is True
    assert store.recall_task(
        "activity-failed", purpose="foreground_recall"
    )["state"] == "completed"


def test_foreground_recall_and_derivation_both_omit_image_pixels() -> None:
    """图片编码只能服务内部派生，不能作为普通工具文本进入 Foreground。"""

    store = RuntimeMemoryStore()
    store.record_task_terminal(
        "cognitive_task.completed",
        {
            "activity_id": "activity-image",
            "brief": "识别图片",
            "summary": "识别到一张截图。",
            "result_text": "图片内容",
            "task_input_snapshot": {
                "source_urls": ["https://example.com/source"],
                "encoded_images": ["data:image/png;base64,secret-image"],
            },
        },
    )

    foreground_result = store.recall_task(
        "activity-image",
        purpose="foreground_recall",
    )
    derivation_result = store.recall_task(
        "activity-image",
        purpose="task_derivation",
    )

    assert foreground_result["input_snapshot"] == {
        "source_urls": ["https://example.com/source"],
        "encoded_image_count": 1,
    }
    assert derivation_result["input_snapshot"] == {
        "source_urls": ["https://example.com/source"],
        "encoded_image_count": 1,
    }


def test_lookup_during_compaction_protects_task_detail() -> None:
    """压缩计划运行期间出现新引用时，父任务详情不能被旧计划提前释放。"""

    store = RuntimeMemoryStore()
    store.record_task_terminal(
        "cognitive_task.completed",
        {
            "activity_id": "activity-referenced",
            "brief": "旧任务",
            "summary": "旧任务已经完成。",
            "result_text": "仍需保留的完整结果",
        },
    )
    _append_assistant_messages(store, count=10)
    plan = store.create_compaction_plan(
        owner_runtime_epoch=5,
        max_context_tokens=1,
        keep_recent_messages=8,
    )

    assert plan is not None
    assert store.recall_task(
        "activity-referenced",
        purpose="foreground_recall",
    )["state"] == "completed"
    assert store.apply_compaction(
        memory_epoch=plan.memory_epoch,
        plan_id=plan.plan_id,
        source_message_id=plan.source_message_id,
        owner_runtime_epoch=5,
        summary="此前的旧任务已经完成。",
    ) is True
    record = store.task_record("activity-referenced")
    assert record is not None
    assert record.full_result == "仍需保留的完整结果"

    _append_assistant_messages(store, count=10, key_prefix="followup")
    next_plan = store.create_compaction_plan(
        owner_runtime_epoch=5,
        max_context_tokens=1,
        keep_recent_messages=8,
    )

    assert next_plan is not None
    assert store.apply_compaction(
        memory_epoch=next_plan.memory_epoch,
        plan_id=next_plan.plan_id,
        source_message_id=next_plan.source_message_id,
        owner_runtime_epoch=5,
        summary="用户后来又追问过这项旧任务。",
    ) is True
    assert store.task_record("activity-referenced") is None


def test_compaction_tracks_authoritative_dates_and_replaces_summary_tokens() -> None:
    """摘要日期来自消息时间，第二次压缩不能继续叠加旧摘要 Token。"""

    store = RuntimeMemoryStore()
    first_day = datetime.fromisoformat("2026-10-02T23:59:00+08:00")
    second_day = datetime.fromisoformat("2026-10-04T00:01:00+08:00")
    for index, occurred_at in enumerate((first_day, second_day, second_day, second_day)):
        store.apply_memory_commit(
            MemoryCommit(
                commit_id=f"dated-{index}",
                request_id=f"dated-request-{index}",
                base_memory_epoch=store.memory_epoch,
                messages=({
                    "role": "assistant",
                    "content": f"日期消息 {index}",
                    "created_at": occurred_at.isoformat(),
                    "metadata": {"origin_key": f"dated-origin-{index}"},
                },),
            )
        )

    plan = store.create_compaction_plan(
        owner_runtime_epoch=6,
        max_context_tokens=1,
        keep_recent_messages=1,
    )

    assert plan is not None
    assert plan.covered_from == date(2026, 10, 2)
    assert plan.covered_to == date(2026, 10, 4)
    assert store.apply_compaction(
        memory_epoch=plan.memory_epoch,
        plan_id=plan.plan_id,
        source_message_id=plan.source_message_id,
        owner_runtime_epoch=6,
        summary="第一份较长摘要",
    ) is True
    snapshot = store.snapshot()
    assert snapshot.summary_covered_from == date(2026, 10, 2)
    assert snapshot.summary_covered_to == date(2026, 10, 4)
    assert snapshot.context_token_count == (
        count_text_tokens("第一份较长摘要")
        + sum(message.token_count for message in snapshot.recent_messages)
    )

    _append_assistant_messages(store, count=10, key_prefix="second-summary")
    second_plan = store.create_compaction_plan(
        owner_runtime_epoch=6,
        max_context_tokens=1,
        keep_recent_messages=1,
    )
    assert second_plan is not None
    assert store.apply_compaction(
        memory_epoch=second_plan.memory_epoch,
        plan_id=second_plan.plan_id,
        source_message_id=second_plan.source_message_id,
        owner_runtime_epoch=6,
        summary="短摘要",
    ) is True
    second_snapshot = store.snapshot()
    assert second_snapshot.context_token_count == (
        count_text_tokens("短摘要")
        + sum(message.token_count for message in second_snapshot.recent_messages)
    )


def test_main_store_counts_complete_tool_protocol() -> None:
    """主进程必须和子进程一样统计角色、工具参数和 tool_call_id。"""

    store = RuntimeMemoryStore()
    tool_calls = ({
        "id": "call-token",
        "type": "function",
        "function": {
            "name": "recall_task_memory",
            "arguments": '{"activity_id":"activity-token"}',
        },
    },)
    store.apply_memory_commit(
        MemoryCommit(
            commit_id="token-commit",
            request_id="token-request",
            base_memory_epoch=0,
            messages=(
                {"role": "assistant", "content": "", "tool_calls": list(tool_calls)},
                {"role": "tool", "content": '{"state":"not_available"}', "tool_call_id": "call-token"},
            ),
        )
    )

    snapshot = store.snapshot()
    expected = count_text_tokens(protocol_message_token_text(
        role="assistant",
        content="",
        tool_calls=tool_calls,
    )) + count_text_tokens(protocol_message_token_text(
        role="tool",
        content='{ "state": "not_available" }'.replace(" ", ""),
        tool_call_id="call-token",
    ))
    assert snapshot.context_token_count == expected


def test_main_store_projects_background_paths_at_process_boundary() -> None:
    """主进程必须再次删除不可信 MemoryCommit 中的本地材料路径。"""

    store = RuntimeMemoryStore()
    store.apply_memory_commit(
        MemoryCommit(
            commit_id="path-projection-commit",
            request_id="path-projection-request",
            base_memory_epoch=0,
            messages=(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "background-path",
                        "type": "function",
                        "function": {
                            "name": "start_background_work",
                            "arguments": (
                                '{"goal":"修改应用","material_paths":'
                                '["D:\\\\project\\\\app.py"]}'
                            ),
                        },
                    }],
                },
                {
                    "role": "tool",
                    "content": '{"state":"accepted"}',
                    "tool_call_id": "background-path",
                },
            ),
        )
    )

    snapshot = store.snapshot()
    assistant_message = snapshot.recent_messages[0]

    assert assistant_message.tool_calls[0]["id"] == "background-path"
    assert assistant_message.tool_calls[0]["function"]["name"] == (
        "start_background_work"
    )
    assert assistant_message.tool_calls[0]["function"]["arguments"] == (
        '{"goal":"修改应用"}'
    )
    assert snapshot.recent_messages[1].tool_call_id == "background-path"
    assert "D:\\project\\app.py" not in str(snapshot.recent_messages)


def test_matching_invalid_compaction_releases_only_its_own_plan() -> None:
    """同一计划的损坏结果应释放槽位，错误计划编号不得影响当前计划。"""

    store = RuntimeMemoryStore()
    _append_assistant_messages(store, count=10)
    plan = store.create_compaction_plan(
        owner_runtime_epoch=7,
        max_context_tokens=1,
        keep_recent_messages=8,
    )
    assert plan is not None

    assert store.apply_compaction(
        memory_epoch=plan.memory_epoch + 1,
        plan_id="another-plan",
        source_message_id=plan.source_message_id,
        owner_runtime_epoch=7,
        summary="错误摘要",
    ) is False
    assert store.create_compaction_plan(
        owner_runtime_epoch=7,
        max_context_tokens=1,
        keep_recent_messages=8,
    ) is None

    assert store.apply_compaction(
        memory_epoch=plan.memory_epoch + 1,
        plan_id=plan.plan_id,
        source_message_id=plan.source_message_id,
        owner_runtime_epoch=7,
        summary="错误摘要",
    ) is False
    assert store.create_compaction_plan(
        owner_runtime_epoch=7,
        max_context_tokens=1,
        keep_recent_messages=8,
    ) is not None


def test_runtime_replacement_releases_only_owned_compaction_plan() -> None:
    """旧助手世代退出时只能释放自己接管的压缩计划。"""

    store = RuntimeMemoryStore()
    _append_assistant_messages(store, count=10)
    plan = store.create_compaction_plan(
        owner_runtime_epoch=8,
        max_context_tokens=1,
        keep_recent_messages=8,
    )
    assert plan is not None

    assert store.release_compaction_plan_for_runtime(9) is False
    assert store.release_compaction_plan_for_runtime(8) is True
    assert store.create_compaction_plan(
        owner_runtime_epoch=9,
        max_context_tokens=1,
        keep_recent_messages=8,
    ) is not None


def test_active_child_and_recent_lineage_keep_parent_until_both_are_covered() -> None:
    """活跃子任务和近期父子关系都必须阻止父任务提前失效。"""

    store = RuntimeMemoryStore()
    store.record_task_terminal(
        "cognitive_task.completed",
        {
            "activity_id": "parent-task",
            "brief": "父任务",
            "summary": "父任务完成。",
            "result_text": "父任务完整结果",
        },
    )
    store.observe_tool_event({
        "activity_id": "child-task",
        "tool_call_id": "child-call",
        "result": {"based_on_activity_id": "parent-task"},
    })
    _append_assistant_messages(store, count=10, key_prefix="active-child")
    first_plan = store.create_compaction_plan(
        owner_runtime_epoch=10,
        max_context_tokens=1,
        keep_recent_messages=8,
    )
    assert first_plan is not None
    assert store.apply_compaction(
        memory_epoch=first_plan.memory_epoch,
        plan_id=first_plan.plan_id,
        source_message_id=first_plan.source_message_id,
        owner_runtime_epoch=10,
        summary="父任务已经完成，随后开始了派生工作。",
    ) is True
    assert store.task_record("parent-task") is not None

    store.record_task_terminal(
        "background_agent.completed",
        {
            "activity_id": "child-task",
            "goal": "子任务",
            "delivery_brief": "子任务完成。",
            "display_content": "子任务完整结果",
            "based_on_activity_id": "parent-task",
        },
    )
    _append_assistant_messages(store, count=10, key_prefix="finished-child")
    second_plan = store.create_compaction_plan(
        owner_runtime_epoch=10,
        max_context_tokens=1,
        keep_recent_messages=8,
    )
    assert second_plan is not None
    assert store.apply_compaction(
        memory_epoch=second_plan.memory_epoch,
        plan_id=second_plan.plan_id,
        source_message_id=second_plan.source_message_id,
        owner_runtime_epoch=10,
        summary="父任务和派生任务都已经结束。",
    ) is True
    assert store.task_record("parent-task") is None


def test_runtime_snapshot_preserves_original_message_time() -> None:
    """子进程热替换后，旧消息不能被重新标成重连发生的当前时间。"""

    store = RuntimeMemoryStore()
    occurred_at = datetime.fromisoformat("2026-08-03T23:59:00+08:00")
    store.apply_memory_commit(
        MemoryCommit(
            commit_id="commit-time",
            request_id="request-time",
            base_memory_epoch=0,
            messages=(
                {
                    "role": "assistant",
                    "content": "跨天之前的回复",
                    "created_at": occurred_at.isoformat(),
                    "metadata": {"origin_key": "time-origin"},
                },
            ),
        )
    )

    snapshot = store.snapshot()
    child_context = SessionContext()
    child_context.load_runtime_snapshot(snapshot, interaction_session_id="session-new")

    assert snapshot.recent_messages[0].created_at == occurred_at
    assert child_context.recent_messages()[0].created_at == occurred_at


def _append_assistant_messages(
    store: RuntimeMemoryStore,
    *,
    count: int,
    key_prefix: str = "commit",
) -> None:
    """向主进程账本追加测试用 assistant 消息。"""

    for index in range(count):
        store.apply_memory_commit(
            MemoryCommit(
                commit_id=f"{key_prefix}-{index}",
                request_id=f"{key_prefix}-request-{index}",
                base_memory_epoch=store.memory_epoch,
                messages=(
                    {
                        "role": "assistant",
                        "content": f"第 {index} 条较长回复内容",
                        "metadata": {"origin_key": f"{key_prefix}-origin-{index}"},
                    },
                ),
            )
        )
