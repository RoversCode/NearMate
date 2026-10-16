import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * createLifecycleAppMock 提供 before-quit 链路测试所需的最小 app 行为。
 * appLifecycle 模块用顶层标记防止重复注册，因此每个用例都要重置模块表后重新挂接。
 */
function createLifecycleAppMock() {
  const listeners = new Map<string, Array<(...args: unknown[]) => void>>();
  return {
    on: vi.fn((eventName: string, listener: (...args: unknown[]) => void) => {
      const eventListeners = listeners.get(eventName) ?? [];
      eventListeners.push(listener);
      listeners.set(eventName, eventListeners);
    }),
    emit(eventName: string, ...args: unknown[]) {
      for (const listener of listeners.get(eventName) ?? []) {
        listener(...args);
      }
    },
    quit: vi.fn()
  };
}

beforeEach(() => {
  vi.resetModules();
  vi.spyOn(console, "error").mockImplementation(() => undefined);
});

async function flushMicrotasks(rounds = 4): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await Promise.resolve();
  }
}

describe("appLifecycle before-quit 兜底", () => {
  it("退出清理同步抛错时不拦截 Electron 退出流程", async () => {
    const appMock = createLifecycleAppMock();
    vi.doMock("electron", () => ({
      app: appMock,
      BrowserWindow: { getAllWindows: () => [] }
    }));
    const { registerAppLifecycleHandlers } = await import("./appLifecycle");
    registerAppLifecycleHandlers({
      getRuntime: () => null,
      recreateRuntime: async () => undefined,
      teardownBeforeQuit: () => {
        throw new Error("受管材料目录被占用（EPERM）");
      }
    });

    const event = { preventDefault: vi.fn() };
    appMock.emit("before-quit", event);

    // 不 preventDefault、不主动 quit：把退出交还给 Electron 原流程，用户不会被卡在退不出的进程里。
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(appMock.quit).not.toHaveBeenCalled();
  });

  it("异步清理被拒绝时记日志后仍然继续退出", async () => {
    const appMock = createLifecycleAppMock();
    vi.doMock("electron", () => ({
      app: appMock,
      BrowserWindow: { getAllWindows: () => [] }
    }));
    const { registerAppLifecycleHandlers } = await import("./appLifecycle");
    registerAppLifecycleHandlers({
      getRuntime: () => null,
      recreateRuntime: async () => undefined,
      teardownBeforeQuit: () => Promise.reject(new Error("Backend 停止超时"))
    });

    const event = { preventDefault: vi.fn() };
    appMock.emit("before-quit", event);
    await flushMicrotasks();

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(appMock.quit).toHaveBeenCalledOnce();
  });

  it("异步清理正常完成后触发 quit", async () => {
    const appMock = createLifecycleAppMock();
    vi.doMock("electron", () => ({
      app: appMock,
      BrowserWindow: { getAllWindows: () => [] }
    }));
    const { registerAppLifecycleHandlers } = await import("./appLifecycle");
    registerAppLifecycleHandlers({
      getRuntime: () => null,
      recreateRuntime: async () => undefined,
      teardownBeforeQuit: () => Promise.resolve()
    });

    const event = { preventDefault: vi.fn() };
    appMock.emit("before-quit", event);
    await flushMicrotasks();

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(appMock.quit).toHaveBeenCalledOnce();
  });

  it("异步清理期间重复退出只复用同一条清理链路", async () => {
    const appMock = createLifecycleAppMock();
    vi.doMock("electron", () => ({
      app: appMock,
      BrowserWindow: { getAllWindows: () => [] }
    }));
    let finishTeardown!: () => void;
    const teardownPromise = new Promise<void>((resolve) => {
      finishTeardown = resolve;
    });
    const teardownBeforeQuit = vi.fn(() => teardownPromise);
    const { registerAppLifecycleHandlers } = await import("./appLifecycle");
    registerAppLifecycleHandlers({
      getRuntime: () => null,
      recreateRuntime: async () => undefined,
      teardownBeforeQuit
    });

    const firstEvent = { preventDefault: vi.fn() };
    const repeatedEvent = { preventDefault: vi.fn() };
    appMock.emit("before-quit", firstEvent);
    appMock.emit("before-quit", repeatedEvent);

    expect(firstEvent.preventDefault).toHaveBeenCalledOnce();
    expect(repeatedEvent.preventDefault).toHaveBeenCalledOnce();
    expect(teardownBeforeQuit).toHaveBeenCalledOnce();
    expect(appMock.quit).not.toHaveBeenCalled();

    finishTeardown();
    await flushMicrotasks();

    expect(appMock.quit).toHaveBeenCalledOnce();
  });
});
