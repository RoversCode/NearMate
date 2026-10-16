import type { BackgroundAgentUsageTaskMetrics } from "@/api/contracts/backgroundAgentUsage";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  formatCacheHitRate,
  formatReferenceCost,
  formatUsageCount
} from "./usageFormat";

type UsageSummaryCardsProps = {
  summary: BackgroundAgentUsageTaskMetrics;
};

/**
 * 展示所选时间范围的后台工作总消耗。
 *
 * Args:
 *   summary: Backend 已按统一口径聚合的任务用量。
 *
 * Returns:
 *   六张总览卡和可选的数据完整性提示。
 */
export function UsageSummaryCards({ summary }: UsageSummaryCardsProps): JSX.Element {
  const cards = [
    { label: "总输入", value: formatUsageCount(summary.total_input_tokens), hint: "含缓存创建与读取" },
    { label: "输出", value: formatUsageCount(summary.output_tokens), hint: "模型生成 Token" },
    { label: "缓存命中量", value: formatUsageCount(summary.cache_hit_tokens), hint: "缓存读取输入" },
    { label: "缓存命中率", value: formatCacheHitRate(summary.cache_hit_rate), hint: "命中量 ÷ 总输入" },
    { label: "任务数", value: formatUsageCount(summary.task_count), hint: `${summary.complete_task_count} 完整 / ${summary.incomplete_task_count} 不完整` },
    { label: "SDK 参考成本", value: formatReferenceCost(summary.reference_cost_usd), hint: `${summary.reference_cost_task_count} / ${summary.task_count} 项有成本` }
  ];
  const hasPartialCostCoverage =
    summary.reference_cost_task_count > 0 &&
    summary.reference_cost_task_count < summary.task_count;

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {cards.map(card => (
          <Card key={card.label}>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">{card.label}</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-semibold tabular-nums">{card.value}</p>
              <p className="mt-1 text-xs text-muted-foreground">{card.hint}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      {hasPartialCostCoverage ? (
        <p className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning">
          SDK 参考成本仅覆盖 {summary.reference_cost_task_count} / {summary.task_count} 项任务，不能视为完整账单。
        </p>
      ) : null}
      {summary.incomplete_task_count > 0 ? (
        <p className="text-xs text-muted-foreground">
          有 {summary.incomplete_task_count} 项任务在取消或运行时异常前只保存了已可靠收集的部分 Token。
        </p>
      ) : null}
    </div>
  );
}
