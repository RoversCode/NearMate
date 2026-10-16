import { type StreamVoxRuntimeState } from "./streamvox";
import { isConsoleSettings, type ConsoleSettings } from "./settings";
import { isPlainObject, isStringArray } from "./validation";

/**
 * 描述角色卡元信息。
 *
 * 核心字段：card_id 是后端主键，name 和 title 是用户可见识别信息。
 * 预期用途：角色卡列表、编辑区和复制动作都依赖这组基础标识。
 * 边界异常：结构版本和作者来源仍处于开发期变动，不进入前端契约。
 */
type CharacterCardMeta = {
  card_id: string;
  name: string;
  title: string;
  user_call_name: string;
  avatar_path: string | null;
  wakeword: CharacterCardWakeword;
  voice_binding: CharacterCardVoiceBinding;
};

type CharacterCardIdentity = {
  social_trajectory: string;
  origin_story: string;
  life_texture: string;
  grounded_impression: string;
};

export type CharacterCardBackgroundContextTerm = {
  name: string;
  aliases: string[];
  explanation: string;
};

type CharacterCardBackgroundContext = {
  terms: CharacterCardBackgroundContextTerm[];
};

type CharacterCardInnerCore = {
  temperament: string;
  values: string[];
  comfort_zones: string[];
  red_lines: string[];
  core_contradiction: string;
  contrasts: string[];
  emotional_metabolism: string;
  conflict_style: string;
};

export type CharacterCardRelationship = {
  type: string;
  pattern: string;
};

type CharacterCardUserBond = {
  bond_type: string;
  default_familiarity: string;
  loyalty_style: string;
  obedience_boundary: string;
  user_profile_usage: string;
};

type CharacterCardRelationships = {
  social_circle: string;
  social_relationships: CharacterCardRelationship[];
  user_bond: CharacterCardUserBond;
  stress_reactions: string[];
};

export type CharacterCardExampleDialogue = {
  user: string;
  char: string;
};

type CharacterCardExampleDialogueSet = {
  items: CharacterCardExampleDialogue[];
};

export const CHARACTER_CARD_EXPECTED_TOOLS = [
  "lookup_current_fact",
  "analyze_deeply",
  "start_background_work"
] as const;

export type CharacterCardExpectedTool = (typeof CHARACTER_CARD_EXPECTED_TOOLS)[number];

export type CharacterCardStreamVoxExampleScenario = {
  title: string;
  user: string;
  char: string;
  expected_tool: CharacterCardExpectedTool | null;
};

/**
 * 判断一个值是不是角色卡允许声明的预期工具。
 *
 * Args:
 *   payload: Backend 响应或编辑器产生的待检查值。
 *
 * Returns:
 *   值属于现役工具名时返回 true，否则返回 false。
 */
export function isCharacterCardExpectedTool(payload: unknown): payload is CharacterCardExpectedTool {
  return CHARACTER_CARD_EXPECTED_TOOLS.some((expectedTool) => expectedTool === payload);
}

export type CharacterCardStreamVoxExampleSet = {
  items: CharacterCardStreamVoxExampleScenario[];
};

type CharacterCardStreamVoxExampleLibrary = {
  generic: CharacterCardStreamVoxExampleSet;
  models: Record<string, CharacterCardStreamVoxExampleSet>;
};

type CharacterCardVoiceprint = {
  speech_style: string;
  catchphrases: string[];
  emotion_grading: Record<string, string>;
  sentence_habits: string[];
  example_dialogues: CharacterCardExampleDialogueSet;
  streamvox_examples: CharacterCardStreamVoxExampleLibrary;
};

type CharacterCardModelConstraints = {
  persona_forbidden_rules: string;
  response_execution_rules: string;
};

type CharacterCardWakeword = {
  keywords: string[];
};

export type CharacterVoiceCaptionTiming = {
  ms_per_unit: number;
  calibration_text: string;
  duration_ms: number;
  unit_count: number;
  measured_at: number;
};

type CharacterCardVoiceBinding = {
  streamvox_model: string;
  role_name: string;
  reference_audio_path: string | null;
  prompt_text: string;
  seed: number | null;
  inference_options: Record<string, unknown>;
  audio_sha256: string | null;
  caption_timing: CharacterVoiceCaptionTiming | null;
};

/**
 * 描述可编辑角色卡完整内容。
 *
 * 核心字段：meta 保留卡片基础信息，其余五段承载角色制作内容。
 * 预期用途：前端编辑页一次性提交完整 card 对象，后端按整卡覆盖保存。
 * 边界异常：旧角色内容字段不进入前端契约，避免控制台继续展示抽象旧表单。
 */
export type CharacterCard = {
  meta: CharacterCardMeta;
  identity: CharacterCardIdentity;
  background_context: CharacterCardBackgroundContext;
  inner_core: CharacterCardInnerCore;
  relationships: CharacterCardRelationships;
  voiceprint: CharacterCardVoiceprint;
  model_constraints: CharacterCardModelConstraints;
};

