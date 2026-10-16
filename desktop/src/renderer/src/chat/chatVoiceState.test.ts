import { describe, expect, it } from "vitest";
import type { BackendEvent } from "../types/events";
import {
  clearChatVoiceDisplay,
  createInitialChatVoiceProjection,
  reduceChatVoiceProjection,
  type ChatVoiceProjection
} from "./chatVoiceState";

const runtime = {
  input_runtime_id: "runtime-a",
  input_runtime_epoch: 1
};

function partial(turnId: string, turnSequence: number, text: string, overrides: Record<string, unknown> = {}): BackendEvent {
  return event("user.partial_transcript", {
    ...runtime,
    turn_id: turnId,
    turn_sequence: turnSequence,
    session_id: "session-a",
    text,
    ...overrides
  });
}

function final(turnId: string, turnSequence: number, text = "完整句子", overrides: Record<string, unknown> = {}): BackendEvent {
  return event("user.final_transcript", {
    ...runtime,
    turn_id: turnId,
    turn_sequence: turnSequence,
    session_id: "session-a",
    history_disposition: "ordinary_turn",
    text,
    ...overrides
  });
}

function turnState(
  turnStateValue: "idle" | "listening" | "processing" | "speaking",
  turnId?: string,
  turnSequence?: number,
  overrides: Record<string, unknown> = {}
): BackendEvent {
  return event("turn.state_changed", {
    ...runtime,
    session_id: "session-a",
    turn_state: turnStateValue,
    ...(turnId === undefined ? {} : { turn_id: turnId, turn_sequence: turnSequence }),
    ...overrides
  });
}

function event(type: string, payload: Record<string, unknown>): BackendEvent {
  return { type, payload } as BackendEvent;
}

function reduce(state: ChatVoiceProjection, nextEvent: BackendEvent): ChatVoiceProjection {
  return reduceChatVoiceProjection(state, nextEvent);
}

