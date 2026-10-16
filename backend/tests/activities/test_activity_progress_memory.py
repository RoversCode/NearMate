from __future__ import annotations

from typing import Any, cast

from companion.activities.foreground_snapshot import ForegroundActivitySnapshot
from companion.activities.notice.delivery_coordinator import NoticeDeliveryCoordinator
from companion.activities.notice.models import (
    ActivityLifecycleEvent,
    NoticeDeliveryCommand,
    NoticeDeliveryOutcome,
)
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.character_cards.models import CharacterCardDefinition
from companion.core.application.assistant_request_context_preparer import (
    AssistantRequestContextPreparer,
)
from companion.prompt.activity_state import compile_activity_context
from companion.realtime.assistant_output_contracts import (
    ActivityProgressReference,
)
from companion.session.context import SessionContext
from backend.tests.foreground_processor_factory import (
    build_foreground_processor_for_test,
)


class RecordingProgressAcknowledger:
    """记录 foreground 对冻结进度的序号确认。"""

    def __init__(self, *, accepts_notice: bool = True) -> None:
        self._accepts_notice = accepts_notice
        self.acknowledged: list[tuple[str, int]] = []

    def acknowledge_progress_notice(self, activity_id: str, sequence: int) -> bool:
        """记录一次活动进度确认，并返回账本是否仍接受该序号。"""

        self.acknowledged.append((activity_id, sequence))
        return self._accepts_notice


class ActivitySnapshotProvider:
    """为 foreground 提供固定活动快照。"""

    def __init__(self, snapshot: ForegroundActivitySnapshot) -> None:
        self._snapshot = snapshot

    def activity_snapshots(
        self,
        interaction_session_id: str,
    ) -> tuple[ForegroundActivitySnapshot, ...]:
        """返回当前交互会话的固定活动快照集合。

        Args:
            interaction_session_id: 当前交互会话编号。

        Returns:
            只包含测试预置活动的快照集合。
        """

        _ = interaction_session_id
        return (self._snapshot,)


class FailIfCalledClient:
    """确保过期系统提醒不会进入模型调用。"""

    def stream_message(self, **_: Any) -> list[object]:
        """在错误调用模型时立即终止测试。

        Args:
            **_: 本测试不使用的模型参数。

        Returns:
            此方法不应正常返回。

        Raises:
            AssertionError: foreground 未在模型调用前拦截空提醒。
        """

        raise AssertionError("过期系统提醒不应调用 foreground 模型")


class CapturingPromptBuilder:
    """保存 ForegroundTurnProcessor 交给提示词构建器的完整输入。"""

    def __init__(self) -> None:
        self.payload: Any = None

    def build(self, payload: Any) -> list[dict[str, object]]:
        """记录本轮构建参数并返回空消息列表。

        Args:
            payload: ForegroundTurnProcessor 已整理好的提示词输入。

        Returns:
            本测试不需要真实提示词，因此固定返回空列表。
        """

        self.payload = payload
        return []


def test_progress_report_ledger_supports_mixed_batch_and_per_activity_cleanup() -> None:
    """混合批次的一条回复应支持逐活动解除关联。"""

    ledger = ActivityProgressLedger()
    ledger.record_visible_progress(
        visible_text="天气还在查，材料也正在整理。",
        references=(
            ActivityProgressReference("cognitive-a", "cognitive", 1),
            ActivityProgressReference("task_result-a", "background_agent", 2),
        ),
    )

    assert ledger.reports_by_activity(
        {
            ("cognitive", "cognitive-a"),
            ("background_agent", "task_result-a"),
            ("cognitive", "missing"),
        }
    ) == {
        ("cognitive", "cognitive-a"): ("天气还在查，材料也正在整理。",),
        ("background_agent", "task_result-a"): ("天气还在查，材料也正在整理。",),
    }

    assert ledger.freeze(source="cognitive", activity_id="cognitive-a") is True
    assert ledger.release_frozen((("cognitive", "cognitive-a"),)) == 1
    assert ledger.reports_by_activity({("cognitive", "cognitive-a")}) == {}
    assert ledger.freeze(
        source="background_agent",
        activity_id="task_result-a",
    ) is True
    assert ledger.release_frozen((("background_agent", "task_result-a"),)) == 1
    assert ledger.snapshot() == ()


def test_session_change_preserves_running_activity_progress_ledger() -> None:
    """交互会话切换不能抹掉仍在运行任务的真实进度播报。"""

    context = SessionContext(token_counter=len)
    context.ensure_session("session-a")
    ledger = ActivityProgressLedger()
    ledger.record_visible_progress(
        visible_text="仍在处理。",
        references=(ActivityProgressReference("task_result-a", "background_agent", 1),),
    )

    context.ensure_session("session-b")

    assert ledger.reports_by_activity({("background_agent", "task_result-a")}) == {
        ("background_agent", "task_result-a"): ("仍在处理。",)
    }


