import { describe, expect, it } from "vitest";
import {
  isAssistantAudioTrack,
  isCleanMicrophoneTrack
} from "./livekitRoomTrack";

describe("livekitRoomTrack", () => {
  it("只把音频类型且轨道名为 assistant-audio 的订阅识别为助手语音轨", () => {
    expect(isAssistantAudioTrack({ kind: "audio" }, { trackName: "assistant-audio" })).toBe(true);
    expect(isAssistantAudioTrack({ kind: "video" }, { trackName: "assistant-audio" })).toBe(false);
    expect(isAssistantAudioTrack({ kind: "audio", name: "screen-share" }, {})).toBe(false);
  });

  it("只把音频类型且轨道名为 clean-mic 的订阅识别为本地干净麦克风", () => {
    expect(isCleanMicrophoneTrack({ kind: "audio" }, { trackName: "clean-mic" })).toBe(true);
    expect(isCleanMicrophoneTrack({ kind: "video" }, { trackName: "clean-mic" })).toBe(false);
    expect(isCleanMicrophoneTrack({ kind: "audio", name: "assistant-audio" }, {})).toBe(false);
  });
});
