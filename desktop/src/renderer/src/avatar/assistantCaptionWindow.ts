export type CharacterVoiceCaptionTiming = {
  ms_per_unit: number;
  calibration_text: string;
  duration_ms: number;
  unit_count: number;
  measured_at: number;
};

export type AssistantCaptionWindowState = {
  text: string;
  visibleText: string;
  progressUnitCount: number;
  generationId?: string;
  textFinal: boolean;
  captionMsPerUnit: number;
};

type CaptionUnitToken = {
  sourceStart: number;
  sourceEnd: number;
  unitStart: number;
  unitEnd: number;
  weight: number;
};

const FALLBACK_CAPTION_MS_PER_UNIT = 220;
const MAX_VISIBLE_UNIT_COUNT = 34;
const MIN_VISIBLE_UNIT_COUNT = 4;
const SOFT_PUNCTUATION_UNIT = 0.45;
const HARD_PUNCTUATION_UNIT = 0.9;
const ELLIPSIS_UNIT = 1.3;
const LATIN_WORD_UNIT = 1.6;
const NUMBER_GROUP_UNIT = 1.4;
const TAG_UNIT_WEIGHTS: Record<string, number> = {
  "[short pause]": 1.5,
  "[pause]": 3,
  "[emphasis]": 0.8
};

/**
 * 创建一段助手字幕窗口状态。
 *
 * Args:
 *   text: 当前已经收到的助手完整文本。
 *   generationId: 助手回复代次编号。
 *   captionTiming: 角色卡音色校准结果；缺失时使用保守 fallback。
 *   textFinal: LLM 文本是否已经完成。
 *   progressUnitCount: 当前已经推进到的字幕单位数。
 *
 * Returns:
 *   带完整文本、可见窗口和推进速度的字幕状态。
 */
export function createAssistantCaptionWindow({
  text,
  generationId,
  captionTiming,
  textFinal = false,
  progressUnitCount = 0
}: {
  text: string;
  generationId?: string;
  captionTiming?: CharacterVoiceCaptionTiming | null;
  textFinal?: boolean;
  progressUnitCount?: number;
}): AssistantCaptionWindowState {
  return buildAssistantCaptionWindow({
    text,
    generationId,
    textFinal,
    captionMsPerUnit: resolveCaptionMsPerUnit(captionTiming),
    progressUnitCount
  });
}

/**
 * 按经过时间推进字幕窗口。
 *
 * Args:
 *   state: 当前字幕窗口状态。
 *   deltaMs: 距离上次 tick 的时间差。
 *
 * Returns:
 *   推进后的字幕状态；进度不会超过已经收到的文本。
 */
export function tickAssistantCaptionWindow(
  state: AssistantCaptionWindowState,
  { deltaMs }: { deltaMs: number }
): AssistantCaptionWindowState {
  if (!Number.isFinite(deltaMs) || deltaMs <= 0) {
    return buildAssistantCaptionWindow(state);
  }

  const totalUnitCount = sumCaptionUnitCount(state.text);
  const nextProgressUnitCount = Math.min(
    totalUnitCount,
    state.progressUnitCount + deltaMs / state.captionMsPerUnit
  );

  return buildAssistantCaptionWindow({
    ...state,
    progressUnitCount: nextProgressUnitCount
  });
}

/**
 * 音频结束时把字幕窗口收束到尾句。
 *
 * Args:
 *   state: 当前字幕窗口状态。
 *
 * Returns:
 *   指向全文末尾的字幕状态，供短 TTL 消失前稳定展示。
 */
export function finishAssistantCaptionWindow(state: AssistantCaptionWindowState): AssistantCaptionWindowState {
  return buildAssistantCaptionWindow({
    ...state,
    progressUnitCount: sumCaptionUnitCount(state.text),
    textFinal: true
  });
}

/**
 * 计算字幕加权单位数。
 *
 * Args:
 *   text: 需要折算的文本。
 *
 * Returns:
 *   至少为 1 的加权单位数，和后端校准口径保持一致。
 */
export function calculateCaptionUnitCount(text: string): number {
  return roundUnitCount(Math.max(1, sumCaptionUnitCount(text)));
}

function buildAssistantCaptionWindow(state: Omit<AssistantCaptionWindowState, "visibleText">): AssistantCaptionWindowState {
  const totalUnitCount = sumCaptionUnitCount(state.text);
  const progressUnitCount = roundUnitCount(clamp(state.progressUnitCount, 0, totalUnitCount));

  return {
    ...state,
    progressUnitCount,
    visibleText: selectVisibleCaptionText(state.text, progressUnitCount)
  };
}

