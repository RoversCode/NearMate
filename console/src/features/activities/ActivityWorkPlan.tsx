import { CheckCircle2, Circle, Loader2, ListTodo } from "lucide-react";
import { useEffect, useState } from "react";
import type {
  ActivityStatus,
  AgentWorkPlan,
  AgentWorkPlanItem
} from "../../api/contracts/activities";

type ActivityWorkPlanProps = {
  activityId: string;
  plan: AgentWorkPlan;
  status: ActivityStatus;
};

/**
 * 展示 Background Agent 自己创建并确认过的真实任务清单。
 *
 * Args:
 *   props: Activity 身份、当前状态和最新完整计划快照。
 *
 * Returns:
 *   运行中默认展开、终态默认折叠的只读任务清单。
 */
export function ActivityWorkPlan({
  activityId,
  plan,
  status
}: ActivityWorkPlanProps): JSX.Element {
  const isTerminal = ["completed", "failed", "cancelled", "interrupted"].includes(status);
  const [isOpen, setIsOpen] = useState(!isTerminal);
  const completedCount = plan.items.filter(item => item.status === "completed").length;

  // 同一详情从运行态变成终态时要按产品规则自动收起；切换到另一项任务也重新套用默认值。
  useEffect(() => {
    setIsOpen(!isTerminal);
  }, [activityId, isTerminal]);

  return (
    <section className="space-y-2">
      <details
        className="group rounded-md border border-border bg-muted/30"
        onToggle={event => setIsOpen(event.currentTarget.open)}
        open={isOpen}
      >
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-semibold">
          <span className="flex min-w-0 items-center gap-2">
            <ListTodo aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
            <span>执行计划</span>
          </span>
          <span className="shrink-0 text-xs font-normal text-muted-foreground">
            {completedCount}/{plan.items.length}
          </span>
        </summary>
        <ol className="border-t border-border px-4 py-3">
          {plan.items.map(item => (
            <WorkPlanItem key={item.task_id} item={item} />
          ))}
        </ol>
      </details>
    </section>
  );
}

/**
 * 按真实状态展示一项待办，不解释或补写 Agent 没有上报的内容。
 *
 * Args:
 *   props: 已经过 Console 契约校验的计划项。
 *
 * Returns:
 *   带状态图标和对应公开文案的清单行。
 */
function WorkPlanItem({ item }: { item: AgentWorkPlanItem }): JSX.Element {
  const copy = item.status === "in_progress" ? item.active_form : item.subject;
  const iconClassName = "mt-0.5 size-4 shrink-0";

  return (
    <li className="flex min-w-0 gap-2 py-1.5 text-sm" data-status={item.status}>
      {item.status === "completed" ? (
        <CheckCircle2 aria-hidden="true" className={`${iconClassName} text-emerald-600`} />
      ) : item.status === "in_progress" ? (
        <Loader2 aria-hidden="true" className={`${iconClassName} animate-spin text-primary`} />
      ) : (
        <Circle aria-hidden="true" className={`${iconClassName} text-muted-foreground`} />
      )}
      <span
        className={item.status === "completed"
          ? "min-w-0 break-words text-muted-foreground line-through"
          : "min-w-0 break-words"}
      >
        {copy}
      </span>
    </li>
  );
}
