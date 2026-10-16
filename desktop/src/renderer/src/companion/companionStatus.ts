import type { ActivityCompact, BackendEvent, ConnectionStatus } from "../types/backendEvents";
import {
  createInitialChatVoiceProjection,
  clearChatVoiceDisplay,
  hasOpenChatVoiceTurn,
  reduceChatVoiceProjection,
  type ChatVoiceProjection,
  type ChatVoiceState
} from "../chat/chatVoiceState";
import type { InteractionStateSnapshot } from "../types/interactionBackendEvents";

/** 当前角色可见状态的名称；聊天窗口和虚拟形象共用，图案不自行猜测业务状态。 */
export type CompanionDisplayState = "syncing" | "waiting_wake" | "available" | "listening"
  | "thinking" | "responding" | "working" | "waiting_user" | "error";

/** 保存服务端状态和待答事实的内存投影，连接重建后重新取得权威快照。 */
export type CompanionStatusState = {
  connectionStatus: ConnectionStatus;
  interaction: InteractionStateSnapshot | null;
  inputRuntime: { id: string; epoch: number } | null;
  legacyVoice: ChatVoiceProjection;
  legacyFollowup: "followup_cool" | "followup_hot" | null;
  outputUnavailable: boolean;
  manualToggleRevision: number | null;
  interactions: Record<string, { activityId: string; kind: "permission" | "question" }>;
};

/** 展示所需的文字、图案状态和独立唤醒提示；Avatar 只绘制图案，文字用于无障碍说明。 */
export type CompanionStatusPresentation = {
  state: CompanionDisplayState;
  label: string;
  description: string;
  wakeMode: "unknown" | "wake_required" | "followup";
  wakeLabel: string;
  manualToggleRevision: number | null;
  /** 输入 Runtime 更换后版本会重建；图案用此身份区分新回执和旧版本。 */
  inputRuntimeId?: string;
};

const STATUS_COPY: Record<CompanionDisplayState, { label: string; description: string }> = {
  syncing: { label: "状态同步中", description: "正在确认当前角色的状态。" },
  waiting_wake: { label: "等待唤醒", description: "叫我一声，再开始语音聊天。" },
  available: { label: "无需再唤醒", description: "跟进模式已开启，可以继续当前对话。" },
  listening: { label: "正在听", description: "我正在听你说，继续就好。" },
  thinking: { label: "思考中", description: "我正在组织回复。" },
  responding: { label: "正在回复", description: "我正在回答你。" },
  working: { label: "执行任务中", description: "任务正在进行，可以在下方查看进度。" },
  waiting_user: { label: "等待你处理", description: "任务需要你的确认或补充。" },
  error: { label: "暂时无法回复", description: "连接恢复后会重新同步状态。" }
};

/**
 * 创建尚未拿到 Backend 状态的空投影。
 *
 * @returns 等待连接和权威快照的角色状态。
 */
export function createInitialCompanionStatusState(): CompanionStatusState {
  return {
    connectionStatus: "connecting",
    interaction: null,
    inputRuntime: null,
    legacyVoice: createInitialChatVoiceProjection(),
    legacyFollowup: null,
    outputUnavailable: false,
    manualToggleRevision: null,
    interactions: {}
  };
}

/**
 * 连接重建时清掉旧进程状态，断线时立即撤下旧跟进模式提示。
 *
 * @param state 当前角色状态。
 * @param status 控制连接的新状态。
 * @returns 等待重新同步或标记断线后的状态。
 */
export function resetCompanionStatusConnection(
  state: CompanionStatusState,
  status: ConnectionStatus
): CompanionStatusState {
  if (status === "connecting") return createInitialCompanionStatusState();
  if (state.connectionStatus === status) return state;
  return { ...state, connectionStatus: status,
    legacyVoice: status === "disconnected" || status === "failed" ? clearChatVoiceDisplay(state.legacyVoice) : state.legacyVoice };
}

