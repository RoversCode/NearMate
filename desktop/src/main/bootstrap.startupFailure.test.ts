import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BackendUnexpectedTermination } from "./services/backendSupervisor";
import {
  FakeBrowserWindowRegistry,
  setupBootstrapHarness
} from "./bootstrap.testHarness";

/**
 * packaged 场景需要 process.resourcesPath 指向一份假的安装目录；
 * dev 场景不读它，但设置它对 dev 路径无副作用。
 */
beforeEach(() => {
  (process as { resourcesPath?: string }).resourcesPath = "C:/NearMate/resources";
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
});

afterEach(() => {
  delete (process as { resourcesPath?: string }).resourcesPath;
  delete process.env["NEARMATE_RUNTIME_ROOT"];
  delete process.env["NEARMATE_RUNTIME_PROFILE"];
  delete process.env["NEARMATE_RUNTIME_MANIFEST"];
  vi.unstubAllGlobals();
});

/**
 * stubBootstrapConsoleFetch 屏蔽安装态启动后打开 Console 前的状态探测请求。
 */
function stubBootstrapConsoleFetch(): void {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(
    JSON.stringify({ is_initialized: true }),
    { status: 200, headers: { "content-type": "application/json" } }
  )));
}

/** 等待异步退出链路跨过连续的 Promise 检查点。 */
async function flushMicrotasks(rounds = 12): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await Promise.resolve();
  }
}

