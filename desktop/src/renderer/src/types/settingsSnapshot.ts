import type { ReminderSettings } from "./settingsPayloads";
import {
  hasExactKeys,
  isNonEmptyString,
  isRecord,
  isStringList
} from "./backendEventParserPrimitives";

type CharacterVoiceCaptionTiming = {
  ms_per_unit: number;
  calibration_text: string;
  duration_ms: number;
  unit_count: number;
  measured_at: number;
};

export type SettingsSnapshot = Required<ReminderSettings> & {
  settings_revision: number;
  user_self_intro: string;
  user_extra_notes: string;
  streamvox_device: "auto" | "cpu" | "gpu";
  streamvox_llama_backend: "cuda" | "vulkan" | null;
  speaker_verification_enabled: boolean;
  speaker_whitelist_audio_paths: string[];
  audio_device_mode: "system_default" | "manual";
  audio_input_device_id: string | null;
  audio_input_device_name: string | null;
  audio_output_reference_device_id: string | null;
  audio_output_reference_device_name: string | null;
  background_agent: {
    environment: Record<string, string>;
    model: string;
    effort_level: "low" | "medium" | "high" | "xhigh" | "max" | null;
    max_turns: number;
    permission_mode: "acceptEdits" | "bypassPermissions";
    /** 产品 Skill 的全局启停偏好；Desktop 只校验和透传，不负责修改。 */
    skills: Record<string, boolean>;
  };
};

const SETTINGS_KEYS = [
  "settings_revision",
  "show_speech_bubbles",
  "active_character_caption_timing",
  "followup_toggle_shortcut_enabled",
  "followup_toggle_shortcut",
  "avatar_visibility_shortcut_enabled",
  "avatar_visibility_shortcut",
  "selection_capture_shortcut_enabled",
  "selection_capture_shortcut",
  "selected_live2d_model_id",
  "user_self_intro",
  "user_extra_notes",
  "streamvox_device",
  "streamvox_llama_backend",
  "speaker_verification_enabled",
  "speaker_whitelist_audio_paths",
  "audio_device_mode",
  "audio_input_device_id",
  "audio_input_device_name",
  "audio_output_reference_device_id",
  "audio_output_reference_device_name",
  "background_agent"
] as const;

const BACKGROUND_AGENT_KEYS = [
  "environment",
  "model",
  "effort_level",
  "max_turns",
  "permission_mode",
  "skills"
] as const;

const FORBIDDEN_ENVIRONMENT_KEYS = new Set([
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_AUTH_TOKEN",
  "CLAUDE_CONFIG_DIR",
  "ANTHROPIC_MODEL",
  "CLAUDE_CODE_EFFORT_LEVEL",
  "CLAUDE_CODE_SUBAGENT_MODEL"
]);

/** 校验 HTTP 和 WebSocket 共用的完整设置快照。 */
export function isSettingsSnapshot(value: unknown): value is SettingsSnapshot {
  if (
    !isRecord(value)
    || !hasExactKeys(value, SETTINGS_KEYS)
    || !Number.isSafeInteger(value.settings_revision)
    || (value.settings_revision as number) < 0
    || typeof value.show_speech_bubbles !== "boolean"
    || !isCaptionTiming(value.active_character_caption_timing)
    || typeof value.followup_toggle_shortcut_enabled !== "boolean"
    || !isNonEmptyString(value.followup_toggle_shortcut)
    || typeof value.avatar_visibility_shortcut_enabled !== "boolean"
    || !isNonEmptyString(value.avatar_visibility_shortcut)
    || typeof value.selection_capture_shortcut_enabled !== "boolean"
    || !isNonEmptyString(value.selection_capture_shortcut)
    || !isNonEmptyString(value.selected_live2d_model_id)
    || typeof value.user_self_intro !== "string"
    || typeof value.user_extra_notes !== "string"
    || !["auto", "cpu", "gpu"].includes(String(value.streamvox_device))
    || ![null, "cuda", "vulkan"].includes(value.streamvox_llama_backend as null | string)
    || typeof value.speaker_verification_enabled !== "boolean"
    || !isStringList(value.speaker_whitelist_audio_paths)
    || !["system_default", "manual"].includes(String(value.audio_device_mode))
    || !isNullableString(value.audio_input_device_id)
    || !isNullableString(value.audio_input_device_name)
    || !isNullableString(value.audio_output_reference_device_id)
    || !isNullableString(value.audio_output_reference_device_name)
    || !isBackgroundAgent(value.background_agent)
  ) {
    return false;
  }
  return true;
}

