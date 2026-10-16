from __future__ import annotations

from collections.abc import Iterable

from companion.materials.foreground_images import (
    ForegroundImageReference,
    VisualEvidence,
    bound_image_evidence,
)

from companion.foreground_tools.cognitive.contracts import (
    CognitiveExecutionPlan,
    CognitiveGoal,
    CognitiveGoalKind,
    CognitiveReasoning,
)


_REASONING_RANK: dict[CognitiveReasoning, int] = {
    "off": 0,
    "minimal": 1,
    "high": 2,
}
_GOAL_REASONING: dict[str, CognitiveReasoning] = {
    "lookup_current_fact": "minimal",
    "analyze_deeply": "high",
}


class CognitivePlanner:
    """把同一 turn 的多个认知意图合并为一次 Knowledge 执行计划。"""

    def plan(
        self,
        *,
        goals: Iterable[CognitiveGoal],
        images: Iterable[VisualEvidence] = (),
        source_urls: Iterable[str] = (),
        based_on_activity_id: str | None = None,
        parent_task_context: dict[str, object] | None = None,
        cognitive_tool_types: Iterable[CognitiveGoalKind] = (),
        evidence_limitations: Iterable[str] = (),
        image_materials: Iterable[ForegroundImageReference] = (),
    ) -> CognitiveExecutionPlan:
        """生成覆盖全部合法目标的最低能力计划。

        Args:
            goals: Foreground 本轮提出的认知目标。
            images: 带来源标签的图片证据，标签随像素一起传给 Cognitive。
            source_urls: 需要网页提取能力读取的窄事实来源。
            based_on_activity_id: 可选父任务编号。
            parent_task_context: 父任务的目标、结果、输入和终态事实。
            cognitive_tool_types: 本计划明确启用的认知能力；未提供时从目标推导。
            evidence_limitations: 当前证据继承中需要明确报告的限制。
            image_materials: 允许后续派生任务重新校验的本地材料。

        Returns:
            去重、保序且能力已经合并的执行计划。

        Raises:
            ValueError: 没有合法认知目标时抛出。
        """

        normalized_goals = _deduplicate_goals(goals)
        if not normalized_goals:
            raise ValueError("认知计划至少需要一个目标")

        # bound_image_evidence 内部按像素去重并保序，来源标签随首次出现保留。
        normalized_images, image_limitations = bound_image_evidence(images)
        normalized_urls = _deduplicate_text(source_urls)
        normalized_limitations = _deduplicate_text((*evidence_limitations, *image_limitations))
        requested_tool_types = (
            tuple(cognitive_tool_types)
            if cognitive_tool_types is not None
            else ()
        )
        normalized_tool_types = _deduplicate_tool_types(
            requested_tool_types or (goal.kind for goal in normalized_goals)
        )
        if not normalized_tool_types:
            raise ValueError("认知计划至少需要一个认知能力")
        reasoning = max(
            (_GOAL_REASONING[kind] for kind in normalized_tool_types),
            key=_REASONING_RANK.__getitem__,
        )
        web_search = "lookup_current_fact" in normalized_tool_types
        web_extract = bool(normalized_urls)
        return CognitiveExecutionPlan(
            goals=normalized_goals,
            reasoning=reasoning,
            web_search=web_search,
            web_extract=web_extract,
            images=normalized_images,
            source_urls=normalized_urls,
            progress_policy="disabled" if reasoning == "off" else "reasoning_driven",
            based_on_activity_id=(
                based_on_activity_id.strip()
                if isinstance(based_on_activity_id, str) and based_on_activity_id.strip()
                else None
            ),
            parent_task_context=(
                dict(parent_task_context)
                if isinstance(parent_task_context, dict)
                else None
            ),
            cognitive_tool_types=normalized_tool_types,
            evidence_limitations=normalized_limitations,
            image_materials=tuple(dict.fromkeys(image_materials)),
        )


def _deduplicate_goals(goals: Iterable[CognitiveGoal]) -> tuple[CognitiveGoal, ...]:
    """按目标类型和正文去重并保留第一次出现顺序。

    Args:
        goals: 原始认知目标序列。

    Returns:
        去除空目标与完全重复目标后的不可变序列。
    """

    result: list[CognitiveGoal] = []
    seen: set[tuple[str, str, str]] = set()
    for goal in goals:
        brief = goal.brief.strip()
        if not brief:
            continue
        response_goal = goal.response_goal.strip()
        key = (goal.kind, brief, response_goal)
        if key in seen:
            continue
        seen.add(key)
        result.append(CognitiveGoal(
            tool_call_id=goal.tool_call_id,
            kind=goal.kind,
            brief=brief,
            response_goal=response_goal,
        ))
    return tuple(result)


def _deduplicate_text(values: Iterable[str]) -> tuple[str, ...]:
    """清洗文本集合并按首次出现顺序去重。

    Args:
        values: 原始文本序列。

    Returns:
        不含空字符串和重复项的不可变序列。
    """

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _deduplicate_tool_types(
    values: Iterable[CognitiveGoalKind],
) -> tuple[CognitiveGoalKind, ...]:
    """按首次出现顺序去重 Cognitive 能力类型。

    Args:
        values: 计划准备启用的认知能力类型。

    Returns:
        已校验、去重并保留首次出现顺序的认知能力。

    Raises:
        ValueError: 任一类型不属于系统支持的认知能力。
    """

    result: list[CognitiveGoalKind] = []
    seen: set[CognitiveGoalKind] = set()
    for value in values:
        if not isinstance(value, str) or value not in _GOAL_REASONING:
            raise ValueError("认知计划包含不支持的认知能力")
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)
