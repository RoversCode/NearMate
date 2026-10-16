// @vitest-environment jsdom

import { describe, expect, it } from "vitest";

import {
  analyzeLive2dAudioFrame,
  calculateLive2dMouthOpenValue
} from "./live2dAudioAnalysis";

describe("calculateLive2dMouthOpenValue", () => {
  it("复刻 SDK 的 RMS 到 dB 归一化算法", () => {
    const buffer = new Float32Array([0.01, -0.01, 0.01, -0.01]);

    const analysis = analyzeLive2dAudioFrame(buffer, {
      maxDecibels: -10,
      minDecibels: -90
    });

    expect(analysis.rms).toBeCloseTo(0.01);
    expect(analysis.level).toBe(0.6);
  });

  it("按 SDK 的 lipSyncGain、pow 和有声最低开口生成嘴型值", () => {
    const buffer = new Float32Array([0.01, -0.01, 0.01, -0.01]);

    const value = calculateLive2dMouthOpenValue(buffer, {
      lipSyncGain: 1.5,
      maxDecibels: -10,
      minDecibels: -90
    });

    expect(value).toBeCloseTo(Math.pow(0.9, 1.15));
  });

  it("静音窗口保持闭嘴", () => {
    const buffer = new Float32Array([0, 0, 0, 0]);

    const value = calculateLive2dMouthOpenValue(buffer, {
      lipSyncGain: 1.5,
      maxDecibels: -10,
      minDecibels: -90
    });

    expect(value).toBe(0);
  });
});