export type CharacterVoiceMatchStatus = "matched" | "mismatched" | "missing_role" | "missing_assets" | "runtime_unavailable";

export type CharacterVoiceStatusPayload = {
  voice_match_status: CharacterVoiceMatchStatus;
  runtime: StreamVoxRuntimeState | null;
  expected: CharacterCardVoiceBinding;
  /** 语音专用 mutation 成功时返回的统一设置提交点快照。 */
  settings?: ConsoleSettings;
};

export type CharacterCardVoiceReferenceUploadResult = {
  reference_audio_path: string;
  effect_message: string;
};

export type CharacterCardPayload = {
  card: CharacterCard | null;
  active_card_id: string;
  shared_world_canon: string;
  validation_messages: string[];
  effect_message: string | null;
  voice_match_status: CharacterVoiceMatchStatus | null;
  /** 角色卡专用 mutation 成功时返回的统一设置提交点快照。 */
  settings?: ConsoleSettings;
};

export type CharacterCardsPayload = {
  cards: CharacterCard[];
  active_card_id: string;
  shared_world_canon: string;
};

export type CharacterCardClonePayload = {
  source_card_id: string;
  card_id: string;
  name: string;
  title: string;
};

function isCharacterCardMeta(payload: unknown): payload is CharacterCardMeta {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.card_id === "string" &&
    typeof payload.name === "string" &&
    typeof payload.title === "string" &&
    typeof payload.user_call_name === "string" &&
    (payload.avatar_path === null || typeof payload.avatar_path === "string") &&
    isCharacterCardWakeword(payload.wakeword) &&
    isCharacterCardVoiceBinding(payload.voice_binding)
  );
}

function isCharacterCardIdentity(payload: unknown): payload is CharacterCardIdentity {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.social_trajectory === "string" &&
    typeof payload.origin_story === "string" &&
    typeof payload.life_texture === "string" &&
    typeof payload.grounded_impression === "string"
  );
}

function isCharacterCardBackgroundContextTerm(payload: unknown): payload is CharacterCardBackgroundContextTerm {
  if (!isPlainObject(payload)) {
    return false;
  }

  return typeof payload.name === "string" && isStringArray(payload.aliases) && typeof payload.explanation === "string";
}

function isCharacterCardBackgroundContext(payload: unknown): payload is CharacterCardBackgroundContext {
  if (!isPlainObject(payload)) {
    return false;
  }

  return Array.isArray(payload.terms) && payload.terms.every(isCharacterCardBackgroundContextTerm);
}

function isCharacterCardInnerCore(payload: unknown): payload is CharacterCardInnerCore {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.temperament === "string" &&
    isStringArray(payload.values) &&
    isStringArray(payload.comfort_zones) &&
    isStringArray(payload.red_lines) &&
    typeof payload.core_contradiction === "string" &&
    isStringArray(payload.contrasts) &&
    typeof payload.emotional_metabolism === "string" &&
    typeof payload.conflict_style === "string"
  );
}

function isCharacterCardRelationship(payload: unknown): payload is CharacterCardRelationship {
  if (!isPlainObject(payload)) {
    return false;
  }

  return typeof payload.type === "string" && typeof payload.pattern === "string";
}

function isCharacterCardUserBond(payload: unknown): payload is CharacterCardUserBond {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.bond_type === "string" &&
    typeof payload.default_familiarity === "string" &&
    typeof payload.loyalty_style === "string" &&
    typeof payload.obedience_boundary === "string" &&
    typeof payload.user_profile_usage === "string"
  );
}

function isCharacterCardRelationships(payload: unknown): payload is CharacterCardRelationships {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.social_circle === "string" &&
    Array.isArray(payload.social_relationships) &&
    payload.social_relationships.every(isCharacterCardRelationship) &&
    isCharacterCardUserBond(payload.user_bond) &&
    isStringArray(payload.stress_reactions)
  );
}

function isCharacterCardExampleDialogue(payload: unknown): payload is CharacterCardExampleDialogue {
  if (!isPlainObject(payload)) {
    return false;
  }

  return typeof payload.user === "string" && typeof payload.char === "string";
}

function isCharacterCardExampleDialogueSet(payload: unknown): payload is CharacterCardExampleDialogueSet {
  if (!isPlainObject(payload)) {
    return false;
  }

  return Array.isArray(payload.items) && payload.items.every(isCharacterCardExampleDialogue);
}

function isCharacterCardStreamVoxExampleScenario(payload: unknown): payload is CharacterCardStreamVoxExampleScenario {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.title === "string" &&
    typeof payload.user === "string" &&
    typeof payload.char === "string" &&
    (payload.expected_tool === null || isCharacterCardExpectedTool(payload.expected_tool))
  );
}

export function isCharacterCardStreamVoxExampleSet(payload: unknown): payload is CharacterCardStreamVoxExampleSet {
  if (!isPlainObject(payload)) {
    return false;
  }

  return Array.isArray(payload.items) && payload.items.every(isCharacterCardStreamVoxExampleScenario);
}

