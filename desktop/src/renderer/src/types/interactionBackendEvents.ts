import { isStandardUuid } from "../../../shared/uuid";
import type { CoreToUiEventNameByDomain } from "./eventCatalog";
import {
  hasExactKeys,
  hasOnlyKeys,
  isNonEmptyString,
  isRecord,
  preservePayloadWhen,
  type BackendPayloadParser
} from "./backendEventParserPrimitives";

type FollowupState = "followup_cool" | "followup_hot";
type TurnState = "idle" | "listening" | "processing" | "speaking";

export type InputRuntimeIdentity = {
  input_runtime_id: string;
  input_runtime_epoch: number;
};

/**
 * 保存同一时刻的跟进模式、轮次状态和交互会话，避免分别接收状态时拼出过期组合。
 *
 * @property input_runtime_id 产生快照的输入 Runtime UUID 身份。
 * @property input_runtime_epoch 当前 Backend 进程内递增的输入 Runtime 世代。
 * @property revision 用于接纳最新状态的非负安全整数版本。
 * @property followup_state 当前跟进模式的冷热状态。
 * @property turn_state 当前轮次的输入或回应状态。
 * @property session_id 当前交互会话的非空身份。
 * @property source 用户手动切换跟进模式时标记为 manual_toggle。
 */
export type InteractionStateSnapshot = InputRuntimeIdentity & {
  revision: number;
  followup_state: FollowupState;
  turn_state: TurnState;
  session_id: string;
  source?: "manual_toggle";
};

type InputTurnIdentity = InputRuntimeIdentity & {
  turn_id: string;
  turn_sequence: number;
};

type FollowupStateChangedEvent = {
  type: "followup.state_changed";
  payload: {
    followup_state: FollowupState;
    input_runtime_id?: string;
    input_runtime_epoch?: number;
    source?: "manual_toggle";
  };
};

type InteractionStateChangedEvent = {
  type: "interaction.state_changed";
  payload: InteractionStateSnapshot;
};

type InputRuntimeChangedEvent = {
  type: "input.runtime_changed";
  payload: InputRuntimeIdentity;
};

type TurnStateChangedEvent = {
  type: "turn.state_changed";
  payload: InputRuntimeIdentity & {
    turn_state: TurnState;
    session_id: string;
    turn_id?: string;
    turn_sequence?: number;
    reason?: string;
    generation_id?: string;
  };
};

type WakeDetectedEvent = {
  type: "wake.detected";
  payload: InputRuntimeIdentity & {
    session_id: string;
    text?: string;
    detected_at?: number;
  };
};

type EnvironmentSnapshotRequestedEvent = {
  type: "environment.snapshot_requested";
  payload: InputTurnIdentity & {
    request_id: string;
    reason: "voice_turn_started";
    session_id: string;
    created_at: string;
  };
};

type SmartTurnWaitingUserStartedEvent = {
  type: "input.smart_turn_waiting_user_started";
  payload: InputTurnIdentity & {
    session_id: string;
    probability: number;
    hint_text: string;
  };
};

type SmartTurnWaitingUserEndedEvent = {
  type: "input.smart_turn_waiting_user_ended";
  payload: InputTurnIdentity & {
    session_id: string;
    probability: number;
    reason: string;
  };
};

type UserPartialTranscriptEvent = {
  type: "user.partial_transcript";
  payload: InputTurnIdentity & {
    session_id: string;
    text: string;
  };
};

type TranscriptReference = {
  display_name: string;
  text: string;
};

type FinalTranscriptHistoryDisposition =
  | {
      history_disposition: "ordinary_turn";
      episode_id?: never;
    }
  | {
      history_disposition: "unattended_episode";
      episode_id: string;
    };

type VoiceFinalTranscriptPayload = InputTurnIdentity & FinalTranscriptHistoryDisposition & {
  session_id: string;
  request_id?: string;
  submission_id?: never;
  text: string;
  references?: TranscriptReference[];
};

type DesktopFinalTranscriptPayload = FinalTranscriptHistoryDisposition & {
  session_id: "desktop-text";
  turn_id: null;
  submission_id: string;
  request_id?: string;
  text: string;
  references?: TranscriptReference[];
  input_runtime_id?: never;
  input_runtime_epoch?: never;
  turn_sequence?: never;
};

type UserFinalTranscriptEvent = {
  type: "user.final_transcript";
  payload: VoiceFinalTranscriptPayload | DesktopFinalTranscriptPayload;
};

type SelfReflectionEpisodeHistoryItem = {
  role: "user" | "assistant";
  content: string;
  occurred_at: string;
  episode_role: "input" | "interruption_reply" | "closure";
  source_identity: string | null;
  reply_settlement: "completed" | "interrupted" | "failed" | null;
};

