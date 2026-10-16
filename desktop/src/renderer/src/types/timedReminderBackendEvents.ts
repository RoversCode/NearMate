import { isStandardUuid } from "../../../shared/uuid";
import type { CoreToUiEventNameByDomain } from "./eventCatalog";
import {
  hasExactKeys,
  isNonEmptyString,
  isRecord,
  preservePayloadWhen,
  type BackendPayloadParser
} from "./backendEventParserPrimitives";

type TimedReminderState = "scheduled" | "ringing" | "dismissed" | "cancelled";
type TimedReminderFinishReason =
  | "user_cancelled"
  | "user_dismissed"
  | "attempt_limit_reached";

export type TimedReminderCompact = {
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
  active: TimedReminderCompact[];
  recent: TimedReminderCompact[];
};

export const MAX_TIMED_REMINDER_CONTENT_LENGTH = 500;

type TimedRemindersSnapshotChangedEvent = {
  type: "reminders.snapshot_changed";
  payload: TimedReminderSnapshot;
};

export type TimedReminderBackendEvent = TimedRemindersSnapshotChangedEvent;

type TimedReminderEventName = CoreToUiEventNameByDomain<"reminder">;

export const TIMED_REMINDER_EVENT_PARSERS = {
  "reminders.snapshot_changed": payload => preservePayloadWhen(
    payload,
    isTimedReminderSnapshot
  )
} satisfies Record<TimedReminderEventName, BackendPayloadParser>;

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

/** 校验一份提醒快照的完整字段、容量和生命周期分区。 */
function isTimedReminderSnapshot(value: unknown): value is TimedReminderSnapshot {
  if (!isRecord(value)) {
    return false;
  }
  const snapshot = value as Partial<TimedReminderSnapshot>;
  return hasExactKeys(value, SNAPSHOT_KEYS)
    && typeof snapshot.runtime_id === "string"
    && isStandardUuid(snapshot.runtime_id)
    && Number.isInteger(snapshot.revision)
    && (snapshot.revision ?? -1) >= 0
    && Array.isArray(snapshot.active)
    && snapshot.active.length <= 5
    && snapshot.active.every(item => isTimedReminder(item, ACTIVE_STATES))
    && Array.isArray(snapshot.recent)
    && snapshot.recent.length <= 20
    && snapshot.recent.every(item => isTimedReminder(item, TERMINAL_STATES));
}

/** 校验单条提醒在指定生命周期分区里的公开事实。 */
function isTimedReminder(
  value: unknown,
  allowedStates: ReadonlySet<TimedReminderState>
): value is TimedReminderCompact {
  if (!isRecord(value)) {
    return false;
  }
  const reminder = value as Partial<TimedReminderCompact>;
  const state = reminder.state as TimedReminderState;
  const isScheduled = state === "scheduled";
  const isActive = ACTIVE_STATES.has(state);
  const finishReason = reminder.finish_reason as TimedReminderFinishReason | null;
  return hasExactKeys(value, REMINDER_KEYS)
    && typeof reminder.reminder_id === "string"
    && isStandardUuid(reminder.reminder_id)
    && isNonEmptyString(reminder.content)
    && Array.from(reminder.content).length <= MAX_TIMED_REMINDER_CONTENT_LENGTH
    && allowedStates.has(state)
    && isIsoDate(reminder.trigger_at)
    && isIsoDate(reminder.created_at)
    && isIsoDate(reminder.updated_at)
    && (isActive ? reminder.finished_at === null : isIsoDate(reminder.finished_at))
    && Number.isInteger(reminder.ring_count)
    && (reminder.ring_count ?? -1) >= 0
    && (reminder.ring_count ?? 4) <= 3
    && (reminder.next_ring_at === null || isIsoDate(reminder.next_ring_at))
    && (finishReason === null || FINISH_REASONS.has(finishReason))
    && hasValidLifecycleFields(reminder, state, isScheduled);
}

/** 校验状态、计数、结束原因和操作权限互相一致。 */
function hasValidLifecycleFields(
  reminder: Partial<TimedReminderCompact>,
  state: TimedReminderState,
  isScheduled: boolean
): boolean {
  if (isScheduled) {
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

/** 判断未知值是否是能形成绝对时刻的非空字符串。 */
function isIsoDate(value: unknown): value is string {
  return typeof value === "string"
    && isNonEmptyString(value)
    && RFC3339_DATE_TIME_PATTERN.test(value)
    && Number.isFinite(Date.parse(value));
}

const RFC3339_DATE_TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