function isCharacterCardStreamVoxExampleLibrary(payload: unknown): payload is CharacterCardStreamVoxExampleLibrary {
  if (!isPlainObject(payload) || !isCharacterCardStreamVoxExampleSet(payload.generic) || !isPlainObject(payload.models)) {
    return false;
  }

  return Object.values(payload.models).every(isCharacterCardStreamVoxExampleSet);
}

function isCharacterCardVoiceprint(payload: unknown): payload is CharacterCardVoiceprint {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.speech_style === "string" &&
    isStringArray(payload.catchphrases) &&
    isPlainObject(payload.emotion_grading) &&
    Object.values(payload.emotion_grading).every((value) => typeof value === "string") &&
    isStringArray(payload.sentence_habits) &&
    isCharacterCardExampleDialogueSet(payload.example_dialogues) &&
    isCharacterCardStreamVoxExampleLibrary(payload.streamvox_examples)
  );
}

function isCharacterCardModelConstraints(payload: unknown): payload is CharacterCardModelConstraints {
  if (!isPlainObject(payload)) {
    return false;
  }

  return typeof payload.persona_forbidden_rules === "string" && typeof payload.response_execution_rules === "string";
}

function isCharacterCardWakeword(payload: unknown): payload is CharacterCardWakeword {
  if (!isPlainObject(payload)) {
    return false;
  }

  return Array.isArray(payload.keywords) && payload.keywords.every((keyword) => typeof keyword === "string");
}

export function isCharacterVoiceCaptionTiming(payload: unknown): payload is CharacterVoiceCaptionTiming {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.ms_per_unit === "number" &&
    typeof payload.calibration_text === "string" &&
    typeof payload.duration_ms === "number" &&
    typeof payload.unit_count === "number" &&
    typeof payload.measured_at === "number"
  );
}

function isCharacterCardVoiceBinding(payload: unknown): payload is CharacterCardVoiceBinding {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.streamvox_model === "string" &&
    typeof payload.role_name === "string" &&
    (payload.reference_audio_path === null || typeof payload.reference_audio_path === "string") &&
    typeof payload.prompt_text === "string" &&
    (payload.seed === null || typeof payload.seed === "number") &&
    isPlainObject(payload.inference_options) &&
    (payload.audio_sha256 === undefined || payload.audio_sha256 === null || typeof payload.audio_sha256 === "string") &&
    (payload.caption_timing === undefined || payload.caption_timing === null || isCharacterVoiceCaptionTiming(payload.caption_timing))
  );
}

function isCharacterVoiceMatchStatus(value: unknown): value is CharacterVoiceMatchStatus {
  return (
    value === "matched" ||
    value === "mismatched" ||
    value === "missing_role" ||
    value === "missing_assets" ||
    value === "runtime_unavailable"
  );
}

function isCharacterCard(payload: unknown): payload is CharacterCard {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    isCharacterCardMeta(payload.meta) &&
    isCharacterCardIdentity(payload.identity) &&
    isCharacterCardBackgroundContext(payload.background_context) &&
    isCharacterCardInnerCore(payload.inner_core) &&
    isCharacterCardRelationships(payload.relationships) &&
    isCharacterCardVoiceprint(payload.voiceprint) &&
    isCharacterCardModelConstraints(payload.model_constraints)
  );
}

export function isCharacterVoiceStatusPayload(payload: unknown): payload is CharacterVoiceStatusPayload {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    isCharacterVoiceMatchStatus(payload.voice_match_status) &&
    (payload.runtime === null || isPlainObject(payload.runtime)) &&
    isCharacterCardVoiceBinding(payload.expected) &&
    (payload.settings === undefined || isConsoleSettings(payload.settings))
  );
}

export function isCharacterCardVoiceReferenceUploadResult(payload: unknown): payload is CharacterCardVoiceReferenceUploadResult {
  if (!isPlainObject(payload)) {
    return false;
  }

  return typeof payload.reference_audio_path === "string" && typeof payload.effect_message === "string";
}

export function isCharacterCardPayload(payload: unknown): payload is CharacterCardPayload {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    (payload.card === null || isCharacterCard(payload.card)) &&
    typeof payload.active_card_id === "string" &&
    typeof payload.shared_world_canon === "string" &&
    Array.isArray(payload.validation_messages) &&
    payload.validation_messages.every((message) => typeof message === "string") &&
    (payload.effect_message === null || typeof payload.effect_message === "string") &&
    (payload.voice_match_status === null || isCharacterVoiceMatchStatus(payload.voice_match_status)) &&
    (payload.settings === undefined || isConsoleSettings(payload.settings))
  );
}

export function isCharacterCardsPayload(payload: unknown): payload is CharacterCardsPayload {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    Array.isArray(payload.cards) &&
    payload.cards.every(isCharacterCard) &&
    typeof payload.active_card_id === "string" &&
    typeof payload.shared_world_canon === "string"
  );
}
