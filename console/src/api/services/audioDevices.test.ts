import { describe, expect, it, vi } from "vitest";

import {
  fetchAudioDevices,
  testAudioDevice,
  updateAudioDeviceSettings
} from "./audioDevices";
describe("audio device api", () => {
  it("读取设备列表、测试麦克风并保存音频设备选择", async () => {
    const devicesPayload = {
      microphones: [
        {
          id: "mic-usb",
          name: "麦克风 (USB Camera)",
          is_default: false,
          status: "available",
          error_type: null,
          error_message: null
        }
      ],
      speaker_loopbacks: [
        {
          id: "speaker-loopback",
          name: "扬声器 (Realtek USB Audio)",
          is_default: true,
          status: "available",
          error_type: null,
          error_message: null
        }
      ],
      selected: {
        mode: "manual",
        microphone_id: "mic-usb",
        microphone_name: "麦克风 (USB Camera)",
        speaker_loopback_id: "speaker-loopback",
        speaker_loopback_name: "扬声器 (Realtek USB Audio)"
      }
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(devicesPayload), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        status: "available",
        peak_level: 0.25,
        rms_level: 0.1,
        duration_ms: 3000,
        sample_rate: 48000,
        level_status: "good",
        audio_wav_b64: "UklGRgAAAAA=",
        error_type: null,
        error_message: null
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ accepted: true, request_id: "audio-change-1" }), { status: 202 }));

    vi.stubGlobal("fetch", fetchMock);

    const devices = await fetchAudioDevices("http://127.0.0.1:8765");
    const testResult = await testAudioDevice("http://127.0.0.1:8765", { microphone_id: "mic-usb" });
    const updateResult = await updateAudioDeviceSettings("http://127.0.0.1:8765", devices.selected);

    expect(devices.microphones[0].status).toBe("available");
    expect(testResult.peak_level).toBe(0.25);
    expect(testResult.duration_ms).toBe(3000);
    expect(testResult.level_status).toBe("good");
    expect(testResult.audio_wav_b64).toBe("UklGRgAAAAA=");
    expect(updateResult.request_id).toBe("audio-change-1");
    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://127.0.0.1:8765/api/audio/devices");
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://127.0.0.1:8765/api/audio/devices/test", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ microphone_id: "mic-usb" })
    });
    expect(fetchMock).toHaveBeenNthCalledWith(3, "http://127.0.0.1:8765/api/settings/audio-devices", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(devices.selected)
    });

    vi.unstubAllGlobals();
  });
});

