import { isNullableString, isPlainObject } from "./validation";
import type { ConsoleSettings } from "./settings";

type AudioDeviceStatus = "available" | "format_incompatible" | "permission_or_busy" | "unavailable" | "unknown";

export type AudioDevice = {
  id: string | null;
  name: string;
  is_default: boolean;
  status: AudioDeviceStatus;
  error_type: string | null;
  error_message: string | null;
};

export type AudioDeviceSelectionPayload = {
  mode: "system_default" | "manual";
  microphone_id: string | null;
  microphone_name: string | null;
  speaker_loopback_id: string | null;
  speaker_loopback_name: string | null;
};

export type AudioDeviceSettingsUpdateResult = {
  accepted: boolean;
  request_id: string;
  /** 保存音频设备后推进的全局设置快照；旧 Backend 可暂时省略此字段。 */
  settings?: ConsoleSettings;
};

export type AudioDeviceApplyResult = {
  request_id: string;
  status: "applied" | "failed";
  mode: "system_default" | "manual";
  reason: string | null;
  applied_at: number;
};

export type AudioDevicesPayload = {
  microphones: AudioDevice[];
  speaker_loopbacks: AudioDevice[];
  selected: AudioDeviceSelectionPayload;
};

export type AudioDeviceTestPayload = {
  microphone_id?: string | null;
  microphone_name?: string | null;
};

type AudioDeviceLevelStatus = "silent" | "low" | "good" | "clipping";

export type AudioDeviceTestResult = {
  status: AudioDeviceStatus;
  peak_level: number;
  rms_level: number;
  duration_ms: number;
  sample_rate: number;
  level_status: AudioDeviceLevelStatus;
  audio_wav_b64: string | null;
  error_type: string | null;
  error_message: string | null;
};

export function isAudioDevicesPayload(payload: unknown): payload is AudioDevicesPayload {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    Array.isArray(payload.microphones) &&
    payload.microphones.every(isAudioDevice) &&
    Array.isArray(payload.speaker_loopbacks) &&
    payload.speaker_loopbacks.every(isAudioDevice) &&
    isAudioDeviceSelectionPayload(payload.selected)
  );
}

function isAudioDevice(payload: unknown): payload is AudioDevice {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    isNullableString(payload.id) &&
    typeof payload.name === "string" &&
    typeof payload.is_default === "boolean" &&
    isAudioDeviceStatus(payload.status) &&
    isNullableString(payload.error_type) &&
    isNullableString(payload.error_message)
  );
}

function isAudioDeviceSelectionPayload(payload: unknown): payload is AudioDeviceSelectionPayload {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    isAudioDeviceMode(payload.mode) &&
    isNullableString(payload.microphone_id) &&
    isNullableString(payload.microphone_name) &&
    isNullableString(payload.speaker_loopback_id) &&
    isNullableString(payload.speaker_loopback_name)
  );
}

export function isAudioDeviceSettingsUpdateResult(payload: unknown): payload is AudioDeviceSettingsUpdateResult {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.accepted === "boolean" &&
    typeof payload.request_id === "string" &&
    payload.request_id.trim().length > 0 &&
    (payload.settings === undefined || isPlainObject(payload.settings))
  );
}

export function isAudioDeviceTestResult(payload: unknown): payload is AudioDeviceTestResult {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    isAudioDeviceStatus(payload.status) &&
    typeof payload.peak_level === "number" &&
    typeof payload.rms_level === "number" &&
    typeof payload.duration_ms === "number" &&
    typeof payload.sample_rate === "number" &&
    isAudioDeviceLevelStatus(payload.level_status) &&
    isNullableString(payload.audio_wav_b64) &&
    isNullableString(payload.error_type) &&
    isNullableString(payload.error_message)
  );
}

export function isAudioDeviceMode(value: unknown): value is "system_default" | "manual" {
  return value === "system_default" || value === "manual";
}

function isAudioDeviceStatus(value: unknown): value is AudioDeviceStatus {
  return (
    value === "available" ||
    value === "format_incompatible" ||
    value === "permission_or_busy" ||
    value === "unavailable" ||
    value === "unknown"
  );
}

function isAudioDeviceLevelStatus(value: unknown): value is AudioDeviceLevelStatus {
  return value === "silent" || value === "low" || value === "good" || value === "clipping";
}
