import {
  createAssistantCaptionWindow
} from "./assistantCaptionWindow";
import type { AvatarBubbleEvent, AvatarBubbleState } from "./avatarBubbleTypes";

/**
 * 推进底部气泡状态。
 * 核心入参是头像气泡总状态与 renderer 事件；预期输出是底部气泡相关事件处理后的状态。
 * 边界异常：非底部气泡事件原样返回，Smart Turn 等待态由对应结束事件关闭。
 */
export function applyBottomAvatarBubbleEvent(
  state: AvatarBubbleState,
  event: AvatarBubbleEvent
): AvatarBubbleState {
  if (event.type === "control.connection_changed") {
    // Backend 重启后 epoch 可以重新开始；connecting 必须清掉旧进程的全部比较基线。
    if (event.payload.status !== "connecting" || (!state.inputTurn && !state.inputRuntime)) {
      return state;
    }
    return {
      ...state,
      inputTurn: undefined,
      inputRuntime: undefined
    };
  }

  if (event.type === "input.runtime_changed") {
    const nextRuntime = createInputRuntimeProjection(event.payload);
    if (!canRuntimeTakeOwnership(nextRuntime, state.inputRuntime)) {
      return state;
    }
    // 同一 Runtime 的首帧或重放只是幂等快照，不能清掉已经 Final 的 Turn 基线。
    if (isSameInputRuntime(nextRuntime, state.inputRuntime)) {
      return state;
    }
    return {
      ...state,
      inputRuntime: nextRuntime,
      inputTurn: undefined
    };
  }

  if (event.type === "avatar.bubble_timeout") {
    return clearTimedBottomBubble(state);
  }

  if (event.type === "wake.detected") {
    if (!state.showSpeechBubbles) {
      return state;
    }

    const wakeText = typeof event.payload.text === "string" && event.payload.text.trim()
      ? event.payload.text.trim()
      : "我在，正在听。";
    return {
      ...state,
      bottomBubble: {
        kind: "speech",
        text: wakeText,
        ttlMs: 2400
      }
    };
  }

  if (event.type === "input.smart_turn_waiting_user_started") {
    if (!state.showSpeechBubbles) {
      return state;
    }

    if (
      state.bottomBubble?.kind === "smart_turn_waiting" &&
      state.bottomBubble.turnId === event.payload.turn_id
    ) {
      return state;
    }

    return {
      ...state,
      bottomBubble: {
        kind: "smart_turn_waiting",
        text: event.payload.hint_text,
        sessionId: event.payload.session_id,
        turnId: event.payload.turn_id,
        probability: event.payload.probability
      }
    };
  }

  if (event.type === "input.smart_turn_waiting_user_ended") {
    if (state.bottomBubble?.kind !== "smart_turn_waiting") {
      return state;
    }

    if (state.bottomBubble.turnId !== event.payload.turn_id) {
      return state;
    }

    return {
      ...state,
      bottomBubble: null
    };
  }

  if (event.type === "turn.state_changed") {
    const turnId = normalizeTurnId(event.payload.turn_id);
    // 只有新的 listening 事实才能切换当前输入 Turn；迟到的 processing/idle 不能把身份回退到旧轮次。
    if (
      event.payload.turn_state !== "listening"
      || !turnId
      || event.payload.turn_sequence === undefined
    ) {
      return state;
    }

    const nextTurn = createInputTurnProjection({
      ...event.payload,
      turn_id: turnId,
      turn_sequence: event.payload.turn_sequence
    }, false);
    // 同一 Turn 的迟到 listening 不能重新打开已经 Final 的输入，旧编号也不能让状态回退。
    if (
      !canRuntimeTakeOwnership(nextTurn, state.inputRuntime)
      || isSameInputTurn(state.inputTurn, nextTurn)
      || isOlderInputTurn(nextTurn, state.inputTurn)
    ) {
      return state;
    }

    return {
      ...state,
      inputRuntime: createInputRuntimeProjection(event.payload),
      inputTurn: nextTurn
    };
  }

  if (event.type === "user.partial_transcript") {
    const normalizedText = event.payload.text.trim();
    // ASR 的空结果不是用户输入，不能创建空气泡，也不能打断正在显示的助手字幕。
    if (!normalizedText) {
      return state;
    }

    const partialTurnId = normalizeTurnId(event.payload.turn_id);
    const partialTurn = partialTurnId
      ? createInputTurnProjection(event.payload, false)
      : undefined;
    // 当前 Turn 已知时，缺失身份、已收口的同 Turn 或更旧 Turn 都不能重新打开用户气泡。
    if (
      !partialTurn
      || !canRuntimeTakeOwnership(partialTurn, state.inputRuntime)
      || !canApplyPartialToInputTurn(state.inputTurn, partialTurn)
    ) {
      return state;
    }

    if (!state.showSpeechBubbles) {
      return {
        ...state,
        inputRuntime: createInputRuntimeProjection(event.payload),
        ...(partialTurn ? { inputTurn: partialTurn } : {}),
        assistantBubble: null
      };
    }

    return {
      ...state,
      inputRuntime: createInputRuntimeProjection(event.payload),
      ...(partialTurn ? { inputTurn: partialTurn } : {}),
      assistantBubble: null,
      bottomBubble: {
        kind: "speech",
        text: normalizedText,
        ttlMs: 1800,
        ...(partialTurnId ? { turnId: partialTurnId } : {})
      }
    };
  }

  if (event.type === "user.final_transcript") {
    const finalTurnId = normalizeTurnId(event.payload.turn_id);
    const voicePayload = finalTurnId
      && typeof event.payload.input_runtime_id === "string"
      && typeof event.payload.input_runtime_epoch === "number"
      && typeof event.payload.turn_sequence === "number"
      ? {
          input_runtime_id: event.payload.input_runtime_id,
          input_runtime_epoch: event.payload.input_runtime_epoch,
          turn_id: finalTurnId,
          turn_sequence: event.payload.turn_sequence
        }
      : undefined;
    const finalTurn = voicePayload
      ? createInputTurnProjection(voicePayload, true)
      : undefined;
    // 同一会话内已经看到更新 Turn 时，迟到的旧 Final 也不能把用户气泡回退。
    if (
      finalTurn
      && (
        !canRuntimeTakeOwnership(finalTurn, state.inputRuntime)
        || isOlderInputTurn(finalTurn, state.inputTurn)
      )
    ) {
      return state;
    }
    if (!state.showSpeechBubbles) {
      return finalTurn ? {
        ...state,
        inputRuntime: createInputRuntimeProjection(voicePayload!),
        inputTurn: finalTurn
      } : state;
    }

    return {
      ...state,
      ...(voicePayload ? { inputRuntime: createInputRuntimeProjection(voicePayload) } : {}),
      ...(finalTurn ? { inputTurn: finalTurn } : {}),
      bottomBubble: {
        kind: "speech",
        text: event.payload.text,
        ttlMs: 3200,
        ...(finalTurnId ? { turnId: finalTurnId } : {})
      }
    };
  }

  if (event.type !== "shell.avatar_bubble_changed") {
    return state;
  }

  if (event.payload.kind === "hidden") {
    if (!state.bottomBubble) {
      return state;
    }
    return { ...state, bottomBubble: null };
  }

  if (event.payload.kind === "speech") {
    if (!state.showSpeechBubbles || state.assistantBubble) {
      return state;
    }

    return {
      ...state,
      assistantBubble: {
        ...createAssistantCaptionWindow({
          text: event.payload.text,
          generationId: undefined,
          captionTiming: state.activeCharacterCaptionTiming,
          textFinal: true,
          progressUnitCount: Number.MAX_SAFE_INTEGER
        }),
        kind: "assistant_reply",
        ttlMs: normalizeTtlMs(event.payload.ttl_ms)
      }
    };
  }

  if (event.payload.kind === "notice") {
    return {
      ...state,
      bottomBubble: {
        kind: "notice",
        text: event.payload.text,
        ttlMs: normalizeTtlMs(event.payload.ttl_ms)
      }
    };
  }

  return state;
}

