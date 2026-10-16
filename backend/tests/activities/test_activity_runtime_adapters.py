from __future__ import annotations

from uuid import UUID

import pytest

from companion.activities.runtime.adapters import adapt_activity_source_event
from companion.activities.runtime.failure_reporting import failure_from_reason


def _uuid(number: int) -> str:
    return str(UUID(int=number, version=4))


def _failure_payload(stage_code: str = "processing") -> dict[str, object]:
    return failure_from_reason(
        "empty_model_output", stage_code=stage_code
    ).to_payload()


def test_background_agent_adapter_exposes_only_public_activity_fields() -> None:
    change = adapt_activity_source_event(
        "background_agent.completed",
        {
            "activity_id": _uuid(1),
            "goal": "整理资料",
            "display_content": "公开结果",
            "delivery_brief": "资料已整理。",
            "reasoning": "不能泄漏",
            "token_count": 123,
        },
        1,
    )

    assert change is not None
    assert change.kind == "background_agent"
    assert change.type_label == "后台工作"
    assert change.completion_result is not None
    assert change.completion_result.display_content == "公开结果"
    assert not hasattr(change, "reasoning")


def test_cognitive_adapter_maps_product_label_and_ignores_progress() -> None:
    activity_id = _uuid(2)
    started = adapt_activity_source_event(
        "cognitive_task.started",
        {
            "activity_id": activity_id,
            "kind": "analyze_deeply",
            "brief": "比较两个方案",
        },
        1,
    )

    assert started is not None
    assert started.kind == "cognitive"
    assert started.type_label == "深度分析"
    assert (
        adapt_activity_source_event(
            "cognitive_task.progress",
            {"activity_id": activity_id, "notice_seq": 1},
            1,
        )
        is None
    )


def test_adapter_rejects_non_uuid_identity_and_limits_cognitive_result() -> None:
    assert (
        adapt_activity_source_event(
            "background_agent.started",
            {"activity_id": "legacy-id", "goal": "任务"},
            1,
        )
        is None
    )

    change = adapt_activity_source_event(
        "cognitive_task.completed",
        {
            "activity_id": _uuid(3),
            "kind": "lookup_current_fact",
            "brief": "目标",
            "result_text": "析" * 21000,
            "summary": "核心结论。",
        },
        1,
    )
    assert change is not None
    assert change.completion_result is not None
    assert len(change.completion_result.display_content) == 20000
    assert change.completion_result.display_content.endswith("…")
    assert change.completion_result.delivery_brief == "核心结论。"


def test_adapter_accepts_uuid_v7_and_rejects_non_rfc_variant() -> None:
    """Activity 标识接受 RFC v1-v8，但拒绝没有标准版本或变体的形状。"""

    uuid_v7 = "01890f3e-7cc2-7d00-8abc-1234567890ab"
    change = adapt_activity_source_event(
        "background_agent.started",
        {"activity_id": uuid_v7, "goal": "整理资料"},
        1,
    )

    assert change is not None
    assert change.activity_id == uuid_v7
    assert (
        adapt_activity_source_event(
            "background_agent.started",
            {
                "activity_id": "01890f3e-7cc2-7d00-0abc-1234567890ab",
                "goal": "整理资料",
            },
            1,
        )
        is None
    )
    assert (
        adapt_activity_source_event(
            "background_agent.started",
            {"activity_id": str(UUID(int=0)), "goal": "整理资料"},
            1,
        )
        is None
    )


def test_adapter_preserves_full_background_result_but_limits_other_public_text() -> None:
    """后台工作目标和详情保留全文，认知结果继续遵守公共长度边界。"""

    started = adapt_activity_source_event(
        "background_agent.started",
        {"activity_id": _uuid(5), "goal": "目" * 3000},
        1,
    )
    completed = adapt_activity_source_event(
        "background_agent.completed",
        {
            "activity_id": _uuid(6),
            "goal": "目标",
            "display_content": "结" * 21000,
            "delivery_brief": "结果已完成。",
        },
        1,
    )

    assert started is not None
    assert completed is not None
    assert len(started.goal) == 2000
    assert started.goal.endswith("…")
    assert completed.completion_result is not None
    assert completed.completion_result.display_content == "结" * 21000


