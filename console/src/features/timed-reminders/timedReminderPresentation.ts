import type {
  TimedReminderFinishReason,
  TimedReminderState
} from "../../api/contracts/timedReminders";

/** 把提醒状态翻译成人类可读文案。 */
export function formatTimedReminderState(
  state: TimedReminderState,
  finishReason: TimedReminderFinishReason | null
): string {
  if (state === "dismissed") {
    return finishReason === "attempt_limit_reached" ? "三次提醒已结束" : "已知道";
  }
  return {
    scheduled: "等待中",
    ringing: "正在提醒",
    cancelled: "已取消"
  }[state];
}

/** 把绝对触发时间格式化为本地日期和分钟。 */
export function formatTimedReminderTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(new Date(value));
}

/** 把触发时间与当前时刻的差值格式化为本地倒计时。 */
export function formatTimedReminderCountdown(triggerAt: string, now: Date): string {
  const seconds = Math.max(0, Math.ceil((Date.parse(triggerAt) - now.getTime()) / 1000));
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  const clock = [hours, minutes, remainingSeconds]
    .map(value => String(value).padStart(2, "0"))
    .join(":");
  return days > 0 ? `${days}天 ${clock}` : clock;
}

/** 把绝对时间投影成 datetime-local 输入框需要的本地格式。 */
export function toDateTimeLocal(value: string): string {
  const date = new Date(value);
  const part = (number: number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${part(date.getMonth() + 1)}-${part(date.getDate())}`
    + `T${part(date.getHours())}:${part(date.getMinutes())}:${part(date.getSeconds())}`;
}
