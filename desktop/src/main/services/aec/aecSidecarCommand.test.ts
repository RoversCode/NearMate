import { delimiter, join } from "node:path";
import { describe, expect, it } from "vitest";
import { buildAecSidecarSpawnArgs, buildAecSidecarSpawnEnv } from "./aecSidecarCommand";
import type { AecSidecarSession } from "./aecSidecarTypes";

function createSessionFixture(): AecSidecarSession {
  return {
    livekitUrl: "wss://livekit.example.test",
    roomName: "desktop-room",
    identity: "desktop-aec-identity",
    token: "desktop-aec-token",
    debugAudio: {
      enabled: true,
      outputDir: "G:\\repo\\backend\\data\\debug-audio\\aec-sidecar",
      maxRuns: 7
    },
    audioDeviceSelection: {
      mode: "manual",
      microphoneId: "mic-usb",
      microphoneName: "麦克风 (USB Camera)",
      speakerLoopbackId: "speaker-loopback",
      speakerLoopbackName: "扬声器 (Realtek USB Audio)"
    }
  };
}

describe("AEC sidecar command", () => {
  it("buildAecSidecarSpawnArgs 会集中生成 Python sidecar 启动参数", () => {
    expect(buildAecSidecarSpawnArgs(createSessionFixture(), 4242)).toEqual([
      "-B",
      "-X",
      "utf8",
      "-m",
      "audio_aec_sidecar.main",
      "--livekit-url",
      "wss://livekit.example.test",
      "--token",
      "desktop-aec-token",
      "--room-name",
      "desktop-room",
      "--identity",
      "desktop-aec-identity",
      "--parent-pid",
      "4242",
      "--sample-rate",
      "48000",
      "--clean-output-sample-rate",
      "16000",
      "--warmup-seconds",
      "2",
      "--audio-device-mode",
      "manual",
      "--microphone-id",
      "mic-usb",
      "--microphone-name",
      "麦克风 (USB Camera)",
      "--speaker-loopback-id",
      "speaker-loopback",
      "--speaker-loopback-name",
      "扬声器 (Realtek USB Audio)",
      "--debug-output-dir",
      "G:\\repo\\backend\\data\\debug-audio\\aec-sidecar",
      "--max-debug-runs",
      "7"
    ]);
  });

  it("安装版保留隔离模式并明确指定中文管道编码", () => {
    expect(buildAecSidecarSpawnArgs(createSessionFixture(), 4242, true).slice(0, 6))
      .toEqual(["-I", "-B", "-X", "utf8", "-c", "from audio_aec_sidecar.main import main; main()"]);
  });

  it("buildAecSidecarSpawnEnv 会把 sidecar 源码路径放到 PYTHONPATH 最前面", () => {
    const sidecarSourcePath = join("G:\\repo", "tools", "audio-aec-poc", "src");
    const env = buildAecSidecarSpawnEnv("G:\\repo", { PYTHONPATH: "C:\\existing" });

    expect(env).toEqual({
      PYTHONPATH: [sidecarSourcePath, "C:\\existing"].join(delimiter)
    });
  });
});
