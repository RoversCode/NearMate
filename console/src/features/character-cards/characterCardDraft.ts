import {
  type CharacterCard,
  type CharacterCardBackgroundContextTerm,
  type CharacterCardExampleDialogue,
  type CharacterCardPayload,
  type CharacterCardRelationship,
  type CharacterCardStreamVoxExampleScenario,
  type CharacterCardStreamVoxExampleSet,
  isCharacterCardExpectedTool
} from "../../api/contracts/characterCards";

export type TextFieldPath =
  | "meta.name"
  | "meta.title"
  | "meta.user_call_name"
  | "identity.social_trajectory"
  | "identity.origin_story"
  | "identity.life_texture"
  | "identity.grounded_impression"
  | "inner_core.temperament"
  | "inner_core.core_contradiction"
  | "inner_core.emotional_metabolism"
  | "inner_core.conflict_style"
  | "relationships.social_circle"
  | "relationships.user_bond.bond_type"
  | "relationships.user_bond.default_familiarity"
  | "relationships.user_bond.loyalty_style"
  | "relationships.user_bond.obedience_boundary"
  | "relationships.user_bond.user_profile_usage"
  | "voiceprint.speech_style"
  | "model_constraints.persona_forbidden_rules"
  | "model_constraints.response_execution_rules"
  | "meta.voice_binding.streamvox_model"
  | "meta.voice_binding.role_name"
  | "meta.voice_binding.prompt_text"
  | "meta.voice_binding.seed";

export type ListFieldPath =
  | "inner_core.values"
  | "inner_core.comfort_zones"
  | "inner_core.red_lines"
  | "inner_core.contrasts"
  | "relationships.stress_reactions"
  | "voiceprint.catchphrases"
  | "voiceprint.sentence_habits";

export type RelationshipRow = CharacterCardRelationship;
export type BackgroundContextRow = CharacterCardBackgroundContextTerm;
export type ExampleDialogueRow = CharacterCardExampleDialogue;
export type StreamVoxExampleRow = CharacterCardStreamVoxExampleScenario;
export type EmotionGradingValue = Record<string, string>;

/**
 * 判断后端激活卡快照是否属于当前编辑草稿。
 *
 * Args:
 *   draftCard: 页面当前正在编辑的角色卡草稿。
 *   activeCard: 父级传入的后端角色卡快照，通常代表当前激活卡或一次保存后的目标卡。
 *
 * Returns:
 *   首次加载或同一卡更新时返回 true；非激活卡编辑期间收到激活卡刷新时返回 false。
 */
export function shouldSyncDraftFromActivePayload(draftCard: CharacterCard | null, activeCard: CharacterCardPayload | null): boolean {
  if (draftCard === null || activeCard?.card === null || activeCard?.card === undefined) {
    return true;
  }

  return draftCard.meta.card_id === activeCard.card.meta.card_id;
}

/**
 * 合并角色卡刷新，只让服务端接管用户没有继续编辑的字段。
 *
 * Args:
 *   draft: 当前草稿，也可以是递归处理的某个字段。
 *   baseline: 开始编辑时的服务端值；保存响应使用提交时的草稿值。
 *   incoming: 后端最新返回的值，包含封面、语音资产和规范化结果。
 *
 * Returns:
 *   保留本地修改后的值；列表整体保留，避免逐项合并把已删除的行加回来。
 */
export function mergeCharacterCardDraft<T>(draft: T, baseline: T, incoming: T): T {
  if (JSON.stringify(draft) === JSON.stringify(baseline)) return incoming;
  if (draft === null || baseline === null || incoming === null
    || typeof draft !== "object" || typeof baseline !== "object" || typeof incoming !== "object"
    || Array.isArray(draft) || Array.isArray(baseline) || Array.isArray(incoming)) return draft;

  // 封面和语音信息嵌在 meta 内；按字段合并才能同时保留本地名字与新资产路径。
  const currentFields = draft as Record<string, unknown>;
  const baselineFields = baseline as Record<string, unknown>;
  const incomingFields = incoming as Record<string, unknown>;
  const keys = new Set([...Object.keys(currentFields), ...Object.keys(incomingFields)]);
  const mergedFields: Record<string, unknown> = {};
  for (const key of keys) {
    const value = mergeCharacterCardDraft(currentFields[key], baselineFields[key], incomingFields[key]);
    // 已删除的模型或参数必须真正消失，不能留下值为 undefined 的空条目。
    if (value !== undefined) mergedFields[key] = value;
  }
  return mergedFields as T;
}

/**
 * 更新角色卡字符串字段。
 *
 * Args:
 *   card: 当前草稿。
 *   path: 待更新字段路径。
 *   value: 新字符串值。
 *
 * Returns:
 *   更新后的角色卡草稿。
 */
