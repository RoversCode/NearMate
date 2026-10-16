import type { ActivityDetail } from "../../api/contracts/activities";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { LinkifiedText } from "../../components/LinkifiedText";
import { ActivityFailureDetails } from "./ActivityFailureDetails";
import { ActivityStageTimeline } from "./ActivityStageTimeline";
import { ActivityWorkPlan } from "./ActivityWorkPlan";
import { formatActivityFailureClipboard } from "./activityFailureClipboard";
import {
  activityStatusVariant,
  formatActivityElapsed,
  formatActivityStage,
  formatActivityStatus,
  formatActivityTime
} from "./activityPresentation";

type ActivityDetailViewProps = {
  activity: ActivityDetail;
  cancelling: boolean;
  now: Date;
  onBack(): void;
  onCancel(activityId: string): void;
};

/**
 * 组合一项 Activity 的详情页面：头部事实、阶段时间线与终态分区。
 *
 * Args:
 *   props: Activity 详情、取消状态、页面共享时钟和返回/取消动作。
 *
 * Returns:
 *   完整目标、当前阶段、耗时、时间线与终态内容分明的详情页。
 */
export function ActivityDetailView({
  activity,
  cancelling,
  now,
  onBack,
  onCancel
}: ActivityDetailViewProps): JSX.Element {
  const copyFailureDetails = async (): Promise<boolean> => {
    try {
      await navigator.clipboard.writeText(formatActivityFailureClipboard(activity));
      return true;
    } catch {
      return false;
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button onClick={onBack} variant="outline">返回任务列表</Button>
        {activity.finished_at === null ? (
          <Button
            disabled={!activity.can_cancel || cancelling || activity.status === "cancelling"}
            onClick={() => onCancel(activity.activity_id)}
            variant="destructive"
          >
            {cancelling || activity.status === "cancelling" ? "取消中" : "取消任务"}
          </Button>
        ) : null}
      </div>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle>{activity.type_label || activity.kind}</CardTitle>
            <Badge variant={activityStatusVariant(activity.status)}>
              {formatActivityStatus(activity.status)}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          <DetailText title="目标" value={activity.goal} emptyCopy="没有公开目标。" linkify />
          <dl className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
            <DetailField label="任务 ID" value={activity.activity_id} mono />
            <DetailField
              label="当前阶段"
              value={formatActivityStage(activity.stage_label, activity.stage_code)}
            />
            <DetailField
              label="总耗时"
              value={formatActivityElapsed(activity.started_at, activity.finished_at, now)}
            />
            <DetailField label="开始时间" value={formatActivityTime(activity.started_at)} />
          </dl>
          <section className="space-y-2">
            <h3 className="text-sm font-semibold">执行阶段</h3>
            <ActivityStageTimeline
              stagePlan={activity.stage_plan}
              stageTimeline={activity.stage_timeline}
              terminalStatus={activity.status}
            />
          </section>
          {activity.work_plan !== null ? (
            <ActivityWorkPlan
              activityId={activity.activity_id}
              plan={activity.work_plan}
              status={activity.status}
            />
          ) : null}
          {activity.turn_outputs.length > 0 ? (
            <section className="space-y-3">
              <h3 className="text-sm font-semibold">智能体运行详情</h3>
              <div
                aria-label="智能体运行详情列表"
                className="max-h-[min(32rem,60vh)] overflow-y-auto overscroll-contain rounded-md border bg-muted/20"
                tabIndex={0}
              >
                <div className="divide-y">
                  {activity.turn_outputs.map(output => (
                    <article className="space-y-2 p-4" key={output.turn_index}>
                      <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
                        <span>Turn {output.turn_index}</span>
                        <time dateTime={output.created_at}>{formatActivityTime(output.created_at)}</time>
                      </div>
                      <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">
                        {output.content_text}
                      </p>
                    </article>
                  ))}
                </div>
              </div>
            </section>
          ) : null}
          {activity.status === "completed" ? (
            <DetailText title="结果" value={activity.result_text} emptyCopy="当前没有公开结果。" linkify />
          ) : null}
          {activity.status === "failed" || activity.status === "interrupted" ? (
            activity.failure !== null ? (
              <ActivityFailureDetails
                failure={activity.failure}
                failureReason={activity.failure_reason}
                onCopy={copyFailureDetails}
              />
            ) : (
              // 契约上失败与中断必然携带故障报告；此处只是契约被破坏时的兜底。
              <p className="rounded-lg bg-muted/60 p-4 text-sm text-muted-foreground">
                任务未能完成，但服务端没有返回故障报告；请把任务编号提供给开发者排查。
              </p>
            )
          ) : null}
          {activity.status === "cancelled" ? (
            <p className="rounded-lg bg-muted/60 p-4 text-sm text-muted-foreground">
              任务已按你的要求取消，未保留结果内容。
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * 渲染一项只读详情元数据。
 *
 * Args:
 *   props: 字段名、字段值和等宽字体选项。
 *
 * Returns:
 *   可放入详情定义列表的字段块。
 */
function DetailField({
  label,
  value,
  mono = false
}: {
  label: string;
  value: string;
  mono?: boolean;
}): JSX.Element {
  return (
    <div className="space-y-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={mono ? "break-all font-mono text-xs" : "font-medium"}>{value}</dd>
    </div>
  );
}

/**
 * 渲染公开目标或结果正文。
 *
 * Args:
 *   props: 分区标题、正文和空态文案。
 *
 * Returns:
 *   保留换行且不会暴露其他字段的正文分区。
 */
function DetailText({
  title,
  value,
  emptyCopy,
  linkify = false
}: {
  title: string;
  value: string;
  emptyCopy: string;
  linkify?: boolean;
}): JSX.Element {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="whitespace-pre-wrap break-words rounded-lg bg-muted/60 p-4 text-sm leading-relaxed text-muted-foreground">
        {value ? (linkify ? <LinkifiedText text={value} /> : value) : emptyCopy}
      </p>
    </section>
  );
}
