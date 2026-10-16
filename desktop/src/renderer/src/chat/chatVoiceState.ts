import type { BackendEvent } from "../types/events";

/** Chat 右栏展示的语音生命周期状态。 */
export type ChatVoiceState = "ready" | "listening" | "thinking" | "responding" | "error";

type InputRuntimeIdentity = {
  inputRuntimeId: string;
  inputRuntimeEpoch: number;
};

type InputTurnIdentity = InputRuntimeIdentity & {
  turnId: string;
  turnSequence: number;
};

/**
 * 保存 Chat 临时转写以及它所属输入世代的纯投影。
 * `closedTurn` 是墓碑：它只阻止同一 Turn 的迟到 partial，不影响更高序号的新 Turn。
 */
export type ChatVoiceProjection = {
  voiceState: ChatVoiceState;
  liveTranscript: string;
  inputRuntime: InputRuntimeIdentity | null;
  inputTurn: InputTurnIdentity | null;
  closedTurn: InputTurnIdentity | null;
};

/**
 * 创建空的实时语音投影。
 *
 * @returns 没有输入 Runtime、Turn 或临时转写的待命状态。
 */
export function createInitialChatVoiceProjection(): ChatVoiceProjection {
  return {
    voiceState: "ready",
    liveTranscript: "",
    inputRuntime: null,
    inputTurn: null,
    closedTurn: null
  };
}

/**
 * 清掉断线时不应继续显示的临时语音内容，同时保留输入身份基线。
 * 下次 connecting 会再清空身份基线，避免旧连接的事件继续接管新连接。
 *
 * @param state 当前实时语音投影。
 * @returns 清空临时展示后的投影。
 */
export function clearChatVoiceDisplay(state: ChatVoiceProjection): ChatVoiceProjection {
  if (state.liveTranscript === "" && state.voiceState === "ready") {
    return state;
  }
  return {
    ...state,
    liveTranscript: "",
    voiceState: "ready"
  };
}

/**
 * 判断当前是否仍有未收口的输入 Turn。
 * 即使实时转写暂时为空，监听中的 Turn 仍属于用户输入，助手输出事件不能抢走它的状态。
 *
 * @param state 当前实时语音投影。
 * @returns 存在尚未被 Final 或输入终态封存的 Turn 时返回 true。
 */
export function hasOpenChatVoiceTurn(state: ChatVoiceProjection): boolean {
  return state.inputTurn !== null && !sameTurn(state.inputTurn, state.closedTurn);
}

/**
 * 推进一条 Backend 输入事件对应的实时语音投影。
 *
 * @param state 事件到达前的投影。
 * @param event 已经过协议解析的 Backend 事件。
 * @returns 应用事件后的新投影；迟到或无关事件返回原状态引用。
 */
