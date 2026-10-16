import { isPlainObject, isStringArray } from "./validation";

type WakeResponseBucketId = "morning" | "afternoon" | "evening" | "night";

export type WakeResponseItem = {
  id: string;
  text: string;
  audio_path?: string | null;
  audio_url?: string | null;
  synthesized_at?: number | null;
};

export type WakeResponseConfig = {
  buckets: Record<WakeResponseBucketId, WakeResponseItem[]>;
};

export type WakeResponseGeneratedTexts = {
  buckets: Record<WakeResponseBucketId, string[]>;
};

export function isWakeResponseConfig(payload: unknown): payload is WakeResponseConfig {
  if (!isPlainObject(payload) || !isPlainObject(payload.buckets)) {
    return false;
  }
  return (
    isWakeResponseItemArray(payload.buckets.morning) &&
    isWakeResponseItemArray(payload.buckets.afternoon) &&
    isWakeResponseItemArray(payload.buckets.evening) &&
    isWakeResponseItemArray(payload.buckets.night)
  );
}

function isWakeResponseItemArray(payload: unknown): payload is WakeResponseItem[] {
  return Array.isArray(payload) && payload.every(isWakeResponseItem);
}

function isWakeResponseItem(payload: unknown): payload is WakeResponseItem {
  if (!isPlainObject(payload)) {
    return false;
  }
  return (
    typeof payload.id === "string" &&
    typeof payload.text === "string" &&
    (payload.audio_path === undefined || payload.audio_path === null || typeof payload.audio_path === "string") &&
    (payload.audio_url === undefined || payload.audio_url === null || typeof payload.audio_url === "string") &&
    (payload.synthesized_at === undefined ||
      payload.synthesized_at === null ||
      typeof payload.synthesized_at === "number")
  );
}

export function isWakeResponseGeneratedTexts(payload: unknown): payload is WakeResponseGeneratedTexts {
  if (!isPlainObject(payload) || !isPlainObject(payload.buckets)) {
    return false;
  }
  return (
    isStringArray(payload.buckets.morning) &&
    isStringArray(payload.buckets.afternoon) &&
    isStringArray(payload.buckets.evening) &&
    isStringArray(payload.buckets.night)
  );
}