/**
 * 接纳一条状态事件；旧状态不能盖住权威快照，同一 Runtime 的切换回执可独立补上反馈。
 *
 * @param state 事件到达前的状态。
 * @param event 已经过公共协议校验的 Backend 事件。
 * @returns 新的状态投影；无关或过期事件保持原引用。
 */
export function reduceCompanionStatusState(
  state: CompanionStatusState,
  event: BackendEvent
): CompanionStatusState {
  if (event.type === "interaction.state_changed") {
    const next = event.payload;
    const baseline = state.inputRuntime;
    if (baseline && (next.input_runtime_epoch < baseline.epoch
      || (next.input_runtime_epoch === baseline.epoch && next.input_runtime_id !== baseline.id))) return state;
    const current = state.interaction;
    if (current && next.input_runtime_epoch < current.input_runtime_epoch) return state;
    const sameRuntimeEpoch = current && next.input_runtime_epoch === current.input_runtime_epoch;
    if (sameRuntimeEpoch && next.input_runtime_id !== current.input_runtime_id) return state;
    const manualToggleRevision = next.source === "manual_toggle"
      && (state.manualToggleRevision === null || next.revision > state.manualToggleRevision)
      ? next.revision : state.manualToggleRevision;
    if (sameRuntimeEpoch && next.revision <= current.revision) {
      // 停止语音的新状态可能抢先送达；只补未见过的切换反馈，不倒退轮次或唤醒模式。
      return manualToggleRevision !== state.manualToggleRevision
        ? { ...state, manualToggleRevision }
        : state;
    }
    return {
      ...state,
      interaction: next,
      inputRuntime: { id: next.input_runtime_id, epoch: next.input_runtime_epoch },
      manualToggleRevision: sameRuntimeEpoch ? manualToggleRevision
        : next.source === "manual_toggle" ? next.revision : null
    };
  }

  if (event.type === "input.runtime_changed") {
    const next = event.payload;
    const baseline = state.inputRuntime;
    if (baseline && next.input_runtime_epoch <= baseline.epoch) return state;
    // 新输入运行时已接管，旧运行时的“无需唤醒”必须先撤下，再等待新快照。
    return { ...state, interaction: null, legacyFollowup: null, manualToggleRevision: null,
      inputRuntime: { id: next.input_runtime_id, epoch: next.input_runtime_epoch },
      legacyVoice: reduceChatVoiceProjection(state.legacyVoice, event) };
  }

  if (event.type === "assistant.output_status_changed") {
    // 旧协议的失败提示没有完整交互快照可覆盖，输出恢复时也要明确清掉旧错误。
    const legacyVoice = event.payload.available && !state.interaction && !hasOpenChatVoiceTurn(state.legacyVoice)
      ? { ...state.legacyVoice, voiceState: "ready" as const } : state.legacyVoice;
    return { ...state, outputUnavailable: !event.payload.available, legacyVoice };
  }
  if (event.type === "background_agent.interactions_snapshot") {
    return {
      ...state,
      interactions: Object.fromEntries(event.payload.items.filter(item => item.status === "pending")
        .map(item => [item.request_id, { activityId: item.activity_id, kind: item.kind }]))
    };
  }
  if (event.type === "background_agent.interaction_requested") {
    return { ...state, interactions: { ...state.interactions,
      [event.payload.request_id]: { activityId: event.payload.activity_id, kind: event.payload.kind } } };
  }
  if (event.type === "background_agent.interaction_submitted" || event.type === "background_agent.interaction_resolved") {
    if (!state.interactions[event.payload.request_id]) return state;
    const interactions = { ...state.interactions };
    delete interactions[event.payload.request_id];
    return { ...state, interactions };
  }

  // 旧事件仍服务旧调用方和尚未升级的 Backend；权威快照到达后不再用正文或音频猜状态。
  if (state.interaction) return state;
  if (event.type === "followup.state_changed") {
    return { ...state, legacyFollowup: event.payload.followup_state };
  }
  let legacyVoice = reduceChatVoiceProjection(state.legacyVoice, event);
  if (!hasOpenChatVoiceTurn(legacyVoice)) {
    let voiceState = legacyVoice.voiceState;
    if (event.type === "assistant.self_reflection_status_changed") voiceState = event.payload.active ? "thinking" : "responding";
    else if (event.type === "assistant.text_chunk" || event.type === "assistant.text_segment_final"
      || event.type === "assistant.text_final") voiceState = "responding";
    else if (event.type === "assistant.audio_finished") voiceState = "ready";
    else if (event.type === "assistant.request_failed") voiceState = "error";
    if (voiceState !== legacyVoice.voiceState) legacyVoice = { ...legacyVoice, voiceState };
  }
  return legacyVoice === state.legacyVoice ? state : { ...state, legacyVoice };
}

