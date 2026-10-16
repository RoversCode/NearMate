import { describe, expect, it } from "vitest";
import { readLlmAccountStatus } from "./llmAccountStatus";

describe("llmAccountStatus", () => {
  it("只接受闭合的厂商余额快照", () => {
    const snapshot = readLlmAccountStatus({
      providers: [{
        provider_id: "deepseek",
        display_name: "DeepSeek",
        active: true,
        status: "available",
        balance: "12.50",
        currency: "CNY",
        fetched_at: "2026-09-25T05:00:00Z",
        message: null
      }]
    });

    expect(snapshot.providers[0].balance).toBe("12.50");
    expect(() => readLlmAccountStatus({ providers: [{ provider_id: "deepseek" }] })).toThrow();
  });
});