describe("bootstrap 启动失败收口", () => {
  it("模型冷启动超时说明等待时长并提供日志入口，不误导用户重装", async () => {
    const timeoutCause = new Error("后台启动超时：等待 120 秒后仍未就绪。本地模型可能加载较慢，请重新打开 NearMate。");
    const startupError = Object.assign(new Error("internal error and local path", { cause: timeoutCause }), {
      failureCode: "startup_timeout"
    });
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn(() => ({
        start: vi.fn(async () => { throw startupError; }),
        stop: vi.fn(async () => undefined)
      }))
    }));
    const harness = await setupBootstrapHarness({ isPackaged: true, awaitBootstrap: false });
    await expect(harness.bootstrapPromise).rejects.toBe(startupError);
    expect(harness.showMessageBox).toHaveBeenCalledOnce();
    const options = harness.showMessageBox.mock.calls[0][0] as { message: string; detail: string };
    expect(options.message).toBe("本地模型未能及时完成启动");
    expect(options.detail).toContain("120 秒");
    expect(options.detail).not.toContain("重新安装");
    expect(options.detail).not.toContain("internal error");
    expect(harness.showItemInFolder).toHaveBeenCalledOnce();
    expect(harness.appMock.exit).toHaveBeenCalledWith(1);
  });

  it.each([false, true])("壳层装配期间服务退出阻止成功交接，装配失败=%s 时也只提示一次", async startupFails => {
    let onUnexpectedTermination!: (termination: BackendUnexpectedTermination) => Promise<void>;
    let releaseStartup!: () => void;
    const gate = new Promise<void>(resolve => { releaseStartup = resolve; });
    const start = vi.fn(async () => {
      await onUnexpectedTermination({
        kind: "exit", pid: 4321, code: -1073741819, signal: null,
        logPath: "C:/NearMate/userData/logs/backend-startup.log"
      });
      await gate;
    });
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn((options: { onUnexpectedTermination: typeof onUnexpectedTermination }) => {
        onUnexpectedTermination = options.onUnexpectedTermination;
        return { start, stop: vi.fn(async () => undefined) };
      })
    }));
    stubBootstrapConsoleFetch();
    const harness = await setupBootstrapHarness({
      isPackaged: true, awaitBootstrap: false,
      attachTray: () => {
        if (startupFails) throw new Error("托盘装配失败");
        return { destroy: vi.fn() };
      }
    });
    await vi.waitFor(() => expect(start).toHaveBeenCalledOnce());
    expect(harness.showMessageBox).not.toHaveBeenCalled();
    releaseStartup();
    if (startupFails) {
      await expect(harness.bootstrapPromise).rejects.toThrow("托盘装配失败");
    } else {
      await expect(harness.bootstrapPromise).rejects.toThrow("启动交接期间停止");
    }
    expect(harness.showMessageBox).toHaveBeenCalledOnce();
    const options = harness.showMessageBox.mock.calls[0][0] as { message: string };
    expect(options.message).toBe("NearMate 启动失败");
  });

  it("运行期后台崩溃提供退出码和日志入口，不自动重新启动", async () => {
    let onUnexpectedTermination!: (termination: BackendUnexpectedTermination) => Promise<void>;
    const start = vi.fn(async () => undefined);
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn((options: { onUnexpectedTermination: typeof onUnexpectedTermination }) => {
        onUnexpectedTermination = options.onUnexpectedTermination;
        return { start, stop: vi.fn(async () => undefined) };
      })
    }));
    stubBootstrapConsoleFetch();
    const harness = await setupBootstrapHarness({ isPackaged: true });
    await onUnexpectedTermination({
      kind: "exit", pid: 4321, code: -1073741819, signal: null,
      logPath: "C:/NearMate/userData/logs/backend-startup.log"
    });
    expect(harness.showMessageBox).toHaveBeenCalledOnce();
    const options = harness.showMessageBox.mock.calls[0][0] as { detail: string };
    expect(options.detail).toContain("0xC0000005");
    expect(options.detail).not.toContain("C:/");
    expect(harness.showItemInFolder).toHaveBeenCalledWith("C:/NearMate/userData/logs/backend-startup.log");
    expect(harness.appMock.quit).not.toHaveBeenCalled();
    expect(start).toHaveBeenCalledOnce();
  });

  it("用户选择退出才结束应用，退出清理期间不重复显示后台错误", async () => {
    let onUnexpectedTermination!: (termination: BackendUnexpectedTermination) => Promise<void>;
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn((options: { onUnexpectedTermination: typeof onUnexpectedTermination }) => {
        onUnexpectedTermination = options.onUnexpectedTermination;
        return { start: vi.fn(async () => undefined), stop: vi.fn(async () => undefined) };
      })
    }));
    stubBootstrapConsoleFetch();
    const harness = await setupBootstrapHarness({ isPackaged: true });
    const termination: BackendUnexpectedTermination = {
      kind: "exit", pid: 4321, code: -1073741819, signal: null,
      logPath: "C:/NearMate/userData/logs/backend-startup.log"
    };
    harness.showMessageBox.mockResolvedValueOnce({ response: 1 });
    await onUnexpectedTermination(termination);
    expect(harness.appMock.quit).toHaveBeenCalledOnce();
    expect(harness.showItemInFolder).not.toHaveBeenCalled();
    harness.appMock.emit("before-quit", { preventDefault: vi.fn() });
    await onUnexpectedTermination(termination);
    expect(harness.showMessageBox).toHaveBeenCalledOnce();
    await flushMicrotasks();
  });

  it("packaged Backend 已就绪但尚未初始化时复用唯一 launcher 打开设置页", async () => {
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: vi.fn(async () => undefined)
      }))
    }));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ is_initialized: false }),
      { status: 200, headers: { "content-type": "application/json" } }
    )));

    const harness = await setupBootstrapHarness({ isPackaged: true });

    expect(harness.openExternal).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/console?section=settings"
    );
  });

  it("packaged 随包资源缺失时弹重装指引并退出，且不创建 Backend supervisor", async () => {
    const createBackendSupervisorSpy = vi.fn(() => ({
      start: vi.fn(async () => undefined),
      stop: vi.fn(async () => undefined)
    }));
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: createBackendSupervisorSpy
    }));

    const harness = await setupBootstrapHarness({
      isPackaged: true,
      awaitBootstrap: false,
      isPathMissing: path => path.endsWith("icon.png")
    });

    await expect(harness.bootstrapPromise).rejects.toThrow("NearMate 安装资源损坏或缺失");
    expect(createBackendSupervisorSpy).not.toHaveBeenCalled();
    expect(harness.showMessageBox).toHaveBeenCalledOnce();
    const dialogOptions = harness.showMessageBox.mock.calls[0][0] as { detail?: string };
    expect(dialogOptions.detail).toContain("icon.png");
    expect(dialogOptions.detail).toContain("重新安装 NearMate");
    expect(harness.appMock.exit).toHaveBeenCalledWith(1);
    expect(FakeBrowserWindowRegistry.getAllWindows()).toHaveLength(0);
  });

  it("packaged Backend 启动失败时停止 Backend、弹可操作错误并退出", async () => {
    const startMock = vi.fn(async () => {
      throw new Error("控制端口 8765 已被另一个正在运行的 NearMate Backend 实例占用（健康检查超时）。请关闭所有 NearMate 进程后重试");
    });
    const stopMock = vi.fn(async () => undefined);
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn(() => ({ start: startMock, stop: stopMock }))
    }));

    const harness = await setupBootstrapHarness({
      isPackaged: true,
      awaitBootstrap: false
    });

    await expect(harness.bootstrapPromise).rejects.toThrow("控制端口 8765 已被另一个正在运行的 NearMate Backend 实例占用");
    expect(startMock).toHaveBeenCalledOnce();
    expect(stopMock).toHaveBeenCalledOnce();
    expect(harness.showMessageBox).toHaveBeenCalledOnce();
    expect(harness.appMock.exit).toHaveBeenCalledWith(1);
    expect(FakeBrowserWindowRegistry.getAllWindows()).toHaveLength(0);
  });

  it("窗口页面资源缺失导致壳装配失败时，销毁已建窗口、弹错误并退出", async () => {
    const harness = await setupBootstrapHarness({
      awaitBootstrap: false,
      isPathMissing: path => path.endsWith(".html")
    });

    await expect(harness.bootstrapPromise).rejects.toThrow("Unable to resolve window asset path");
    expect(harness.avatarWindow.isDestroyed()).toBe(true);
    expect(harness.panelWindow.isDestroyed()).toBe(true);
    expect(harness.chatWindow.isDestroyed()).toBe(true);
    expect(FakeBrowserWindowRegistry.getAllWindows()).toHaveLength(0);
    expect(harness.showMessageBox).toHaveBeenCalledOnce();
    expect(harness.appMock.exit).toHaveBeenCalledWith(1);
  });

  it("长期服务启动后窗口装配失败时，启动失败路径也完整停止服务", async () => {
    const controlSettingsStop = vi.fn();
    const shortcutStop = vi.fn();
    const watcherStop = vi.fn();
    vi.doMock("./services/controlSettingsSync", () => ({
      createControlSettingsSyncService: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: controlSettingsStop,
        subscribe: vi.fn(() => vi.fn())
      }))
    }));
    vi.doMock("./services/shortcuts/desktopShortcutRegistry", () => ({
      createDesktopShortcutRegistryService: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: shortcutStop
      }))
    }));
    vi.doMock("./services/runtimeContextEventWatcher", () => ({
      createRuntimeContextEventWatcher: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: watcherStop
      }))
    }));

    const harness = await setupBootstrapHarness({
      awaitBootstrap: false,
      isPathMissing: path => path.endsWith(".html")
    });

    await expect(harness.bootstrapPromise).rejects.toThrow("Unable to resolve window asset path");
    expect(controlSettingsStop).toHaveBeenCalledOnce();
    expect(shortcutStop).toHaveBeenCalledOnce();
    expect(watcherStop).toHaveBeenCalledOnce();
    expect(harness.appMock.exit).toHaveBeenCalledWith(1);
  });

  it("托盘图标无效导致壳装配失败时，销毁已建窗口、弹错误并退出", async () => {
    const harness = await setupBootstrapHarness({
      awaitBootstrap: false,
      attachTray: () => {
        throw new Error("NearMate 托盘图标资源无效：C:/NearMate/resources/runtime/assets/icon.png");
      }
    });

    await expect(harness.bootstrapPromise).rejects.toThrow("托盘图标资源无效");
    expect(harness.avatarWindow.isDestroyed()).toBe(true);
    expect(harness.panelWindow.isDestroyed()).toBe(true);
    expect(harness.chatWindow.isDestroyed()).toBe(true);
    expect(harness.showMessageBox).toHaveBeenCalledOnce();
    expect(harness.appMock.exit).toHaveBeenCalledWith(1);
  });
});