export function updateTextField(card: CharacterCard, path: TextFieldPath, value: string): CharacterCard {
  const nextCard = cloneCard(card);

  switch (path) {
    case "meta.name":
      nextCard.meta.name = value;
      return nextCard;
    case "meta.title":
      nextCard.meta.title = value;
      return nextCard;
    case "meta.user_call_name":
      nextCard.meta.user_call_name = value;
      return nextCard;
    case "identity.social_trajectory":
      nextCard.identity.social_trajectory = value;
      return nextCard;
    case "identity.origin_story":
      nextCard.identity.origin_story = value;
      return nextCard;
    case "identity.life_texture":
      nextCard.identity.life_texture = value;
      return nextCard;
    case "identity.grounded_impression":
      nextCard.identity.grounded_impression = value;
      return nextCard;
    case "inner_core.temperament":
      nextCard.inner_core.temperament = value;
      return nextCard;
    case "inner_core.core_contradiction":
      nextCard.inner_core.core_contradiction = value;
      return nextCard;
    case "inner_core.emotional_metabolism":
      nextCard.inner_core.emotional_metabolism = value;
      return nextCard;
    case "inner_core.conflict_style":
      nextCard.inner_core.conflict_style = value;
      return nextCard;
    case "relationships.social_circle":
      nextCard.relationships.social_circle = value;
      return nextCard;
    case "relationships.user_bond.bond_type":
      nextCard.relationships.user_bond.bond_type = value;
      return nextCard;
    case "relationships.user_bond.default_familiarity":
      nextCard.relationships.user_bond.default_familiarity = value;
      return nextCard;
    case "relationships.user_bond.loyalty_style":
      nextCard.relationships.user_bond.loyalty_style = value;
      return nextCard;
    case "relationships.user_bond.obedience_boundary":
      nextCard.relationships.user_bond.obedience_boundary = value;
      return nextCard;
    case "relationships.user_bond.user_profile_usage":
      nextCard.relationships.user_bond.user_profile_usage = value;
      return nextCard;
    case "voiceprint.speech_style":
      nextCard.voiceprint.speech_style = value;
      return nextCard;
    case "model_constraints.persona_forbidden_rules":
      nextCard.model_constraints.persona_forbidden_rules = value;
      return nextCard;
    case "model_constraints.response_execution_rules":
      nextCard.model_constraints.response_execution_rules = value;
      return nextCard;
    case "meta.voice_binding.streamvox_model":
      nextCard.meta.voice_binding.streamvox_model = value;
      return nextCard;
    case "meta.voice_binding.role_name":
      nextCard.meta.voice_binding.role_name = value;
      return nextCard;
    case "meta.voice_binding.prompt_text":
      nextCard.meta.voice_binding.prompt_text = value;
      nextCard.meta.voice_binding.audio_sha256 = null;
      return nextCard;
    case "meta.voice_binding.seed": {
      const trimmedValue = value.trim();
      const parsedSeed = Number(trimmedValue);
      nextCard.meta.voice_binding.seed = trimmedValue && Number.isInteger(parsedSeed) && parsedSeed >= 0 ? parsedSeed : null;
      return nextCard;
    }
  }
}

/**
 * 更新角色卡字符串列表字段。
 *
 * Args:
 *   card: 当前草稿。
 *   path: 待更新列表路径。
 *   value: 多行文本。
 *
 * Returns:
 *   更新后的角色卡草稿。
 */
export function updateListField(card: CharacterCard, path: ListFieldPath, value: string): CharacterCard {
  const nextCard = cloneCard(card);
  const nextItems = value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);

  switch (path) {
    case "inner_core.values":
      nextCard.inner_core.values = nextItems;
      return nextCard;
    case "inner_core.comfort_zones":
      nextCard.inner_core.comfort_zones = nextItems;
      return nextCard;
    case "inner_core.red_lines":
      nextCard.inner_core.red_lines = nextItems;
      return nextCard;
    case "inner_core.contrasts":
      nextCard.inner_core.contrasts = nextItems;
      return nextCard;
    case "relationships.stress_reactions":
      nextCard.relationships.stress_reactions = nextItems;
      return nextCard;
    case "voiceprint.catchphrases":
      nextCard.voiceprint.catchphrases = nextItems;
      return nextCard;
    case "voiceprint.sentence_habits":
      nextCard.voiceprint.sentence_habits = nextItems;
      return nextCard;
  }
}

export function updateWakewordKeywordsDraft(card: CharacterCard, keywords: string[]): CharacterCard {
  const nextCard = cloneCard(card);
  nextCard.meta.wakeword.keywords = [...keywords];
  return nextCard;
}

