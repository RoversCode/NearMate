// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBootstrapStatus, type BootstrapStatus } from "./useBootstrapStatus";

const readyBootstrapEvent = {
  type: "bootstrap.status_changed",
  payload: {
    is_initialized: true,
    has_qweather_api_key: false,
    llm: { is_ready: true, missing: [], errors: [] },
    voice: {
      provider: "local_streamvox",
      is_ready: true,
      phase: "ready",
      model_id: null,
      role_name: null,
      has_confirmed_voice: true
    }
  }
};

const initializingRuntime = {
  runtime_id: "11111111-1111-4111-8111-111111111111",
  operation_id: "22222222-2222-4222-8222-222222222222",
  revision: 2,
  state: "initializing",
  phase: "input_models",
  message: "正在准备语音输入模型",
  retryable: false
};

function Probe({ onStatus, baseUrl }: { onStatus: (status: BootstrapStatus) => void; baseUrl?: string }) {
  onStatus(useBootstrapStatus(baseUrl));
  return null;
}

describe("useBootstrapStatus", () => {
  let root: ReturnType<typeof createRoot>;
  let host: HTMLDivElement;
  let latest: BootstrapStatus | null;
  let fetchMock: ReturnType<typeof vi.fn>;
  let sockets: Array<{ onopen: (() => void) | null; onmessage: ((event: { data: string }) => void) | null; onclose: (() => void) | null; onerror: (() => void) | null; close: () => void }>;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    latest = null;
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    sockets = [];
    vi.stubGlobal("WebSocket", class {
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor() { sockets.push(this); }
      close() { this.onclose?.(); }
    });
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function flush() {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("首次只读取一次 Bootstrap HTTP 状态", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ ...readyBootstrapEvent.payload, is_initialized: true }) });
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(30000); await Promise.resolve(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(latest?.status).toBe("ready");
  });

  it("HTTP 仍支持手动重试和断线低频兜底", async () => {
    fetchMock
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ...readyBootstrapEvent.payload, is_initialized: false }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ...readyBootstrapEvent.payload, is_initialized: true }) });
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    await flush();
    expect(latest?.reason).toBe("uninitialized");
    act(() => latest?.retry());
    await flush();
    expect(latest?.status).toBe("ready");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("通过状态 WebSocket 接收 Bootstrap 就绪变化", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ ...readyBootstrapEvent.payload, is_initialized: false }) });
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    await flush();
    expect(sockets).toHaveLength(1);
    act(() => sockets[0].onopen?.());
    act(() => sockets[0].onmessage?.({ data: JSON.stringify(readyBootstrapEvent) }));
    expect(latest?.status).toBe("ready");
  });

  it.each(["网络异常", "HTTP 500", "五秒超时"])("WebSocket 已就绪后，较早 HTTP 请求的%s不覆盖状态", async failure => {
    let resolveRequest!: (response: { ok: boolean }) => void;
    let rejectRequest!: (error: Error) => void;
    fetchMock.mockImplementationOnce((_input: string, options: RequestInit) => new Promise((resolve, reject) => {
      resolveRequest = resolve;
      rejectRequest = reject;
      options.signal?.addEventListener("abort", () => reject(new DOMException("请求超时", "AbortError")));
    }));
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    expect(latest?.status).toBe("loading");
    act(() => {
      sockets[0].onopen?.();
      sockets[0].onmessage?.({ data: JSON.stringify(readyBootstrapEvent) });
    });
    expect(latest?.status).toBe("ready");

    await act(async () => {
      if (failure === "网络异常") rejectRequest(new Error("网络断开"));
      else if (failure === "HTTP 500") resolveRequest({ ok: false });
      else vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(latest?.status).toBe("ready");
    expect(latest?.reason).toBeNull();
  });

  it("WebSocket 已就绪后，较早 HTTP 成功响应也不覆盖状态", async () => {
    let resolveRequest!: (response: { ok: boolean; json: () => Promise<unknown> }) => void;
    fetchMock.mockImplementationOnce(() => new Promise(resolve => { resolveRequest = resolve; }));
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    act(() => sockets[0].onmessage?.({ data: JSON.stringify(readyBootstrapEvent) }));
    await act(async () => {
      resolveRequest({ ok: true, json: async () => ({ ...readyBootstrapEvent.payload, is_initialized: false }) });
    });
    expect(latest?.status).toBe("ready");
  });

  it("缺少 HTTP 基址时不发请求并阻断", async () => {
    act(() => root.render(createElement(Probe, { baseUrl: undefined, onStatus: value => { latest = value; } })));
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(latest?.reason).toBe("backend_unavailable");
  });

  it("初始化进度之后返回的旧 HTTP 就绪快照不能越过门禁", async () => {
    let resolveRequest!: (response: { ok: boolean; json: () => Promise<unknown> }) => void;
    fetchMock.mockImplementationOnce(() => new Promise(resolve => { resolveRequest = resolve; }));
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({ type: "runtime.initialization_changed", payload: initializingRuntime }) }));
    await act(async () => { resolveRequest({ ok: true, json: async () => ({ ...readyBootstrapEvent.payload, is_initialized: true }) }); });
    expect(latest?.status).toBe("blocked");
    expect(latest?.reason).toBe("uninitialized");
  });

  it("较旧的初始化进度不能重新关闭已就绪的同一运行时", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ ...readyBootstrapEvent.payload, is_initialized: false }) });
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    await flush();
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({
      type: "runtime.initialization_changed", payload: { ...initializingRuntime, revision: 3, state: "ready", phase: "ready" }
    }) }));
    act(() => sockets[0].onmessage?.({ data: JSON.stringify(readyBootstrapEvent) }));
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({ type: "runtime.initialization_changed", payload: initializingRuntime }) }));
    expect(latest?.status).toBe("ready");
  });

  it("初始化阶段立即关闭业务，ready 仍等待完整 Bootstrap", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => readyBootstrapEvent.payload });
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    await flush();
    expect(latest?.status).toBe("ready");
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({ type: "runtime.initialization_changed", payload: initializingRuntime }) }));
    expect(latest?.reason).toBe("uninitialized");
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({ type: "runtime.initialization_changed", payload: { ...initializingRuntime, revision: 3, state: "ready", phase: "ready" } }) }));
    expect(latest?.status).toBe("blocked");
    act(() => sockets[0].onmessage?.({ data: JSON.stringify(readyBootstrapEvent) }));
    expect(latest?.status).toBe("ready");
  });

  it("新 Backend 从零接管后忽略已退出进程的迟到阶段", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => readyBootstrapEvent.payload });
    act(() => root.render(createElement(Probe, { baseUrl: "http://127.0.0.1:8765", onStatus: value => { latest = value; } })));
    await flush();
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({ type: "runtime.initialization_changed", payload: initializingRuntime }) }));
    const restarted = { ...initializingRuntime, runtime_id: "33333333-3333-4333-8333-333333333333", revision: 0, state: "ready", phase: "ready" };
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({ type: "runtime.initialization_changed", payload: restarted }) }));
    act(() => sockets[0].onmessage?.({ data: JSON.stringify(readyBootstrapEvent) }));
    act(() => sockets[0].onmessage?.({ data: JSON.stringify({ type: "runtime.initialization_changed", payload: { ...initializingRuntime, revision: 99 } }) }));
    expect(latest?.status).toBe("ready");
  });

});