def test_background_agent_stage_changed_event_carries_validated_stage() -> None:
    change = adapt_activity_source_event(
        "background_agent.stage_changed",
        {"activity_id": _uuid(10), "stage": "processing"},
        1,
    )

    assert change is not None
    assert change.change == "stage_changed"
    assert change.kind == "background_agent"
    assert change.stage == "processing"


def test_cognitive_stage_changed_event_carries_validated_stage() -> None:
    change = adapt_activity_source_event(
        "cognitive_task.stage_changed",
        {"activity_id": _uuid(11), "kind": "analyze_deeply", "stage": "generating_result"},
        1,
    )

    assert change is not None
    assert change.change == "stage_changed"
    assert change.kind == "cognitive"
    assert change.type_label == "深度分析"
    assert change.stage == "generating_result"


def test_stage_changed_rejects_illegal_stage_code() -> None:
    with pytest.raises(ValueError):
        adapt_activity_source_event(
            "background_agent.stage_changed",
            {"activity_id": _uuid(12), "stage": "reasoning"},
            1,
        )
    with pytest.raises(ValueError):
        adapt_activity_source_event(
            "cognitive_task.stage_changed",
            {"activity_id": _uuid(13), "stage": ""},
            1,
        )


def test_failed_events_require_valid_public_failure() -> None:
    background_agent = adapt_activity_source_event(
        "background_agent.failed",
        {"activity_id": _uuid(14), "goal": "整理资料", "failure": _failure_payload()},
        1,
    )
    cognitive = adapt_activity_source_event(
        "cognitive_task.failed",
        {
            "activity_id": _uuid(15),
            "kind": "lookup_current_fact",
            "brief": "目标",
            "failure": _failure_payload("generating_result"),
        },
        1,
    )

    assert background_agent is not None and background_agent.failure is not None
    assert background_agent.failure.code == "invalid_provider_response"
    assert cognitive is not None and cognitive.failure is not None
    assert cognitive.failure.stage_code == "generating_result"


def test_cognitive_business_failure_keeps_only_safe_specific_reason() -> None:
    """Cognitive 自主失败原因经过同一安全门禁后才可进入 Activity 详情。"""

    activity_id = _uuid(16)
    failure = failure_from_reason(
        "cognitive_task_failed",
        stage_code="generating_result",
    ).to_payload()

    change = adapt_activity_source_event(
        "cognitive_task.failed",
        {
            "activity_id": activity_id,
            "kind": "lookup_current_fact",
            "brief": "核对最新事实",
            "failure_reason": "必要网页来源无法读取。",
            "failure": failure,
        },
        1,
    )

    assert change is not None
    assert change.failure_reason == "必要网页来源无法读取。"

    for unsafe_reason in (
        "Traceback (most recent call last): 任务失败",
        "api_key=secret-value",
        r"请检查 C:\\Users\\name\\secret.txt",
    ):
        with pytest.raises(ValueError):
            adapt_activity_source_event(
                "cognitive_task.failed",
                {
                    "activity_id": activity_id,
                    "kind": "lookup_current_fact",
                    "brief": "核对最新事实",
                    "failure_reason": unsafe_reason,
                    "failure": failure,
                },
                1,
            )


def test_non_business_cognitive_failure_does_not_publish_specific_reason() -> None:
    """协议与基础设施失败继续只使用结构化故障报告。"""

    change = adapt_activity_source_event(
        "cognitive_task.failed",
        {
            "activity_id": _uuid(17),
            "kind": "lookup_current_fact",
            "brief": "核对最新事实",
            "failure_reason": "认知结果没有通过结构化协议校验。",
            "failure": failure_from_reason(
                "cognitive_result_protocol_failed",
                stage_code="generating_result",
            ).to_payload(),
        },
        1,
    )

    assert change is not None
    assert change.failure_reason is None


def test_failed_events_reject_missing_or_illegal_failure() -> None:
    with pytest.raises(ValueError):
        adapt_activity_source_event(
            "background_agent.failed",
            {"activity_id": _uuid(16), "goal": "整理资料"},
            1,
        )
    with pytest.raises(ValueError):
        adapt_activity_source_event(
            "background_agent.failed",
            {"activity_id": _uuid(17), "goal": "整理资料", "failure": {"code": "sdk_error"}},
            1,
        )
    with pytest.raises(ValueError):
        adapt_activity_source_event(
            "cognitive_task.failed",
            {"activity_id": _uuid(18), "kind": "analyze_deeply", "failure": "oops"},
            1,
        )


