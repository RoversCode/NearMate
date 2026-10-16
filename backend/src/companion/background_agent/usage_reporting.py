"""把 Repository 用量事实聚合成与传输层无关的统计报告。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date

from companion.background_agent.usage_models import AgentModelTokenUsage
from companion.background_agent.usage_repository import StoredUsageTask


@dataclass(frozen=True, slots=True)
class UsageTaskMetrics:
    """表示总览或单日使用的完整任务统计。"""

    task_count: int
    complete_task_count: int
    incomplete_task_count: int
    non_cached_input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    total_input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    cache_hit_rate: float | None
    reference_cost_usd: float | None
    reference_cost_task_count: int


@dataclass(frozen=True, slots=True)
class UsageByModelMetrics:
    """表示一个模型的 Token 和任务覆盖统计。"""

    model: str
    task_count: int
    complete_task_count: int
    incomplete_task_count: int
    non_cached_input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    total_input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    cache_hit_rate: float | None


@dataclass(frozen=True, slots=True)
class UsageByDayMetrics(UsageTaskMetrics):
    """表示一个固化本地日期的任务统计。"""

    date: date


@dataclass(frozen=True, slots=True)
class BackgroundAgentUsageReport:
    """表示一次日期查询的完整后台工作用量报告。"""

    from_date: date
    to_date: date
    summary: UsageTaskMetrics
    by_model: tuple[UsageByModelMetrics, ...]
    by_day: tuple[UsageByDayMetrics, ...]


@dataclass(slots=True)
class _UsageAggregate:
    """累计同一统计维度内的任务数、Token 与参考成本。"""

    task_count: int = 0
    complete_task_count: int = 0
    incomplete_task_count: int = 0
    reference_cost_task_count: int = 0
    reference_cost_usd: float = 0.0
    non_cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0

    def add_task(
        self,
        *,
        is_complete: bool,
        reference_cost_usd: float | None,
    ) -> None:
        """把一项任务的完整性与可选参考成本计入当前维度。"""

        self.task_count += 1
        if is_complete:
            self.complete_task_count += 1
        else:
            self.incomplete_task_count += 1
        if reference_cost_usd is not None:
            self.reference_cost_task_count += 1
            self.reference_cost_usd += reference_cost_usd

    def add_tokens(self, usage: AgentModelTokenUsage) -> None:
        """把一条模型用量计入当前维度。"""

        self.non_cached_input_tokens += usage.non_cached_input_tokens
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens
        self.output_tokens += usage.output_tokens

    def task_metrics(self) -> UsageTaskMetrics:
        """生成包含任务完整性和成本覆盖的统计。"""

        token_values = self._token_values()
        return UsageTaskMetrics(
            task_count=self.task_count,
            complete_task_count=self.complete_task_count,
            incomplete_task_count=self.incomplete_task_count,
            reference_cost_usd=(
                self.reference_cost_usd
                if self.reference_cost_task_count > 0
                else None
            ),
            reference_cost_task_count=self.reference_cost_task_count,
            **token_values,
        )

    def model_metrics(self, model: str) -> UsageByModelMetrics:
        """生成一个不伪造任务级参考成本的模型统计。"""

        return UsageByModelMetrics(
            model=model,
            task_count=self.task_count,
            complete_task_count=self.complete_task_count,
            incomplete_task_count=self.incomplete_task_count,
            **self._token_values(),
        )

    def _token_values(self) -> dict[str, int | float | None]:
        """按统一公式生成 Token 总量和缓存命中率。"""

        total_input_tokens = (
            self.non_cached_input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )
        return {
            "non_cached_input_tokens": self.non_cached_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_input_tokens": total_input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_tokens": self.cache_read_input_tokens,
            "cache_hit_rate": (
                self.cache_read_input_tokens / total_input_tokens
                if total_input_tokens > 0
                else None
            ),
        }


def build_usage_report(
    *,
    tasks: tuple[StoredUsageTask, ...],
    from_date: date,
    to_date: date,
) -> BackgroundAgentUsageReport:
    """把任务事实聚合成总览、按模型和按日报告。

    Args:
        tasks: Repository 从同一快照读取的任务记录。
        from_date: 查询包含的首日。
        to_date: 查询包含的末日。

    Returns:
        不依赖 FastAPI 或 JSON 字典的类型化用量报告。
    """

    summary = _UsageAggregate()
    by_day: dict[date, _UsageAggregate] = {}
    by_model: dict[str, _UsageAggregate] = {}
    model_activity_ids: dict[str, set[str]] = defaultdict(set)
    task_by_id = {task.activity_id: task for task in tasks}

    for task in tasks:
        summary.add_task(
            is_complete=task.is_complete,
            reference_cost_usd=task.reference_cost_usd,
        )
        day_aggregate = by_day.setdefault(
            task.terminal_local_date,
            _UsageAggregate(),
        )
        day_aggregate.add_task(
            is_complete=task.is_complete,
            reference_cost_usd=task.reference_cost_usd,
        )
        for usage in task.by_model:
            summary.add_tokens(usage)
            day_aggregate.add_tokens(usage)
            by_model.setdefault(usage.model, _UsageAggregate()).add_tokens(usage)
            model_activity_ids[usage.model].add(task.activity_id)

    model_metrics: list[UsageByModelMetrics] = []
    for model in sorted(by_model):
        aggregate = by_model[model]
        for activity_id in model_activity_ids[model]:
            task = task_by_id[activity_id]
            aggregate.add_task(
                is_complete=task.is_complete,
                reference_cost_usd=None,
            )
        model_metrics.append(aggregate.model_metrics(model))

    return BackgroundAgentUsageReport(
        from_date=from_date,
        to_date=to_date,
        summary=summary.task_metrics(),
        by_model=tuple(model_metrics),
        by_day=tuple(
            UsageByDayMetrics(
                date=local_date,
                **asdict(by_day[local_date].task_metrics()),
            )
            for local_date in sorted(by_day)
        ),
    )