type SelfReflectionEpisodeCommittedEvent = {
  type: "self_reflection.episode_committed";
  payload: {
    self_reflection_id: string;
    episode_id: string;
    runtime_epoch: number;
    revision: number;
    actionable: false;
    history_block: SelfReflectionEpisodeHistoryItem[];
  };
};

type BargeInDetectedEvent = {
  type: "barge_in.detected";
  payload: InputRuntimeIdentity & {
    session_id: string;
    generation_id?: string;
  };
};

export type InteractionBackendEvent =
  | FollowupStateChangedEvent
  | InteractionStateChangedEvent
  | InputRuntimeChangedEvent
  | TurnStateChangedEvent
  | WakeDetectedEvent
  | EnvironmentSnapshotRequestedEvent
  | SmartTurnWaitingUserStartedEvent
  | SmartTurnWaitingUserEndedEvent
  | UserPartialTranscriptEvent
  | UserFinalTranscriptEvent
  | SelfReflectionEpisodeCommittedEvent
  | BargeInDetectedEvent;

type InteractionEventName = CoreToUiEventNameByDomain<"interaction">;

export const INTERACTION_EVENT_PARSERS = {
  "followup.state_changed": payload => preservePayloadWhen(payload, isFollowupStateChangedPayload),
  "interaction.state_changed": payload => preservePayloadWhen(payload, isInteractionStateSnapshot),
  "input.runtime_changed": payload => preservePayloadWhen(payload, isInputRuntimeIdentity),
  "wake.detected": payload => preservePayloadWhen(payload, isWakeDetectedPayload),
  "environment.snapshot_requested": payload => preservePayloadWhen(
    payload,
    isEnvironmentSnapshotRequestedPayload
  ),
  "turn.state_changed": payload => preservePayloadWhen(payload, isTurnStateChangedPayload),
  "input.smart_turn_waiting_user_started": payload => preservePayloadWhen(
    payload,
    isSmartTurnWaitingUserStartedPayload
  ),
  "input.smart_turn_waiting_user_ended": payload => preservePayloadWhen(
    payload,
    isSmartTurnWaitingUserEndedPayload
  ),
  "user.partial_transcript": payload => preservePayloadWhen(
    payload,
    isUserPartialTranscriptPayload
  ),
  "user.final_transcript": payload => preservePayloadWhen(payload, isUserFinalTranscriptPayload),
  "self_reflection.episode_committed": payload => preservePayloadWhen(
    payload,
    isSelfReflectionEpisodeCommittedPayload
  ),
  "barge_in.detected": payload => preservePayloadWhen(payload, isBargeInDetectedPayload)
} satisfies Record<InteractionEventName, BackendPayloadParser>;

/** 校验输入 Runtime 身份，事件排序只允许使用数值 epoch。 */
function isInputRuntimeIdentity(payload: unknown): payload is InputRuntimeIdentity {
  return isRecord(payload)
    && hasExactKeys(payload, ["input_runtime_id", "input_runtime_epoch"])
    && hasInputRuntimeIdentity(payload);
}

/** 判断对象是否携带完整且合法的输入 Runtime 身份。 */
function hasInputRuntimeIdentity(payload: Record<string, unknown>): boolean {
  return isInputRuntimeId(payload.input_runtime_id)
    && Number.isSafeInteger(payload.input_runtime_epoch)
    && (payload.input_runtime_epoch as number) > 0;
}

/** 判断对象是否携带完整且合法的输入 Turn 身份。 */
function hasInputTurnIdentity(payload: Record<string, unknown>): boolean {
  return hasInputRuntimeIdentity(payload)
    && isNonEmptyString(payload.turn_id)
    && Number.isSafeInteger(payload.turn_sequence)
    && (payload.turn_sequence as number) > 0;
}

/** 校验进程内唯一的输入 Runtime UUID 身份。 */
function isInputRuntimeId(value: unknown): value is string {
  if (typeof value !== "string" || !value.startsWith("input-runtime-")) {
    return false;
  }
  return isStandardUuid(value.slice("input-runtime-".length));
}

/** 校验追问冷热状态及其可选 Runtime 关联。 */
function isFollowupStateChangedPayload(payload: unknown): boolean {
  if (!isRecord(payload) || !hasOnlyKeys(payload, [
    "followup_state", "input_runtime_id", "input_runtime_epoch", "source"
  ])) {
    return false;
  }
  if (payload.followup_state !== "followup_cool" && payload.followup_state !== "followup_hot") {
    return false;
  }
  const hasRuntimeId = payload.input_runtime_id !== undefined;
  const hasRuntimeEpoch = payload.input_runtime_epoch !== undefined;
  return hasRuntimeId === hasRuntimeEpoch
    && (!hasRuntimeId || hasInputRuntimeIdentity(payload))
    && (payload.source === undefined || payload.source === "manual_toggle");
}

