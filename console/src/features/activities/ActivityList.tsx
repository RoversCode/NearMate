import type { ActivityCompact, ActivitySnapshot } from "../../api/contracts/activities";
import { EmptyState } from "../../components/layout/EmptyState";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from "../../components/ui/table";
import {
  activityStatusVariant,
  formatActivityElapsed,
  formatActivityStage,
  formatActivityStatus
} from "./activityPresentation";

type ActivityListProps = {
  snapshot: ActivitySnapshot;
  cancellingIds: ReadonlySet<string>;
  now: Date;
  onCancel(activityId: string): void;
  onOpenDetail(activityId: string): void;
};

/**
 * 按活动优先、终态倒序展示本次运行任务。
 *
 * Args:
 *   props: Activity 快照、取消请求集合、页面共享时钟与用户动作。
 *
 * Returns:
 *   目标、阶段、耗时与状态清晰的两个任务分区。
 */
export function ActivityList({
  snapshot,
  cancellingIds,
  now,
  onCancel,
  onOpenDetail
}: ActivityListProps): JSX.Element {
  if (snapshot.active.length === 0 && snapshot.recent.length === 0) {
    return <EmptyState>本次运行还没有任务记录。</EmptyState>;
  }

  return (
    <div className="space-y-5">
      <ActivityTable
        activities={snapshot.active}
        cancellingIds={cancellingIds}
        emptyCopy="当前没有活动任务。"
        now={now}
        onCancel={onCancel}
        onOpenDetail={onOpenDetail}
        title="进行中"
      />
      <ActivityTable
        activities={snapshot.recent}
        cancellingIds={cancellingIds}
        emptyCopy="本次运行还没有终态任务。"
        now={now}
        onCancel={onCancel}
        onOpenDetail={onOpenDetail}
        title="最近任务"
      />
    </div>
  );
}

type ActivityTableProps = Omit<ActivityListProps, "snapshot"> & {
  activities: ActivityCompact[];
  emptyCopy: string;
  title: string;
};

/**
 * 渲染同一状态分区的任务表格。
 *
 * Args:
 *   props: 当前分区标题、活动数据、页面共享时钟和用户动作。
 *
 * Returns:
 *   目标为主、类型/阶段/耗时/状态为辅与操作列组成的表格卡片。
 */
function ActivityTable({
  activities,
  cancellingIds,
  emptyCopy,
  now,
  onCancel,
  onOpenDetail,
  title
}: ActivityTableProps): JSX.Element {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {activities.length === 0 ? (
          <p className="text-sm text-muted-foreground">{emptyCopy}</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>目标</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>阶段</TableHead>
                <TableHead>耗时</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {activities.map(activity => {
                const requesting = cancellingIds.has(activity.activity_id);
                const cancelling = requesting || activity.status === "cancelling";
                return (
                  <TableRow key={activity.activity_id}>
                    <TableCell className="max-w-md">
                      <span className="block truncate font-medium" title={activity.goal_summary}>
                        {activity.goal_summary || "（无目标摘要）"}
                      </span>
                      {activity.failure_summary ? (
                        <span
                          className="mt-1 block truncate text-xs text-destructive"
                          title={activity.failure_summary}
                        >
                          {activity.failure_summary}
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {activity.type_label || activity.kind}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-muted-foreground">
                      {formatActivityStage(activity.stage_label, activity.stage_code)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-muted-foreground">
                      {formatActivityElapsed(activity.started_at, activity.finished_at, now)}
                    </TableCell>
                    <TableCell>
                      <Badge variant={activityStatusVariant(activity.status)}>
                        {formatActivityStatus(activity.status)}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-2">
                        <Button
                          onClick={() => onOpenDetail(activity.activity_id)}
                          size="sm"
                          variant="outline"
                        >
                          详情
                        </Button>
                        {activity.finished_at === null ? (
                          <Button
                            disabled={!activity.can_cancel || cancelling}
                            onClick={() => onCancel(activity.activity_id)}
                            size="sm"
                            variant="destructive"
                          >
                            {cancelling ? "取消中" : "取消"}
                          </Button>
                        ) : null}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
