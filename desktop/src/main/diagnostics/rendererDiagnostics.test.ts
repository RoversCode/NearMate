import { afterEach, describe, expect, it, vi } from "vitest";

import { logRendererDiagnostic } from "./rendererDiagnostics";

describe("logRendererDiagnostic", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("把唇形同步音频帧诊断 code 翻译成终端中文文案", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const details = {
      mouthValue: 0.8,
      parameterValueAfter: 0.8,
      parameterValueBefore: 0
    };

    logRendererDiagnostic("avatar", {
      details,
      level: "warn",
      message: "avatar.live2d_lip_sync_frame"
    });

    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("[companion-live2d] 唇形同步音频帧"),
      details
    );
  });

  it("把唇形同步音频绑定诊断 code 翻译成终端中文文案", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const details = {
      audioSourceKind: "media_stream",
      mediaStreamTrackCount: 1
    };

    logRendererDiagnostic("avatar", {
      details,
      level: "warn",
      message: "avatar.live2d_lip_sync_audio_bound"
    });

    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("[companion-live2d] 唇形同步音频源已绑定"),
      details
    );
  });
});