export function updateVoiceReferenceAudioDraft(card: CharacterCard, referenceAudioPath: string): CharacterCard {
  const nextCard = cloneCard(card);
  nextCard.meta.voice_binding.reference_audio_path = referenceAudioPath;
  nextCard.meta.voice_binding.audio_sha256 = null;
  return nextCard;
}

/**
 * 保存前清理角色卡草稿。
 *
 * Args:
 *   card: 当前草稿。
 *
 * Returns:
 *   去除空白项、重复项和无效示例后的角色卡。
 */
export function prepareCharacterCardForSave(card: CharacterCard): CharacterCard {
  const nextCard = cloneCard(card);
  nextCard.meta.wakeword.keywords = normalizeUniqueTexts(nextCard.meta.wakeword.keywords);
  nextCard.meta.voice_binding.prompt_text = nextCard.meta.voice_binding.prompt_text.trim();
  nextCard.background_context.terms = normalizeBackgroundContextTerms(nextCard.background_context.terms);
  nextCard.inner_core.values = normalizeUniqueTexts(nextCard.inner_core.values);
  nextCard.inner_core.comfort_zones = normalizeUniqueTexts(nextCard.inner_core.comfort_zones);
  nextCard.inner_core.red_lines = normalizeUniqueTexts(nextCard.inner_core.red_lines);
  nextCard.inner_core.contrasts = normalizeUniqueTexts(nextCard.inner_core.contrasts);
  nextCard.relationships.stress_reactions = normalizeUniqueTexts(nextCard.relationships.stress_reactions);
  nextCard.voiceprint.catchphrases = normalizeUniqueTexts(nextCard.voiceprint.catchphrases);
  nextCard.voiceprint.sentence_habits = normalizeUniqueTexts(nextCard.voiceprint.sentence_habits);
  nextCard.voiceprint.streamvox_examples = normalizeStreamVoxExampleLibrary(nextCard.voiceprint.streamvox_examples);
  return nextCard;
}

export function updateVoiceInferenceOptionsField(card: CharacterCard, rawValue: string): CharacterCard {
  const nextCard = cloneCard(card);
  try {
    const parsedValue: unknown = JSON.parse(rawValue);
    if (parsedValue !== null && typeof parsedValue === "object" && !Array.isArray(parsedValue)) {
      nextCard.meta.voice_binding.inference_options = parsedValue as Record<string, unknown>;
    }
  } catch {
    return nextCard;
  }
  return nextCard;
}

export function updateBackgroundContextTermsField(card: CharacterCard, terms: BackgroundContextRow[]): CharacterCard {
  const nextCard = cloneCard(card);
  nextCard.background_context.terms = terms.map((term) => ({
    name: term.name,
    aliases: [...term.aliases],
    explanation: term.explanation
  }));
  return nextCard;
}

export function updateRelationshipsField(card: CharacterCard, relationships: RelationshipRow[]): CharacterCard {
  const nextCard = cloneCard(card);
  nextCard.relationships.social_relationships = relationships
    .map((relationship) => ({
      type: relationship.type.trim(),
      pattern: relationship.pattern.trim()
    }))
    .filter((relationship) => relationship.type || relationship.pattern);
  return nextCard;
}

export function updateExampleDialoguesField(card: CharacterCard, examples: ExampleDialogueRow[]): CharacterCard {
  const nextCard = cloneCard(card);
  nextCard.voiceprint.example_dialogues.items = examples
    .map((dialogue) => ({
      user: dialogue.user.trim(),
      char: dialogue.char.trim()
    }))
    .filter((dialogue) => dialogue.user && dialogue.char);
  return nextCard;
}

export function updateStreamVoxExamplesField(card: CharacterCard, targetModel: string, exampleSet: CharacterCardStreamVoxExampleSet): CharacterCard {
  const nextCard = cloneCard(card);
  const normalizedSet = {
    items: normalizeStreamVoxExampleRows(exampleSet.items)
  };
  if (targetModel === "generic") {
    nextCard.voiceprint.streamvox_examples.generic = normalizedSet;
    return nextCard;
  }

  nextCard.voiceprint.streamvox_examples.models[targetModel] = normalizedSet;
  return nextCard;
}

export function updateEmotionGradingField(card: CharacterCard, rawValue: string): CharacterCard {
  const nextCard = cloneCard(card);
  const grading: Record<string, string> = {};

  for (const line of rawValue.split(/\r?\n/)) {
    const trimmedLine = line.trim();
    if (!trimmedLine) {
      continue;
    }

    const separatorIndex = trimmedLine.indexOf(":");
    if (separatorIndex === -1) {
      continue;
    }

    const grade = trimmedLine.slice(0, separatorIndex).trim();
    const phrase = trimmedLine.slice(separatorIndex + 1).trim();
    if (!grade || !phrase) {
      continue;
    }

    grading[grade] = phrase;
  }

  nextCard.voiceprint.emotion_grading = grading;
  return nextCard;
}

