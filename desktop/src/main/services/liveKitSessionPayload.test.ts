import { describe, expect, it } from "vitest";
import { readLiveKitDesktopSession } from "./liveKitSessionPayload";

function createRequiredPayload() {
  return {
    url: "wss://livekit.example.test",
    room_name: "desktop-room",
    desktop_ui: {
      identity: "desktop-ui-identity",
      token: "desktop-ui-token"
    },
    desktop_aec: {
      identity: "desktop-aec-identity",
      token: "desktop-aec-token"
    }
  };
}

describe("LiveKit session payload parser", () => {
  it("缺失可选调试配置和音频设备时，会回退到安全默认值", () => {
    expect(readLiveKitDesktopSession(createRequiredPayload())).toEqual({
      url: "wss://livekit.example.test",
      roomName: "desktop-room",
      desktopUi: {
        identity: "desktop-ui-identity",
        token: "desktop-ui-token"
      },
      desktopAec: {
        identity: "desktop-aec-identity",
        token: "desktop-aec-token"
      },
      aecSidecarDebugAudio: {
        enabled: false,
        outputDir: "",
        maxRuns: 10
      },
      audioDeviceSelection: {
        mode: "system_default",
        microphoneId: null,
        microphoneName: null,
        speakerLoopbackId: null,
        speakerLoopbackName: null
      }
    });
  });

  it("会把后端 snake_case 设备选择转换成主进程 camelCase 结构", () => {
    expect(readLiveKitDesktopSession({
      ...createRequiredPayload(),
      audio_devices: {
        mode: "manual",
        microphone_id: "mic-usb",
        microphone_name: "麦克风 (USB Camera)",
        speaker_loopback_id: "speaker-loopback",
        speaker_loopback_name: "扬声器 (Realtek USB Audio)"
      }
    }).audioDeviceSelection).toEqual({
      mode: "manual",
      microphoneId: "mic-usb",
      microphoneName: "麦克风 (USB Camera)",
      speakerLoopbackId: "speaker-loopback",
      speakerLoopbackName: "扬声器 (Realtek USB Audio)"
    });
  });

  it("显式非法 audio_devices 结构会返回稳定错误", () => {
    expect(() => readLiveKitDesktopSession({
      ...createRequiredPayload(),
      audio_devices: {
        mode: "manual",
        microphone_id: 42,
        microphone_name: null,
        speaker_loopback_id: null,
        speaker_loopback_name: null
      }
    })).toThrow("LiveKit session response is missing audio_devices.");
  });
});
