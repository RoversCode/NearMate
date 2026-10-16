import { describe, expect, it, vi } from "vitest";
import { createEnvironmentContextClient } from "./environmentContextClient";

describe("createEnvironmentContextClient", () => {
  it("通过 multipart 上传元数据和 PNG 并返回 snapshot_id", async () => {
    const fetch = vi.fn(async (_input: string, _init?: RequestInit) => new Response(JSON.stringify({
      snapshot_id: "environment-1"
    }), { status: 202, headers: { "Content-Type": "application/json" } }));
    const client = createEnvironmentContextClient({ httpBaseUrl: "http://127.0.0.1:8765", fetch });

    const snapshotId = await client.upload({
      capturedAt: "2026-07-12T00:00:00.000Z",
      activeWindowTitle: "Code",
      screenshotBytes: Buffer.from("png"),
      captureError: null,
      timingsMs: {}
    }, { reason: "chat_submit" });

    expect(snapshotId).toBe("environment-1");
    const init = fetch.mock.calls[0]?.[1];
    expect(init?.body).toBeInstanceOf(FormData);
    const body = init?.body as FormData;
    expect(JSON.parse(String(body.get("metadata")))).toMatchObject({
      active_window_title: "Code",
      reason: "chat_submit"
    });
    expect(body.get("screenshot")).toBeInstanceOf(Blob);
  });

  it("图片被拒绝时重传无图片元数据并保留错误状态", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 422 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ snapshot_id: "environment-metadata" }), {
        status: 202,
        headers: { "Content-Type": "application/json" }
      }));
    const client = createEnvironmentContextClient({ httpBaseUrl: "http://127.0.0.1:8765", fetch });

    await expect(client.upload({
      capturedAt: "2026-07-12T00:00:00.000Z",
      activeWindowTitle: "Code",
      screenshotBytes: Buffer.from("invalid-png"),
      captureError: null,
      timingsMs: {}
    }, { reason: "chat_submit" })).resolves.toBe("environment-metadata");

    const retryBody = fetch.mock.calls[1]?.[1]?.body as FormData;
    expect(retryBody.get("screenshot")).toBeNull();
    expect(JSON.parse(String(retryBody.get("metadata")))).toMatchObject({
      active_window_title: "Code",
      capture_error: "screenshot_upload_rejected:422"
    });
  });
});