/**
 * 只清除会自动消失的底部普通气泡，Smart Turn 等待由明确事件关闭。
 */
export function clearTimedBottomBubble(state: AvatarBubbleState): AvatarBubbleState {
  if (state.bottomBubble?.kind === "speech" || state.bottomBubble?.kind === "notice") {
    return {
      ...state,
      bottomBubble: null
    };
  }

  return state;
}

/**
 * 规范化外部传入的气泡停留时间。
 * 核心入参是后端 ttl_ms；预期输出是安全的毫秒数，避免负数或非数字导致计时器行为异常。
 */
function normalizeTtlMs(ttlMs: number): number {
  if (!Number.isFinite(ttlMs)) {
    return 0;
  }

  return Math.max(0, ttlMs);
}

/**
 * 清理可选 Turn 身份；空白身份按未提供处理，避免进入气泡状态。
 * @param turnId 控制事件携带的可选输入 Turn 身份。
 * @returns 清洗后的身份；空白或缺失时返回空。
 */
function normalizeTurnId(turnId: string | null | undefined): string | undefined {
  const normalizedTurnId = turnId?.trim();
  return normalizedTurnId || undefined;
}

/**
 * 创建统一的输入 Turn 投影，避免各事件分支用不同方式比较身份。
 * @param sessionId 控制事件携带的可选交互会话身份。
 * @param turnId 已清洗的输入 Turn 身份。
 * @param finalized 该 Turn 是否已经收到 Final transcript。
 * @returns 可写入 Avatar 状态的输入 Turn 投影。
 */
function createInputTurnProjection(
  payload: {
    input_runtime_id: string;
    input_runtime_epoch: number;
    turn_id: string;
    turn_sequence: number;
  },
  finalized: boolean
): NonNullable<AvatarBubbleState["inputTurn"]> {
  return {
    inputRuntimeId: payload.input_runtime_id,
    inputRuntimeEpoch: payload.input_runtime_epoch,
    turnId: payload.turn_id,
    turnSequence: payload.turn_sequence,
    finalized
  };
}

