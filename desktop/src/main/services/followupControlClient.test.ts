import { describe, expect, it, vi } from "vitest";

import { createFollowupControlClient } from "./followupControlClient";

describe("createFollowupControlClient", () => {
  it("快速连按按顺序请求，前次失败不会卡住之后的切换", async () => {
    let rejectFirst!: (error: Error) => void;
    const first = new Promise<Response>((_resolve, reject) => { rejectFirst = reject; });
    const fetchMock = vi.fn(async () => Response.json({ accepted: true, followup_state: "followup_cool", interrupted_request_ids: [] }, { status: 202 }))
      .mockReturnValueOnce(first);
    const client = createFollowupControlClient({ httpBaseUrl: "http://localhost:8765", fetch: fetchMock });
    const one = client.toggleFollowup();
    const failure = expect(one).rejects.toThrow("offline");
    const two = client.toggleFollowup();
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledOnce();
    rejectFirst(new Error("offline"));
    await failure;
    await expect(two).resolves.toEqual({ interruptedRequestIds: [] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("缺少取消回执的 2xx 不能错误解除本地静音", async () => {
    const client = createFollowupControlClient({ httpBaseUrl: "http://localhost:8765", fetch: async () => Response.json({ accepted: true }) });
    await expect(client.toggleFollowup()).rejects.toThrow("followup toggle response invalid");
  });
  it("请求控制面切换追问状态", async () => {
    const fetchMock = vi.fn(async () => Response.json({ accepted: true, followup_state: "followup_cool", interrupted_request_ids: ["old-request"], speech_interrupted_at: 100 }, { status: 202 }));
    const client = createFollowupControlClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: fetchMock
    });

    await expect(client.toggleFollowup()).resolves.toEqual({ interruptedRequestIds: ["old-request"], speechInterruptedAt: 100 });

    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/followup/toggle", {
      method: "POST",
      signal: expect.any(AbortSignal)
    });
  });

  it("切换请求失败时抛出稳定错误", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 503 }));
    const client = createFollowupControlClient({
      httpBaseUrl: "http://127.0.0.1:8765/",
      fetch: fetchMock
    });

    await expect(client.toggleFollowup()).rejects.toThrow("followup toggle request failed: 503");
  });
});