export function resolveStreamVoxExampleSet(card: CharacterCard, targetModel: string): CharacterCardStreamVoxExampleSet {
  if (targetModel === "generic") {
    return card.voiceprint.streamvox_examples.generic;
  }
  return card.voiceprint.streamvox_examples.models[targetModel] ?? { items: [] };
}

export function cloneCard(card: CharacterCard): CharacterCard {
  return {
    ...card,
    meta: {
      ...card.meta,
      wakeword: {
        ...card.meta.wakeword,
        keywords: [...card.meta.wakeword.keywords]
      },
      voice_binding: {
        ...card.meta.voice_binding,
        audio_sha256: card.meta.voice_binding.audio_sha256 ?? null,
        inference_options: { ...card.meta.voice_binding.inference_options }
      }
    },
    identity: { ...card.identity },
    background_context: {
      ...card.background_context,
      terms: card.background_context.terms.map((term) => ({
        ...term,
        aliases: [...term.aliases]
      }))
    },
    inner_core: {
      ...card.inner_core,
      values: [...card.inner_core.values],
      comfort_zones: [...card.inner_core.comfort_zones],
      red_lines: [...card.inner_core.red_lines],
      contrasts: [...card.inner_core.contrasts]
    },
    relationships: {
      ...card.relationships,
      social_relationships: card.relationships.social_relationships.map((relationship) => ({ ...relationship })),
      user_bond: { ...card.relationships.user_bond },
      stress_reactions: [...card.relationships.stress_reactions]
    },
    voiceprint: {
      ...card.voiceprint,
      catchphrases: [...card.voiceprint.catchphrases],
      emotion_grading: { ...card.voiceprint.emotion_grading },
      sentence_habits: [...card.voiceprint.sentence_habits],
      example_dialogues: {
        items: card.voiceprint.example_dialogues.items.map((dialogue) => ({ ...dialogue }))
      },
      streamvox_examples: {
        generic: {
          items: card.voiceprint.streamvox_examples.generic.items.map((scenario) => ({ ...scenario }))
        },
        models: Object.fromEntries(
          Object.entries(card.voiceprint.streamvox_examples.models).map(([model, exampleSet]) => [
            model,
            {
              items: exampleSet.items.map((scenario) => ({ ...scenario }))
            }
          ])
        )
      }
    },
    model_constraints: { ...card.model_constraints }
  };
}

export function buildCloneCardId(sourceCardId: string, cards: CharacterCard[]): string {
  const usedCardIds = new Set(cards.map((card) => card.meta.card_id));
  let index = 1;
  let candidateCardId = `${sourceCardId}_copy`;
  while (usedCardIds.has(candidateCardId)) {
    index += 1;
    candidateCardId = `${sourceCardId}_copy_${index}`;
  }
  return candidateCardId;
}

function normalizeUniqueTexts(values: string[]): string[] {
  const normalizedValues: string[] = [];
  const seenValues = new Set<string>();
  for (const value of values) {
    const normalizedValue = value.trim();
    if (!normalizedValue || seenValues.has(normalizedValue)) {
      continue;
    }
    normalizedValues.push(normalizedValue);
    seenValues.add(normalizedValue);
  }
  return normalizedValues;
}

function normalizeBackgroundContextTerms(terms: BackgroundContextRow[]): BackgroundContextRow[] {
  return terms
    .map((term) => ({
      name: term.name.trim(),
      aliases: normalizeUniqueTexts(term.aliases),
      explanation: term.explanation.trim()
    }))
    .filter((term) => term.name && term.explanation);
}

function normalizeStreamVoxExampleLibrary(
  library: CharacterCard["voiceprint"]["streamvox_examples"]
): CharacterCard["voiceprint"]["streamvox_examples"] {
  const models: Record<string, CharacterCardStreamVoxExampleSet> = {};
  for (const [model, exampleSet] of Object.entries(library.models)) {
    const normalizedModel = model.trim().toLowerCase();
    if (!normalizedModel) {
      continue;
    }
    models[normalizedModel] = {
      items: normalizeStreamVoxExampleRows(exampleSet.items)
    };
  }
  return {
    generic: {
      items: normalizeStreamVoxExampleRows(library.generic.items)
    },
    models
  };
}

function normalizeStreamVoxExampleRows(rows: StreamVoxExampleRow[]): StreamVoxExampleRow[] {
  return rows
    .map((row) => ({
      title: row.title.trim(),
      user: row.user.trim(),
      char: row.char.trim(),
      expected_tool: isCharacterCardExpectedTool(row.expected_tool) ? row.expected_tool : null
    }))
    .filter((row) => row.title && row.user && row.char);
}
