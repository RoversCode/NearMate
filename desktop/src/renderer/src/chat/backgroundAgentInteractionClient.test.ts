import { describe, expect, it, vi } from "vitest";
import { submitBackgroundAgentPermissionDecision } from "./backgroundAgentInteractionClient";

describe("backgroundAgentInteractionClient", () => {
  it("按 request id 提交权限决定", async () => {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 202 }));

    await submitBackgroundAgentPermissionDecision(
      "http://127.0.0.1:8765",
      {
        requestId: "request/a",
        kind: "permission",
        outcome: "allow"
      },
      fetchImpl
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/background-agent/interactions/request%2Fa/permission-decision",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          outcome: "allow"
        })
      })
    );
  });

  it("Backend 拒绝重复回答时向 UI 抛出失败", async () => {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 409 }));

    await expect(submitBackgroundAgentPermissionDecision(
      "http://127.0.0.1:8765",
      {
        requestId: "request-a",
        kind: "permission",
        outcome: "deny"
      },
      fetchImpl
    )).rejects.toThrow("409");
  });
});
