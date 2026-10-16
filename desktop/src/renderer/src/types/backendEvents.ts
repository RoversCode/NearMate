import type { CoreToUiEventName } from "./eventCatalog";
import {
  ACTIVITY_EVENT_PARSERS,
  type ActivityBackendEvent
} from "./activityBackendEvents";
import {
  APPLICATION_EVENT_PARSERS,
  type ApplicationBackendEvent
} from "./applicationBackendEvents";
import {
  ASSISTANT_OUTPUT_EVENT_PARSERS,
  type AssistantOutputBackendEvent
} from "./assistantOutputBackendEvents";
import {
  hasExactKeys,
  isRecord,
  type BackendPayloadParser
} from "./backendEventParserPrimitives";
import {
  INTERACTION_EVENT_PARSERS,
  type InteractionBackendEvent
} from "./interactionBackendEvents";
import {
  TIMED_REMINDER_EVENT_PARSERS,
  type TimedReminderBackendEvent
} from "./timedReminderBackendEvents";

export type { ActivityCompact, ActivitySnapshot } from "./activityBackendEvents";
export type {
  TimedReminderCompact,
  TimedReminderSnapshot
} from "./timedReminderBackendEvents";
export { MAX_TIMED_REMINDER_CONTENT_LENGTH } from "./timedReminderBackendEvents";
export type {
  AudioDevicesChangeRequestedEvent,
  SettingsChangedPayload
} from "./applicationBackendEvents";

export type ConnectionStatus = "idle" | "connecting" | "connected" | "disconnected" | "failed";

export type BackendEvent =
  | InteractionBackendEvent
  | AssistantOutputBackendEvent
  | ActivityBackendEvent
  | TimedReminderBackendEvent
  | ApplicationBackendEvent;

type BackendEventParseFailureCode =
  | "invalid_envelope"
  | "unknown_event_type"
  | "invalid_payload";

export type BackendEventParseResult =
  | { ok: true; event: BackendEvent }
  | {
      ok: false;
      reasonCode: BackendEventParseFailureCode;
      eventType: string | null;
    };

const BACKEND_EVENT_PARSERS = {
  ...INTERACTION_EVENT_PARSERS,
  ...ASSISTANT_OUTPUT_EVENT_PARSERS,
  ...ACTIVITY_EVENT_PARSERS,
  ...TIMED_REMINDER_EVENT_PARSERS,
  ...APPLICATION_EVENT_PARSERS
} satisfies Record<CoreToUiEventName, BackendPayloadParser>;

/**
 * 解析一条 Backend 公共事件，并把协议失败收敛成稳定原因码。
 *
 * @param value WebSocket JSON 反序列化后的未知值。
 * @returns 成功时返回领域解析器确认后的事件，失败时返回脱敏分类。
 */
export function parseBackendEvent(value: unknown): BackendEventParseResult {
  if (!isRecord(value) || !hasExactKeys(value, ["type", "payload"])) {
    return { ok: false, reasonCode: "invalid_envelope", eventType: null };
  }
  if (typeof value.type !== "string") {
    return { ok: false, reasonCode: "invalid_envelope", eventType: null };
  }

  const eventType = value.type;
  if (!Object.hasOwn(BACKEND_EVENT_PARSERS, eventType)) {
    return { ok: false, reasonCode: "unknown_event_type", eventType };
  }

  const parsedPayload = BACKEND_EVENT_PARSERS[eventType as CoreToUiEventName](value.payload);
  if (parsedPayload === null) {
    return { ok: false, reasonCode: "invalid_payload", eventType };
  }

  return {
    ok: true,
    event: {
      type: eventType,
      payload: parsedPayload
    } as BackendEvent
  };
}
