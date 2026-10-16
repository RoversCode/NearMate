from __future__ import annotations

from types import SimpleNamespace
from collections.abc import Callable

import pytest

from companion.activities.notice.delivery_coordinator import NoticeDeliveryCoordinator
from companion.activities.notice.activity_result_context import (
    ActivityNoticeResultContextProjector,
)
from companion.activities.runtime.adapters import adapt_activity_source_event
from companion.activities.runtime.models import ActivityCompletionResult
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.runtime_memory.store import RuntimeMemoryStore

from backend.tests.activities.notice_delivery_test_support import (
    build_assistant_request_context_preparer_for_test,
)


def test_system_notice_request_context_carries_only_notice_projection() -> None:
    """system notice 冻结阶段不得读取或携带全局环境与活动材料。"""

    service = build_assistant_request_context_preparer_for_test()
    service.realtime_input_runtime_owner = SimpleNamespace(
        current_interaction_state=lambda: SimpleNamespace(
            interaction_session_id="interaction-a"
        )
    )
    service.hub = SimpleNamespace(
        current_prompt_profile_payload=lambda: {
            "character_card": "card-a",
            "user_call_name": "用户",
            "user_self_intro": "",
            "user_extra_notes": "",
        },
        turn_materials=SimpleNamespace(
            active_context=lambda: pytest.fail(
                "system notice 不应读取当前活动材料"
            )
        ),
    )
    service.prepare_notice_context = lambda **_kwargs: {
        "notice_batch": [{"intent": "work_result", "goal": "汇报结果"}],
    }
    service._freeze_environment_snapshot = lambda **_kwargs: None
    service.runtime_memory_store = RuntimeMemoryStore()
    service.activity_progress_ledger = ActivityProgressLedger()
    service.build_foreground_context = lambda **_kwargs: pytest.fail(
        "system notice 不应进入完整动态上下文构建"
    )

    snapshot = service._freeze_request_context(
        reserve_notice_batch=True,
        request_id="request-a",
        request_source="system_notice",
    )

    assert snapshot.turn_material_context_id is None
    assert snapshot.environment_snapshot is None
    assert snapshot.foreground_context == {
        "notice_batch": [{"intent": "work_result", "goal": "汇报结果"}],
    }


def test_system_notice_projects_completed_activity_result_for_foreground() -> None:
    """主动提醒应按 Activity 详情读取成品，供 Foreground 概述实际内容。"""

    coordinator = NoticeDeliveryCoordinator()
    service = build_assistant_request_context_preparer_for_test()
    service.notice_coordinator = coordinator
    requested_activity_ids: list[str] = []

    def resolve_result(activity_id: str) -> ActivityCompletionResult:
        """记录结果读取并返回完成结果。"""

        requested_activity_ids.append(activity_id)
        return ActivityCompletionResult(
            display_content="正文讨论了效率、协作和管理三个方面。",
            delivery_brief="文章已经覆盖效率、协作和管理三个方面。",
            output_paths=(),
        )

    service.activity_notice_result_context = ActivityNoticeResultContextProjector(
        result_provider=resolve_result,
    )
    service.handle_activity_event(
        "background_agent.completed",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "goal": "写一篇关于 AI 与远程工作的文章",
            "display_content": "正文讨论了效率、协作和管理三个方面。",
        },
        1,
    )

    context = service.prepare_notice_context(
        interaction_session_id="session-a",
        reserve_batch=True,
        request_id="system-notice-a",
        request_source="system_notice",
    )

    assert requested_activity_ids == ["task_result-a"]
    assert context["notice_batch"][0]["result_context"] == (
        "文章已经覆盖效率、协作和管理三个方面。"
    )