describe("bootstrap before-quit 清理", () => {
  it("先停 AEC 与 Backend 子进程再做文件清理，单步失败不中断后续步骤", async () => {
    const teardownCalls: string[] = [];
    let finishBackendStop!: () => void;
    const backendStopPromise = new Promise<void>(resolve => {
      finishBackendStop = resolve;
    });
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({
      createAecSidecarSupervisor: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: () => {
          teardownCalls.push("aecStop");
        }
      }))
    }));
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: () => {
          teardownCalls.push("backendStop");
          return backendStopPromise;
        }
      }))
    }));
    vi.doMock("./services/environmentImageEncoder", () => ({
      createEnvironmentImageEncoder: vi.fn(() => ({
        encode: vi.fn(),
        stop: () => {
          teardownCalls.push("imageEncoderStop");
          throw new Error("编码器正忙");
        }
      }))
    }));
    vi.doMock("./services/runtimeContextEventWatcher", () => ({
      createRuntimeContextEventWatcher: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: () => {
          teardownCalls.push("watcherStop");
        }
      }))
    }));
    vi.doMock("./services/managedMaterialAssetStore", () => ({
      createManagedMaterialAssetStore: vi.fn(() => ({
        rootDir: "C:/NearMate/test-user-data/runtime-material-assets",
        prepareRuntime: vi.fn(async () => undefined),
        registerGeneratedAsset: vi.fn(() => "managed-asset-1"),
        release: vi.fn(async () => true),
        releaseAll: vi.fn(async () => undefined),
        releaseAllSync: () => {
          teardownCalls.push("releaseAllSync");
          throw new Error("EPERM: 目录被占用");
        }
      }))
    }));
    stubBootstrapConsoleFetch();

    const harness = await setupBootstrapHarness({ isPackaged: true });
    const beforeQuitEvent = { preventDefault: vi.fn() };
    harness.appMock.emit("before-quit", beforeQuitEvent);
    await flushMicrotasks();

    expect(beforeQuitEvent.preventDefault).toHaveBeenCalledOnce();
    expect(teardownCalls).toEqual(["aecStop", "backendStop"]);
    expect(harness.appMock.quit).not.toHaveBeenCalled();

    finishBackendStop();
    await vi.waitFor(() => {
      expect(harness.appMock.quit).toHaveBeenCalledOnce();
    });

    // 子进程必须最先收口，被占用风险最高的文件清理放最后。
    expect(teardownCalls[0]).toBe("aecStop");
    expect(teardownCalls[1]).toBe("backendStop");
    expect(teardownCalls[teardownCalls.length - 1]).toBe("releaseAllSync");
    // 图片编码器抛错不得跳过后续步骤。
    expect(teardownCalls.indexOf("imageEncoderStop")).toBeLessThan(teardownCalls.indexOf("watcherStop"));
    expect(teardownCalls.indexOf("watcherStop")).toBeLessThan(teardownCalls.indexOf("releaseAllSync"));
  });

  it("Backend 停止永久挂起时最多等待五秒并继续退出", async () => {
    const forceStop = vi.fn();
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({
      createAecSidecarSupervisor: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: vi.fn(),
        forceStop: vi.fn()
      }))
    }));
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn(() => ({
        start: vi.fn(async () => undefined),
        stop: vi.fn(() => new Promise<void>(() => undefined)),
        forceStop
      }))
    }));
    stubBootstrapConsoleFetch();

    const harness = await setupBootstrapHarness({ isPackaged: true });
    vi.useFakeTimers();
    try {
      const beforeQuitEvent = { preventDefault: vi.fn() };
      harness.appMock.emit("before-quit", beforeQuitEvent);
      await flushMicrotasks();

      expect(beforeQuitEvent.preventDefault).toHaveBeenCalledOnce();
      expect(harness.appMock.quit).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(5_000);
      await flushMicrotasks();

      expect(forceStop).toHaveBeenCalledOnce();
      expect(harness.appMock.quit).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });
});
