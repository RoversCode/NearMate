import { describe, expect, it } from "vitest";
import {
  ASSISTANT_AUDIO_FINISHED_TTL_MS,
  initialAvatarBubbleState,
  reduceAvatarBubbleState
} from "./avatarBubbleState";
import { parseBackendEvent } from "../types/backendEvents";

describe("avatarBubbleState", () => {
  it("Self-Reflection 开始后创建独立思考气泡，220 毫秒才出现第一个字", () => {
    const started = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-request-1",
        session_id: "session-reflection",
        generation_id: "generation-reflection-1",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });
    const beforeFirstCharacter = reduceAvatarBubbleState(started, {
      type: "avatar.self_reflection_thinking_tick",
      payload: { delta_ms: 219 }
    });
    const firstCharacter = reduceAvatarBubbleState(beforeFirstCharacter, {
      type: "avatar.self_reflection_thinking_tick",
      payload: { delta_ms: 1 }
    });

    expect(started.reflectionStatus).toEqual({
      kind: "reflection_status",
      mode: "thinking",
      text: "",
      selfReflectionId: "self-reflection-1",
      episodeId: "episode-1",
      revision: 1,
      recoveryRequestId: "reflection-request-1",
      recoverySessionId: "session-reflection",
      recoveryGenerationId: "generation-reflection-1",
      phase: "typing",
      elapsedMs: 0
    });
    expect(beforeFirstCharacter.reflectionStatus).toMatchObject({
      kind: "reflection_status",
      text: "",
      phase: "typing",
      elapsedMs: 219
    });
    expect(firstCharacter.reflectionStatus).toMatchObject({
      kind: "reflection_status",
      text: "思",
      phase: "typing",
      elapsedMs: 0
    });
    expect(started.assistantBubble).toBeNull();
  });

  it("Recovery 开始时清除 Entry 旧气泡，迟到音频终态不能打断思考与正文交接", () => {
    const entryVisible = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "reflection-entry-request",
        session_id: "session-reflection",
        generation_id: "generation-entry",
        text: "我回去重新查清楚。"
      }
    });
    const recoveryStarted = reduceAvatarBubbleState(entryVisible, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-recovery-request",
        session_id: "session-reflection",
        generation_id: "generation-recovery",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });
    const firstThinkingCharacter = reduceAvatarBubbleState(recoveryStarted, {
      type: "avatar.self_reflection_thinking_tick",
      payload: { delta_ms: 220 }
    });
    const lateEntryAudioFinished = reduceAvatarBubbleState(firstThinkingCharacter, {
      type: "assistant.audio_finished",
      payload: {
        request_id: "reflection-entry-request",
        session_id: "session-reflection",
        generation_id: "generation-entry"
      }
    });
    const recoveryTextStarted = reduceAvatarBubbleState(lateEntryAudioFinished, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "reflection-recovery-request",
        session_id: "session-reflection",
        generation_id: "generation-recovery",
        text: "我重新想清楚了。"
      }
    });

    expect(recoveryStarted.assistantBubble).toBeNull();
    expect(firstThinkingCharacter.reflectionStatus).toMatchObject({ text: "思" });
    expect(lateEntryAudioFinished).toBe(firstThinkingCharacter);
    expect(recoveryTextStarted.reflectionStatus).toBeNull();
    expect(recoveryTextStarted.assistantBubble).toMatchObject({
      text: "我重新想清楚了。",
      generationId: "generation-recovery"
    });
  });

  it("Recovery 在正文前结束时不会恢复 Entry 旧气泡", () => {
    const entryVisible = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_final",
      payload: {
        request_id: "reflection-entry-request",
        session_id: "session-reflection",
        generation_id: "generation-entry",
        text: "我回去重新查清楚。"
      }
    });
    const recoveryStarted = reduceAvatarBubbleState(entryVisible, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-recovery-request",
        session_id: "session-reflection",
        generation_id: "generation-recovery",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });
    const recoveryStopped = reduceAvatarBubbleState(recoveryStarted, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-recovery-request",
        session_id: "session-reflection",
        generation_id: "generation-recovery",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 2,
        active: false
      }
    });

    expect(recoveryStarted.assistantBubble).toBeNull();
    expect(recoveryStopped.reflectionStatus).toBeNull();
    expect(recoveryStopped.assistantBubble).toBeNull();
  });

  it("完整思考文案停留 500 毫秒，清空 220 毫秒后重新逐字出现", () => {
    const started = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-request-1",
        session_id: "session-reflection",
        generation_id: "generation-reflection-1",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });
    const fullText = reduceAvatarBubbleState(started, {
      type: "avatar.self_reflection_thinking_tick",
      payload: { delta_ms: 2200 }
    });
    const beforeClear = reduceAvatarBubbleState(fullText, {
      type: "avatar.self_reflection_thinking_tick",
      payload: { delta_ms: 499 }
    });
    const cleared = reduceAvatarBubbleState(beforeClear, {
      type: "avatar.self_reflection_thinking_tick",
      payload: { delta_ms: 1 }
    });
    const clearPauseFinished = reduceAvatarBubbleState(cleared, {
      type: "avatar.self_reflection_thinking_tick",
      payload: { delta_ms: 220 }
    });
    const restarted = reduceAvatarBubbleState(clearPauseFinished, {
      type: "avatar.self_reflection_thinking_tick",
      payload: { delta_ms: 220 }
    });

    expect(fullText.reflectionStatus).toMatchObject({
      text: "思考中.......",
      phase: "full_pause",
      elapsedMs: 0
    });
    expect(beforeClear.reflectionStatus).toMatchObject({
      text: "思考中.......",
      phase: "full_pause",
      elapsedMs: 499
    });
    expect(cleared.reflectionStatus).toMatchObject({
      text: "",
      phase: "clear_pause",
      elapsedMs: 0
    });
    expect(clearPauseFinished.reflectionStatus).toMatchObject({
      text: "",
      phase: "typing",
      elapsedMs: 0
    });
    expect(restarted.reflectionStatus).toMatchObject({
      text: "思",
      phase: "typing",
      elapsedMs: 0
    });
  });

  it("只有完整 Recovery 身份匹配且 revision 更新的结束事件才能清除状态", () => {
    const started = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-request-new",
        session_id: "session-reflection",
        generation_id: "generation-reflection-new",
        self_reflection_id: "self-reflection-new",
        episode_id: "episode-new",
        revision: 1,
        active: true
      }
    });
    const staleStop = reduceAvatarBubbleState(started, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-request-old",
        session_id: "session-reflection",
        generation_id: "generation-reflection-old",
        self_reflection_id: "self-reflection-old",
        episode_id: "episode-old",
        revision: 2,
        active: false
      }
    });
    const firstText = reduceAvatarBubbleState(staleStop, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "reflection-request-new",
        session_id: "session-reflection",
        generation_id: "generation-reflection-new",
        text: "我重新想清楚了。"
      }
    });
    const matchingStop = reduceAvatarBubbleState(staleStop, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-request-new",
        session_id: "session-reflection",
        generation_id: "generation-reflection-new",
        self_reflection_id: "self-reflection-new",
        episode_id: "episode-new",
        revision: 2,
        active: false
      }
    });

    expect(staleStop).toBe(started);
    expect(matchingStop.reflectionStatus).toBeNull();
    expect(firstText.reflectionStatus).toBeNull();
    expect(firstText.assistantBubble?.text).toBe("我重新想清楚了。");
  });

  it("替代 Recovery 可以接管同一 episode，旧 generation 不能再清除它", () => {
    const original = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "recovery-old",
        session_id: "session-old",
        generation_id: "generation-old",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });
    const replacement = reduceAvatarBubbleState(original, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "recovery-new",
        session_id: "session-new",
        generation_id: "generation-new",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });
    const staleStop = reduceAvatarBubbleState(replacement, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "recovery-old",
        session_id: "session-old",
        generation_id: "generation-old",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 99,
        active: false
      }
    });

    expect(replacement.reflectionStatus).toMatchObject({
      recoveryRequestId: "recovery-new",
      recoverySessionId: "session-new",
      recoveryGenerationId: "generation-new"
    });
    expect(staleStop).toBe(replacement);
  });

  it("非当前 Recovery 的正文不能清除新 generation 的思考状态", () => {
    const started = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "recovery-new",
        session_id: "session-new",
        generation_id: "generation-new",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });
    const staleText = reduceAvatarBubbleState(started, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "recovery-old",
        session_id: "session-old",
        generation_id: "generation-old",
        text: "迟到正文"
      }
    });

    expect(staleText.reflectionStatus).toEqual(started.reflectionStatus);
  });

  it("辅助回应覆盖头部状态，新输入和音频结束恢复思考，首段正文才永久接管", () => {
    const started = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "recovery-1",
        session_id: "session-reflection",
        generation_id: "generation-recovery-1",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });
    const auxiliary = reduceAvatarBubbleState(started, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "reply-1",
        session_id: "session-reflection",
        generation_id: "generation-reply-1",
        request_source: "self_reflection_interruption_reply",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        text: "我听到了。"
      }
    });
    const nextInput = reduceAvatarBubbleState(auxiliary, {
      type: "user.final_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-1",
        turn_sequence: 1,
        history_disposition: "unattended_episode",
        episode_id: "episode-1",
        text: "你慢慢想"
      }
    });
    const secondAuxiliary = reduceAvatarBubbleState(nextInput, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "reply-2",
        session_id: "session-reflection",
        generation_id: "generation-reply-2",
        request_source: "self_reflection_interruption_reply",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        text: "好。"
      }
    });
    const audioFinished = reduceAvatarBubbleState(secondAuxiliary, {
      type: "assistant.audio_finished",
      payload: {
        request_id: "reply-2",
        session_id: "session-reflection",
        generation_id: "generation-reply-2",
        request_source: "self_reflection_interruption_reply",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1"
      }
    });
    const bodyStarted = reduceAvatarBubbleState(audioFinished, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "recovery-1",
        session_id: "session-reflection",
        generation_id: "generation-recovery-1",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 2,
        active: false
      }
    });
    const officialBody = reduceAvatarBubbleState(bodyStarted, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "recovery-1",
        session_id: "session-reflection",
        generation_id: "generation-recovery-1",
        text: "我想清楚了。"
      }
    });

    expect(auxiliary.reflectionStatus).toMatchObject({
      mode: "interruption_reply",
      text: "我听到了。"
    });
    expect(nextInput.reflectionStatus).toMatchObject({ mode: "thinking", text: "" });
    expect(nextInput.bottomBubble).toMatchObject({ text: "你慢慢想" });
    expect(audioFinished.reflectionStatus).toMatchObject({ mode: "thinking", text: "" });
    expect(bodyStarted.reflectionStatus).toBeNull();
    expect(officialBody.assistantBubble?.text).toBe("我想清楚了。");
  });

  it("关闭语音气泡设置时不展示 Self-Reflection 临时状态", () => {
    const entryVisible = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_final",
      payload: {
        request_id: "reflection-entry-request",
        session_id: "session-reflection",
        generation_id: "generation-entry",
        text: "我回去重新查清楚。"
      }
    });
    const hiddenState = {
      ...entryVisible,
      showSpeechBubbles: false
    };
    const nextState = reduceAvatarBubbleState(hiddenState, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-request-1",
        session_id: "session-reflection",
        generation_id: "generation-reflection-1",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    });

    expect(nextState.bottomBubble).toBeNull();
    expect(nextState.assistantBubble).toBeNull();
    expect(nextState.reflectionStatus).toBeNull();
  });

  it("语音链路事件会驱动唤醒、实时转写和最终文本底部气泡", () => {
    const wakeState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "wake.detected",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice"
      }
    });
    const partialState = reduceAvatarBubbleState(wakeState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-voice",
        turn_sequence: 1,
        text: "帮我查"
      }
    });
    const finalState = reduceAvatarBubbleState(partialState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-voice",
        turn_sequence: 1,
        text: "帮我查一下天气"
      }
    });

    expect(wakeState.bottomBubble).toEqual({
      kind: "speech",
      text: "我在，正在听。",
      ttlMs: 2400
    });
    expect(partialState.bottomBubble).toEqual({
      kind: "speech",
      text: "帮我查",
      ttlMs: 1800,
      turnId: "turn-voice"
    });
    expect(finalState.bottomBubble).toEqual({
      kind: "speech",
      text: "帮我查一下天气",
      ttlMs: 3200,
      turnId: "turn-voice"
    });
  });

  it("空 partial 不创建空气泡，也不清除已有助手字幕", () => {
    const assistantState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-1",
        text: "我正在回答"
      }
    });
    const stateWithUserBubble = reduceAvatarBubbleState(assistantState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-previous",
        turn_sequence: 1,
        text: "上一条用户输入"
      }
    });
    const nextState = reduceAvatarBubbleState(stateWithUserBubble, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-voice",
        turn_sequence: 1,
        text: "  \t"
      }
    });

    expect(nextState).toBe(stateWithUserBubble);
    expect(nextState.bottomBubble?.text).toBe("上一条用户输入");
    expect(nextState.assistantBubble?.text).toBe("我正在回答");
  });

  it("同一 Turn 的有效 partial 会覆盖上一条有效 partial，并去掉首尾空白", () => {
    const firstState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-voice",
        turn_sequence: 1,
        text: "第一段"
      }
    });
    const secondState = reduceAvatarBubbleState(firstState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-voice",
        turn_sequence: 1,
        text: " 第二段 "
      }
    });

    expect(secondState.bottomBubble).toEqual({
      kind: "speech",
      text: "第二段",
      ttlMs: 1800,
      turnId: "turn-voice"
    });
  });

  it("新 Turn 已进入 listening 后，迟到的旧 partial 不能覆盖当前气泡", () => {
    const listeningState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "turn.state_changed",
      payload: {
        turn_state: "listening",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_sequence: 2,
        turn_id: "turn-new"
      }
    });
    const currentState = reduceAvatarBubbleState(listeningState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-new",
        turn_sequence: 2,
        text: "新一轮"
      }
    });
    const lateState = reduceAvatarBubbleState(currentState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-old",
        turn_sequence: 1,
        text: "旧一轮迟到结果"
      }
    });

    expect(lateState).toBe(currentState);
    expect(lateState.bottomBubble?.text).toBe("新一轮");
  });

  it("Final 到达后，同一 Turn 的迟到 partial 不能覆盖最终文本", () => {
    const finalState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "这是最终文本"
      }
    });
    const latePartialState = reduceAvatarBubbleState(finalState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "迟到的中间结果"
      }
    });

    expect(latePartialState).toBe(finalState);
    expect(latePartialState.bottomBubble?.text).toBe("这是最终文本");
  });

  it("相同 Runtime 身份快照重放后仍拒绝已 Final Turn 的迟到 partial", () => {
    const finalState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "这是最终文本"
      }
    });
    const replayedRuntimeState = reduceAvatarBubbleState(finalState, {
      type: "input.runtime_changed",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1
      }
    });
    const latePartialState = reduceAvatarBubbleState(replayedRuntimeState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "迟到的中间结果"
      }
    });

    expect(replayedRuntimeState).toBe(finalState);
    expect(latePartialState).toBe(finalState);
    expect(latePartialState.bottomBubble?.text).toBe("这是最终文本");
  });

  it("重连漏掉 listening 时，更新 Turn 的 partial 可以接管已收口的旧 Turn", () => {
    const previousFinalState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "上一轮"
      }
    });
    const nextPartialState = reduceAvatarBubbleState(previousFinalState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-10",
        turn_sequence: 10,
        text: "新一轮实时文字"
      }
    });

    expect(nextPartialState.bottomBubble).toEqual({
      kind: "speech",
      text: "新一轮实时文字",
      ttlMs: 1800,
      turnId: "turn-10"
    });
  });

  it("重连漏掉 Final 和 listening 时，编号更大的 partial 仍可接管旧活跃 Turn", () => {
    const previousPartialState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "上一轮中间结果"
      }
    });
    const nextPartialState = reduceAvatarBubbleState(previousPartialState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-10",
        turn_sequence: 10,
        text: "新一轮实时文字"
      }
    });

    expect(nextPartialState.inputTurn).toEqual({
      inputRuntimeId: "input-runtime-00000000-0000-4000-8000-000000000001",
      inputRuntimeEpoch: 1,
      turnId: "turn-10",
      turnSequence: 10,
      finalized: false
    });
    expect(nextPartialState.bottomBubble?.text).toBe("新一轮实时文字");
  });

  it("控制连接换代后会清除旧 Turn 基线，允许重建 Runtime 的 turn-1 显示", () => {
    const previousFinalState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-20",
        turn_sequence: 20,
        text: "旧 Runtime 的最终文本"
      }
    });
    const nextConnectionState = reduceAvatarBubbleState(previousFinalState, {
      type: "control.connection_changed",
      payload: {
        status: "connecting"
      }
    });
    const rebuiltRuntimePartialState = reduceAvatarBubbleState(nextConnectionState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-1",
        turn_sequence: 1,
        text: "新 Runtime 的实时文字"
      }
    });

    expect(nextConnectionState.inputTurn).toBeUndefined();
    expect(nextConnectionState.bottomBubble?.text).toBe("旧 Runtime 的最终文本");
    expect(rebuiltRuntimePartialState.inputTurn).toEqual({
      inputRuntimeId: "input-runtime-00000000-0000-4000-8000-000000000001",
      inputRuntimeEpoch: 1,
      turnId: "turn-1",
      turnSequence: 1,
      finalized: false
    });
    expect(rebuiltRuntimePartialState.bottomBubble?.text).toBe("新 Runtime 的实时文字");
  });

  it("更高 Runtime epoch 从 turn-1 接管，旧 Runtime 的迟到 partial 不能覆盖", () => {
    const runtimeNineState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000009",
        input_runtime_epoch: 9,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "旧 Runtime 正文"
      }
    });
    const runtimeChangedState = reduceAvatarBubbleState(runtimeNineState, {
      type: "input.runtime_changed",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000010",
        input_runtime_epoch: 10
      }
    });
    const runtimeTenState = reduceAvatarBubbleState(runtimeChangedState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000010",
        input_runtime_epoch: 10,
        session_id: "session-voice",
        turn_id: "turn-1",
        turn_sequence: 1,
        text: "新 Runtime 正文"
      }
    });
    const lateOldRuntimeState = reduceAvatarBubbleState(runtimeTenState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000009",
        input_runtime_epoch: 9,
        session_id: "session-voice",
        turn_id: "turn-10",
        turn_sequence: 10,
        text: "不应覆盖"
      }
    });

    expect(runtimeTenState.inputTurn).toMatchObject({
      inputRuntimeEpoch: 10,
      turnSequence: 1
    });
    expect(lateOldRuntimeState).toBe(runtimeTenState);
    expect(lateOldRuntimeState.bottomBubble?.text).toBe("新 Runtime 正文");
  });

  it("相同 Runtime epoch 携带不同 id 时拒绝接管", () => {
    const currentState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "input.runtime_changed",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000011",
        input_runtime_epoch: 11
      }
    });
    const conflictingState = reduceAvatarBubbleState(currentState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000012",
        input_runtime_epoch: 11,
        session_id: "session-voice",
        turn_id: "turn-1",
        turn_sequence: 1,
        text: "冲突 Runtime 正文"
      }
    });

    expect(conflictingState).toBe(currentState);
    expect(conflictingState.bottomBubble).toBeNull();
  });

  it("旧 Runtime 的 Final 不覆盖当前 Runtime 气泡", () => {
    const currentState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000010",
        input_runtime_epoch: 10,
        session_id: "session-voice",
        turn_id: "turn-1",
        turn_sequence: 1,
        text: "当前 Runtime 正文"
      }
    });
    const lateFinalState = reduceAvatarBubbleState(currentState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000009",
        input_runtime_epoch: 9,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "旧 Runtime 最终正文"
      }
    });

    expect(lateFinalState).toBe(currentState);
    expect(lateFinalState.bottomBubble?.text).toBe("当前 Runtime 正文");
  });

  it("Final 后迟到的同 Turn listening 不能重新开放旧 Turn", () => {
    const finalState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "这是最终文本"
      }
    });
    const lateListeningState = reduceAvatarBubbleState(finalState, {
      type: "turn.state_changed",
      payload: {
        turn_state: "listening",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_sequence: 9,
        turn_id: "turn-9"
      }
    });
    const latePartialState = reduceAvatarBubbleState(lateListeningState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        text: "不应重新出现"
      }
    });

    expect(lateListeningState).toBe(finalState);
    expect(latePartialState).toBe(finalState);
  });

  it("唤醒事件携带回复文本时优先显示该文本", () => {
    const wakeState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "wake.detected",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        text: "早呀，我在"
      }
    });

    expect(wakeState.bottomBubble).toEqual({
      kind: "speech",
      text: "早呀，我在",
      ttlMs: 2400
    });
  });

  it("Chat 展开时隐藏旧气泡，收起后只显示之后的新唤醒气泡", () => {
    const expandedState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "avatar.chat_mode_changed",
      mode: "expanded"
    });
    const suppressedWake = reduceAvatarBubbleState(expandedState, {
      type: "wake.detected",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        text: "展开期间的唤醒"
      }
    });
    const collapsedState = reduceAvatarBubbleState(suppressedWake, {
      type: "avatar.chat_mode_changed",
      mode: "collapsed"
    });
    const visibleWake = reduceAvatarBubbleState(collapsedState, {
      type: "wake.detected",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        text: "收起后的唤醒"
      }
    });

    expect(suppressedWake.bottomBubble).toBeNull();
    expect(visibleWake.bottomBubble).toEqual({
      kind: "speech",
      text: "收起后的唤醒",
      ttlMs: 2400
    });
  });

  it("final 后 thinking 状态不会立刻清空用户最终文本底部气泡", () => {
    const finalState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-voice",
        turn_sequence: 1,
        text: "帮我查一下天气"
      }
    });
    const thinkingState = reduceAvatarBubbleState(finalState, {
      type: "turn.state_changed",
      payload: {
        turn_state: "processing",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_sequence: 1,
        turn_id: "turn-voice"
      }
    });

    expect(thinkingState.bottomBubble).toEqual(finalState.bottomBubble);
  });

  it("助手文本 chunk 会累加完整文本，但头部气泡只展示字幕窗口", () => {
    const firstChunkState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-1",
        text: "我查"
      }
    });
    const secondChunkState = reduceAvatarBubbleState(firstChunkState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-1",
        text: "一下天气。"
      }
    });
    const finalState = reduceAvatarBubbleState(secondChunkState, {
      type: "assistant.text_final",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-1",
        text: "我查一下天气。"
      }
    });

    expect(firstChunkState.assistantBubble).toEqual({
      kind: "assistant_reply",
      text: "我查",
      visibleText: "我查",
      generationId: "generation-1",
      textFinal: false,
      progressUnitCount: 0,
      captionMsPerUnit: 220,
      ttlMs: null
    });
    expect(secondChunkState.assistantBubble?.text).toBe("我查一下天气。");
    expect(secondChunkState.assistantBubble?.visibleText).toBe("我查一下");
    expect(finalState.assistantBubble).toEqual({
      kind: "assistant_reply",
      text: "我查一下天气。",
      visibleText: "我查一下",
      generationId: "generation-1",
      textFinal: true,
      progressUnitCount: 0,
      captionMsPerUnit: 220,
      ttlMs: null
    });
  });

  it("WebSocket 事件经过解析和 reducer 后以 Final 权威正文覆盖流式草稿", () => {
    const wireEvents: unknown[] = JSON.parse(JSON.stringify([
      {
        type: "assistant.text_chunk",
        payload: {
          request_id: "request-stream-whitespace",
          session_id: "session-voice",
          generation_id: "generation-stream-whitespace",
          text: "[Shh]"
        }
      },
      {
        type: "assistant.text_chunk",
        payload: {
          request_id: "request-stream-whitespace",
          session_id: "session-voice",
          generation_id: "generation-stream-whitespace",
          text: " 正文"
        }
      },
      {
        type: "assistant.text_final",
        payload: {
          request_id: "request-stream-whitespace",
          session_id: "session-voice",
          generation_id: "generation-stream-whitespace",
          text: "[Shh] 正文"
        }
      }
    ]));
    let state = initialAvatarBubbleState;

    for (const wireEvent of wireEvents) {
      const parsed = parseBackendEvent(wireEvent);
      expect(parsed.ok).toBe(true);
      if (!parsed.ok) {
        throw new Error(`测试事件解析失败：${parsed.reasonCode}`);
      }
      state = reduceAvatarBubbleState(state, parsed.event);
    }

    expect(state.assistantBubble?.text).toBe("[Shh] 正文");
    expect(state.assistantBubble?.textFinal).toBe(true);
  });

  it("协议失败空 Final 会清理已经显示的助手流式气泡", () => {
    const streamingState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-a",
        session_id: "session-voice",
        generation_id: "generation-a",
        text: "尚未完成的回复"
      }
    });
    const failedState = reduceAvatarBubbleState(streamingState, {
      type: "assistant.text_final",
      payload: {
        request_id: "request-a",
        session_id: "session-voice",
        generation_id: "generation-a",
        text: "",
        notice_delivery_failed: true,
        notice_failure_reason: "provider_protocol_leak"
      }
    });

    expect(streamingState.assistantBubble?.text).toBe("尚未完成的回复");
    expect(failedState.assistantBubble).toBeNull();
  });

  it("助手分段完成后，同一 generation 的新 chunk 会开始一条新气泡", () => {
    const prefixState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-a",
        session_id: "session-voice",
        generation_id: "generation-a",
        text: "我先去处理。"
      }
    });
    const segmentedState = reduceAvatarBubbleState(prefixState, {
      type: "assistant.text_segment_final",
      payload: {
        request_id: "request-a",
        session_id: "session-voice",
        generation_id: "generation-a",
        text: "我先去处理。"
      }
    });
    const followupState = reduceAvatarBubbleState(segmentedState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-a",
        session_id: "session-voice",
        generation_id: "generation-a",
        text: "不过现在还不能开始。"
      }
    });

    expect(segmentedState.assistantBubble?.textFinal).toBe(true);
    expect(followupState.assistantBubble?.text).toBe("不过现在还不能开始。");
    expect(followupState.assistantBubble?.textFinal).toBe(false);
  });

  it("LLM 快速给出长文本时不会把全文塞进助手头部气泡", () => {
    const longText =
      "第一段先铺垫很多内容，第二段继续解释背景，第三段靠近当前语音，第四段才适合显示在短字幕窗口里。";
    const assistantState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_final",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-long",
        text: longText
      }
    });

    expect(assistantState.assistantBubble?.text).toBe(longText);
    expect(assistantState.assistantBubble?.visibleText).not.toBe(longText);
  });

  it("字幕 tick 会按角色校准语速推进窗口，并挤出前端旧文本", () => {
    const calibratedState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "settings.updated",
      payload: {
        show_speech_bubbles: true,
        selected_live2d_model_id: "Haru",
        active_character_caption_timing: {
          ms_per_unit: 100,
          calibration_text: "我在这里，听见你说话了。",
          duration_ms: 4000,
          unit_count: 40,
          measured_at: 1710000000
        }
      }
    });
    const shortAssistantState = reduceAvatarBubbleState(calibratedState, {
      type: "assistant.text_final",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-short",
        text: "一二三四五六七八九十"
      }
    });
    const shortTickedState = reduceAvatarBubbleState(shortAssistantState, {
      type: "avatar.assistant_caption_tick",
      payload: {
        delta_ms: 300
      }
    });
    const assistantState = reduceAvatarBubbleState(calibratedState, {
      type: "assistant.text_final",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-1",
        text:
          "第一句先出现然后应该被挤出窗口。第二句继续铺垫气泡不要变长。第三句靠近当前语音位置。第四句才是最终可见的字幕内容。"
      }
    });
    const tickedState = reduceAvatarBubbleState(assistantState, {
      type: "avatar.assistant_caption_tick",
      payload: {
        delta_ms: 9200
      }
    });

    expect(shortTickedState.assistantBubble?.progressUnitCount).toBe(3);
    expect(tickedState.assistantBubble?.visibleText).not.toContain("第一句先出现");
    expect(tickedState.assistantBubble?.visibleText).toContain("第三句靠近当前语音位置");
  });

  it("助手音频播放完成后才为头部气泡设置阅读缓冲 TTL，超时只清除助手气泡", () => {
    const assistantState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_final",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-1",
        text: "今天天气不错。"
      }
    });
    const bottomState = reduceAvatarBubbleState(assistantState, {
      type: "user.final_transcript",
      payload: {
        history_disposition: "ordinary_turn",
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-voice",
        turn_sequence: 1,
        text: "天气怎么样"
      }
    });
    const audioFinishedState = reduceAvatarBubbleState(bottomState, {
      type: "assistant.audio_finished",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-1"
      }
    });
    const timeoutState = reduceAvatarBubbleState(audioFinishedState, {
      type: "avatar.assistant_bubble_timeout"
    });

    expect(audioFinishedState.assistantBubble).toEqual({
      kind: "assistant_reply",
      text: "今天天气不错。",
      visibleText: "今天天气不错。",
      generationId: "generation-1",
      textFinal: true,
      progressUnitCount: 6.9,
      captionMsPerUnit: 220,
      ttlMs: ASSISTANT_AUDIO_FINISHED_TTL_MS
    });
    expect(timeoutState.assistantBubble).toBeNull();
    expect(timeoutState.bottomBubble).toEqual(bottomState.bottomBubble);
  });

  it("用户新一轮 partial 或插话会清除旧助手头部气泡", () => {
    const assistantState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-1",
        text: "我正在回答"
      }
    });
    const partialState = reduceAvatarBubbleState(assistantState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-next",
        turn_sequence: 2,
        text: "等一下"
      }
    });
    const assistantStateAgain = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-2",
        text: "新的回答"
      }
    });
    const bargeInState = reduceAvatarBubbleState(assistantStateAgain, {
      type: "barge_in.detected",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        generation_id: "generation-2"
      }
    });

    expect(partialState.assistantBubble).toBeNull();
    expect(partialState.bottomBubble?.text).toBe("等一下");
    expect(bargeInState.assistantBubble).toBeNull();
  });

  it("助手音频结束后的隐藏 TTL 不再随全文长度增长", () => {
    const assistantState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "assistant.text_final",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-long",
        text: "长".repeat(200)
      }
    });
    const audioFinishedState = reduceAvatarBubbleState(assistantState, {
      type: "assistant.audio_finished",
      payload: {
        request_id: "request-voice",
        session_id: "session-voice",
        generation_id: "generation-long"
      }
    });

    expect(audioFinishedState.assistantBubble?.ttlMs).toBe(ASSISTANT_AUDIO_FINISHED_TTL_MS);
  });

  it("关闭普通语音气泡时忽略 speech 气泡", () => {
    const hiddenSpeechState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "settings.updated",
      payload: {
        show_speech_bubbles: false,
        followup_toggle_shortcut_enabled: true,
        followup_toggle_shortcut: "CommandOrControl+Alt+Space",
        selected_live2d_model_id: "Haru"
      }
    });

    const nextState = reduceAvatarBubbleState(hiddenSpeechState, {
      type: "shell.avatar_bubble_changed",
      payload: {
        kind: "speech",
        text: "这句话不应该显示在头像旁边。",
        ttl_ms: 1800
      }
    });

    expect(nextState.bottomBubble).toBeNull();
  });

  it("Smart Turn 等待提示创建 sticky 气泡，超时不会清除", () => {
    const waitingState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "input.smart_turn_waiting_user_started",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.12,
        hint_text: "还在听；如果说完了，请说“我说完了”。"
      }
    });
    const timeoutState = reduceAvatarBubbleState(waitingState, {
      type: "avatar.bubble_timeout"
    });

    expect(waitingState.bottomBubble).toEqual({
      kind: "smart_turn_waiting",
      text: "还在听；如果说完了，请说“我说完了”。",
      sessionId: "session-voice",
      turnId: "turn-smart",
      probability: 0.12
    });
    expect(timeoutState.bottomBubble).toEqual(waitingState.bottomBubble);
  });

  it("Smart Turn 等待提示遇到同一 turn 重复开始事件时保持原气泡", () => {
    const waitingState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "input.smart_turn_waiting_user_started",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.12,
        hint_text: "第一条提示"
      }
    });
    const repeatedState = reduceAvatarBubbleState(waitingState, {
      type: "input.smart_turn_waiting_user_started",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.2,
        hint_text: "第二条提示不应该刷新"
      }
    });

    expect(repeatedState.bottomBubble).toEqual(waitingState.bottomBubble);
  });

  it("Smart Turn 等待提示只被匹配 turn 的结束事件清除", () => {
    const waitingState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "input.smart_turn_waiting_user_started",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.12,
        hint_text: "还在听。"
      }
    });
    const mismatchedState = reduceAvatarBubbleState(waitingState, {
      type: "input.smart_turn_waiting_user_ended",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-other",
        turn_sequence: 2,
        probability: 0.12,
        reason: "waiting_timeout"
      }
    });
    const matchedState = reduceAvatarBubbleState(waitingState, {
      type: "input.smart_turn_waiting_user_ended",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.12,
        reason: "speech_resumed"
      }
    });

    expect(mismatchedState.bottomBubble).toEqual(waitingState.bottomBubble);
    expect(matchedState.bottomBubble).toBeNull();
  });

  it("Smart Turn 等待提示遇到实时转写时清除并显示转写文本", () => {
    const waitingState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "input.smart_turn_waiting_user_started",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.12,
        hint_text: "还在听。"
      }
    });
    const partialState = reduceAvatarBubbleState(waitingState, {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        text: "我继续说"
      }
    });

    expect(partialState.bottomBubble).toEqual({
      kind: "speech",
      text: "我继续说",
      ttlMs: 1800,
      turnId: "turn-smart"
    });
  });

  it("关闭普通语音气泡时忽略 Smart Turn 等待提示", () => {
    const hiddenSpeechState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "settings.updated",
      payload: {
        show_speech_bubbles: false,
        followup_toggle_shortcut_enabled: true,
        followup_toggle_shortcut: "CommandOrControl+Alt+Space",
        selected_live2d_model_id: "Haru"
      }
    });

    const nextState = reduceAvatarBubbleState(hiddenSpeechState, {
      type: "input.smart_turn_waiting_user_started",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.12,
        hint_text: "还在听。"
      }
    });

    expect(nextState.bottomBubble).toBeNull();
  });

  it("关闭普通语音气泡时仍显示 notice 气泡", () => {
    const hiddenSpeechState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "settings.updated",
      payload: {
        show_speech_bubbles: false,
        followup_toggle_shortcut_enabled: true,
        followup_toggle_shortcut: "CommandOrControl+Alt+Space",
        selected_live2d_model_id: "Haru"
      }
    });

    const nextState = reduceAvatarBubbleState(hiddenSpeechState, {
      type: "shell.avatar_bubble_changed",
      payload: {
        kind: "notice",
        text: "系统提醒已经到达。",
        ttl_ms: 2400
      }
    });

    expect(nextState.bottomBubble).toEqual({
      kind: "notice",
      text: "系统提醒已经到达。",
      ttlMs: 2400
    });
  });

  it("avatar.bubble_timeout 清除底部 speech", () => {
    const speechState = reduceAvatarBubbleState(initialAvatarBubbleState, {
      type: "shell.avatar_bubble_changed",
      payload: {
        kind: "speech",
        text: "普通语音会自动消失。",
        ttl_ms: 1000
      }
    });
    expect(
      reduceAvatarBubbleState(speechState, {
        type: "avatar.bubble_timeout"
      }).bottomBubble
    ).toBeNull();
  });
});