export function reduceChatVoiceProjection(
  state: ChatVoiceProjection,
  event: BackendEvent
): ChatVoiceProjection {
  if (event.type === "wake.detected") {
    const nextRuntime = toRuntimeIdentity(event.payload);
    const runtimeOrder = compareRuntime(nextRuntime, state.inputRuntime);
    if (runtimeOrder === "stale") {
      return state;
    }

    // 唤醒表示一段新的聆听窗口已经开始。先前 Turn 遗留的 partial 不能继续占据输入栏，
    // 但仍保留同一 Runtime 的 closedTurn 墓碑，避免旧 Turn 的迟到帧重新复活。
    return {
      ...state,
      inputRuntime: nextRuntime,
      inputTurn: runtimeOrder === "new"
        ? null
        : state.inputTurn,
      closedTurn: runtimeOrder === "new"
        ? null
        : state.inputTurn ?? state.closedTurn,
      liveTranscript: "",
      voiceState: "listening"
    };
  }

  if (event.type === "input.runtime_changed") {
    const nextRuntime = toRuntimeIdentity(event.payload);
    const runtimeOrder = compareRuntime(nextRuntime, state.inputRuntime);
    if (runtimeOrder !== "new") {
      return state;
    }
    return {
      ...state,
      inputRuntime: nextRuntime,
      inputTurn: null,
      closedTurn: null,
      liveTranscript: "",
      voiceState: "ready"
    };
  }

  if (event.type === "user.partial_transcript") {
    const nextTurn = toTurnIdentity(event.payload);
    const runtimeOrder = compareRuntime(nextTurn, state.inputRuntime);
    if (runtimeOrder === "stale") {
      return state;
    }

    const turnOrder = compareTurn(nextTurn, state.inputTurn);
    if (
      turnOrder === "stale"
      || (turnOrder === "same" && sameTurn(nextTurn, state.closedTurn))
    ) {
      return state;
    }

    return {
      ...state,
      inputRuntime: nextTurn,
      inputTurn: nextTurn,
      closedTurn: null,
      liveTranscript: event.payload.text.trim(),
      voiceState: "listening"
    };
  }

  if (event.type === "user.final_transcript") {
    const voiceTurn = toVoiceFinalTurn(event.payload);
    if (!voiceTurn) {
      // Desktop 文本提交没有输入身份，但仍要收口本地临时转写。
      return {
        ...state,
        liveTranscript: "",
        voiceState: "thinking"
      };
    }

    const runtimeOrder = compareRuntime(voiceTurn, state.inputRuntime);
    const turnOrder = compareTurn(voiceTurn, state.inputTurn);
    if (runtimeOrder === "stale" || turnOrder === "stale") {
      return state;
    }
    if (turnOrder === "same" && sameTurn(voiceTurn, state.closedTurn)) {
      return state;
    }

    return {
      ...state,
      inputRuntime: voiceTurn,
      inputTurn: voiceTurn,
      closedTurn: voiceTurn,
      liveTranscript: "",
      voiceState: "thinking"
    };
  }

  if (event.type === "turn.state_changed") {
    return reduceTurnStateChanged(state, event.payload);
  }

  if (event.type === "input.smart_turn_waiting_user_ended") {
    // ended 可能只是等待续说状态被 speech_resumed 清掉；不写入 closedTurn，后续同 Turn partial 仍可进入。
    const nextTurn = toTurnIdentity(event.payload);
    const runtimeOrder = compareRuntime(nextTurn, state.inputRuntime);
    const turnOrder = compareTurn(nextTurn, state.inputTurn);
    if (runtimeOrder === "stale" || turnOrder === "stale") {
      return state;
    }
    return {
      ...state,
      inputRuntime: nextTurn,
      inputTurn: nextTurn,
      liveTranscript: ""
    };
  }

  return state;
}

function reduceTurnStateChanged(
  state: ChatVoiceProjection,
  payload: Extract<BackendEvent, { type: "turn.state_changed" }>["payload"]
): ChatVoiceProjection {
  const nextRuntime = toRuntimeIdentity(payload);
  const runtimeOrder = compareRuntime(nextRuntime, state.inputRuntime);
  if (runtimeOrder === "stale") {
    return state;
  }

  const hasTurnIdentity = typeof payload.turn_id === "string"
    && typeof payload.turn_sequence === "number";
  if (!hasTurnIdentity) {
    if (payload.turn_state === "listening") {
      // 没有 Turn 身份的 listening 不能抢走已有输入；仅在没有基线时建立 Runtime。
      if (state.inputTurn && runtimeOrder !== "new") {
        return state;
      }
      return {
        ...state,
        inputRuntime: nextRuntime,
        inputTurn: null,
        closedTurn: null,
        liveTranscript: "",
        voiceState: "listening"
      };
    }

    // 无身份 idle 可能是助手输出收口，也可能是输入 reset。助手收口不能抹掉新输入；
    // 输入 reset 则需要把当前 Turn 标记为已收口，拒绝同 Turn 迟到 partial。
    if (
      payload.turn_state === "idle"
      && runtimeOrder !== "new"
      && hasOpenChatVoiceTurn(state)
      && !isInputResetReason(payload.reason)
    ) {
      return state;
    }
    if (payload.turn_state === "processing" || payload.turn_state === "speaking") {
      if (state.inputTurn && !sameTurn(state.inputTurn, state.closedTurn)) {
        return state;
      }
      return {
        ...state,
        inputRuntime: runtimeOrder === "new" ? nextRuntime : state.inputRuntime,
        inputTurn: runtimeOrder === "new" ? null : state.inputTurn,
        closedTurn: runtimeOrder === "new" ? null : state.closedTurn,
        liveTranscript: "",
        voiceState: payload.turn_state === "processing" ? "thinking" : "responding"
      };
    }
    return {
      ...state,
      inputRuntime: runtimeOrder === "new" ? nextRuntime : state.inputRuntime,
      inputTurn: runtimeOrder === "new" ? null : state.inputTurn,
      closedTurn: runtimeOrder === "new"
        ? null
        : state.inputTurn ?? state.closedTurn,
      liveTranscript: "",
      voiceState: "ready"
    };
  }

  const nextTurn = toTurnIdentity({
    ...payload,
    turn_id: payload.turn_id as string,
    turn_sequence: payload.turn_sequence as number
  });
  const turnOrder = compareTurn(nextTurn, state.inputTurn);
  if (turnOrder === "stale") {
    return state;
  }
  if (payload.turn_state === "listening") {
    // 同 Turn listening 重放只是快照，不能清掉已经显示的 partial。
    if (turnOrder === "same") {
      return state;
    }
    return {
      ...state,
      inputRuntime: nextRuntime,
      inputTurn: nextTurn,
      closedTurn: null,
      liveTranscript: "",
      voiceState: "listening"
    };
  }

  return {
    ...state,
    inputRuntime: nextRuntime,
    inputTurn: nextTurn,
    closedTurn: nextTurn,
    liveTranscript: "",
    voiceState: payload.turn_state === "processing"
      ? "thinking"
      : payload.turn_state === "speaking"
        ? "responding"
        : "ready"
  };
}

