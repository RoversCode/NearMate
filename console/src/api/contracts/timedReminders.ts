import { isStandardUuid } from "../../lib/uuid";

export type TimedReminderState = "scheduled" | "ringing" | "dismissed" | "cancelled";
export type TimedReminderFinishReason =
  | "user_cancelled"
  | "user_dismissed"
  | "attempt_limit_reached";

export type TimedReminder = {
  reminder_id: string;
  content: string;
  state: TimedReminderState;
  trigger_at: string;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  ring_count: number;
  next_ring_at: string | null;
  finish_reason: TimedReminderFinishReason | null;
  can_modify: boolean;
  can_cancel: boolean;
  can_dismiss: boolean;
};

export type TimedReminderSnapshot = {
  runtime_id: string;
  revision: number;
  active: TimedReminder[];
  recent: TimedReminder[];
};

export const MAX_TIMED_REMINDER_CONTENT_LENGTH = 500;

const ACTIVE_STATES = new Set<TimedReminderState>(["scheduled", "ringing"]);
const TERMINAL_STATES = new Set<TimedReminderState>(["dismissed", "cancelled"]);
const FINISH_REASONS = new Set<TimedReminderFinishReason>([
  "user_cancelled",
  "user_dismissed",
  "attempt_limit_reached"
]);
const SNAPSHOT_KEYS = ["runtime_id", "revision", "active", "recent"] as const;
const REMINDER_KEYS = [
  "reminder_id",
  "content",
  "state",
  "trigger_at",
  "created_at",
  "updated_at",
  "finished_at",
  "ring_count",
  "next_ring_at",
  "finish_reason",
  "can_modify",
  "can_cancel",
  "can_dismiss"
] as const;

/**
 * 校验并重建 Backend 返回的完整定时提醒快照。
 *
 * Args:
 *   value: 未受信任的 HTTP JSON 响应。
 *
 * Returns:
 *   只含公开字段、生命周期分区正确的提醒快照。
 *
 * Raises:
 *   Error: 顶层或任一提醒的字段、容量、身份、状态或时间非法。
 */
export function parseTimedReminderSnapshot(value: unknown): TimedReminderSnapshot {
  if (!isRecord(value) || !hasExactKeys(value, SNAPSHOT_KEYS)) {
    throw new Error("定时提醒列表响应格式无效");
  }
  const snapshot = value as Partial<TimedReminderSnapshot>;
  if (
    typeof snapshot.runtime_id !== "string"
    || !isStandardUuid(snapshot.runtime_id)
    || !Number.isInteger(snapshot.revision)
    || (snapshot.revision ?? -1) < 0
    || !Array.isArray(snapshot.active)
    || snapshot.active.length > 5
    || !Array.isArray(snapshot.recent)
    || snapshot.recent.length > 20
  ) {
    throw new Error("定时提醒列表响应格式无效");
  }

  const active = snapshot.active.map(item => parseTimedReminder(item, ACTIVE_STATES));
  const recent = snapshot.recent.map(item => parseTimedReminder(item, TERMINAL_STATES));
  return {
    runtime_id: snapshot.runtime_id,
    revision: snapshot.revision as number,
    active,
    recent
  };
}

/** 校验并白名单重建指定生命周期分区里的一条提醒。 */
function parseTimedReminder(
  value: unknown,
  allowedStates: ReadonlySet<TimedReminderState>
): TimedReminder {
  if (!isRecord(value) || !hasExactKeys(value, REMINDER_KEYS)) {
    throw new Error("定时提醒列表响应格式无效");
  }
  const reminder = value as Partial<TimedReminder>;
  const state = reminder.state as TimedReminderState;
  const isActive = ACTIVE_STATES.has(state);
  const finishReason = reminder.finish_reason as TimedReminderFinishReason | null;
  if (
    typeof reminder.reminder_id !== "string"
    || !isStandardUuid(reminder.reminder_id)
    || typeof reminder.content !== "string"
    || reminder.content.trim().length === 0
    || Array.from(reminder.content).length > MAX_TIMED_REMINDER_CONTENT_LENGTH
    || !allowedStates.has(state)
    || !isIsoDate(reminder.trigger_at)
    || !isIsoDate(reminder.created_at)
    || !isIsoDate(reminder.updated_at)
    || (isActive ? reminder.finished_at !== null : !isIsoDate(reminder.finished_at))
    || !Number.isInteger(reminder.ring_count)
    || (reminder.ring_count ?? -1) < 0
    || (reminder.ring_count ?? 4) > 3
    || (reminder.next_ring_at !== null && !isIsoDate(reminder.next_ring_at))
    || (finishReason !== null && !FINISH_REASONS.has(finishReason))
    || !hasValidLifecycleFields(reminder, state)
  ) {
    throw new Error("定时提醒列表响应格式无效");
  }
  return {
    reminder_id: reminder.reminder_id,
    content: reminder.content,
    state,
    trigger_at: reminder.trigger_at,
    created_at: reminder.created_at,
    updated_at: reminder.updated_at,
    finished_at: reminder.finished_at ?? null,
    ring_count: reminder.ring_count as number,
    next_ring_at: reminder.next_ring_at ?? null,
    finish_reason: finishReason,
    can_modify: reminder.can_modify as boolean,
    can_cancel: reminder.can_cancel as boolean,
    can_dismiss: reminder.can_dismiss as boolean
  };
}

/** 校验生命周期字段组合，防止前端接受互相矛盾的权限或终态原因。 */
function hasValidLifecycleFields(
  reminder: Partial<TimedReminder>,
  state: TimedReminderState
): boolean {
  if (state === "scheduled") {
    return reminder.ring_count === 0
      && reminder.next_ring_at === null
      && reminder.finish_reason === null
      && reminder.can_modify === true
      && reminder.can_cancel === true
      && reminder.can_dismiss === false;
  }
  if (state === "ringing") {
    return reminder.finish_reason === null
      && reminder.can_modify === false
      && reminder.can_cancel === false
      && reminder.can_dismiss === true;
  }
  if (state === "cancelled") {
    return reminder.ring_count === 0
      && reminder.next_ring_at === null
      && reminder.finish_reason === "user_cancelled"
      && reminder.can_modify === false
      && reminder.can_cancel === false
      && reminder.can_dismiss === false;
  }
  return reminder.next_ring_at === null
    && (reminder.finish_reason === "user_dismissed"
      || (reminder.finish_reason === "attempt_limit_reached" && reminder.ring_count === 3))
    && reminder.can_modify === false
    && reminder.can_cancel === false
    && reminder.can_dismiss === false;
}

/** 判断未知值是不是普通对象。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** 检查对象没有遗漏字段，也没有混入内部字段。 */
function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actualKeys = Object.keys(value);
  return actualKeys.length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

/** 判断未知值是否能形成有效绝对时刻。 */
function isIsoDate(value: unknown): value is string {
  return typeof value === "string"
    && value.trim().length > 0
    && RFC3339_DATE_TIME_PATTERN.test(value)
    && Number.isFinite(Date.parse(value));
}

const RFC3339_DATE_TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