/**
 * 校验完整交互快照，状态与版本必须一起合法才能交给消费者。
 *
 * @param payload 尚未验证的公共事件载荷。
 * @returns 字段闭合、输入 Runtime 身份和全部状态合法时返回真。
 */
function isInteractionStateSnapshot(payload: unknown): payload is InteractionStateSnapshot {
  if (!isRecord(payload) || !hasOnlyKeys(payload, [
    "input_runtime_id", "input_runtime_epoch", "revision", "followup_state",
    "turn_state", "session_id", "source"
  ])) {
    return false;
  }

  // 每个状态都带完整身份与版本，避免把迟到快照当成当前交互现场。
  return hasInputRuntimeIdentity(payload)
    && Number.isSafeInteger(payload.revision)
    && (payload.revision as number) >= 0
    && (payload.followup_state === "followup_cool" || payload.followup_state === "followup_hot")
    && (
      payload.turn_state === "idle"
      || payload.turn_state === "listening"
      || payload.turn_state === "processing"
      || payload.turn_state === "speaking"
    )
    && isNonEmptyString(payload.session_id)
    && (payload.source === undefined || payload.source === "manual_toggle");
}

/** 校验唤醒事件；唤醒词原文不属于公共载荷。 */
function isWakeDetectedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasOnlyKeys(payload, ["input_runtime_id", "input_runtime_epoch", "session_id", "text", "detected_at"])
    && hasInputRuntimeIdentity(payload)
    && isNonEmptyString(payload.session_id)
    && (payload.text === undefined || typeof payload.text === "string")
    && (payload.detected_at === undefined || (
      typeof payload.detected_at === "number" && Number.isFinite(payload.detected_at) && payload.detected_at >= 0
    ));
}

/** 校验语音 Turn 请求环境快照时的完整输入身份。 */
function isEnvironmentSnapshotRequestedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, [
      "input_runtime_id", "input_runtime_epoch", "turn_id", "turn_sequence",
      "request_id", "reason", "session_id", "created_at"
    ])
    && hasInputTurnIdentity(payload)
    && isNonEmptyString(payload.request_id)
    && payload.reason === "voice_turn_started"
    && isNonEmptyString(payload.session_id)
    && isNonEmptyString(payload.created_at);
}

/** 校验 Turn 状态；turn_id 与 turn_sequence 必须同时出现或同时缺失。 */
function isTurnStateChangedPayload(payload: unknown): boolean {
  if (!isRecord(payload) || !hasOnlyKeys(payload, [
    "input_runtime_id", "input_runtime_epoch", "session_id", "turn_state",
    "turn_id", "turn_sequence", "reason", "generation_id"
  ])) {
    return false;
  }
  const hasTurnId = payload.turn_id !== undefined;
  const hasTurnSequence = payload.turn_sequence !== undefined;
  return hasInputRuntimeIdentity(payload)
    && isNonEmptyString(payload.session_id)
    && ["idle", "listening", "processing", "speaking"].includes(String(payload.turn_state))
    && hasTurnId === hasTurnSequence
    && (!hasTurnId || hasInputTurnIdentity(payload))
    && (payload.reason === undefined || isNonEmptyString(payload.reason))
    && (payload.generation_id === undefined || isNonEmptyString(payload.generation_id));
}

/** 校验 Smart Turn 开始提示及其所属输入 Turn。 */
function isSmartTurnWaitingUserStartedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, [
      "input_runtime_id", "input_runtime_epoch", "turn_id", "turn_sequence",
      "session_id", "probability", "hint_text"
    ])
    && hasInputTurnIdentity(payload)
    && isNonEmptyString(payload.session_id)
    && typeof payload.probability === "number"
    && Number.isFinite(payload.probability)
    && isNonEmptyString(payload.hint_text);
}

/** 校验 Smart Turn 结束提示及其所属输入 Turn。 */
function isSmartTurnWaitingUserEndedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, [
      "input_runtime_id", "input_runtime_epoch", "turn_id", "turn_sequence",
      "session_id", "probability", "reason"
    ])
    && hasInputTurnIdentity(payload)
    && isNonEmptyString(payload.session_id)
    && typeof payload.probability === "number"
    && Number.isFinite(payload.probability)
    && isNonEmptyString(payload.reason);
}

