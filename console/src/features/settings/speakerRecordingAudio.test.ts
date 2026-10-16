import { describe, expect, it } from "vitest";

import { float32ToPcm16 } from "./speakerRecordingAudio";

describe("speaker recording audio helpers", () => {
  it("把 float32 音频转换为 16kHz PCM16", () => {
    const pcm16 = float32ToPcm16(new Float32Array([-1, 0, 1]), 16000);
    const pcmView = new Int16Array(pcm16);

    expect(pcmView[0]).toBe(-32768);
    expect(pcmView[1]).toBe(0);
    expect(pcmView[2]).toBe(32767);
  });
});