function createInputRuntimeProjection(payload: {
  input_runtime_id: string;
  input_runtime_epoch: number;
}): NonNullable<AvatarBubbleState["inputRuntime"]> {
  return {
    inputRuntimeId: payload.input_runtime_id,
    inputRuntimeEpoch: payload.input_runtime_epoch
  };
}

function canRuntimeTakeOwnership(
  candidateRuntime: NonNullable<AvatarBubbleState["inputRuntime"]>,
  currentRuntime: AvatarBubbleState["inputRuntime"]
): boolean {
  if (!currentRuntime) {
    return true;
  }
  if (candidateRuntime.inputRuntimeEpoch > currentRuntime.inputRuntimeEpoch) {
    return true;
  }
  return candidateRuntime.inputRuntimeEpoch === currentRuntime.inputRuntimeEpoch
    && candidateRuntime.inputRuntimeId === currentRuntime.inputRuntimeId;
}

/**
 * 判断候选身份是否就是 Avatar 当前已经接管的输入 Runtime。
 * @param candidateRuntime 本次事件携带的输入 Runtime。
 * @param currentRuntime Avatar 当前保存的输入 Runtime。
 * @returns epoch 和 UUID 都一致时返回 `true`。
 */
function isSameInputRuntime(
  candidateRuntime: NonNullable<AvatarBubbleState["inputRuntime"]>,
  currentRuntime: AvatarBubbleState["inputRuntime"]
): boolean {
  return currentRuntime !== undefined
    && candidateRuntime.inputRuntimeEpoch === currentRuntime.inputRuntimeEpoch
    && candidateRuntime.inputRuntimeId === currentRuntime.inputRuntimeId;
}

/**
 * 判断两个投影是否明确属于同一个输入 Turn。
 * @param currentTurn Avatar 当前保存的输入 Turn。
 * @param candidateTurn 本次事件携带的输入 Turn。
 * @returns 身份可确认相同时返回 `true`。
 */
function isSameInputTurn(
  currentTurn: AvatarBubbleState["inputTurn"],
  candidateTurn: NonNullable<AvatarBubbleState["inputTurn"]>
): boolean {
  if (!currentTurn) {
    return false;
  }
  return currentTurn.inputRuntimeEpoch === candidateTurn.inputRuntimeEpoch
    && currentTurn.inputRuntimeId === candidateTurn.inputRuntimeId
    && currentTurn.turnSequence === candidateTurn.turnSequence;
}

/**
 * 判断候选 Turn 是否在同一会话中明确早于当前 Turn。
 * @param candidateTurn 本次事件携带的输入 Turn。
 * @param currentTurn Avatar 当前保存的输入 Turn。
 * @returns 两边编号可比较且候选更旧时返回 `true`。
 */
function isOlderInputTurn(
  candidateTurn: NonNullable<AvatarBubbleState["inputTurn"]>,
  currentTurn: AvatarBubbleState["inputTurn"]
): boolean {
  if (!currentTurn) {
    return false;
  }
  if (
    currentTurn.inputRuntimeEpoch !== candidateTurn.inputRuntimeEpoch
    || currentTurn.inputRuntimeId !== candidateTurn.inputRuntimeId
  ) {
    return false;
  }
  return candidateTurn.turnSequence < currentTurn.turnSequence;
}

/**
 * 判断 Partial 是否仍可进入当前气泡。
 * 已收口的同 Turn 和明确更旧的 Turn 必须拒绝；更新 Turn 可在重连漏掉 listening 时自行接管。
 * @param currentTurn Avatar 当前保存的输入 Turn。
 * @param partialTurn 本次 Partial 携带的输入 Turn。
 * @returns Partial 可以成为当前可见输入时返回 `true`。
 */
function canApplyPartialToInputTurn(
  currentTurn: AvatarBubbleState["inputTurn"],
  partialTurn: NonNullable<AvatarBubbleState["inputTurn"]> | undefined
): boolean {
  if (!currentTurn) {
    return true;
  }
  if (!partialTurn) {
    return false;
  }
  if (isSameInputTurn(currentTurn, partialTurn)) {
    return !currentTurn.finalized;
  }
  return isNewerInputTurn(partialTurn, currentTurn);
}

/**
 * 判断候选 Turn 是否明确晚于当前 Turn。
 * @param candidateTurn 本次事件携带的输入 Turn。
 * @param currentTurn Avatar 当前保存的输入 Turn。
 * @returns 会话已经切换，或两边编号可比较且候选更大时返回 `true`。
 */
function isNewerInputTurn(
  candidateTurn: NonNullable<AvatarBubbleState["inputTurn"]>,
  currentTurn: NonNullable<AvatarBubbleState["inputTurn"]>
): boolean {
  if (candidateTurn.inputRuntimeEpoch !== currentTurn.inputRuntimeEpoch) {
    return candidateTurn.inputRuntimeEpoch > currentTurn.inputRuntimeEpoch;
  }
  if (candidateTurn.inputRuntimeId !== currentTurn.inputRuntimeId) {
    return false;
  }
  return candidateTurn.turnSequence > currentTurn.turnSequence;
}
