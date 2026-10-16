import { describe, expect, it } from "vitest";
import {
  createLive2dLipSyncSpeechState,
  updateLive2dLipSyncSpeechState
} from "./live2dLipSyncSpeechState";

describe("live2dLipSyncSpeechState", () => {
  it("有声帧会激活语音状态，静音未达阈值时保持 active，达到阈值后关闭", () => {
    let state = createLive2dLipSyncSpeechState();

    const voiced = updateLive2dLipSyncSpeechState(state, 0.4, 3);
    state = voiced.state;
    expect(voiced.speechActive).toBe(true);

    const firstSilent = updateLive2dLipSyncSpeechState(state, 0, 3);
    state = firstSilent.state;
    expect(firstSilent.speechActive).toBe(true);

    const secondSilent = updateLive2dLipSyncSpeechState(state, 0, 3);
    state = secondSilent.state;
    expect(secondSilent.speechActive).toBe(true);

    const thirdSilent = updateLive2dLipSyncSpeechState(state, 0, 3);
    expect(thirdSilent.speechActive).toBe(false);
  });
});
