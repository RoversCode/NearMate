import type { RendererAppEvent } from "../types/events";
import type { CharacterVoiceCaptionTiming } from "./assistantCaptionWindow";

/**
 * 底部普通气泡用于展示用户语音或系统通知。
 */
type AvatarSpeechOrNoticeBubble = {
  kind: "speech" | "notice";
  text: string;
  ttlMs: number;
  /** 用户语音气泡所属的输入 Turn；系统通知没有这个关联。 */
  turnId?: string;
};

/**
 * Smart Turn 等待气泡是后端低置信续说提示，必须保持 sticky，直到后端结束事件或用户继续说话。
 */
type AvatarSmartTurnWaitingBubble = {
  kind: "smart_turn_waiting";
  text: string;
  sessionId?: string;
  turnId?: string;
  probability: number;
};

/** Self-Reflection 正文公开前的头部临时状态或辅助回应。 */
export type AvatarReflectionStatus = {
  kind: "reflection_status";
  mode: "thinking" | "interruption_reply";
  text: string;
  selfReflectionId: string;
  episodeId: string;
  revision: number;
  recoveryRequestId: string;
  recoverySessionId: string;
  recoveryGenerationId: string;
  auxiliaryRequestId?: string;
  auxiliaryGenerationId?: string;
  phase: "typing" | "full_pause" | "clear_pause";
  elapsedMs: number;
};

export type BottomAvatarBubble =
  | AvatarSpeechOrNoticeBubble
  | AvatarSmartTurnWaitingBubble;

/**
 * 助手回复气泡只承载桌宠自身输出，生命周期绑定文本流和音频播放完成。
 */
export type AvatarAssistantBubble = {
  kind: "assistant_reply";
  text: string;
  visibleText: string;
  generationId?: string;
  textFinal: boolean;
  progressUnitCount: number;
  captionMsPerUnit: number;
  ttlMs: number | null;
};

/**
 * Avatar 当前知道的输入 Turn。Final 到达后保留身份但标记收口，用来挡住迟到 partial。
 */
type AvatarInputTurnProjection = {
  inputRuntimeId: string;
  inputRuntimeEpoch: number;
  turnId: string;
  turnSequence: number;
  finalized: boolean;
};

type AvatarInputRuntimeProjection = {
  inputRuntimeId: string;
  inputRuntimeEpoch: number;
};

/**
 * 头像气泡状态拆成底部槽和助手槽。
 * 核心入参是后端事件或本地超时事件；预期输出是下一帧可渲染的双气泡状态。
 * 边界异常：Smart Turn 等待态只允许对应结束事件清除。
 */
export type AvatarBubbleState = {
  showSpeechBubbles: boolean;
  /** Chat 展开期间冻结 Avatar 的对话视觉投影，关闭后只接受新事件。 */
  chatExpanded?: boolean;
  activeCharacterCaptionTiming: CharacterVoiceCaptionTiming | null;
  bottomBubble: BottomAvatarBubble | null;
  assistantBubble: AvatarAssistantBubble | null;
  reflectionStatus?: AvatarReflectionStatus | null;
  /** 最近一次已知输入 Turn；即使 Final 已到达，也保留身份用于拒绝迟到结果。 */
  inputTurn?: AvatarInputTurnProjection;
  /** 控制面最近确认的输入 Runtime；用于隔离热替换前的迟到瞬时事件。 */
  inputRuntime?: AvatarInputRuntimeProjection;
};

type AvatarBubbleTimeoutEvent = {
  type: "avatar.bubble_timeout";
};

type AvatarAssistantBubbleTimeoutEvent = {
  type: "avatar.assistant_bubble_timeout";
};

type AvatarAssistantCaptionTickEvent = {
  type: "avatar.assistant_caption_tick";
  payload: {
    delta_ms: number;
  };
};

type AvatarSelfReflectionThinkingTickEvent = {
  type: "avatar.self_reflection_thinking_tick";
  payload: { delta_ms: number };
};

type AvatarChatModeChangedEvent = {
  type: "avatar.chat_mode_changed";
  mode: "collapsed" | "expanded";
};

export type AvatarBubbleEvent =
  | RendererAppEvent
  | AvatarBubbleTimeoutEvent
  | AvatarAssistantBubbleTimeoutEvent
  | AvatarAssistantCaptionTickEvent
  | AvatarSelfReflectionThinkingTickEvent
  | AvatarChatModeChangedEvent;