def test_notice_delivery_wrapper_releases_frozen_progress_only_after_final_disposition() -> None:
    """取消重排必须保留 frozen Progress，完成或永久丢弃才允许真正删除。"""

    final_outcomes: tuple[NoticeDeliveryOutcome, ...] = (
        "llm_output_completed",
        "output_unavailable",
    )
    for final_outcome in final_outcomes:
        ledger = ActivityProgressLedger()
        ledger.record_visible_progress(
            visible_text="已经公开过的任务进度。",
            references=(
                ActivityProgressReference("cognitive-a", "cognitive", 1),
            ),
        )
        ledger.freeze(source="cognitive", activity_id="cognitive-a")
        coordinator = NoticeDeliveryCoordinator()
        coordinator.apply_event(
            ActivityLifecycleEvent(
                lifecycle="terminal",
                activity_id="cognitive-a",
                interaction_session_id="session-a",
                source="cognitive",
                category="事实查询",
                goal="核对事实",
                intent="cognitive_result",
                result_context="核对已经结束。",
                terminal_state="completed",
            ),
            runtime_epoch=1,
        )
        preparer = AssistantRequestContextPreparer(
            hub=cast(Any, None),
            realtime_input_runtime_owner=cast(Any, None),
            notice_coordinator=coordinator,
            activity_progress_ledger=ledger,
        )

        coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="notice-a",
        )
        preparer.apply_notice_delivery(
            NoticeDeliveryCommand(
                request_id="notice-a",
                outcome="delivery_cancelled",
            )
        )
        assert ledger.snapshot()[0].lifecycle == "frozen"

        coordinator.build_notice_context(
            "session-a",
            mode="system_notice",
            request_id="notice-b",
        )
        preparer.apply_notice_delivery(
            NoticeDeliveryCommand(
                request_id="notice-b",
                outcome=final_outcome,
            )
        )
        assert ledger.snapshot() == ()


def test_activity_prompt_compiler_keeps_only_sequence_matched_progress() -> None:
    """弱提醒必须与本地快照的活动、来源、序号和冻结正文同时匹配。"""

    snapshots = [
        ForegroundActivitySnapshot(
            activity_id="cognitive-a",
            interaction_session_id="session-a",
            source="cognitive",
            category="实时信息查询",
            goal="查询天气",
            progress_reasoning_text="已经找到气象站数据",
            progress_sequence=2,
        ),
        ForegroundActivitySnapshot(
            activity_id="task_result-a",
            interaction_session_id="session-a",
            source="background_agent",
            category="材料处理",
            goal="总结资料",
            progress_reasoning_text="",
            progress_sequence=1,
        ),
    ]
    compiled = compile_activity_context(
        {
            "notice_batch": [
                {
                    "activity_id": "cognitive-a",
                    "source": "cognitive",
                    "intent": "progress_report",
                    "sequence": 2,
                },
                {
                    "activity_id": "task_result-a",
                    "source": "background_agent",
                    "intent": "progress_report",
                    "sequence": 1,
                },
                {
                    "activity_id": "task_result-finished",
                    "source": "background_agent",
                    "intent": "work_result",
                },
            ]
        },
        snapshots,
        previous_reports_by_activity={
            ("cognitive", "cognitive-a"): ("之前已经说过正在查天气。",),
            ("background_agent", "task_result-finished"): ("之前已经说过正在整理材料。",),
        },
    )

    assert [notice["intent"] for notice in compiled["notice_batch"]] == [
        "progress_report",
        "work_result",
    ]
    assert [activity["activity_id"] for activity in compiled["active_activities"]] == [
        "cognitive-a",
        "task_result-a",
    ]
    assert compiled["active_activities"][0]["previous_reports"] == [
        "之前已经说过正在查天气。"
    ]
    assert compiled["notice_batch"][1]["previous_reports"] == [
        "之前已经说过正在整理材料。"
    ]


def test_activity_prompt_compiler_drops_stale_sequence() -> None:
    """旧 request 中的序号失配提醒不得让 foreground 编造进度。"""

    snapshot = ForegroundActivitySnapshot(
        activity_id="cognitive-a",
        interaction_session_id="session-a",
        source="cognitive",
        category="实时信息查询",
        goal="查询天气",
        progress_reasoning_text="更新后的进展",
        progress_sequence=3,
    )

    compiled = compile_activity_context(
        {
            "notice_batch": [
                {
                    "activity_id": "cognitive-a",
                    "source": "cognitive",
                    "intent": "progress_report",
                    "sequence": 2,
                }
            ]
        },
        [snapshot],
        previous_reports_by_activity={},
    )

    assert compiled["notice_batch"] == []


def test_activity_prompt_compiler_seeds_empty_activity_list_without_snapshots() -> None:
    """没有执行器快照时仍由编译器提供稳定的空 Activity 列表。"""

    compiled = compile_activity_context(
        {
            "notice_batch": [
                {
                    "activity_id": "cognitive-a",
                    "source": "cognitive",
                    "intent": "cognitive_result",
                    "result_context": "完成结果",
                }
            ]
        },
        [],
        previous_reports_by_activity={},
    )

    assert compiled["active_activities"] == []
    assert compiled["notice_batch"][0]["activity_id"] == "cognitive-a"


