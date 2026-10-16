from __future__ import annotations

import pytest

from companion.activities.progress_ledger import ActivityProgressLedger
from companion.realtime.assistant_output_contracts import ActivityProgressReference


def _reference(sequence: int = 1) -> ActivityProgressReference:
    """构造固定 Activity 的测试引用。"""

    return ActivityProgressReference(
        activity_id="activity-a",
        source="cognitive",
        notice_sequence=sequence,
    )


def test_visible_progress_moves_from_active_to_frozen_then_releases() -> None:
    """公开 Progress 应在终态冻结，并只由终态 Notice 收口删除。"""

    ledger = ActivityProgressLedger()
    assert ledger.record_visible_progress(
        visible_text="已经核对了第一批资料。",
        references=(_reference(),),
    ) == 1
    assert ledger.snapshot()[0].lifecycle == "active"

    assert ledger.freeze(source="cognitive", activity_id="activity-a") is True
    frozen = ledger.snapshot()[0]
    assert frozen.lifecycle == "frozen"
    assert frozen.reports == ("已经核对了第一批资料。",)

    assert ledger.release_frozen((("cognitive", "activity-a"),)) == 1
    assert ledger.snapshot() == ()


def test_late_progress_settlement_is_appended_to_frozen_entry() -> None:
    """终态与 Progress settlement 竞态时，已公开正文不能因先冻结而丢失。"""

    ledger = ActivityProgressLedger()
    assert ledger.freeze(source="cognitive", activity_id="activity-a") is True

    assert ledger.record_visible_progress(
        visible_text="终态到达前已经公开的半句。",
        references=(_reference(sequence=2),),
    ) == 1

    snapshot = ledger.snapshot()
    assert snapshot[0].lifecycle == "frozen"
    assert snapshot[0].reports == ("终态到达前已经公开的半句。",)


def test_equal_visible_progress_reports_are_both_preserved() -> None:
    """两次实际公开的相同正文也必须按发生次数完整保留。"""

    ledger = ActivityProgressLedger()
    assert ledger.record_visible_progress(
        visible_text="仍在核对。",
        references=(_reference(),),
    ) == 1
    assert ledger.record_visible_progress(
        visible_text="仍在核对。",
        references=(_reference(sequence=2),),
    ) == 1

    reports = ledger.reports_by_activity({("cognitive", "activity-a")})
    assert reports == {
        ("cognitive", "activity-a"): ("仍在核对。", "仍在核对。")
    }


def test_release_does_not_delete_still_active_progress() -> None:
    """迟到的终态 Notice 清理不得删除同编号尚未冻结的活跃记录。"""

    ledger = ActivityProgressLedger()
    ledger.record_visible_progress(
        visible_text="仍在处理。",
        references=(_reference(),),
    )

    assert ledger.release_frozen((("cognitive", "activity-a"),)) == 0
    assert ledger.snapshot()[0].reports == ("仍在处理。",)


def test_late_settlement_cannot_resurrect_released_terminal_progress() -> None:
    """终态已经清账后，迟到的旧输出结算不能重新创建 active Progress。"""

    ledger = ActivityProgressLedger()
    ledger.freeze(source="cognitive", activity_id="activity-a")
    assert ledger.release_frozen((("cognitive", "activity-a"),)) == 1

    recorded = ledger.record_visible_progress(
        visible_text="这是热替换前已经公开、但稍后才结算的进度。",
        references=(_reference(sequence=2),),
    )

    assert recorded == 0
    assert ledger.snapshot() == ()


def test_visible_progress_keeps_all_public_text_without_silent_truncation() -> None:
    """已经公开的长正文和连续报告必须完整保留到 Activity 终态。"""

    ledger = ActivityProgressLedger()
    long_report = "进" * 9_000
    assert ledger.record_visible_progress(
        visible_text=long_report,
        references=(_reference(),),
    ) == 1
    for report_index in range(40):
        ledger.record_visible_progress(
            visible_text=f"第 {report_index + 1} 条公开进度",
            references=(_reference(sequence=report_index + 2),),
        )

    reports = ledger.snapshot()[0].reports
    assert reports[0] == long_report
    assert len(reports[0]) == 9_000
    assert reports[1:] == tuple(
        f"第 {report_index + 1} 条公开进度" for report_index in range(40)
    )


def test_multi_activity_progress_capacity_failure_is_atomic() -> None:
    """整批 Activity 放不下时，不得先把正文追加到其中一部分。

    Returns:
        无。
    """

    ledger = ActivityProgressLedger(max_activities=1)
    ledger.record_visible_progress(
        visible_text="已经公开的旧进度。",
        references=(_reference(),),
    )
    second_reference = ActivityProgressReference(
        activity_id="activity-b",
        source="cognitive",
        notice_sequence=1,
    )

    with pytest.raises(RuntimeError, match="账本已达到容量上限"):
        ledger.record_visible_progress(
            visible_text="不能只写入半批的新进度。",
            references=(_reference(sequence=2), second_reference),
        )

    assert ledger.reports_by_activity({("cognitive", "activity-a")}) == {
        ("cognitive", "activity-a"): ("已经公开的旧进度。",)
    }


def test_active_environment_hides_frozen_progress_until_terminal_notice_requests_it() -> None:
    """终态 Progress 不得继续污染普通动态环境，只能按终态 Activity 定向读取。"""

    ledger = ActivityProgressLedger()
    ledger.record_visible_progress(
        visible_text="终态前已经公开。",
        references=(_reference(),),
    )
    ledger.freeze(source="cognitive", activity_id="activity-a")

    assert ledger.active_snapshot_payload() == []
    assert ledger.snapshot_payload_for({("cognitive", "activity-a")}) == [
        {
            "source": "cognitive",
            "activity_id": "activity-a",
            "lifecycle": "frozen",
            "reports": ["终态前已经公开。"],
        }
    ]


def test_user_request_environment_includes_active_and_attached_terminal_progress() -> None:
    """用户轮既要看运行中进度，也要看本轮强终态提醒定向引用的 frozen 进度。"""

    ledger = ActivityProgressLedger()
    ledger.record_visible_progress(
        visible_text="仍在运行的任务进度。",
        references=(_reference(),),
    )
    ledger.record_visible_progress(
        visible_text="本轮终态提醒对应的旧进度。",
        references=(
            ActivityProgressReference(
                activity_id="activity-terminal",
                source="background_agent",
                notice_sequence=1,
            ),
        ),
    )
    ledger.freeze(source="background_agent", activity_id="activity-terminal")
    ledger.record_visible_progress(
        visible_text="与本轮无关的 frozen 进度。",
        references=(
            ActivityProgressReference(
                activity_id="activity-unrelated",
                source="background_agent",
                notice_sequence=1,
            ),
        ),
    )
    ledger.freeze(source="background_agent", activity_id="activity-unrelated")

    payload = ledger.snapshot_payload_for_user_request(
        {("background_agent", "activity-terminal")}
    )

    assert [item["activity_id"] for item in payload] == [
        "activity-a",
        "activity-terminal",
    ]