function selectVisibleCaptionText(text: string, progressUnitCount: number): string {
  const tokens = tokenizeCaptionUnits(text);
  if (tokens.length === 0) {
    return "";
  }

  const totalUnitCount = tokens[tokens.length - 1].unitEnd;
  const visibleEndUnitCount = clamp(
    Math.max(progressUnitCount, Math.min(totalUnitCount, MIN_VISIBLE_UNIT_COUNT)),
    0,
    totalUnitCount
  );
  const visibleStartUnitCount = Math.max(0, visibleEndUnitCount - MAX_VISIBLE_UNIT_COUNT);
  const firstToken = tokens.find((token) => token.unitEnd > visibleStartUnitCount) ?? tokens[0];
  const lastToken =
    [...tokens].reverse().find((token) => token.unitStart < visibleEndUnitCount) ?? tokens[tokens.length - 1];

  return text.slice(firstToken.sourceStart, lastToken.sourceEnd).trimStart();
}

function resolveCaptionMsPerUnit(captionTiming: CharacterVoiceCaptionTiming | null | undefined): number {
  if (captionTiming === null || captionTiming === undefined) {
    return FALLBACK_CAPTION_MS_PER_UNIT;
  }

  if (!Number.isFinite(captionTiming.ms_per_unit) || captionTiming.ms_per_unit <= 0) {
    return FALLBACK_CAPTION_MS_PER_UNIT;
  }

  return captionTiming.ms_per_unit;
}

function sumCaptionUnitCount(text: string): number {
  const tokens = tokenizeCaptionUnits(text);
  if (tokens.length === 0) {
    return 0;
  }
  return roundUnitCount(tokens[tokens.length - 1].unitEnd);
}

function tokenizeCaptionUnits(text: string): CaptionUnitToken[] {
  const tokens: CaptionUnitToken[] = [];
  let index = 0;
  let unitCursor = 0;

  while (index < text.length) {
    const tagMatch = /^\[[a-zA-Z ]+\]/.exec(text.slice(index));
    if (tagMatch !== null) {
      const rawTag = tagMatch[0];
      const weight = TAG_UNIT_WEIGHTS[rawTag.toLowerCase()] ?? 0.6;
      const nextIndex = index + rawTag.length;
      tokens.push(buildToken(index, nextIndex, unitCursor, weight));
      unitCursor += weight;
      index = nextIndex;
      continue;
    }

    const currentChar = text[index];
    if (/\s/.test(currentChar)) {
      index += 1;
      continue;
    }

    if (isAsciiLetter(currentChar)) {
      const nextIndex = readWhile(text, index + 1, isAsciiLetter);
      tokens.push(buildToken(index, nextIndex, unitCursor, LATIN_WORD_UNIT));
      unitCursor += LATIN_WORD_UNIT;
      index = nextIndex;
      continue;
    }

    if (isDigit(currentChar)) {
      const nextIndex = readWhile(text, index + 1, isDigit);
      const weight = Math.max(NUMBER_GROUP_UNIT, (nextIndex - index) * 0.55);
      tokens.push(buildToken(index, nextIndex, unitCursor, weight));
      unitCursor += weight;
      index = nextIndex;
      continue;
    }

    const weight = getCharacterUnitWeight(currentChar);
    tokens.push(buildToken(index, index + currentChar.length, unitCursor, weight));
    unitCursor += weight;
    index += currentChar.length;
  }

  return tokens;
}

function buildToken(sourceStart: number, sourceEnd: number, unitStart: number, weight: number): CaptionUnitToken {
  return {
    sourceStart,
    sourceEnd,
    unitStart,
    unitEnd: unitStart + weight,
    weight
  };
}

function getCharacterUnitWeight(character: string): number {
  if ("，、；,;：:".includes(character)) {
    return SOFT_PUNCTUATION_UNIT;
  }

  if ("。！？.!?".includes(character)) {
    return HARD_PUNCTUATION_UNIT;
  }

  if ("…—".includes(character)) {
    return ELLIPSIS_UNIT;
  }

  if (isCjkCharacter(character)) {
    return 1;
  }

  return 0.8;
}

function readWhile(text: string, startIndex: number, predicate: (character: string) => boolean): number {
  let index = startIndex;
  while (index < text.length && predicate(text[index])) {
    index += 1;
  }
  return index;
}

function isAsciiLetter(character: string): boolean {
  return /^[A-Za-z]$/.test(character);
}

function isDigit(character: string): boolean {
  return /^\d$/.test(character);
}

function isCjkCharacter(character: string): boolean {
  return /[\u3400-\u9fff\uf900-\ufaff]/.test(character);
}

function roundUnitCount(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