describe("chatVoiceState", () => {
  it("新的唤醒窗口会清除上一轮遗留的实时转写", () => {
    let state = reduce(
      createInitialChatVoiceProjection(),
      partial("turn-old", 1, "上一轮残留文字")
    );

    state = reduce(state, event("wake.detected", {
      ...runtime,
      session_id: "session-a"
    }));

    expect(state.liveTranscript).toBe("");
    expect(state.voiceState).toBe("listening");
    expect(state.inputTurn).toMatchObject({ turnId: "turn-old", turnSequence: 1 });
    expect(reduce(state, partial("turn-old", 1, "迟到的旧文字"))).toBe(state);
  });

  it("同 Turn Final 后拒绝迟到 partial，并允许更高序号的新 Turn", () => {
    let state = reduce(createInitialChatVoiceProjection(), partial("turn-1", 1, "旧片段"));
    state = reduce(state, final("turn-1", 1));
    expect(reduce(state, partial("turn-1", 1, "迟到片段"))).toBe(state);

    const nextState = reduce(state, partial("turn-2", 2, "新片段"));
    expect(nextState.liveTranscript).toBe("新片段");
    expect(nextState.voiceState).toBe("listening");
  });

  it("拒绝旧 Runtime、旧 Turn 的 partial、Final 和 idle，不覆盖新输入", () => {
    let state = reduce(createInitialChatVoiceProjection(), event("input.runtime_changed", {
      input_runtime_id: "runtime-a",
      input_runtime_epoch: 1
    }));
    state = reduce(state, partial("turn-2", 2, "新片段"));
    const newerRuntimePartial = partial("turn-3", 3, "更新片段", {
      input_runtime_id: "runtime-b",
      input_runtime_epoch: 2
    });
    state = reduce(state, newerRuntimePartial);
    const beforeLateEvents = state;

    expect(reduce(state, partial("turn-2", 2, "旧 partial"))).toBe(beforeLateEvents);
    expect(reduce(state, final("turn-2", 2, "旧 final"))).toBe(beforeLateEvents);
    expect(reduce(state, turnState("idle", "turn-2", 2))).toBe(beforeLateEvents);
  });

  it("同 Runtime 快照幂等，不会清除已经建立的 Final 墓碑", () => {
    let state = reduce(createInitialChatVoiceProjection(), partial("turn-1", 1, "片段"));
    state = reduce(state, final("turn-1", 1));
    const snapshot = reduce(state, event("input.runtime_changed", runtime));
    expect(snapshot).toBe(state);
    expect(reduce(snapshot, partial("turn-1", 1, "迟到片段"))).toBe(snapshot);
  });

  it("同 Turn listening 重放保留正在显示的 partial", () => {
    const state = reduce(
      reduce(createInitialChatVoiceProjection(), partial("turn-1", 1, "正在输入")),
      turnState("listening", "turn-1", 1)
    );
    expect(state.liveTranscript).toBe("正在输入");
    expect(state.voiceState).toBe("listening");
  });

  it("Smart Turn ended 只清当前文字，不封死后续同 Turn partial", () => {
    let state = reduce(createInitialChatVoiceProjection(), partial("turn-1", 1, "等待续说"));
    state = reduce(state, event("input.smart_turn_waiting_user_ended", {
      ...runtime,
      turn_id: "turn-1",
      turn_sequence: 1,
      session_id: "session-a",
      probability: 0.4,
      reason: "speech_resumed"
    }));
    expect(state.liveTranscript).toBe("");
    expect(reduce(state, partial("turn-1", 1, "续说内容")).liveTranscript).toBe("续说内容");
  });

  it("助手无身份 idle 不抹掉新输入，输入 reset 则建立同 Turn 墓碑", () => {
    let state = reduce(createInitialChatVoiceProjection(), partial("turn-1", 1, "仍在说"));
    state = reduce(state, turnState("idle", undefined, undefined, { reason: "assistant_audio_finished" }));
    expect(state.liveTranscript).toBe("仍在说");

    state = reduce(state, turnState("idle", undefined, undefined, { reason: "track_switched" }));
    expect(state.liveTranscript).toBe("");
    expect(reduce(state, partial("turn-1", 1, "旧轨道尾帧"))).toBe(state);
    expect(reduce(state, partial("turn-2", 2, "新轨道内容")).liveTranscript).toBe("新轨道内容");
  });

  it("未知或缺失原因的无身份 idle 对开放输入采取保守保留", () => {
    let state = reduce(createInitialChatVoiceProjection(), partial("turn-1", 1, "仍在说"));
    state = reduce(state, turnState("idle", undefined, undefined, { reason: "unknown_backend_reason" }));
    expect(state.liveTranscript).toBe("仍在说");
    expect(state.voiceState).toBe("listening");

    const withoutReason = reduce(state, turnState("idle"));
    expect(withoutReason).toBe(state);
  });

  it("助手音频完成或请求失败没有输入身份时不清正在说的新输入", () => {
    let state = reduce(createInitialChatVoiceProjection(), partial("turn-1", 1, "仍在说话"));
    state = reduce(state, event("assistant.audio_finished", {
      request_id: "request-old",
      generation_id: "generation-old"
    }));
    expect(state.liveTranscript).toBe("仍在说话");
    expect(reduce(state, event("assistant.request_failed", {
      request_id: "request-old",
      message: "输出失败"
    })).liveTranscript).toBe("仍在说话");
  });

  it("断线只清临时展示，不制造新的输入身份", () => {
    const state = reduce(createInitialChatVoiceProjection(), partial("turn-1", 1, "临时文字"));
    const cleared = clearChatVoiceDisplay(state);
    expect(cleared.liveTranscript).toBe("");
    expect(cleared.voiceState).toBe("ready");
    expect(cleared.inputTurn).toEqual(state.inputTurn);
  });
});