def test_system_notice_projects_background_agent_delivery_brief() -> None:
    """后台任务完成提醒必须拿到真实成果摘要，不再只知道任务已完成。"""

    coordinator = NoticeDeliveryCoordinator()
    service = build_assistant_request_context_preparer_for_test()
    service.notice_coordinator = coordinator
    service.activity_notice_result_context = ActivityNoticeResultContextProjector(
        result_provider=lambda _activity_id: ActivityCompletionResult(
            display_content="完整调研报告正文",
            delivery_brief="调研已经归纳出三项可直接落地的产品改进。",
            output_paths=(),
        )
    )
    service.handle_activity_event(
        "background_agent.completed",
        {
            "activity_id": "agent-a",
            "interaction_session_id": "session-a",
            "goal": "调研参考项目",
            "display_content": "完整调研报告正文",
            "delivery_brief": "调研已经归纳出三项可直接落地的产品改进。",
        },
        1,
    )

    context = service.prepare_notice_context(
        interaction_session_id="session-a",
        reserve_batch=True,
        request_id="system-notice-agent-a",
        request_source="system_notice",
    )

    assert context["notice_batch"][0]["result_context"] == (
        "调研已经归纳出三项可直接落地的产品改进。"
    )


def _missing_activity_result(_activity_id: str) -> ActivityCompletionResult:
    """模拟 Activity 详情已不可读取。

    Args:
        _activity_id: 调用方请求读取的 Activity 编号。

    Raises:
        LookupError: 始终表示目标详情不存在。
    """

    raise LookupError("activity missing")


def _adapted_oversized_delivery_brief(
    _activity_id: str,
) -> ActivityCompletionResult:
    """让超长摘要先经过真实 Activity 适配器，再交给 Notice 投影器。"""

    change = adapt_activity_source_event(
        "background_agent.completed",
        {
            "activity_id": "00000000-0000-4000-8000-000000000021",
            "goal": "整理资料",
            "display_content": "不应回退读取的完整正文",
            "delivery_brief": "摘" * 2_001,
        },
        1,
    )
    assert change is not None and change.completion_result is not None
    assert len(change.completion_result.delivery_brief) == 2_001
    return change.completion_result


@pytest.mark.parametrize(
    "resolve_result",
    [
        pytest.param(_missing_activity_result, id="missing-detail"),
        pytest.param(
            lambda _activity_id: ActivityCompletionResult(
                display_content="不应回退读取的完整正文",
                delivery_brief="",
                output_paths=(),
            ),
            id="empty-delivery-brief",
        ),
        pytest.param(_adapted_oversized_delivery_brief, id="oversized-delivery-brief"),
    ],
)
def test_system_notice_falls_back_to_goal_when_task_result_result_cannot_be_projected(
    resolve_result: Callable[[str], ActivityCompletionResult],
) -> None:
    """详情缺失、摘要为空或超长时，提醒必须安全退回只表达任务目标。"""

    coordinator = NoticeDeliveryCoordinator()
    service = build_assistant_request_context_preparer_for_test()
    service.notice_coordinator = coordinator
    service.activity_notice_result_context = ActivityNoticeResultContextProjector(
        result_provider=resolve_result,
    )
    service.handle_activity_event(
        "background_agent.completed",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "goal": "写一篇关于 AI 与远程工作的文章",
            "display_content": "正文",
        },
        1,
    )

    context = service.prepare_notice_context(
        interaction_session_id="session-a",
        reserve_batch=True,
        request_id="system-notice-a",
        request_source="system_notice",
    )

    notice = context["notice_batch"][0]
    assert notice["goal"] == "写一篇关于 AI 与远程工作的文章"
    assert "result_context" not in notice


def test_system_notice_discards_forged_work_result_context_before_projection() -> None:
    """后台成果摘要必须来自权威结果提供器，不能相信上游预置字段。"""

    projector = ActivityNoticeResultContextProjector(
        result_provider=lambda _activity_id: ActivityCompletionResult(
            display_content="完整成果正文",
            delivery_brief="",
            output_paths=(),
        )
    )

    projected = projector.project(
        {
            "notice_batch": [
                {
                    "activity_id": "task_result-a",
                    "intent": "work_result",
                    "goal": "整理资料",
                    "result_context": "上游异常塞入的完整成果",
                }
            ]
        }
    )

    assert "result_context" not in projected["notice_batch"][0]


