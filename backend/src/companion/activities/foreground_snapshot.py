"""定义 Foreground prompt 使用的活动只读快照。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from companion.activities.work_plan import AgentWorkPlanSummary


ActivityPhase = Literal["reasoning", "finalizing"]


@dataclass(frozen=True)
class ForegroundActivitySnapshot:
    """保存 foreground 当前可读取的一项活动事实。

    Args:
        activity_id: 活动编号，仅用于内部关联提醒。
        interaction_session_id: 活动所属的交互会话编号。
        source: 活动来源类别。
        category: 面向 foreground 的自然任务类别。
        goal: 用户希望活动完成的目标。
        goal_summary: 跨交互会话仍可表达的有界目标摘要。
        activity_kind: 面向 Prompt 的稳定活动类别，用于选择进度表达规则。
        cognitive_tool_types: Cognitive Activity 当前计划已经包含的认知工具类型。
        state: 当前活动运行状态。
        phase: 当前处于推理或结果生成阶段。
        stage_code: 当前公共执行阶段代码，供已到期的弱进度提醒组织文案。
        progress_reasoning_text: 已冻结但尚未实际播报的 reasoning。
        progress_sequence: 最新冻结进度对应的提醒序号。
        work_plan_summary: 当前真实计划的压缩摘要；Agent 未创建计划时为空。
        previous_reports: 该活动此前已经实际告诉用户的临时记录。
    """

    activity_id: str
    interaction_session_id: str
    source: str
    category: str
    goal: str
    goal_summary: str = ""
    activity_kind: str = ""
    cognitive_tool_types: tuple[str, ...] = ()
    state: Literal["running", "cancelling", "attention", "waiting_user"] = "running"
    phase: ActivityPhase = "reasoning"
    stage_code: str = ""
    progress_reasoning_text: str = ""
    progress_sequence: int = 0
    work_plan_summary: AgentWorkPlanSummary | None = None
    previous_reports: tuple[str, ...] = ()

    def with_previous_reports(
        self,
        reports: tuple[str, ...],
    ) -> ForegroundActivitySnapshot:
        """返回附带既有实际汇报记录的只读副本。

        Args:
            reports: 与当前活动关联的历史汇报正文。

        Returns:
            保持其它任务事实不变的新快照。
        """

        return replace(self, previous_reports=reports)

    def to_payload(self) -> dict[str, object]:
        """转换为 prompt route context 可使用的字典。

        Returns:
            不包含 Activity Executor 内部可变状态的 JSON 友好载荷。
        """

        return {
            "activity_id": self.activity_id,
            "interaction_session_id": self.interaction_session_id,
            "source": self.source,
            "category": self.category,
            "goal": self.goal,
            "goal_summary": self.goal_summary,
            "activity_kind": self.activity_kind,
            "cognitive_tool_types": list(self.cognitive_tool_types),
            "state": self.state,
            "phase": self.phase,
            "stage_code": self.stage_code,
            "progress_reasoning_text": self.progress_reasoning_text,
            "progress_sequence": self.progress_sequence,
            "work_plan": (
                self.work_plan_summary.to_payload()
                if self.work_plan_summary is not None
                else None
            ),
            "previous_reports": list(self.previous_reports),
            # 只要快照还存在，这项 Activity 就仍占用对应入口；等待用户和注意状态也
            # 不能让 Foreground 误以为可以再开同类型任务。
            "duplicate_tool_calls_forbidden": self.state
            in {"running", "cancelling", "attention", "waiting_user"},
        }
