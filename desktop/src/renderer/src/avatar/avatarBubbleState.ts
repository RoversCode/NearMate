import sharedDefaultSettings from "../../../../../shared/default-settings.json";
import {
  appendAssistantTextChunk,
  applyAssistantAudioFinished,
  applyAssistantCaptionTick,
  applyAssistantTextFinal,
  ASSISTANT_AUDIO_FINISHED_TTL_MS,
  clearAssistantBubble
} from "./avatarAssistantBubble";
import { applyBottomAvatarBubbleEvent } from "./avatarBubbleBottom";
import type {
  AvatarBubbleEvent,
  AvatarBubbleState,
  AvatarReflectionStatus
} from "./avatarBubbleTypes";

export type { AvatarAssistantBubble, AvatarBubbleEvent, AvatarBubbleState, BottomAvatarBubble } from "./avatarBubbleTypes";

export { ASSISTANT_AUDIO_FINISHED_TTL_MS };

const defaultSettingsPayload = sharedDefaultSettings as {
  show_speech_bubbles: boolean;
};

export const initialAvatarBubbleState: AvatarBubbleState = {
  showSpeechBubbles: defaultSettingsPayload.show_speech_bubbles,
  chatExpanded: false,
  activeCharacterCaptionTiming: null,
  bottomBubble: null,
  assistantBubble: null,
  reflectionStatus: null
};

const REFLECTION_THINKING_TEXT = "思考中.......";
const REFLECTION_THINKING_STEP_MS = 220;
const REFLECTION_THINKING_FULL_PAUSE_MS = 500;
const REFLECTION_THINKING_CLEAR_PAUSE_MS = 220;

/**
 * 根据 renderer 事件推进头像气泡状态。
 * 核心入参是当前状态与事件；预期输出是可直接供 AvatarApp 渲染的新状态。
 * 边界异常：未知事件保持原状态，Smart Turn 等待态只接受对应结束事件关闭。
 */
export function reduceAvatarBubbleState(
  state: AvatarBubbleState,
  event: AvatarBubbleEvent
): AvatarBubbleState {
  if (event.type === "avatar.chat_mode_changed") {
    if (event.mode === "expanded") {
      return {
        ...state,
        chatExpanded: true,
        bottomBubble: null,
        assistantBubble: null,
        reflectionStatus: null
      };
    }
    return {
      ...state,
      chatExpanded: false,
      bottomBubble: null,
      assistantBubble: null,
      reflectionStatus: null
    };
  }

  if (event.type === "avatar.assistant_bubble_timeout") {
    return clearAssistantBubble(state);
  }

  if (event.type === "avatar.assistant_caption_tick") {
    return applyAssistantCaptionTick(state, event.payload.delta_ms);
  }

  if (event.type === "avatar.self_reflection_thinking_tick") {
    return applySelfReflectionThinkingTick(state, event.payload.delta_ms);
  }

  if (event.type === "settings.changed" || event.type === "settings.updated") {
    return {
      ...state,
      showSpeechBubbles: event.payload.show_speech_bubbles,
      activeCharacterCaptionTiming: event.payload.active_character_caption_timing ?? null,
      bottomBubble: event.payload.show_speech_bubbles ? state.bottomBubble : null,
      reflectionStatus: event.payload.show_speech_bubbles
        ? state.reflectionStatus
        : null
    };
  }

  // 展开 Chat 后，Avatar 不再缓存任何会在关闭时复活的对话视觉状态。
  if (state.chatExpanded && event.type !== "control.connection_changed" && event.type !== "input.runtime_changed") {
    return state;
  }

  if (event.type === "assistant.self_reflection_status_changed") {
    if (!event.payload.active) {
      return clearMatchingReflectionStatus(state, event.payload);
    }
    if (!state.showSpeechBubbles) {
      // 即使用户关闭了气泡，Recovery 接管仍要清掉 Entry 遗留状态，避免设置恢复后旧回复复活。
      return { ...state, assistantBubble: null, reflectionStatus: null };
    }
    if (
      state.reflectionStatus
      && recoveryIdentityMatches(state.reflectionStatus, event.payload)
      && event.payload.revision <= state.reflectionStatus.revision
    ) {
      return state;
    }

    // Recovery 已经取代 Entry 成为当前 Foreground，请立即交出头部展示权。
    // 旧气泡可能因音频终态被新请求忽略而永不启动隐藏倒计时，不能继续留在状态里遮住思考提示。
    return {
      ...state,
      assistantBubble: null,
      reflectionStatus: createReflectionStatus(event.payload)
    };
  }

  if (event.type === "assistant.text_chunk") {
    if (event.payload.request_source === "self_reflection_interruption_reply") {
      return appendInterruptionReplyChunk(state, event.payload);
    }
    if (!state.showSpeechBubbles) {
      return state;
    }

    const nextState = clearReflectionStatusForRecoveryText(state, event.payload);
    return appendAssistantTextChunk(
      nextState,
      event.payload.text,
      event.payload.generation_id
    );
  }

  if (
    event.type === "assistant.text_segment_final"
    || event.type === "assistant.text_final"
  ) {
    if (event.payload.request_source === "self_reflection_interruption_reply") {
      return applyInterruptionReplyFinal(state, event.payload);
    }
    if (!state.showSpeechBubbles) {
      return state;
    }

    // 协议失败终态没有可展示正文，它只负责关闭此前已经出现的流式草稿。
    if (!event.payload.text.trim()) {
      return clearAssistantBubble(state);
    }

    return applyAssistantTextFinal(
      state,
      event.payload.text,
      event.payload.generation_id
    );
  }

  if (event.type === "assistant.audio_finished") {
    if (event.payload.request_source === "self_reflection_interruption_reply") {
      return restoreThinkingAfterInterruptionReply(state, event.payload);
    }
    return applyAssistantAudioFinished(state, event.payload.generation_id);
  }

  if (
    event.type === "user.final_transcript"
    && event.payload.history_disposition === "unattended_episode"
  ) {
    const nextState = restoreThinkingForEpisode(state, event.payload.episode_id);
    return applyBottomAvatarBubbleEvent(nextState, event);
  }

  if (event.type === "self_reflection.episode_committed") {
    return state.reflectionStatus?.episodeId === event.payload.episode_id
      && state.reflectionStatus.selfReflectionId === event.payload.self_reflection_id
      ? { ...state, reflectionStatus: null }
      : state;
  }

  if (event.type === "barge_in.detected") {
    return clearAssistantBubble(state);
  }

  return applyBottomAvatarBubbleEvent(state, event);
}

