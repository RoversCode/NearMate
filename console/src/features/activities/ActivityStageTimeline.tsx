import type {
  ActivityStagePeriod,
  ActivityStagePlan
} from "../../api/contracts/activities";
import { formatActivityTime } from "./activityPresentation";

type ActivityStageTimelineProps = {
  stagePlan: ActivityStagePlan;
  stageTimeline: ActivityStagePeriod[];
  terminalStatus: string;
};

type StageRenderState = "done" | "current" | "skipped" | "pending";

const STATE_BADGE: Record<StageRenderState, { text: string; className: string }> = {
  done: { text: "已完成", className: "bg-emerald-500/15 text-emerald-700" },
  current: { text: "当前", className: "bg-amber-500/15 text-amber-700" },
  skipped: { text: "已跳过", className: "bg-muted text-muted-foreground" },
  pending: { text: "未进入", className: "bg-muted text-muted-foreground" }
};

/**
 * 按五阶段计划与实际时间线渲染任务执行进度。
 *
 * Args:
 *   props: Backend 返回的完整阶段计划、实际阶段时间线和终态状态。
 *
 * Returns:
 *   已完成、当前、已跳过与未进入阶段一目了然的时间线。
 */
export function ActivityStageTimeline({
  stagePlan,
  stageTimeline,
  terminalStatus
}: ActivityStageTimelineProps): JSX.Element {
  const states = resolveStageStates(stagePlan, stageTimeline, terminalStatus);
  return (
    <ol className="space-y-3">
      {stagePlan.map((entry, index) => {
        const state = states[index];
        const period = stageTimeline.find(item => item.stage_code === entry.stage_code);
        return (
          <li className="flex items-start gap-3" key={entry.stage_code}>
            <span
              aria-label={STATE_BADGE[state].text}
              className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[10px] font-medium ${STATE_BADGE[state].className}`}
            >
              {index + 1}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{entry.stage_label}</span>
                <span className="text-xs text-muted-foreground">{STATE_BADGE[state].text}</span>
              </div>
              {period !== undefined ? (
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {formatActivityTime(period.started_at)}
                  {period.finished_at !== null ? ` → ${formatActivityTime(period.finished_at)}` : " → 进行中"}
                </p>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * 推导每个计划阶段的展示状态。
 *
 * 已进入过的阶段为已完成（终态时全部关闭），时间线最后一项在活动未结束时为当前；
 * 排在最高已进入阶段之前但未进入的为已跳过；其余为未进入。
 */
function resolveStageStates(
  stagePlan: ActivityStagePlan,
  stageTimeline: ActivityStagePeriod[],
  terminalStatus: string
): StageRenderState[] {
  const enteredCodes = new Set(stageTimeline.map(period => period.stage_code));
  const planCodes = stagePlan.map(entry => entry.stage_code);
  const highestEnteredIndex = stageTimeline.length === 0
    ? -1
    : Math.max(...stageTimeline.map(period => planCodes.indexOf(period.stage_code)));
  const isTerminal = terminalStatus !== "running" &&
    terminalStatus !== "waiting_user" &&
    terminalStatus !== "cancelling";
  return planCodes.map((code, index) => {
    if (enteredCodes.has(code)) {
      const period = stageTimeline.find(item => item.stage_code === code);
      if (!isTerminal && period !== undefined && period.finished_at === null) {
        return "current";
      }
      return "done";
    }
    if (index < highestEnteredIndex) {
      return "skipped";
    }
    return "pending";
  });
}
