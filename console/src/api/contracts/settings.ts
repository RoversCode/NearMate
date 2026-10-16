import { type CharacterVoiceCaptionTiming, isCharacterVoiceCaptionTiming } from "./characterCards";
import { isAudioDeviceMode } from "./audioDevices";
import { isNonNegativeSafeInteger, isNullableString } from "./validation";

/**
 * 描述控制台从 Backend 设置快照中消费的字段投影。
 *
 * 核心字段：气泡开关控制本地提醒行为，selected_live2d_model_id 保留后端当前人物模型选择，
 * user_* 字段保存用户在控制台主动填写的稳定用户资料，角色体验配置由角色卡接口管理。
 * 预期用途：设置页修改普通设置时保留投影内的其它字段；后台工作与 Skill 偏好由各自接口管理。
 * 边界异常：读取时缺少任一投影字段会被 isConsoleSettings 拒绝；Backend 响应中的额外领域字段会被忽略。
 */
export type ConsoleSettings = {
  settings_revision: number;
  show_speech_bubbles: boolean;
  active_character_caption_timing: CharacterVoiceCaptionTiming | null;
  selected_live2d_model_id: string;
  user_self_intro: string;
  user_extra_notes: string;
  streamvox_device: string;
  streamvox_llama_backend: string | null;
  speaker_verification_enabled: boolean;
  speaker_whitelist_audio_paths: string[];
  followup_toggle_shortcut_enabled: boolean;
  followup_toggle_shortcut: string;
  avatar_visibility_shortcut_enabled: boolean;
  avatar_visibility_shortcut: string;
  selection_capture_shortcut_enabled: boolean;
  selection_capture_shortcut: string;
  audio_device_mode: "system_default" | "manual";
  audio_input_device_id: string | null;
  audio_input_device_name: string | null;
  audio_output_reference_device_id: string | null;
  audio_output_reference_device_name: string | null;
};

/**
 * 判断未知响应是否符合控制台设置契约。
 *
 * Args:
 *   payload: JSON 解析后的未知值。
 *
 * Returns:
 *   Console 消费的投影字段完整且合法时返回 true；不要求 Backend 响应只能包含这些字段。
 */
export function isConsoleSettings(payload: unknown): payload is ConsoleSettings {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }

  const candidate = payload as Record<string, unknown>;

  return (
    isNonNegativeSafeInteger(candidate.settings_revision) &&
    typeof candidate.show_speech_bubbles === "boolean" &&
    (candidate.active_character_caption_timing === null ||
      isCharacterVoiceCaptionTiming(candidate.active_character_caption_timing)) &&
    typeof candidate.selected_live2d_model_id === "string" &&
    typeof candidate.user_self_intro === "string" &&
    typeof candidate.user_extra_notes === "string" &&
    typeof candidate.streamvox_device === "string" &&
    (candidate.streamvox_llama_backend === null || typeof candidate.streamvox_llama_backend === "string") &&
    typeof candidate.speaker_verification_enabled === "boolean" &&
    Array.isArray(candidate.speaker_whitelist_audio_paths) &&
    candidate.speaker_whitelist_audio_paths.every((path) => typeof path === "string") &&
    typeof candidate.followup_toggle_shortcut_enabled === "boolean" &&
    typeof candidate.followup_toggle_shortcut === "string" &&
    typeof candidate.avatar_visibility_shortcut_enabled === "boolean" &&
    typeof candidate.avatar_visibility_shortcut === "string" &&
    typeof candidate.selection_capture_shortcut_enabled === "boolean" &&
    typeof candidate.selection_capture_shortcut === "string" &&
    isAudioDeviceMode(candidate.audio_device_mode) &&
    isNullableString(candidate.audio_input_device_id) &&
    isNullableString(candidate.audio_input_device_name) &&
    isNullableString(candidate.audio_output_reference_device_id) &&
    isNullableString(candidate.audio_output_reference_device_name)
  );
}

/**
 * 规范化控制台设置快照。
 *
 * Args:
 *   payload: 已通过契约校验的设置对象。
 *
 * Returns:
 *   可安全写入 React 状态的设置副本。
 */
export function normalizeConsoleSettings(payload: ConsoleSettings): ConsoleSettings {
  return {
    settings_revision: payload.settings_revision,
    show_speech_bubbles: payload.show_speech_bubbles,
    active_character_caption_timing: payload.active_character_caption_timing ?? null,
    selected_live2d_model_id: payload.selected_live2d_model_id,
    user_self_intro: payload.user_self_intro,
    user_extra_notes: payload.user_extra_notes,
    streamvox_device: payload.streamvox_device,
    streamvox_llama_backend: payload.streamvox_llama_backend,
    speaker_verification_enabled: payload.speaker_verification_enabled,
    speaker_whitelist_audio_paths: [...payload.speaker_whitelist_audio_paths],
    followup_toggle_shortcut_enabled: payload.followup_toggle_shortcut_enabled,
    followup_toggle_shortcut: payload.followup_toggle_shortcut,
    avatar_visibility_shortcut_enabled: payload.avatar_visibility_shortcut_enabled,
    avatar_visibility_shortcut: payload.avatar_visibility_shortcut,
    selection_capture_shortcut_enabled: payload.selection_capture_shortcut_enabled,
    selection_capture_shortcut: payload.selection_capture_shortcut,
    audio_device_mode: payload.audio_device_mode,
    audio_input_device_id: payload.audio_input_device_id,
    audio_input_device_name: payload.audio_input_device_name,
    audio_output_reference_device_id: payload.audio_output_reference_device_id,
    audio_output_reference_device_name: payload.audio_output_reference_device_name
  };
}
