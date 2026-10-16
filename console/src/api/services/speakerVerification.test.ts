import { describe, expect, it, vi } from "vitest";

import {
  createSpeakerVerificationSample,
  deleteSpeakerVerificationSample,
  fetchSpeakerVerificationProfile,
  updateSpeakerVerificationProfile
} from "./speakerVerification";
describe("speaker verification api", () => {
  it("读取、开启、上传和删除说话人白名单样本", async () => {
    const profilePayload = {
      enabled: false,
      samples: [
        {
          id: "sample-1",
          audio_url: "/api/speaker-verification/samples/sample-1/audio",
          created_at: 123
        }
      ],
      max_sample_count: 10,
      required_speech_seconds: 3,
      can_enable: true
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(profilePayload), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...profilePayload, enabled: true }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(profilePayload), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...profilePayload, samples: [] }), { status: 202 }));

    vi.stubGlobal("fetch", fetchMock);

    const profile = await fetchSpeakerVerificationProfile("http://127.0.0.1:8765");
    await updateSpeakerVerificationProfile("http://127.0.0.1:8765", true);
    await createSpeakerVerificationSample("http://127.0.0.1:8765", { recording_id: "recording-1", replace_sample_id: "sample-1" });
    await deleteSpeakerVerificationSample("http://127.0.0.1:8765", "sample-1");

    expect(profile.samples[0].id).toBe("sample-1");
    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://127.0.0.1:8765/api/speaker-verification/profile");
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://127.0.0.1:8765/api/speaker-verification/profile", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ enabled: true })
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8765/api/speaker-verification/samples",
      expect.objectContaining({
        method: "POST",
        body: expect.any(FormData)
      })
    );
    const formData = fetchMock.mock.calls[2][1]?.body as FormData;
    expect(formData.get("recording_id")).toBe("recording-1");
    expect(formData.get("replace_sample_id")).toBe("sample-1");
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8765/api/speaker-verification/samples/sample-1",
      {
        method: "DELETE"
      }
    );

    vi.unstubAllGlobals();
  });

  it("multipart 样本保存失败时保留 Backend 的 detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ detail: "录音会话已过期，请重新录制" }),
      { status: 409, headers: { "Content-Type": "application/json" } }
    )));

    await expect(createSpeakerVerificationSample("http://127.0.0.1:8765", {
      recording_id: "expired-recording"
    })).rejects.toThrow("保存说话人白名单录音失败：录音会话已过期，请重新录制");

    vi.unstubAllGlobals();
  });
});

