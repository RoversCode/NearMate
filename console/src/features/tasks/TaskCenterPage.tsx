import type { TaskView } from "../../app/consoleTypes";
import { PageHeader } from "../../components/layout/PageHeader";
import { cn } from "../../lib/utils";
import { ActivityCenterPage } from "../activities/ActivityCenterPage";
import { TimedReminderCenterPage } from "../timed-reminders/TimedReminderCenterPage";

type TaskCenterPageProps = {
  baseUrl: string;
  taskView: TaskView;
  activityId: string | null;
  onNavigateTaskView(taskView: TaskView): void;
  onNavigateActivity(activityId: string | null): void;
};

/**
 * 渲染任务中心总入口，并把执行任务与定时提醒保持为两个独立子视图。
 *
 * Args:
 *   props: 控制面地址、URL 子视图、可选 Activity 深链接和导航命令。
 *
 * Returns:
 *   带稳定页签导航的任务中心。
 */
export function TaskCenterPage({
  baseUrl,
  taskView,
  activityId,
  onNavigateTaskView,
  onNavigateActivity
}: TaskCenterPageProps): JSX.Element {
  return (
    <section className="space-y-6">
      <PageHeader
        description="管理本次运行中的任务与定时提醒。应用重启后，这些记录会清空。"
        title="任务中心"
      />
      <div aria-label="任务类型" className="settings-tabs" role="tablist">
        {(["activities", "reminders"] as const).map((view) => (
          <button
            aria-controls="task-view-panel"
            aria-selected={taskView === view}
            className={cn("settings-tab", taskView === view && "settings-tab-active")}
            id={`task-tab-${view}`}
            key={view}
            onClick={() => onNavigateTaskView(view)}
            onKeyDown={(event) => {
              if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
              event.preventDefault();
              const nextView = event.key === "Home" ? "activities" : event.key === "End" ? "reminders" : view === "activities" ? "reminders" : "activities";
              onNavigateTaskView(nextView);
              document.getElementById(`task-tab-${nextView}`)?.focus();
            }}
            role="tab"
            tabIndex={taskView === view ? 0 : -1}
            type="button"
          >{view === "activities" ? "执行任务" : "定时提醒"}</button>
        ))}
      </div>
      <div aria-labelledby={`task-tab-${taskView}`} id="task-view-panel" role="tabpanel" tabIndex={0}>

      {taskView === "reminders" ? (
        <TimedReminderCenterPage baseUrl={baseUrl} />
      ) : (
        <ActivityCenterPage
          activityId={activityId}
          baseUrl={baseUrl}
          onNavigateActivity={onNavigateActivity}
        />
      )}
      </div>
    </section>
  );
}
