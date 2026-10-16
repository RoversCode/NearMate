// @vitest-environment jsdom

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BackgroundAgentUsagePage } from "./BackgroundAgentUsagePage";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("background agent usage page", () => {
  it("默认查询今天并展示总览、部分覆盖、模型表和趋势", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse(createUsagePayload())
    );
    vi.stubGlobal("fetch", fetchMock);

    const view = render(
      <BackgroundAgentUsagePage baseUrl="http://127.0.0.1:8765" />
    );

    expect(await screen.findByText("$0.1250")).toBeTruthy();
    expect(screen.getByText("deepseek-v4-pro")).toBeTruthy();
    expect(screen.getByText(/SDK 参考成本仅覆盖 1 \/ 2 项任务/)).toBeTruthy();
    expect(screen.getByRole("img", { name: "每日后台工作输入和输出 Token 趋势" })).toBeTruthy();
    expect(view.container.querySelectorAll("svg circle")).toHaveLength(2);

    const requestUrl = new URL(fetchMock.mock.calls[0][0].toString());
    const today = new Date();
    const expectedDate = [
      today.getFullYear(),
      String(today.getMonth() + 1).padStart(2, "0"),
      String(today.getDate()).padStart(2, "0")
    ].join("-");
    expect(requestUrl.searchParams.get("from_date")).toBe(expectedDate);
    expect(requestUrl.searchParams.get("to_date")).toBe(expectedDate);
  });

  it("切换时间范围时取消过期请求", async () => {
    const pendingResponse = new Promise<Response>(() => undefined);
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => pendingResponse)
      .mockImplementationOnce(async () => jsonResponse(createEmptyUsagePayload()));
    vi.stubGlobal("fetch", fetchMock);

    render(<BackgroundAgentUsagePage baseUrl="http://127.0.0.1:8765" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const firstSignal = (fetchMock.mock.calls[0][1] as RequestInit).signal;

    fireEvent.click(screen.getByRole("button", { name: "近 7 天" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(firstSignal?.aborted).toBe(true);
    expect(await screen.findByText("所选时间范围内还没有后台工作用量。")).toBeTruthy();
  });

  it("展示查询错误并允许重试", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(jsonResponse(createEmptyUsagePayload()));
    vi.stubGlobal("fetch", fetchMock);

    render(<BackgroundAgentUsagePage baseUrl="http://127.0.0.1:8765" />);

    expect(await screen.findByText("读取后台工作用量失败：HTTP 503")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("所选时间范围内还没有后台工作用量。")).toBeTruthy();
  });

  it("跨午夜后自动滚动今天并重新查询", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 2, 23, 59, 59, 500));
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse(createEmptyUsagePayload())
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<BackgroundAgentUsagePage baseUrl="http://127.0.0.1:8765" />);
    await act(async () => Promise.resolve());

    expect(new URL(fetchMock.mock.calls[0][0].toString()).searchParams.get("from_date")).toBe(
      "2026-08-02"
    );

    await act(async () => {
      vi.advanceTimersByTime(500);
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(new URL(fetchMock.mock.calls[1][0].toString()).searchParams.get("from_date")).toBe(
      "2026-08-03"
    );
  });

  it("跨午夜后保持自定义日期不变", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 2, 23, 59, 59, 500));
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse(createEmptyUsagePayload())
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<BackgroundAgentUsagePage baseUrl="http://127.0.0.1:8765" />);
    await act(async () => Promise.resolve());

    fireEvent.change(screen.getByLabelText("开始日期"), {
      target: { value: "2026-07-01" }
    });
    await act(async () => Promise.resolve());
    const callsBeforeMidnight = fetchMock.mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(500);
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(callsBeforeMidnight);
    expect((screen.getByLabelText("开始日期") as HTMLInputElement).value).toBe("2026-07-01");
  });

  it("系统休眠跨日后在窗口恢复焦点时重新校准", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 2, 12));
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse(createEmptyUsagePayload())
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<BackgroundAgentUsagePage baseUrl="http://127.0.0.1:8765" />);
    await act(async () => Promise.resolve());

    vi.setSystemTime(new Date(2026, 7, 3, 12));
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(new URL(fetchMock.mock.calls[1][0].toString()).searchParams.get("from_date")).toBe(
      "2026-08-03"
    );
  });
});

function createUsagePayload() {
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
    from_date: "2026-08-02",
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

function createEmptyUsagePayload() {
  const tokenMetrics = {
    non_cached_input_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_input_tokens: 0,
    output_tokens: 0,
    cache_hit_tokens: 0,
    cache_hit_rate: null
  };
  return {
    from_date: "2026-08-02",
    to_date: "2026-08-02",
    summary: {
      ...tokenMetrics,
      task_count: 0,
      complete_task_count: 0,
      incomplete_task_count: 0,
      reference_cost_usd: null,
      reference_cost_task_count: 0
    },
    by_model: [],
    by_day: []
  };
}

function jsonResponse(payload: object): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" }
  });
}
