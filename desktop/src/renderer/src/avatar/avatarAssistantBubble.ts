import {
  createAssistantCaptionWindow,
  finishAssistantCaptionWindow,
  tickAssistantCaptionWindow
} from "./assistantCaptionWindow";
import type { AvatarBubbleState } from "./avatarBubbleTypes";

export const ASSISTANT_AUDIO_FINISHED_TTL_MS = 1200;

/**
 * 清除头部助手回复气泡。
 */
export function clearAssistantBubble(state: AvatarBubbleState): AvatarBubbleState {
  if (!state.assistantBubble) {
    return state;
  }

  return {
    ...state,
    assistantBubble: null
  };
}

/**
 * 累加助手流式文本，保持同一 generation 的头部气泡连续增长。
 */
export function appendAssistantTextChunk(
  state: AvatarBubbleState,
  textChunk: string,
  generationId: string | undefined
): AvatarBubbleState {
  const appendTarget =
    state.assistantBubble &&
    state.assistantBubble.generationId === generationId &&
    !state.assistantBubble.textFinal &&
    !state.assistantBubble.ttlMs
      ? state.assistantBubble
      : null;

  return {
    ...state,
    assistantBubble: {
      ...createAssistantCaptionWindow({
        text: appendTarget ? `${appendTarget.text}${textChunk}` : textChunk,
        generationId,
        captionTiming: state.activeCharacterCaptionTiming,
        progressUnitCount: appendTarget?.progressUnitCount ?? 0
      }),
      kind: "assistant_reply",
      ttlMs: null
    }
  };
}

/**
 * 应用助手最终文本，文字完成只更新内容和状态，不启动隐藏倒计时。
 */
export function applyAssistantTextFinal(
  state: AvatarBubbleState,
  text: string,
  generationId: string | undefined
): AvatarBubbleState {
  const currentAssistantBubble = state.assistantBubble;
  const previousProgressUnitCount =
    currentAssistantBubble !== null && currentAssistantBubble.generationId === generationId
      ? currentAssistantBubble.progressUnitCount
      : 0;

  return {
    ...state,
    assistantBubble: {
      ...createAssistantCaptionWindow({
        text,
        generationId,
        captionTiming: state.activeCharacterCaptionTiming,
        textFinal: true,
        progressUnitCount: previousProgressUnitCount
      }),
      kind: "assistant_reply",
      ttlMs: null
    }
  };
}

/**
 * 按本地字幕 tick 推进助手气泡，只移动可见字幕窗口，不暴露 LLM 已生成全文。
 */
export function applyAssistantCaptionTick(
  state: AvatarBubbleState,
  deltaMs: number
): AvatarBubbleState {
  if (!state.assistantBubble || state.assistantBubble.ttlMs !== null) {
    return state;
  }

  return {
    ...state,
    assistantBubble: {
      ...tickAssistantCaptionWindow(state.assistantBubble, { deltaMs }),
      kind: "assistant_reply",
      ttlMs: null
    }
  };
}

/**
 * 音频真正播完后才启动助手气泡阅读缓冲倒计时。
 */
export function applyAssistantAudioFinished(
  state: AvatarBubbleState,
  generationId: string
): AvatarBubbleState {
  if (!state.assistantBubble) {
    return state;
  }

  if (state.assistantBubble.generationId && state.assistantBubble.generationId !== generationId) {
    return state;
  }

  return {
    ...state,
    assistantBubble: {
      ...finishAssistantCaptionWindow(state.assistantBubble),
      kind: "assistant_reply",
      ttlMs: ASSISTANT_AUDIO_FINISHED_TTL_MS
    }
  };
}
