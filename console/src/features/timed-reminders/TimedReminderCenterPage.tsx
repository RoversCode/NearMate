import { useEffect, useState } from "react";
import { AlertCircle, LoaderCircle } from "lucide-react";
import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/layout/EmptyState";
import { TimedReminderList } from "./TimedReminderList";
import { useTimedRemindersRuntime } from "./useTimedRemindersRuntime";

/**
 * 渲染任务中心里的定时提醒子视图。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   HTTP 权威列表、运行期提示和行内管理界面。
 */
export function TimedReminderCenterPage({ baseUrl }: { baseUrl: string }): JSX.Element {
  const runtime = useTimedRemindersRuntime(baseUrl);
  const [now, setNow] = useState(() => new Date());
  const hasActiveReminders = (runtime.snapshot?.active.length ?? 0) > 0;
  useEffect(() => {
    if (!hasActiveReminders) {
      return;
    }
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, [hasActiveReminders]);

  const errorMessage = runtime.mutationErrorMessage ?? runtime.loadErrorMessage;
  return (
    <div className="space-y-6">
      {runtime.restartMessage ? (
        <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700" role="status">
          {runtime.restartMessage}
        </p>
      ) : null}
      {runtime.connectionMessage ? (
        <p className="rounded-lg border border-border bg-muted/60 px-4 py-3 text-sm text-muted-foreground" role="status">
          {runtime.connectionMessage}
        </p>
      ) : null}
      {errorMessage ? (
        <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive" role="status">
          {errorMessage}
        </p>
      ) : null}

      {runtime.snapshot === null && runtime.isLoading ? (
        <EmptyState icon={LoaderCircle}>正在加载本次运行的定时提醒。</EmptyState>
      ) : runtime.snapshot === null ? (
        <EmptyState icon={AlertCircle}><p>定时提醒列表暂不可用，请稍后重试。</p><Button className="mt-3" onClick={() => void runtime.reload()} variant="outline">重新加载提醒</Button></EmptyState>
      ) : (
        <TimedReminderList
          now={now}
          onCancel={reminderId => void runtime.requestCancel(reminderId)}
          onDismiss={(runtimeId, reminderIds) => {
            void runtime.requestDismiss(runtimeId, reminderIds);
          }}
          onUpdate={runtime.requestUpdate}
          requestingIds={runtime.requestingIds}
          snapshot={runtime.snapshot}
        />
      )}
    </div>
  );
}
