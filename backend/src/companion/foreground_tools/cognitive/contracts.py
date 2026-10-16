from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from companion.materials.foreground_images import ForegroundImageReference, VisualEvidence


CognitiveGoalKind = Literal[
    "lookup_current_fact",
    "analyze_deeply",
]
CognitiveReasoning = Literal["off", "minimal", "high"]
CognitiveProgressPolicy = Literal["disabled", "reasoning_driven"]


@dataclass(frozen=True)
class CognitiveGoal:
    """描述 Foreground 在当前轮提出的一项认知目标。

    Args:
        tool_call_id: 产生本目标的原始工具调用编号。
        kind: 目标所需的最低认知能力。
        brief: 已清洗的具体认知问题。
        response_goal: 可选的回答目的，用于保留跨轮讨论、比较或决策语境。
    """

    tool_call_id: str
    kind: CognitiveGoalKind
    brief: str
    response_goal: str = ""


@dataclass(frozen=True)
class CognitiveExecutionPlan:
    """描述一次 Cognitive 请求需要覆盖的完整认知计划。

    Args:
        goals: 按原工具调用顺序去重后的认知目标。
        reasoning: 本轮最低够用的推理档位。
        web_search: 是否允许 Cognitive 主动联网检索。
        web_extract: 是否允许 Cognitive 提取指定网页。
        images: 带来源标签的图片证据，标签随像素一起进入 Cognitive 请求。
        source_urls: 需要由网页提取能力读取的窄事实来源。
        progress_policy: 是否允许根据真实 reasoning 产生弱进度提醒。
        cognitive_tool_types: 本计划实际启用的全部认知工具类型；普通计划默认由 goals 推导，
            纠正计划可在旧能力上追加类型。
        evidence_limitations: 本轮明确存在、但 Cognitive 不应假定可用的证据限制。
        image_materials: 派生时必须重新校验的本地图片引用。
    """

    goals: tuple[CognitiveGoal, ...]
    reasoning: CognitiveReasoning
    web_search: bool
    web_extract: bool
    images: tuple[VisualEvidence, ...]
    source_urls: tuple[str, ...]
    progress_policy: CognitiveProgressPolicy
    based_on_activity_id: str | None = None
    parent_task_context: dict[str, object] | None = None
    cognitive_tool_types: tuple[CognitiveGoalKind, ...] = ()
    evidence_limitations: tuple[str, ...] = ()
    image_materials: tuple[ForegroundImageReference, ...] = ()

    @property
    def encoded_images(self) -> tuple[str, ...]:
        """返回图片证据的纯像素视图，供计数和序列化场景使用。

        Returns:
            按证据顺序排列的 data_url 元组，不含来源标签。
        """

        return tuple(image.data_url for image in self.images)

    @property
    def primary_kind(self) -> CognitiveGoalKind:
        """返回计划的主目标类型。

        Returns:
            原始顺序中的第一个目标类型。

        Raises:
            ValueError: 计划没有任何目标。
        """

        if not self.goals:
            raise ValueError("认知计划至少需要一个目标")
        return self.goals[0].kind

    @property
    def progress_kind(self) -> CognitiveGoalKind:
        """返回最适合约束弱进度表达的目标类型。

        混合计划可以同时核查和分析，进度规则优先使用分析目标的非终态边界。

        Returns:
            深度分析优先，其次是事实核查。
        """

        goal_kinds = set(self.effective_cognitive_tool_types)
        if "analyze_deeply" in goal_kinds:
            return "analyze_deeply"
        if "lookup_current_fact" in goal_kinds:
            return "lookup_current_fact"
        return self.primary_kind

    @property
    def effective_cognitive_tool_types(self) -> tuple[CognitiveGoalKind, ...]:
        """返回本计划最终启用的认知工具类型。

        Returns:
            纠正计划显式保存的能力集合；普通旧计划则从目标类型推导。
        """

        if self.cognitive_tool_types:
            return self.cognitive_tool_types
        return tuple(dict.fromkeys(goal.kind for goal in self.goals))

    @property
    def goal_text(self) -> str:
        """返回适合活动快照展示的综合目标文本。

        Returns:
            按计划顺序合并且不重复的目标描述。
        """

        return "；".join(goal.brief for goal in self.goals if goal.brief)
