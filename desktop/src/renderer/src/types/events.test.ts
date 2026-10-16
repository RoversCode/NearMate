import { describe, expect, it } from "vitest";
import { parseBackendEvent as parseBackendEventResult } from "./events";
import type { BackendEvent } from "./backendEvents";

const INPUT_RUNTIME_ID = "input-runtime-00000000-0000-4000-8000-000000000001";
const INPUT_RUNTIME_EPOCH = 1;

function parseBackendEvent(value: unknown): BackendEvent | null {
  const result = parseBackendEventResult(value);
  return result.ok ? result.event : null;
}

describe("parseBackendEvent", () => {
  it("识别唤醒、用户转写与助手音频结束事件", () => {
    const wakeEvent = parseBackendEvent({
      type: "wake.detected",
      payload: {
        input_runtime_id: INPUT_RUNTIME_ID,
        input_runtime_epoch: INPUT_RUNTIME_EPOCH,
        session_id: "session-voice"
      }
    });
    const partialTranscriptEvent = parseBackendEvent({
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: INPUT_RUNTIME_ID,
        input_runtime_epoch: INPUT_RUNTIME_EPOCH,
        session_id: "session-voice",
        turn_id: "turn-1",
        turn_sequence: 1,
        text: "帮我"
      }
    });
    const audioFinishedEvent = parseBackendEvent({
      type: "assistant.audio_finished",
      payload: {
        request_id: "request-a",
        session_id: "session-voice",
        generation_id: "generation-1"
      }
    });

    if (wakeEvent?.type !== "wake.detected") {
      throw new Error("Expected wake.detected");
    }

    if (partialTranscriptEvent?.type !== "user.partial_transcript") {
      throw new Error("Expected user.partial_transcript");
    }

    if (audioFinishedEvent?.type !== "assistant.audio_finished") {
      throw new Error("Expected assistant.audio_finished");
    }

    expect(wakeEvent.payload.session_id).toBe("session-voice");
    expect(partialTranscriptEvent.payload.text).toBe("帮我");
    expect(audioFinishedEvent.payload.generation_id).toBe("generation-1");
  });

  it("拒绝旧助手音频分片事件", () => {
    const parsedEvent = parseBackendEvent({
      type: "assistant.audio_chunk",
      payload: {
        session_id: "session-voice",
        generation_id: "generation-1",
        pcm16_b64: "AAE=",
        sample_rate: 24000
      }
    });

    expect(parsedEvent).toBeNull();
  });

  it("识别头像气泡投影事件", () => {
    const parsedEvent = parseBackendEvent({
      type: "shell.avatar_bubble_changed",
      payload: {
        kind: "speech",
        text: "我先帮你查一下。",
        ttl_ms: 3200
      }
    });

    if (parsedEvent?.type !== "shell.avatar_bubble_changed") {
      throw new Error("Expected shell.avatar_bubble_changed");
    }

    if (parsedEvent.payload.kind !== "speech") {
      throw new Error("Expected speech avatar bubble");
    }

    expect(parsedEvent.payload.kind).toBe("speech");
    expect(parsedEvent.payload.text).toBe("我先帮你查一下。");
    expect(parsedEvent.payload.ttl_ms).toBe(3200);
  });

  it("识别 Smart Turn 等待提示开始与结束事件", () => {
    const startedEvent = parseBackendEvent({
      type: "input.smart_turn_waiting_user_started",
      payload: {
        input_runtime_id: INPUT_RUNTIME_ID,
        input_runtime_epoch: INPUT_RUNTIME_EPOCH,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.12,
        hint_text: "还在听；如果说完了，请说“我说完了”。"
      }
    });
    const endedEvent = parseBackendEvent({
      type: "input.smart_turn_waiting_user_ended",
      payload: {
        input_runtime_id: INPUT_RUNTIME_ID,
        input_runtime_epoch: INPUT_RUNTIME_EPOCH,
        session_id: "session-voice",
        turn_id: "turn-smart",
        turn_sequence: 1,
        probability: 0.12,
        reason: "speech_resumed"
      }
    });

    if (startedEvent?.type !== "input.smart_turn_waiting_user_started") {
      throw new Error("Expected input.smart_turn_waiting_user_started");
    }

    if (endedEvent?.type !== "input.smart_turn_waiting_user_ended") {
      throw new Error("Expected input.smart_turn_waiting_user_ended");
    }

    expect(startedEvent.payload.hint_text).toContain("我说完了");
    expect(endedEvent.payload.reason).toBe("speech_resumed");
  });

  it("bootstrap.status_changed 只接受四键载荷：移除 tts 后拒收旧五键载荷", () => {
    const fourKeyPayload = {
      is_initialized: true,
      has_qweather_api_key: false,
      llm: { is_ready: true, missing: [], errors: [] },
      voice: {
        provider: "local_streamvox",
        is_ready: true,
        phase: "ready",
        model_id: null,
        role_name: null,
        has_confirmed_voice: true
      }
    };

    const accepted = parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: fourKeyPayload
    });

    if (accepted?.type !== "bootstrap.status_changed") {
      throw new Error("Expected bootstrap.status_changed");
    }
    expect(accepted.payload.is_initialized).toBe(true);

    // 夹带旧 tts 字段的五键载荷必须被拒收，避免版本错配时静默接受错误快照。
    expect(parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: { ...fourKeyPayload, tts: {} }
    })).toBeNull();

    const { has_confirmed_voice: _confirmation, ...missingConfirmation } = fourKeyPayload.voice;
    expect(parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: { ...fourKeyPayload, voice: missingConfirmation }
    })).toBeNull();

    // 缺 voice 键同样不属于四键契约。
    const { voice: _voice, ...missingVoicePayload } = fourKeyPayload;
    expect(parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: missingVoicePayload
    })).toBeNull();

    // 公开语音摘要不允许把模型执行目录、设备能力等内部字段带到 Renderer。
    expect(parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: {
        ...fourKeyPayload,
        voice: { ...fourKeyPayload.voice, execution: { model_dir: "C:\\secret" } }
      }
    })).toBeNull();

    // 任务编号和原始异常属于 TTS 专项接口内部字段，不能混入公开状态事件。
    expect(parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: {
        ...fourKeyPayload,
        voice: { ...fourKeyPayload.voice, task_id: "internal-task" }
      }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: {
        ...fourKeyPayload,
        voice: { ...fourKeyPayload.voice, error: "原始异常：C:\\secret\\trace.log" }
      }
    })).toBeNull();

    // 语音摘要字段缺失时拒收，避免 UI 把不完整快照当成已就绪状态。
    expect(parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: {
        ...fourKeyPayload,
        voice: { provider: "local_streamvox", is_ready: true }
      }
    })).toBeNull();

    expect(parseBackendEvent({
      type: "bootstrap.status_changed",
      payload: {
        ...fourKeyPayload,
        llm: { ...fourKeyPayload.llm, provider_debug: "internal" }
      }
    })).toBeNull();
  });
});