function toRuntimeIdentity(payload: {
  input_runtime_id: string;
  input_runtime_epoch: number;
}): InputRuntimeIdentity {
  return {
    inputRuntimeId: payload.input_runtime_id,
    inputRuntimeEpoch: payload.input_runtime_epoch
  };
}

function toTurnIdentity(payload: {
  input_runtime_id: string;
  input_runtime_epoch: number;
  turn_id: string;
  turn_sequence: number;
}): InputTurnIdentity {
  return {
    ...toRuntimeIdentity(payload),
    turnId: payload.turn_id,
    turnSequence: payload.turn_sequence
  };
}

function toVoiceFinalTurn(
  payload: Extract<BackendEvent, { type: "user.final_transcript" }>["payload"]
): InputTurnIdentity | null {
  if (
    !payload.input_runtime_id
    || typeof payload.input_runtime_epoch !== "number"
    || !payload.turn_id
    || typeof payload.turn_sequence !== "number"
  ) {
    return null;
  }
  return toTurnIdentity(payload as Parameters<typeof toTurnIdentity>[0]);
}

function compareRuntime(
  candidate: InputRuntimeIdentity,
  current: InputRuntimeIdentity | null
): "new" | "same" | "stale" {
  if (!current || candidate.inputRuntimeEpoch > current.inputRuntimeEpoch) {
    return "new";
  }
  if (candidate.inputRuntimeEpoch < current.inputRuntimeEpoch) {
    return "stale";
  }
  return candidate.inputRuntimeId === current.inputRuntimeId ? "same" : "stale";
}

function compareTurn(
  candidate: InputTurnIdentity,
  current: InputTurnIdentity | null
): "new" | "same" | "stale" {
  const runtimeOrder = compareRuntime(candidate, current);
  if (runtimeOrder !== "same" || !current) {
    return runtimeOrder;
  }
  if (candidate.turnSequence > current.turnSequence) {
    return "new";
  }
  return candidate.turnSequence === current.turnSequence ? "same" : "stale";
}

function sameTurn(first: InputTurnIdentity | null, second: InputTurnIdentity | null): boolean {
  return first !== null
    && second !== null
    && first.inputRuntimeEpoch === second.inputRuntimeEpoch
    && first.inputRuntimeId === second.inputRuntimeId
    && first.turnSequence === second.turnSequence;
}

function isInputResetReason(reason: string | undefined): boolean {
  return reason === "track_switched"
    || reason === "room_disconnected"
    || reason === "short_turn_ignored"
    || reason === "speaker_rejected"
    || reason === "final_asr_submit_rejected"
    || reason === "final_asr_failed"
    || reason === "turn_buffers_reset"
    || reason === "timed_reminder_takeover";
}