/** 把完整快照投影为可编辑设置，避免 reducer 持有后端内部字段。 */
export function projectReminderSettings(snapshot: SettingsSnapshot): ReminderSettings {
  return {
    show_speech_bubbles: snapshot.show_speech_bubbles,
    active_character_caption_timing: snapshot.active_character_caption_timing,
    followup_toggle_shortcut_enabled: snapshot.followup_toggle_shortcut_enabled,
    followup_toggle_shortcut: snapshot.followup_toggle_shortcut,
    avatar_visibility_shortcut_enabled: snapshot.avatar_visibility_shortcut_enabled,
    avatar_visibility_shortcut: snapshot.avatar_visibility_shortcut,
    selection_capture_shortcut_enabled: snapshot.selection_capture_shortcut_enabled,
    selection_capture_shortcut: snapshot.selection_capture_shortcut,
    selected_live2d_model_id: snapshot.selected_live2d_model_id
  };
}

function isCaptionTiming(value: unknown): value is CharacterVoiceCaptionTiming | null {
  if (value === null) {
    return true;
  }
  return isRecord(value)
    && hasExactKeys(value, [
      "ms_per_unit", "calibration_text", "duration_ms", "unit_count", "measured_at"
    ])
    && isPositiveFiniteNumber(value.ms_per_unit)
    && isNonEmptyString(value.calibration_text)
    && isPositiveSafeInteger(value.duration_ms)
    && isPositiveFiniteNumber(value.unit_count)
    && isPositiveFiniteNumber(value.measured_at);
}

function isBackgroundAgent(value: unknown): boolean {
  if (
    !isRecord(value)
    || !hasExactKeys(value, BACKGROUND_AGENT_KEYS)
    || !isRecord(value.environment)
    || typeof value.model !== "string"
    || ![null, "low", "medium", "high", "xhigh", "max"].includes(value.effort_level as null | string)
    || !Number.isSafeInteger(value.max_turns)
    || (value.max_turns as number) < 0
    || (value.max_turns as number) > 200
    || !["acceptEdits", "bypassPermissions"].includes(String(value.permission_mode))
    || !isSkillPreferences(value.skills)
  ) {
    return false;
  }
  return Object.entries(value.environment).every(([key, environmentValue]) => (
    key === key.toUpperCase()
    && (key.startsWith("ANTHROPIC_") || key.startsWith("CLAUDE_CODE_"))
    && !FORBIDDEN_ENVIRONMENT_KEYS.has(key)
    && typeof environmentValue === "string"
  ));
}

/**
 * 校验 Desktop 只读接收的产品 Skill 启停偏好。
 *
 * Args:
 *   value: Backend 完整设置快照中的 skills 字段。
 *
 * Returns:
 *   每个键都是稳定 Skill ID 且每个值都是真实布尔值时返回 true。
 */
function isSkillPreferences(value: unknown): value is Record<string, boolean> {
  return isRecord(value) && Object.entries(value).every(([skillId, enabled]) => (
    /^[a-z0-9][a-z0-9._-]*$/.test(skillId) && typeof enabled === "boolean"
  ));
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isPositiveSafeInteger(value: unknown): boolean {
  return Number.isSafeInteger(value) && (value as number) > 0;
}

function isPositiveFiniteNumber(value: unknown): boolean {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}
