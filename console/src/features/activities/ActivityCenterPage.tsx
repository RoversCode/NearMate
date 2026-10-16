import { useEffect, useState } from "react";
import { AlertCircle, LoaderCircle } from "lucide-react";
import { EmptyState } from "../../components/layout/EmptyState";
import { Button } from "../../components/ui/button";
import { ActivityDetailView } from "./ActivityDetailView";
import { ActivityList } from "./ActivityList";
import { useActivitiesRuntime } from "./useActivitiesRuntime";

type ActivityCenterPageProps = {
  baseUrl: string;
  activityId: string | null;
  onNavigateActivity(activityId: string | null): void;
};

/**
 * 渲染本次运行任务中心并按 URL 在列表/详情间切换。
 *
 * Args:
 *   props: 控制面地址、当前深链接 Activity UUID 和导航回调。
 *
 * Returns:
 *   可无闪烁刷新、可取消、支持过期深链接的任务中心。
 */
export function ActivityCenterPage({
  baseUrl,
  activityId,
  onNavigateActivity
}: ActivityCenterPageProps): JSX.Element {
  const runtime = useActivitiesRuntime(baseUrl, activityId);
  const errorMessage = runtime.cancelErrorMessage ?? runtime.listErrorMessage;
  // 共享耗时时钟：整个页面只有一个 timer，存在活动任务或进行中的详情时启动，每行不得自建 timer。
  const hasActiveActivities =
    (runtime.snapshot?.active.length ?? 0) > 0 ||
    (runtime.detailState.status === "ready" && runtime.detailState.activity.finished_at === null);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!hasActiveActivities) {
      return;
    }
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, [hasActiveActivities]);

  return (
    <div className="space-y-6">
      {runtime.restartMessage !== null ? (
        <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700" role="status">
          {runtime.restartMessage}
        </p>
      ) : null}
      {runtime.connectionMessage !== null ? (
        <p className="rounded-lg border border-border bg-muted/60 px-4 py-3 text-sm text-muted-foreground" role="status">
          {runtime.connectionMessage}
        </p>
      ) : null}
      {errorMessage !== null ? (
        <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive" role="status">
          {errorMessage}
        </p>
      ) : null}

      {activityId === null ? (
        runtime.snapshot === null && runtime.isLoadingList ? (
          <EmptyState icon={LoaderCircle}>正在加载本次运行任务。</EmptyState>
        ) : runtime.snapshot === null ? (
          <EmptyState icon={AlertCircle}><p>任务列表暂不可用，请稍后重试。</p><Button className="mt-3" onClick={() => void runtime.reloadList()} variant="outline">重新加载任务</Button></EmptyState>
        ) : (
          <ActivityList
            cancellingIds={runtime.cancellingIds}
            now={now}
            onCancel={activityId => void runtime.requestCancel(activityId)}
            onOpenDetail={onNavigateActivity}
            snapshot={runtime.snapshot}
          />
        )
      ) : (
        <ActivityDetailContent
          activityId={activityId}
          now={now}
          runtime={runtime}
          onBack={() => onNavigateActivity(null)}
        />
      )}
    </div>
  );
}

type ActivityDetailContentProps = {
  activityId: string;
  now: Date;
  runtime: ReturnType<typeof useActivitiesRuntime>;
  onBack(): void;
};

/**
 * 根据详情请求状态渲染加载、过期、失败或公开详情。
 *
 * Args:
 *   props: 当前 Activity UUID、页面共享时钟、feature runtime 和返回动作。
 *
 * Returns:
 *   与详情请求状态严格对应的单一视图。
 */
function ActivityDetailContent({
  activityId,
  now,
  runtime,
  onBack
}: ActivityDetailContentProps): JSX.Element {
  if (
    runtime.detailState.status === "loading" ||
    runtime.detailState.status === "idle" ||
    (runtime.detailState.status === "ready" &&
      runtime.detailState.activity.activity_id !== activityId)
  ) {
    return <EmptyState icon={LoaderCircle}>正在加载任务详情。</EmptyState>;
  }
  if (runtime.detailState.status === "stale") {
    return (
      <EmptyState icon={AlertCircle}>
        <p>这项任务已不在本次运行记录中。</p>
        <Button className="mt-3" onClick={onBack} variant="outline">返回任务列表</Button>
      </EmptyState>
    );
  }
  if (runtime.detailState.status === "failed") {
    return (
      <EmptyState icon={AlertCircle}>
        <p>{runtime.detailState.errorMessage}</p>
        <Button className="mt-3" onClick={() => void runtime.reloadDetail?.()} variant="outline">
          重新加载
        </Button>
        <Button className="ml-2 mt-3" onClick={onBack} variant="ghost">返回任务列表</Button>
      </EmptyState>
    );
  }
  return (
    <ActivityDetailView
      activity={runtime.detailState.activity}
      cancelling={runtime.cancellingIds.has(activityId)}
      now={now}
      onBack={onBack}
      onCancel={activityId => void runtime.requestCancel(activityId)}
    />
  );
}