def test_user_request_task_result_notice_projects_only_delivery_brief() -> None:
    """附着用户请求的完成提醒应携带短摘要，但绝不能携带完整成果。"""

    coordinator = NoticeDeliveryCoordinator()
    service = build_assistant_request_context_preparer_for_test()
    service.notice_coordinator = coordinator
    service.activity_notice_result_context = ActivityNoticeResultContextProjector(
        result_provider=lambda _activity_id: ActivityCompletionResult(
            display_content="不应进入本轮 Prompt 的完整正文",
            delivery_brief="文章已经覆盖效率、协作和管理三个方面。",
            output_paths=("G:/private/result.md",),
        ),
    )
    service.handle_activity_event(
        "background_agent.completed",
        {
            "activity_id": "task_result-a",
            "interaction_session_id": "session-a",
            "goal": "写一篇关于 AI 与远程工作的文章",
            "display_content": "不应进入本轮 Prompt 的完整正文",
        },
        1,
    )

    context = service.prepare_notice_context(
        interaction_session_id="session-a",
        reserve_batch=True,
        request_id="user-request-a",
        request_source="text_input",
    )

    notice = context["notice_batch"][0]
    assert notice["goal"] == "写一篇关于 AI 与远程工作的文章"
    assert notice["result_context"] == "文章已经覆盖效率、协作和管理三个方面。"
    assert "完整正文" not in str(notice)
    assert "result.md" not in str(notice)


def test_system_notice_projects_result_card_fact_for_deliverable_task() -> None:
    """登记了结果卡的完成提醒必须携带 deliverable 事实，供前台指引聊天窗口。"""

    coordinator = NoticeDeliveryCoordinator()
    service = build_assistant_request_context_preparer_for_test()
    service.notice_coordinator = coordinator
    service.activity_notice_result_context = ActivityNoticeResultContextProjector(
        result_provider=lambda _activity_id: ActivityCompletionResult(
            display_content="完整调研报告正文",
            delivery_brief="调研已经归纳出三项可直接落地的产品改进。",
            output_paths=("G:/workspace/report.md",),
            deliverable=True,
        )
    )
    service.handle_activity_event(
        "background_agent.completed",
        {
            "activity_id": "agent-a",
            "interaction_session_id": "session-a",
            "goal": "调研参考项目",
            "display_content": "完整调研报告正文",
            "delivery_brief": "调研已经归纳出三项可直接落地的产品改进。",
            "deliverable": True,
        },
        1,
    )

    context = service.prepare_notice_context(
        interaction_session_id="session-a",
        reserve_batch=True,
        request_id="system-notice-agent-a",
        request_source="system_notice",
    )

    notice = context["notice_batch"][0]
    assert notice["deliverable"] is True
    assert notice["result_context"] == "调研已经归纳出三项可直接落地的产品改进。"


def test_system_notice_omits_result_card_fact_for_non_deliverable_task() -> None:
    """未登记结果卡的完成提醒不得携带 deliverable 字段，前台据此保持交付静默。"""

    projector = ActivityNoticeResultContextProjector(
        result_provider=lambda _activity_id: ActivityCompletionResult(
            display_content="执行概况正文",
            delivery_brief="磁盘占用已经看完。",
            output_paths=(),
            deliverable=False,
        )
    )

    projected = projector.project(
        {
            "notice_batch": [
                {
                    "activity_id": "task_result-a",
                    "intent": "work_result",
                    "goal": "检查磁盘占用",
                }
            ]
        }
    )

    notice = projected["notice_batch"][0]
    assert notice["result_context"] == "磁盘占用已经看完。"
    assert "deliverable" not in notice


def test_system_notice_discards_forged_result_card_fact_before_projection() -> None:
    """上游伪造的 deliverable 标记必须在投影前清除，不能指引不存在的入口。"""

    projector = ActivityNoticeResultContextProjector(
        result_provider=lambda _activity_id: ActivityCompletionResult(
            display_content="完整成果正文",
            delivery_brief="",
            output_paths=(),
            deliverable=False,
        )
    )

    projected = projector.project(
        {
            "notice_batch": [
                {
                    "activity_id": "task_result-a",
                    "intent": "work_result",
                    "goal": "整理资料",
                    "deliverable": True,
                }
            ]
        }
    )

    assert "deliverable" not in projected["notice_batch"][0]
