from __future__ import annotations

import pytest

from companion.foreground_tools.cognitive.contracts import CognitiveGoal
from companion.foreground_tools.cognitive.planner import CognitivePlanner
from companion.materials.foreground_images import VisualEvidence


@pytest.mark.parametrize(
    ("kinds", "reasoning", "web_search", "progress_policy", "progress_kind"),
    [
        (("lookup_current_fact",), "minimal", True, "reasoning_driven", "lookup_current_fact"),
        (("analyze_deeply",), "high", False, "reasoning_driven", "analyze_deeply"),
        (
            ("lookup_current_fact", "lookup_current_fact"),
            "minimal",
            True,
            "reasoning_driven",
            "lookup_current_fact",
        ),
        (
            ("lookup_current_fact", "analyze_deeply"),
            "high",
            True,
            "reasoning_driven",
            "analyze_deeply",
        ),
        (
            ("lookup_current_fact", "lookup_current_fact", "analyze_deeply"),
            "high",
            True,
            "reasoning_driven",
            "analyze_deeply",
        ),
    ],
)
def test_planner_selects_lowest_capability_covering_all_goals(
    kinds: tuple[str, ...],
    reasoning: str,
    web_search: bool,
    progress_policy: str,
    progress_kind: str,
) -> None:
    """Planner 应按目标并集选择最低够用档位。

    Args:
        kinds: 本轮目标类型序列。
        reasoning: 预期推理档位。
        web_search: 预期联网开关。
        progress_policy: 预期进度提醒策略。
        progress_kind: 预期采用的弱进度表达类别。
    """

    goals = [
        CognitiveGoal(
            tool_call_id=f"call-{index}",
            kind=kind,  # type: ignore[arg-type]
            brief=f"目标 {index}",
        )
        for index, kind in enumerate(kinds)
    ]

    plan = CognitivePlanner().plan(goals=goals)

    assert plan.reasoning == reasoning
    assert plan.web_search is web_search
    assert plan.progress_policy == progress_policy
    assert plan.progress_kind == progress_kind


def test_planner_deduplicates_goals_and_evidence_in_stable_order() -> None:
    """完全相同目标和证据只保留第一次出现，图片来源标签随首次出现保留。"""

    image_a = VisualEvidence("desktop", "桌面截图，采集于 2026-09-13 10:00", "image-a")
    image_a_duplicate = VisualEvidence("material", "重复标签.png", "image-a")
    image_b = VisualEvidence("material", "商品截图.png", "image-b")
    plan = CognitivePlanner().plan(
        goals=[
            CognitiveGoal("inspect-a", "lookup_current_fact", "读取报错"),
            CognitiveGoal("inspect-b", "lookup_current_fact", "读取报错"),
            CognitiveGoal("lookup-a", "lookup_current_fact", "核查最新修复方案"),
        ],
        images=[image_a, image_a_duplicate, image_b],
        source_urls=["https://example.com/a", "https://example.com/a"],
    )

    assert [goal.tool_call_id for goal in plan.goals] == ["inspect-a", "lookup-a"]
    assert plan.images == (image_a, image_b)
    assert plan.encoded_images == ("image-a", "image-b")
    assert plan.source_urls == ("https://example.com/a",)
    assert plan.web_extract is True
    assert plan.progress_policy == "reasoning_driven"


def test_planner_rejects_empty_goal_batch() -> None:
    """没有合法目标时不能创建空 Knowledge 请求。"""

    with pytest.raises(ValueError, match="至少需要一个目标"):
        CognitivePlanner().plan(goals=[])


def test_planner_rejects_unknown_explicit_cognitive_type() -> None:
    """纠正计划不能用未知类型绕过能力归一化。"""

    with pytest.raises(ValueError, match="不支持的认知能力"):
        CognitivePlanner().plan(
            goals=[CognitiveGoal("call", "analyze_deeply", "分析问题")],
            cognitive_tool_types=["unknown"],  # type: ignore[list-item]
        )


def test_planner_normalizes_explicit_capabilities_and_evidence_limitations() -> None:
    """Planner 应按有效能力重算执行档位，并清洗继承限制。"""

    plan = CognitivePlanner().plan(
        goals=[CognitiveGoal("call", "lookup_current_fact", "核查并分析")],
        cognitive_tool_types=[
            "lookup_current_fact",
            "analyze_deeply",
            "lookup_current_fact",
        ],
        evidence_limitations=[" 父图片失效 ", "父图片失效", ""],
    )

    assert plan.effective_cognitive_tool_types == (
        "lookup_current_fact",
        "analyze_deeply",
    )
    assert plan.reasoning == "high"
    assert plan.web_search is True
    assert plan.evidence_limitations == ("父图片失效",)
