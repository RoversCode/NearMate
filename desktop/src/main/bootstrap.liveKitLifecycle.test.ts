import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setupBootstrapHarness } from "./bootstrap.testHarness";
import type { LiveKitUnexpectedTermination } from "./services/managedLiveKitSupervisor";

const connection = { url: "ws://127.0.0.1:20001", apiKey: "test-key", apiSecret: "test-secret" };

beforeEach(() => {
  (process as { resourcesPath?: string }).resourcesPath = "C:/NearMate/resources";
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ is_initialized: true }))));
});
afterEach(() => { delete (process as { resourcesPath?: string }).resourcesPath; vi.unstubAllGlobals(); });

describe("安装态内置媒体服务生命周期", () => {
  it("媒体服务就绪之前不启动 Backend，随后注入同一份连接配置", async () => {
    let ready!: () => void;
    const pending = new Promise<void>(resolve => { ready = resolve; });
    const mediaStart = vi.fn(async () => { await pending; return connection; });
    const backendFactory = vi.fn(() => ({ start: vi.fn(async () => {}), stop: vi.fn(async () => {}), forceStop: vi.fn() }));
    vi.doMock("./services/managedLiveKitSupervisor", () => ({ createManagedLiveKitSupervisor: vi.fn(() => ({ start: mediaStart, stop: vi.fn(async () => {}), forceStop: vi.fn() })) }));
    vi.doMock("./services/backendSupervisor", () => ({ createBackendSupervisor: backendFactory }));
    const h = await setupBootstrapHarness({ isPackaged: true, awaitBootstrap: false });
    await vi.waitFor(() => expect(mediaStart).toHaveBeenCalledOnce());
    expect(backendFactory).not.toHaveBeenCalled();
    ready();
    await h.bootstrapPromise;
    expect(backendFactory).toHaveBeenCalledWith(expect.objectContaining({ liveKitConnection: connection }));
  });

  it("Backend 启动失败仍回收媒体服务，不留下独立进程", async () => {
    const mediaStop = vi.fn(async () => {});
    vi.doMock("./services/managedLiveKitSupervisor", () => ({ createManagedLiveKitSupervisor: vi.fn(() => ({ start: vi.fn(async () => connection), stop: mediaStop, forceStop: vi.fn() })) }));
    vi.doMock("./services/backendSupervisor", () => ({ createBackendSupervisor: vi.fn(() => ({ start: vi.fn(async () => { throw new Error("backend failed"); }), stop: vi.fn(async () => {}), forceStop: vi.fn() })) }));
    const h = await setupBootstrapHarness({ isPackaged: true, awaitBootstrap: false });
    await expect(h.bootstrapPromise).rejects.toThrow("backend failed");
    expect(mediaStop).toHaveBeenCalledOnce();
  });

  it("正常退出在 AEC 与 Backend 完成后才关闭媒体服务", async () => {
    const order: string[] = [];
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({ createAecSidecarSupervisor: vi.fn(() => ({ start: vi.fn(), stop: vi.fn(), stopAndWait: async () => { order.push("aec"); }, forceStop: vi.fn() })) }));
    vi.doMock("./services/backendSupervisor", () => ({ createBackendSupervisor: vi.fn(() => ({ start: vi.fn(async () => {}), stop: async () => { order.push("backend"); }, forceStop: vi.fn() })) }));
    vi.doMock("./services/managedLiveKitSupervisor", () => ({ createManagedLiveKitSupervisor: vi.fn(() => ({ start: vi.fn(async () => connection), stop: async () => { order.push("media"); }, forceStop: vi.fn() })) }));
    const h = await setupBootstrapHarness({ isPackaged: true });
    h.appMock.emit("before-quit", { preventDefault: vi.fn() });
    await vi.waitFor(() => expect(h.appMock.quit).toHaveBeenCalledOnce());
    expect(order).toEqual(["aec", "backend", "media"]);
  });

  it("媒体服务意外退出有恢复说明与日志入口，不自动重启", async () => {
    let notify!: (value: LiveKitUnexpectedTermination) => Promise<void>;
    const start = vi.fn(async () => connection);
    vi.doMock("./services/managedLiveKitSupervisor", () => ({ createManagedLiveKitSupervisor: vi.fn((options: { onUnexpectedTermination: typeof notify }) => {
      notify = options.onUnexpectedTermination;
      return { start, stop: vi.fn(async () => {}), forceStop: vi.fn() };
    }) }));
    vi.doMock("./services/backendSupervisor", () => ({ createBackendSupervisor: vi.fn(() => ({ start: vi.fn(async () => {}), stop: vi.fn(async () => {}), forceStop: vi.fn() })) }));
    const h = await setupBootstrapHarness({ isPackaged: true });
    await notify({ kind: "exit", pid: 10, code: 1, signal: null, logPath: "C:/test/livekit-runtime.log" });
    expect(h.showMessageBox).toHaveBeenCalledWith(expect.objectContaining({ message: "本地语音服务意外停止" }));
    expect(h.showItemInFolder).toHaveBeenCalledWith("C:/test/livekit-runtime.log");
    expect(start).toHaveBeenCalledOnce();
  });
});