def test_cancelled_events_carry_no_failure() -> None:
    change = adapt_activity_source_event(
        "background_agent.cancelled",
        {"activity_id": _uuid(19), "goal": "整理资料"},
        1,
    )

    assert change is not None
    assert change.change == "cancelled"
    assert change.failure is None
    assert change.completion_result is None


def test_background_agent_completed_requires_semantic_result_and_keeps_scanned_paths() -> None:
    """后台成功终态只接受完整正文、交付摘要和 Backend 文件事实。"""

    change = adapt_activity_source_event(
        "background_agent.completed",
        {
            "activity_id": _uuid(20),
            "goal": "整理资料",
            "display_content": "完整报告",
            "delivery_brief": "报告已整理出三项结论。",
            "output_paths": [r"C:\workspace\outputs\report.md", 123, ""],
        },
        1,
    )

    assert change is not None and change.completion_result is not None
    assert change.completion_result.display_content == "完整报告"
    assert change.completion_result.delivery_brief == "报告已整理出三项结论。"
    assert change.completion_result.output_paths == (
        r"C:\workspace\outputs\report.md",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"display_content": "完整报告"},
        {"delivery_brief": "报告已完成。"},
        {"display_content": " ", "delivery_brief": "报告已完成。"},
    ],
)
def test_background_agent_completed_rejects_incomplete_semantic_result(
    payload: dict[str, object],
) -> None:
    """跨进程边界不能接纳缺少正文或摘要的后台成功事件。"""

    with pytest.raises(ValueError):
        adapt_activity_source_event(
            "background_agent.completed",
            {"activity_id": _uuid(21), **payload},
            1,
        )


def test_background_agent_work_plan_adapter_accepts_strict_full_snapshot() -> None:
    """WorkPlan 来源事件必须转换成经过白名单校验的完整计划。"""

    change = adapt_activity_source_event(
        "background_agent.work_plan_changed",
        {
            "activity_id": _uuid(22),
            "work_plan": {
                "revision": 2,
                "items": [
                    {
                        "task_id": "1",
                        "subject": "理解参考项目",
                        "active_form": "正在理解参考项目",
                        "status": "completed",
                        "order": 1,
                    }
                ],
            },
        },
        1,
    )

    assert change is not None
    assert change.change == "work_plan_changed"
    assert change.work_plan is not None
    assert change.work_plan.revision == 2
    assert change.work_plan.items[0].subject == "理解参考项目"


def test_background_agent_work_plan_adapter_rejects_invalid_snapshot() -> None:
    """重复顺序和未知状态不能进入 Activity Registry。"""

    with pytest.raises(ValueError):
        adapt_activity_source_event(
            "background_agent.work_plan_changed",
            {
                "activity_id": _uuid(23),
                "work_plan": {
                    "revision": 1,
                    "items": [
                        {
                            "task_id": "1",
                            "subject": "任务",
                            "active_form": "正在任务",
                            "status": "unknown",
                            "order": 1,
                        }
                    ],
                },
            },
            1,
        )


def test_background_agent_completed_carries_result_card_fact_into_activity_result() -> None:
    """后台成功终态的 deliverable 布尔必须进入 Activity 完成结果，供提醒投影使用。"""

    change = adapt_activity_source_event(
        "background_agent.completed",
        {
            "activity_id": _uuid(30),
            "goal": "整理资料",
            "display_content": "完整报告",
            "delivery_brief": "报告已整理出三项结论。",
            "deliverable": True,
        },
        1,
    )

    assert change is not None and change.completion_result is not None
    assert change.completion_result.deliverable is True


@pytest.mark.parametrize(
    "payload_extra",
    [{}, {"deliverable": "true"}, {"deliverable": 1}],
    ids=["missing", "string", "int"],
)
def test_background_agent_completed_treats_abnormal_result_card_fact_as_unregistered(
    payload_extra: dict[str, object],
) -> None:
    """缺失或形状异常的 deliverable 一律按未登记结果卡处理，不向用户误报入口。"""

    change = adapt_activity_source_event(
        "background_agent.completed",
        {
            "activity_id": _uuid(31),
            "goal": "整理资料",
            "display_content": "完整报告",
            "delivery_brief": "报告已整理出三项结论。",
            **payload_extra,
        },
        1,
    )

    assert change is not None and change.completion_result is not None
    assert change.completion_result.deliverable is False
