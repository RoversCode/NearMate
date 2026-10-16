import type { CoreToUiEventNameByDomain } from "./eventCatalog";
import {
  hasExactKeys,
  hasOnlyKeys,
  isNonEmptyString,
  isRecord,
  preservePayloadWhen,
  type BackendPayloadParser
} from "./backendEventParserPrimitives";

type AuxiliaryReplyIdentity =
  | {
      request_source?: never;
      self_reflection_id?: never;
      episode_id?: never;
    }
  | {
      request_source: "self_reflection_interruption_reply";
      self_reflection_id: string;
      episode_id: string;
    };

type AssistantTextPayload = AuxiliaryReplyIdentity & {
  request_id: string;
  session_id: string;
  generation_id: string;
  text: string;
};

type AssistantStreamingTextEvent = {
  type: "assistant.text_chunk" | "assistant.text_segment_final";
  payload: AssistantTextPayload;
};

type AssistantTextFinalEvent = {
  type: "assistant.text_final";
  payload: AssistantTextPayload & (
    | { notice_delivery_failed?: never; notice_failure_reason?: never }
    | {
        notice_delivery_failed: true;
        notice_failure_reason?: "provider_protocol_leak";
      }
  );
};

type AssistantRequestFailedEvent = {
  type: "assistant.request_failed";
  payload: {
    request_id: string;
    code: "request_start_failed" | "output_unavailable" | "request_identity_conflict";
    message: string;
  };
};

type AssistantSelfReflectionStatusChangedEvent = {
  type: "assistant.self_reflection_status_changed";
  payload: {
    request_id: string;
    session_id: string;
    generation_id: string;
    self_reflection_id: string;
    episode_id: string;
    revision: number;
    active: boolean;
  };
};

type AssistantOutputStatusChangedEvent = {
  type: "assistant.output_status_changed";
  payload:
    | { available: true; code: null; message: null }
    | {
        available: false;
        code: "output_unavailable" | "request_identity_conflict";
        message: string;
      };
};

type AssistantAudioFinishedEvent = {
  type: "assistant.audio_finished";
  payload: AuxiliaryReplyIdentity & {
    request_id: string;
    session_id: string;
    generation_id: string;
  };
};

type AssistantPlayoutDeliveryReadyEvent = {
  type: "assistant.playout_delivery_ready";
  payload: {
    delivery_id: string;
    request_id: string;
    generation_id: string;
  };
};

type AssistantPlayoutDeliveryFailedEvent = {
  type: "assistant.playout_delivery_failed";
  payload: {
    delivery_id: string;
    request_id: string;
    generation_id: string;
    client_instance_id: string | null;
    code: string;
    message: string;
  };
};

export type AssistantOutputBackendEvent =
  | AssistantStreamingTextEvent
  | AssistantTextFinalEvent
  | AssistantRequestFailedEvent
  | AssistantSelfReflectionStatusChangedEvent
  | AssistantOutputStatusChangedEvent
  | AssistantAudioFinishedEvent
  | AssistantPlayoutDeliveryReadyEvent
  | AssistantPlayoutDeliveryFailedEvent;

type AssistantOutputEventName = CoreToUiEventNameByDomain<"assistant_output">;

export const ASSISTANT_OUTPUT_EVENT_PARSERS = {
  "assistant.text_chunk": payload => preservePayloadWhen(payload, isAssistantTextPayload),
  "assistant.text_segment_final": payload => preservePayloadWhen(payload, isAssistantTextPayload),
  "assistant.text_final": payload => preservePayloadWhen(payload, isAssistantTextFinalPayload),
  "assistant.request_failed": payload => preservePayloadWhen(
    payload,
    isAssistantRequestFailedPayload
  ),
  "assistant.self_reflection_status_changed": payload => preservePayloadWhen(
    payload,
    isAssistantSelfReflectionStatusChangedPayload
  ),
  "assistant.output_status_changed": payload => preservePayloadWhen(
    payload,
    isAssistantOutputStatusChangedPayload
  ),
  "assistant.audio_finished": payload => preservePayloadWhen(
    payload,
    isAssistantAudioFinishedPayload
  ),
  "assistant.playout_delivery_ready": payload => preservePayloadWhen(
    payload,
    value => isAssistantPlayoutDeliveryPayload("assistant.playout_delivery_ready", value)
  ),
  "assistant.playout_delivery_failed": payload => preservePayloadWhen(
    payload,
    value => isAssistantPlayoutDeliveryPayload("assistant.playout_delivery_failed", value)
  )
} satisfies Record<AssistantOutputEventName, BackendPayloadParser>;

/** 校验助手可见正文，空白文本不能进入 UI 或字幕链路。 */
function isAssistantTextPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasOnlyKeys(payload, [
      "request_id", "session_id", "generation_id", "text",
      "request_source", "self_reflection_id", "episode_id"
    ])
    && hasValidAssistantTextFields(payload);
}