function createReflectionStatus(payload: {
  request_id: string;
  session_id: string;
  generation_id: string;
  self_reflection_id: string;
  episode_id: string;
  revision: number;
}): AvatarReflectionStatus {
  return {
    kind: "reflection_status",
    mode: "thinking",
    text: "",
    selfReflectionId: payload.self_reflection_id,
    episodeId: payload.episode_id,
    revision: payload.revision,
    recoveryRequestId: payload.request_id,
    recoverySessionId: payload.session_id,
    recoveryGenerationId: payload.generation_id,
    phase: "typing",
    elapsedMs: 0
  };
}

function clearMatchingReflectionStatus(
  state: AvatarBubbleState,
  identity: {
    request_id: string;
    session_id: string;
    generation_id: string;
    self_reflection_id: string;
    episode_id: string;
    revision: number;
  }
): AvatarBubbleState {
  const status = state.reflectionStatus;
  if (
    !status
    || !recoveryIdentityMatches(status, identity)
    || identity.revision < status.revision
  ) {
    return state;
  }
  return { ...state, reflectionStatus: null };
}

function reflectionEpisodeMatches(
  status: AvatarReflectionStatus,
  identity: {
    self_reflection_id: string;
    episode_id: string;
  }
): boolean {
  return status.selfReflectionId === identity.self_reflection_id
    && status.episodeId === identity.episode_id;
}

function recoveryIdentityMatches(
  status: AvatarReflectionStatus,
  identity: {
    request_id: string;
    session_id: string;
    generation_id: string;
    self_reflection_id: string;
    episode_id: string;
  }
): boolean {
  return reflectionEpisodeMatches(status, identity)
    && status.recoveryRequestId === identity.request_id
    && status.recoverySessionId === identity.session_id
    && status.recoveryGenerationId === identity.generation_id;
}

function clearReflectionStatusForRecoveryText(
  state: AvatarBubbleState,
  payload: {
    request_id: string;
    session_id: string;
    generation_id: string;
  }
): AvatarBubbleState {
  const status = state.reflectionStatus;
  if (
    !status
    || status.recoveryRequestId !== payload.request_id
    || status.recoverySessionId !== payload.session_id
    || status.recoveryGenerationId !== payload.generation_id
  ) {
    return state;
  }
  return { ...state, reflectionStatus: null };
}