def test_terminal_prompt_keeps_stable_history_and_task_progress_without_running_snapshot() -> None:
    """纯终态汇报应同时看到稳定历史和已经实际播报过的任务进度。"""

    context = SessionContext(token_counter=len)
    context.rolling_summary = "用户此前让我核查一项实时事实。"
    context.record_user_message("帮我核查这个结论。")
    context.record_assistant_message("好，我认真查一下。")
    prompt_builder = CapturingPromptBuilder()
    agent = build_foreground_processor_for_test()
    agent._session_context = context
    agent._prompt_builder = prompt_builder
    agent._cognitive_activity_executor = None

    execution_context = agent._build_execution_context(
        frozen_foreground_context={
            "activity_progress_reports": [
                {
                    "activity_id": "cognitive-a",
                    "source": "cognitive",
                    "lifecycle": "frozen",
                    "reports": ["我已经查到一部分资料，还在交叉确认。"],
                }
            ],
            "notice_batch": [
                {
                    "activity_id": "cognitive-a",
                    "source": "cognitive",
                    "intent": "terminal_status",
                    "terminal_state": "failed",
                    "status_context": "外部数据源暂时不可用。",
                }
            ]
        },
        interaction_session_id="session-a",
        request_mode="system_notice",
        foreground_text_available=False,
    )
    foreground_context = execution_context.prompt_environment
    agent._build_prompt_messages(
        current_user_text="请汇报任务结果。",
        current_user_message=None,
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        streamvox_model="",
        current_time="2026-07-16 12:00",
        foreground_context=foreground_context,
        request_mode="system_notice",
        available_tool_names=frozenset(),
        current_text_materials=[],
    )

    assert prompt_builder.payload.session_summary == ""
    assert [message.content for message in prompt_builder.payload.recent_messages] == [
        "帮我核查这个结论。",
        "好，我认真查一下。",
    ]
    assert foreground_context["notice_batch"][0]["previous_reports"] == [
        "我已经查到一部分资料，还在交叉确认。"
    ]


def test_foreground_skips_model_when_system_notice_expires_before_prompt_build() -> None:
    """系统提醒在子进程现场校验时过期，应以失败终态收束且不调用模型。"""

    agent = build_foreground_processor_for_test()
    agent._session_context = SessionContext(token_counter=len)
    agent._foreground_tools = None
    agent._client = FailIfCalledClient()
    agent._cognitive_activity_executor = ActivitySnapshotProvider(
        ForegroundActivitySnapshot(
            activity_id="cognitive-a",
            interaction_session_id="session-a",
            source="cognitive",
            category="实时信息查询",
            goal="查询天气",
            progress_reasoning_text="更新后的进展",
            progress_sequence=3,
        )
    )

    events = list(
        agent.stream_user_text(
            "请主动汇报当前任务。",
            now=0.0,
            interaction_session_id="session-a",
            character_card=cast(CharacterCardDefinition, None),
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            foreground_context={
                "notice_batch": [
                    {
                        "activity_id": "cognitive-a",
                        "source": "cognitive",
                        "intent": "progress_report",
                        "sequence": 2,
                    }
                ]
            },
            request_mode="system_notice",
        )
    )

    assert len(events) == 1
    assert events[0].type == "assistant.text_final"
    assert events[0].payload == {"text": "", "notice_delivery_failed": True}


def test_foreground_acknowledges_progress_only_after_main_process_confirmation() -> None:
    """子进程只消费主进程回执，不再拥有或修改 Progress 正文账本。"""

    progress_acknowledger = RecordingProgressAcknowledger()
    agent = build_foreground_processor_for_test()
    agent._cognitive_activity_executor = progress_acknowledger

    acknowledged_count = agent.acknowledge_activity_progress(
        references=(
            ActivityProgressReference("cognitive-a", "cognitive", 2),
        ),
    )

    assert acknowledged_count == 1
    assert progress_acknowledger.acknowledged == [("cognitive-a", 2)]


def test_mixed_terminal_batch_records_one_visible_reply() -> None:
    """跨来源终态事实由主进程任务记忆保存，Processor 只记录可见回复。"""

    context = SessionContext(token_counter=len)
    agent = build_foreground_processor_for_test()
    agent._session_context = context
    agent._cognitive_activity_executor = None
    events = agent._finish_visible_text_stream(
        visible_parts=["查询失败了，不过材料已经整理完成。"],
        foreground_context={
            "notice_batch": [
                {
                    "source": "cognitive",
                    "intent": "terminal_status",
                    "terminal_state": "failed",
                    "status_context": "不应写入历史的失败底稿",
                },
                {
                    "source": "background_agent",
                    "intent": "work_result",
                },
            ]
        },
        request_mode="system_notice",
        request_id="",
    )

    commit_action = events[-1].commit_action
    assert commit_action is not None
    assert commit_action.accept() is True
    assert commit_action.commit_enqueued() is True

    history = context.recent_messages()
    assert [message.role for message in history] == ["assistant"]
    assert history[0].content == "查询失败了，不过材料已经整理完成。"
    assert "不应写入历史的失败底稿" not in "".join(
        message.content for message in history
    )
