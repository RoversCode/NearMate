import type { TimedReminderSnapshot } from "../types/backendEvents";

/**
 * 按运行编号和版本裁决是否接受一份提醒快照。
 *
 * Args:
 *   current: 当前持有的提醒快照，尚未收到首帧时为空。
 *   next: 已经通过公共事件契约校验的新快照。
 *
 * Returns:
 *   新快照应当接管当前状态时返回真。
 */
export function shouldAcceptTimedReminderSnapshot(
  current: TimedReminderSnapshot | null,
  next: TimedReminderSnapshot
): boolean {
  if (current === null) {
    return true;
  }
  if (next.runtime_id !== current.runtime_id) {
    return true;
  }
  return next.revision > current.revision;
}