/** 校验实时用户转写，空白正文不能进入头像气泡。 */
function isUserPartialTranscriptPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, [
      "input_runtime_id", "input_runtime_epoch", "turn_id", "turn_sequence",
      "session_id", "text"
    ])
    && hasInputTurnIdentity(payload)
    && isNonEmptyString(payload.session_id)
    && isNonEmptyString(payload.text);
}

/** 校验 Voice Final 与 Desktop 文本提交两种互斥身份。 */
function isUserFinalTranscriptPayload(payload: unknown): boolean {
  if (!isRecord(payload) || !hasOnlyKeys(payload, [
    "input_runtime_id", "input_runtime_epoch", "turn_id", "turn_sequence",
    "session_id", "request_id", "submission_id", "text", "references",
    "history_disposition", "episode_id"
  ]) || !isNonEmptyString(payload.text)) {
    return false;
  }

  const isDesktopText = payload.submission_id !== undefined;
  if (isDesktopText && (
    !isStandardUuid(payload.submission_id as string)
    || payload.session_id !== "desktop-text"
    || payload.turn_id !== null
    || payload.input_runtime_id !== undefined
    || payload.input_runtime_epoch !== undefined
    || payload.turn_sequence !== undefined
  )) {
    return false;
  }
  if (!isDesktopText && (!hasInputTurnIdentity(payload) || !isNonEmptyString(payload.session_id))) {
    return false;
  }
  if (payload.request_id !== undefined && !isNonEmptyString(payload.request_id)) {
    return false;
  }
  if (!hasValidHistoryDisposition(payload)) {
    return false;
  }
  return payload.references === undefined || (
    Array.isArray(payload.references)
    && payload.references.every(isTranscriptReference)
  );
}

/** 校验 Final 是立即入历史，还是只作为当前 episode 临时显示。 */
function hasValidHistoryDisposition(payload: Record<string, unknown>): boolean {
  if (payload.history_disposition === "ordinary_turn") {
    return payload.episode_id === undefined;
  }
  return payload.history_disposition === "unattended_episode"
    && isNonEmptyString(payload.episode_id);
}

/** 校验 Self-Reflection 已闭合 episode 的原子历史块。 */
function isSelfReflectionEpisodeCommittedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, [
      "self_reflection_id", "episode_id", "runtime_epoch", "revision",
      "actionable", "history_block"
    ])
    && isNonEmptyString(payload.self_reflection_id)
    && isNonEmptyString(payload.episode_id)
    && Number.isSafeInteger(payload.runtime_epoch)
    && (payload.runtime_epoch as number) > 0
    && Number.isSafeInteger(payload.revision)
    && (payload.revision as number) > 0
    && payload.actionable === false
    && Array.isArray(payload.history_block)
    && payload.history_block.every(isSelfReflectionEpisodeHistoryItem);
}

/** 校验 episode 历史项的角色、来源身份和回应结算互相匹配。 */
function isSelfReflectionEpisodeHistoryItem(value: unknown): boolean {
  if (
    !isRecord(value)
    || !hasExactKeys(value, [
      "role", "content", "occurred_at", "episode_role",
      "source_identity", "reply_settlement"
    ])
    || !["user", "assistant"].includes(String(value.role))
    || !isNonEmptyString(value.content)
    || !isNonEmptyString(value.occurred_at)
    || !Number.isFinite(Date.parse(value.occurred_at as string))
  ) {
    return false;
  }
  if (value.episode_role === "input") {
    return value.role === "user"
      && isNonEmptyString(value.source_identity)
      && value.reply_settlement === null;
  }
  if (value.episode_role === "interruption_reply") {
    return value.role === "assistant"
      && isNonEmptyString(value.source_identity)
      && ["completed", "interrupted", "failed"].includes(
        String(value.reply_settlement)
      );
  }
  return value.episode_role === "closure"
    && value.role === "assistant"
    && value.source_identity === null
    && value.reply_settlement === null;
}

/** 校验一条用户消息材料引用。 */
function isTranscriptReference(value: unknown): boolean {
  return isRecord(value)
    && hasExactKeys(value, ["display_name", "text"])
    && isNonEmptyString(value.display_name)
    && typeof value.text === "string";
}

/** 校验插话事件及其输入 Runtime 所有权。 */
function isBargeInDetectedPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasOnlyKeys(payload, [
      "input_runtime_id", "input_runtime_epoch", "session_id", "generation_id"
    ])
    && hasInputRuntimeIdentity(payload)
    && isNonEmptyString(payload.session_id)
    && (payload.generation_id === undefined || isNonEmptyString(payload.generation_id));
}
