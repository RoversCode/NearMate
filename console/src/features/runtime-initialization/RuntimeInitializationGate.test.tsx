import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimeInitializationGate } from "./RuntimeInitializationGate";
import { installConsoleControlWebSocketMock, resetConsoleAppTestEnvironment } from "@/test/consoleAppTestHarness";

const initial = {
  runtime_id: "d01f2fa6-8e4d-430f-8b72-4bc3d02051ec",
  operation_id: "88c655af-c333-4e30-b424-a1fb67794847",
  revision: 1, state: "initializing", phase: "input_models",
  message: "正在加载语音输入模型", retryable: false
};
const reply = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
let sockets: ReturnType<typeof installConsoleControlWebSocketMock>;
const emit = (payload: unknown) => sockets.forEach(socket => socket.emitMessage({ type: "runtime.initialization_changed", payload }));
const renderGate = () => render(<RuntimeInitializationGate><button>控制台操作</button></RuntimeInitializationGate>);
beforeEach(() => { sockets = installConsoleControlWebSocketMock(); });
afterEach(resetConsoleAppTestEnvironment);

describe("独立运行时初始化门禁", () => {
  it("显示真实阶段且不能关闭，完成后挂载业务页面", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(initial)));
    renderGate();
    const dialog = await screen.findByRole("dialog", { name: "正在初始化 NearMate" });
    expect(screen.getByText(initial.message)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "关闭" })).toBeNull();
    fireEvent.keyDown(dialog, { key: "Escape" });
    fireEvent.pointerDown(document.body);
    expect(screen.getByRole("dialog")).toBeTruthy();
    await act(async () => emit({ ...initial, revision: 2, phase: "assistant", message: "正在启动助手" }));
    expect(screen.getByText("正在启动助手")).toBeTruthy();
    await act(async () => emit({ ...initial, revision: 3, state: "ready", phase: "ready" }));
    await screen.findByRole("button", { name: "控制台操作" });
  });

  it("失败后保持阻断，重复点击只发送一次重试", async () => {
    const failed = { ...initial, state: "failed", phase: "failed", retryable: true };
    let finish!: (value: Response) => void;
    const fetchMock = vi.fn(async (url: string) => url.endsWith("/retry")
      ? new Promise<Response>(resolve => { finish = resolve; }) : reply(failed));
    vi.stubGlobal("fetch", fetchMock);
    renderGate();
    const button = await screen.findByRole("button", { name: "重新初始化" });
    fireEvent.click(button); fireEvent.click(button);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/runtime/initialization/retry")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "控制台操作" })).toBeNull();
    await act(async () => finish(reply({ ...initial, revision: 2 })));
    await screen.findByRole("dialog", { name: "正在初始化 NearMate" });
  });

  it.each([false, true])("较早 HTTP 的完成或失败都不能覆盖新事件（失败：%s）", async fails => {
    let finish!: (value: Response) => void;
    let reject!: (error: Error) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve, fail) => { finish = resolve; reject = fail; })));
    renderGate();
    await act(async () => emit({ ...initial, revision: 3, phase: "media", message: "正在连接语音通道" }));
    await act(async () => fails ? reject(new Error("private network detail"))
      : finish(reply({ ...initial, revision: 2, state: "ready", phase: "ready" })));
    expect(screen.getByText("正在连接语音通道")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "控制台操作" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("Backend 重启从零接管，旧进程的迟到事件不能回退门禁", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(initial)));
    renderGate();
    await screen.findByRole("dialog");
    const restarted = { ...initial, runtime_id: "82f957d6-e169-41d6-8289-d36865a44747", revision: 0 };
    await act(async () => emit(restarted));
    await act(async () => emit({ ...initial, revision: 99, state: "ready", phase: "ready" }));
    expect(screen.queryByRole("button", { name: "控制台操作" })).toBeNull();
    await act(async () => emit({ ...restarted, revision: 1, state: "ready", phase: "ready" }));
    await screen.findByRole("button", { name: "控制台操作" });
    await act(async () => emit(restarted));
    expect(screen.getByRole("button", { name: "控制台操作" })).toBeTruthy();
  });

  it("读取断线保留真实阶段，错误提示允许重新读取", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(reply(initial)).mockRejectedValue(new Error("private detail"));
    vi.stubGlobal("fetch", fetchMock);
    renderGate();
    await screen.findByRole("dialog");
    await act(async () => window.dispatchEvent(new Event("focus")));
    await screen.findByText("暂时无法读取初始化进度。正在重新连接本机服务。");
    expect(screen.getByText(initial.message)).toBeTruthy();
    expect(screen.queryByText("private detail")).toBeNull();
    fetchMock.mockResolvedValue(reply({ ...initial, revision: 2, state: "ready", phase: "ready" }));
    fireEvent.click(screen.getByRole("button", { name: "重新读取进度" }));
    await screen.findByRole("button", { name: "控制台操作" });
  });

  it("idle 允许继续配置，后续必要重建立即卸载业务", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply({ ...initial, operation_id: null, state: "idle", phase: "idle" })));
    renderGate();
    await screen.findByRole("button", { name: "控制台操作" });
    await act(async () => emit({ ...initial, revision: 2 }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "控制台操作" })).toBeNull();
  });

  it("初始服务不可达时保持检查页，重连后才能开放配置", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("offline"));
    vi.stubGlobal("fetch", fetchMock);
    renderGate();
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "控制台操作" })).toBeNull();
    fetchMock.mockResolvedValue(reply({ ...initial, operation_id: null, state: "idle", phase: "idle" }));
    fireEvent.click(screen.getByRole("button", { name: "重新连接" }));
    await screen.findByRole("button", { name: "控制台操作" });
  });

  it("连接正常时不轮询，断线期间每分钟兜底重取", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => reply(initial));
    vi.stubGlobal("fetch", fetchMock);
    renderGate();
    await act(async () => { await Promise.resolve(); });
    await act(async () => sockets[0].emitOpen());
    const baseline = fetchMock.mock.calls.length;
    await act(async () => { vi.advanceTimersByTime(60000); await Promise.resolve(); });
    expect(fetchMock.mock.calls.length).toBe(baseline);
    await act(async () => sockets[0].emitClose());
    const afterDisconnect = fetchMock.mock.calls.length;
    await act(async () => { vi.advanceTimersByTime(60000); await Promise.resolve(); });
    expect(fetchMock.mock.calls.length).toBeGreaterThan(afterDisconnect);
    vi.useRealTimers();
  });
});
