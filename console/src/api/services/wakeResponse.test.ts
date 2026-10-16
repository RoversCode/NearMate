import { describe, expect, it, vi } from "vitest";

import {
  deleteWakeResponseItem,
  fetchWakeResponseConfig,
  generateWakeResponseTexts,
  previewWakeResponseText,
  synthesizeWakeResponseItem,
  saveWakeResponseConfig
} from "./wakeResponse";
describe("wake response api", () => {
  it("读取、生成、保存和试听唤醒回复配置", async () => {
    const previewAudioBlob = new Blob(["wav"], { type: "audio/wav" });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/character-cards/yae-miko/wake-response") && init === undefined) {
        return new Response(
          JSON.stringify({
            buckets: {
              morning: [{ id: "m1", text: "早呀，我在", audio_url: "/audio/m1.wav", synthesized_at: 1 }],
              afternoon: [],
              evening: [],
              night: []
            }
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/character-cards/yae-miko/wake-response/generate")) {
        return new Response(
          JSON.stringify({
            buckets: {
              morning: ["早呀，我在"],
              afternoon: ["下午我在"],
              evening: ["晚上好"],
              night: ["夜里我也在"]
            }
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/character-cards/yae-miko/wake-response") && init?.method === "PUT") {
        return new Response(
          JSON.stringify({
            buckets: {
              morning: [{ id: "m1", text: "早呀，我在", audio_url: "/audio/m1.wav", synthesized_at: 1 }],
              afternoon: [],
              evening: [],
              night: []
            }
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/character-cards/yae-miko/wake-response/preview")) {
        // 直接返回同一浏览器运行时创建的 Blob，避免 jsdom Blob 与 Node Response 跨实现转换失真。
        return {
          ok: true,
          status: 200,
          blob: async () => previewAudioBlob
        } as Response;
      }
      if (url.endsWith("/api/character-cards/yae-miko/wake-response/m1/synthesize")) {
        return new Response(
          JSON.stringify({
            id: "m1", text: "早呀，我在", audio_url: "/audio/m1.wav", synthesized_at: 2
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/character-cards/yae-miko/wake-response/m1") && init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return new Response("missing", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchWakeResponseConfig("http://127.0.0.1:8765", "yae-miko")).resolves.toMatchObject({
      buckets: { morning: [{ text: "早呀，我在" }] }
    });
    await expect(generateWakeResponseTexts("http://127.0.0.1:8765", "yae-miko")).resolves.toMatchObject({
      buckets: { night: ["夜里我也在"] }
    });
    await expect(
      saveWakeResponseConfig("http://127.0.0.1:8765", "yae-miko", {
        buckets: { morning: [], afternoon: [], evening: [], night: [] }
      })
    ).resolves.toMatchObject({ buckets: { morning: [{ text: "早呀，我在" }] } });
    const previewAudio = await previewWakeResponseText("http://127.0.0.1:8765", "yae-miko", "早呀，我在");
    expect(previewAudio).toBe(previewAudioBlob);
    expect(previewAudio.type).toBe("audio/wav");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/character-cards/yae-miko/wake-response/preview",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ text: "早呀，我在" })
      }
    );
    await expect(synthesizeWakeResponseItem("http://127.0.0.1:8765", "yae-miko", "m1", "morning", "早呀，我在"))
      .resolves.toMatchObject({ id: "m1", synthesized_at: 2 });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/character-cards/yae-miko/wake-response/m1/synthesize",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bucket: "morning", text: "早呀，我在" })
      }
    );
    await expect(deleteWakeResponseItem("http://127.0.0.1:8765", "yae-miko", "m1")).resolves.toBeUndefined();
  });

  it("二进制试听失败时保留 Backend 的 detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ detail: "当前角色尚未准备语音" }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    )));

    await expect(
      previewWakeResponseText("http://127.0.0.1:8765", "yae-miko", "早呀，我在")
    ).rejects.toThrow("试听唤醒回复失败：当前角色尚未准备语音");

    vi.unstubAllGlobals();
  });
});