/**
 * 把聊天、唤醒模式和后台任务组合为一个可见状态，同时保留独立的唤醒提示。
 *
 * @param state 当前权威交互投影。
 * @param activities 当前运行期的活动任务。
 * @returns 聊天文案与 Avatar 图案共用的展示数据。
 */
export function projectCompanionStatus(
  state: CompanionStatusState,
  activities: readonly ActivityCompact[]
): CompanionStatusPresentation {
  const followup = state.interaction?.followup_state ?? state.legacyFollowup;
  const connected = state.connectionStatus === "connected";
  const wakeMode = !connected || followup === null ? "unknown"
    : followup === "followup_hot" ? "followup" : "wake_required";
  const turn = state.interaction?.turn_state;
  const voiceState = turn === "listening" ? "listening" : turn === "processing" ? "thinking"
    : turn === "speaking" ? "responding" : turn === "idle" ? "ready" : state.legacyVoice.voiceState;
  let displayState: CompanionDisplayState = wakeMode === "followup" ? "available" : "waiting_wake";
  const waiting = activities.filter(activity => activity.status === "waiting_user");
  const pending = Object.values(state.interactions).find(item => waiting.some(activity => activity.activity_id === item.activityId));
  if (!connected) displayState = state.connectionStatus === "connecting" || state.connectionStatus === "idle" ? "syncing" : "error";
  else if (state.outputUnavailable || voiceState === "error") displayState = "error";
  else if (voiceState !== "ready") displayState = voiceState;
  else if (waiting.length > 0) displayState = "waiting_user";
  else if (activities.some(activity => activity.kind === "background_agent")) displayState = "working";
  else if (activities.length > 0) displayState = "thinking";
  else if (wakeMode === "unknown") displayState = "syncing";
  const copy = STATUS_COPY[displayState];
  return {
    state: displayState,
    label: displayState === "waiting_user" && pending
      ? pending.kind === "permission" ? "等你授权" : "等你补充" : copy.label,
    description: copy.description,
    wakeMode,
    wakeLabel: wakeMode === "unknown" ? "正在同步唤醒状态" : wakeMode === "followup" ? "无需再唤醒" : "需要唤醒",
    manualToggleRevision: state.manualToggleRevision,
    ...(state.inputRuntime ? { inputRuntimeId: state.inputRuntime.id } : {})
  };
}

/**
 * 保留旧语音状态接口供现有调用方使用，新的界面展示以 companionStatus 为准。
 *
 * @param state 当前交互投影。
 * @returns 不包含后台任务状态的旧语音生命周期值。
 */
export function projectLegacyChatVoiceState(state: CompanionStatusState): ChatVoiceState {
  if (state.outputUnavailable) return "error";
  const turn = state.interaction?.turn_state;
  if (!turn) return state.legacyVoice.voiceState;
  return turn === "listening" ? "listening" : turn === "processing" ? "thinking"
    : turn === "speaking" ? "responding" : "ready";
}
