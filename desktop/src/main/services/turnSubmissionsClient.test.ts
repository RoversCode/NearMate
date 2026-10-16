import { describe, expect, it, vi } from "vitest";
import { createTurnSubmissionsClient } from "./turnSubmissionsClient";

const SUBMISSION_ID = "6d77fa4e-177c-49b6-a4e3-42df9b46211d";

describe("turnSubmissionsClient", () => {
  it("把 reservation 和环境提交映射到两阶段 HTTP 协议", async () => {
    const fetchMock = vi.fn(async (_input: string, _init?: RequestInit) => acceptedResponse());
    const client = createTurnSubmissionsClient({
      httpBaseUrl: "http://127.0.0.1:8765/",
      fetch: fetchMock
    });

    await client.reserve({ submissionId: SUBMISSION_ID, messageText: "整理材料" });
    await client.commitContext({
      submissionId: SUBMISSION_ID,
      environmentSnapshotId: "environment-a"
    });

    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://127.0.0.1:8765/api/turn-submissions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ submission_id: SUBMISSION_ID, message_text: "整理材料" })
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://127.0.0.1:8765/api/turn-submissions/${SUBMISSION_ID}/context`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ environment_snapshot_id: "environment-a" })
      }
    );
  });

  it("队列操作使用同一 submission 身份和窄命令路径", async () => {
    const fetchMock = vi.fn(async (_input: string, _init?: RequestInit) => acceptedResponse());
    const client = createTurnSubmissionsClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: fetchMock
    });

    await client.edit({ submissionId: SUBMISSION_ID, messageText: "新正文" });
    await client.delete(SUBMISSION_ID);
    await client.resume(SUBMISSION_ID);
    await client.retry(SUBMISSION_ID);
    await client.reportPreparationFailed(SUBMISSION_ID);

    expect(fetchMock.mock.calls.map(call => [call[0], call[1]?.method])).toEqual([
      [`http://127.0.0.1:8765/api/turn-submissions/${SUBMISSION_ID}`, "PATCH"],
      [`http://127.0.0.1:8765/api/turn-submissions/${SUBMISSION_ID}`, "DELETE"],
      [`http://127.0.0.1:8765/api/turn-submissions/${SUBMISSION_ID}/resume`, "POST"],
      [`http://127.0.0.1:8765/api/turn-submissions/${SUBMISSION_ID}/retry`, "POST"],
      [`http://127.0.0.1:8765/api/turn-submissions/${SUBMISSION_ID}/preparation-failed`, "POST"]
    ]);
  });

  it("拒绝失败状态", async () => {
    const failedClient = createTurnSubmissionsClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => new Response(null, { status: 409 }))
    });

    await expect(failedClient.resume(SUBMISSION_ID)).rejects.toThrow(
      "turn submission request failed: 409"
    );
  });

  it("拒绝字段不完整的权威快照", async () => {
    const invalidClient = createTurnSubmissionsClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => new Response(JSON.stringify({
        accepted: true,
        turn_submissions: { revision: 1, items: [{ state: "unknown" }] }
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
    });

    await expect(invalidClient.resume(SUBMISSION_ID)).rejects.toThrow(
      "turn submission item fields are invalid"
    );
  });

  it("拒绝字段完整但身份非法的权威快照", async () => {
    const invalidClient = createTurnSubmissionsClient({
      httpBaseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn(async () => new Response(JSON.stringify({
        accepted: true,
        turn_submissions: {
          revision: 1,
          items: [{
            submission_id: "not-a-uuid",
            message_text: "整理材料",
            state: "queued",
            created_order: 1,
            error_message: null
          }]
        }
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
    });

    await expect(invalidClient.resume(SUBMISSION_ID)).rejects.toThrow(
      "turn submission id is invalid"
    );
  });
});

function acceptedResponse(): Response {
  return new Response(JSON.stringify({
    accepted: true,
    turn_submissions: { revision: 1, items: [] }
  }), {
    status: 202,
    headers: { "Content-Type": "application/json" }
  });
}