/** 校验 Final 专属的 Notice 交付失败标记，且不把该字段放宽到流式正文。 */
function isAssistantTextFinalPayload(payload: unknown): boolean {
  if (
    !isRecord(payload)
    || !hasOnlyKeys(payload, [
      "request_id",
      "session_id",
      "generation_id",
      "text",
      "notice_delivery_failed",
      "notice_failure_reason",
      "request_source",
      "self_reflection_id",
      "episode_id"
    ])
    || typeof payload.text !== "string"
    || !hasValidAssistantTextIdentityFields(payload)
    || !hasValidAuxiliaryReplyIdentity(payload)
  ) {
    return false;
  }
  if (payload.notice_delivery_failed === undefined) {
    return payload.notice_failure_reason === undefined && isNonEmptyString(payload.text);
  }
  if (payload.notice_delivery_failed !== true) {
    return false;
  }
  if (payload.notice_failure_reason === undefined) {
    return payload.text === "" || isNonEmptyString(payload.text);
  }
  return payload.notice_failure_reason === "provider_protocol_leak"
    && payload.text === "";
}

/** 校验三类助手正文共用的身份和非空文本字段。 */
function hasValidAssistantTextFields(payload: Record<string, unknown>): boolean {
  return isNonEmptyString(payload.text)
    && hasValidAssistantTextIdentityFields(payload)
    && hasValidAuxiliaryReplyIdentity(payload);
}

/** 校验助手正文事件共用的可选执行身份。 */
function hasValidAssistantTextIdentityFields(payload: Record<string, unknown>): boolean {
  return isNonEmptyString(payload.request_id)
    && isNonEmptyString(payload.session_id)
    && isNonEmptyString(payload.generation_id);
}

/** 校验助手请求失败的稳定错误码和用户文案。 */
function isAssistantRequestFailedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, ["request_id", "code", "message"])
    && isNonEmptyString(payload.request_id)
    && [
      "request_start_failed",
      "output_unavailable",
      "request_identity_conflict"
    ].includes(String(payload.code))
    && isNonEmptyString(payload.message);
}

/** 校验 Self-Reflection 临时状态的 generation 身份和布尔状态。 */
function isAssistantSelfReflectionStatusChangedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, [
      "request_id", "session_id", "generation_id", "self_reflection_id",
      "episode_id", "revision", "active"
    ])
    && isNonEmptyString(payload.request_id)
    && isNonEmptyString(payload.session_id)
    && isNonEmptyString(payload.generation_id)
    && isNonEmptyString(payload.self_reflection_id)
    && isNonEmptyString(payload.episode_id)
    && Number.isSafeInteger(payload.revision)
    && (payload.revision as number) > 0
    && typeof payload.active === "boolean";
}

/** 校验助手输出健康快照，禁止内部异常字段混入公共协议。 */
function isAssistantOutputStatusChangedPayload(payload: unknown): boolean {
  if (!isRecord(payload) || !hasExactKeys(payload, ["available", "code", "message"])) {
    return false;
  }
  if (payload.available === true) {
    return payload.code === null && payload.message === null;
  }
  return payload.available === false
    && ["output_unavailable", "request_identity_conflict"].includes(String(payload.code))
    && isNonEmptyString(payload.message);
}

/** 校验助手音频终态及其输出代际。 */
function isAssistantAudioFinishedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasOnlyKeys(payload, [
      "request_id", "session_id", "generation_id",
      "request_source", "self_reflection_id", "episode_id"
    ])
    && isNonEmptyString(payload.request_id)
    && isNonEmptyString(payload.session_id)
    && isNonEmptyString(payload.generation_id)
    && hasValidAuxiliaryReplyIdentity(payload);
}

/** 校验辅助回应的三个公共归属字段必须同时出现，普通输出则全部省略。 */
function hasValidAuxiliaryReplyIdentity(payload: Record<string, unknown>): boolean {
  const hasRequestSource = payload.request_source !== undefined;
  const hasReflectionId = payload.self_reflection_id !== undefined;
  const hasEpisodeId = payload.episode_id !== undefined;
  if (!hasRequestSource && !hasReflectionId && !hasEpisodeId) {
    return true;
  }
  return payload.request_source === "self_reflection_interruption_reply"
    && isNonEmptyString(payload.self_reflection_id)
    && isNonEmptyString(payload.episode_id);
}

/** 校验助手语音交付的准备和失败两种公共载荷。 */
function isAssistantPlayoutDeliveryPayload(type: string, payload: unknown): boolean {
  if (!isRecord(payload)) {
    return false;
  }
  const identityKeys = ["delivery_id", "request_id", "generation_id"];
  const expectedKeys = type === "assistant.playout_delivery_ready"
    ? identityKeys
    : [...identityKeys, "client_instance_id", "code", "message"];
  if (
    !hasExactKeys(payload, expectedKeys)
    || !identityKeys.every(key => isNonEmptyString(payload[key]))
  ) {
    return false;
  }
  if (type === "assistant.playout_delivery_ready") {
    return true;
  }
  return (payload.client_instance_id === null || typeof payload.client_instance_id === "string")
    && isNonEmptyString(payload.code)
    && isNonEmptyString(payload.message);
}
