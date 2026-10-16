import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchBackgroundAgentUsage } from "./backgroundAgentUsage";

afterEach(() => vi.unstubAllGlobals());

describe("background agent usage service", () => {
  it("携带包含首尾的日期边界和取消信号查询用量", async () => {
    const signal = new AbortController().signal;
    const fetchMock = vi.fn(async () => jsonResponse(createUsagePayload()));
    vi.stubGlobal("fetch", fetchMock);

    const response = await fetchBackgroundAgentUsage(
      "http://127.0.0.1:8765/",
      { fromDate: "2026-08-01", toDate: "2026-08-02" },
      signal
    );

    expect(response.summary.total_input_tokens).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/background-agent/usage?from_date=2026-08-01&to_date=2026-08-02",
      { signal }
    );
  });

  it("拒绝总输入和缓存公式不一致的响应", async () => {
    const payload = createUsagePayload();
    payload.summary.total_input_tokens = 999;
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(payload)));

    await expect(
      fetchBackgroundAgentUsage("http://127.0.0.1:8765", {
        fromDate: "2026-08-01",
        toDate: "2026-08-02"
      })
    ).rejects.toThrow("响应格式不符合契约");
  });

  it("保留 Backend 返回的结构化错误正文", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ detail: "后台用量数据库暂不可用" }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    )));

    await expect(
      fetchBackgroundAgentUsage("http://127.0.0.1:8765", {
        fromDate: "2026-08-01",
        toDate: "2026-08-02"
      })
    ).rejects.toThrow("读取后台工作用量失败：后台用量数据库暂不可用");
  });
});

export function createUsagePayload() {
  const tokenMetrics = {
    non_cached_input_tokens: 100,
    cache_creation_input_tokens: 20,
    cache_read_input_tokens: 80,
    total_input_tokens: 200,
    output_tokens: 50,
    cache_hit_tokens: 80,
    cache_hit_rate: 0.4
  };
  return {
    from_date: "2026-08-01",
    to_date: "2026-08-02",
    summary: {
      ...tokenMetrics,
      task_count: 2,
      complete_task_count: 1,
      incomplete_task_count: 1,
      reference_cost_usd: 0.125,
      reference_cost_task_count: 1
    },
    by_model: [
      {
        model: "deepseek-v4-pro",
        ...tokenMetrics,
        task_count: 2,
        complete_task_count: 1,
        incomplete_task_count: 1
      }
    ],
    by_day: [
      {
        date: "2026-08-02",
        ...tokenMetrics,
        task_count: 2,
        complete_task_count: 1,
        incomplete_task_count: 1,
        reference_cost_usd: 0.125,
        reference_cost_task_count: 1
      }
    ]
  };
}

function jsonResponse(payload: object): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" }
  });
}