function applySelfReflectionThinkingTick(
  state: AvatarBubbleState,
  deltaMs: number
): AvatarBubbleState {
  const status = state.reflectionStatus;
  if (
    !status
    || status.mode !== "thinking"
    || !Number.isFinite(deltaMs)
    || deltaMs <= 0
  ) {
    return state;
  }

  let phase = status.phase;
  let elapsedMs = status.elapsedMs + deltaMs;
  let text = status.text;

  while (true) {
    const limit = phase === "typing"
      ? REFLECTION_THINKING_STEP_MS
      : phase === "full_pause"
        ? REFLECTION_THINKING_FULL_PAUSE_MS
        : REFLECTION_THINKING_CLEAR_PAUSE_MS;
    if (elapsedMs < limit) {
      break;
    }
    elapsedMs -= limit;
    if (phase === "typing") {
      text = REFLECTION_THINKING_TEXT.slice(0, text.length + 1);
      if (text.length >= REFLECTION_THINKING_TEXT.length) {
        phase = "full_pause";
      }
    } else if (phase === "full_pause") {
      text = "";
      phase = "clear_pause";
    } else {
      phase = "typing";
    }
  }

  return {
    ...state,
    reflectionStatus: {
      ...status,
      text,
      phase,
      elapsedMs
    }
  };
}

function appendInterruptionReplyChunk(
  state: AvatarBubbleState,
  payload: {
    request_id: string;
    generation_id: string;
    self_reflection_id: string;
    episode_id: string;
    text: string;
  }
): AvatarBubbleState {
  const status = state.reflectionStatus;
  if (
    !state.showSpeechBubbles
    || !status
    || !reflectionEpisodeMatches(status, payload)
  ) {
    return state;
  }
  const sameReply = status.auxiliaryRequestId === payload.request_id
    && status.auxiliaryGenerationId === payload.generation_id;
  return {
    ...state,
    reflectionStatus: {
      ...status,
      mode: "interruption_reply",
      text: sameReply ? `${status.text}${payload.text}` : payload.text,
      auxiliaryRequestId: payload.request_id,
      auxiliaryGenerationId: payload.generation_id
    }
  };
}

function applyInterruptionReplyFinal(
  state: AvatarBubbleState,
  payload: {
    request_id: string;
    generation_id: string;
    self_reflection_id: string;
    episode_id: string;
    text: string;
  }
): AvatarBubbleState {
  const status = state.reflectionStatus;
  if (
    !status
    || !reflectionEpisodeMatches(status, payload)
    || (
      status.auxiliaryRequestId !== undefined
      && status.auxiliaryRequestId !== payload.request_id
    )
  ) {
    return state;
  }
  return {
    ...state,
    reflectionStatus: {
      ...status,
      mode: "interruption_reply",
      text: payload.text,
      auxiliaryRequestId: payload.request_id,
      auxiliaryGenerationId: payload.generation_id
    }
  };
}

function restoreThinkingAfterInterruptionReply(
  state: AvatarBubbleState,
  payload: {
    request_id: string;
    generation_id: string;
    episode_id: string;
  }
): AvatarBubbleState {
  const status = state.reflectionStatus;
  if (
    !status
    || status.episodeId !== payload.episode_id
    || status.auxiliaryRequestId !== payload.request_id
    || status.auxiliaryGenerationId !== payload.generation_id
  ) {
    return state;
  }
  return resetReflectionStatusToThinking(state, status);
}

function restoreThinkingForEpisode(
  state: AvatarBubbleState,
  episodeId: string
): AvatarBubbleState {
  const status = state.reflectionStatus;
  if (!status || status.episodeId !== episodeId || status.mode === "thinking") {
    return state;
  }
  return resetReflectionStatusToThinking(state, status);
}

function resetReflectionStatusToThinking(
  state: AvatarBubbleState,
  status: AvatarReflectionStatus
): AvatarBubbleState {
  return {
    ...state,
    reflectionStatus: {
      ...status,
      mode: "thinking",
      text: "",
      phase: "typing",
      elapsedMs: 0,
      auxiliaryRequestId: undefined,
      auxiliaryGenerationId: undefined
    }
  };
}
