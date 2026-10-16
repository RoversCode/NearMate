import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeElectronWindow, setupBootstrapHarness } from "./bootstrap.testHarness";

let releaseCompletion: () => void;
let progressWindow: { update: ReturnType<typeof vi.fn>; complete: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn> };

beforeEach(() => {
  (process as { resourcesPath?: string }).resourcesPath = "C:/NearMate/resources";
  const completion = new Promise<void>(resolve => { releaseCompletion = resolve; });
  progressWindow = { update: vi.fn(), complete: vi.fn(() => completion), close: vi.fn() };
  vi.doMock("./windows/startupProgressWindow", () => ({ createStartupProgressWindow: vi.fn(() => progressWindow) }));
  vi.doMock("./services/backendSupervisor", () => ({
    createBackendSupervisor: vi.fn(() => ({ start: vi.fn(async () => undefined), stop: vi.fn(async () => undefined) }))
  }));
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ is_initialized: false }))));
});

afterEach(() => {
  delete (process as { resourcesPath?: string }).resourcesPath;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("启动窗口和桌面入口交接", () => {
  it("100% 交接前和退出期间忽略快捷键与托盘召回，交接后继续可用", async () => {
    let shortcutActions!: { toggleAvatarVisibility(): void };
    let trayInput!: { toggleAvatarVisibility(): void };
    vi.doMock("./services/shortcuts/desktopShortcutRegistry", () => ({
      createDesktopShortcutRegistryService: (options: { actions: typeof shortcutActions }) => {
        shortcutActions = options.actions;
        return { start: vi.fn(async () => undefined), stop: vi.fn() };
      }
    }));
    const harness = await setupBootstrapHarness({
      isPackaged: true, awaitBootstrap: false,
      attachTray: input => { trayInput = input; return { destroy: vi.fn() }; }
    });
    await vi.waitFor(() => expect(progressWindow.complete).toHaveBeenCalledOnce());
    const { createShellController } = await import("./controllers/shellController");
    const controller = vi.mocked(createShellController).mock.results[0].value;
    shortcutActions.toggleAvatarVisibility();
    shortcutActions.toggleAvatarVisibility();
    trayInput.toggleAvatarVisibility();
    expect(controller.toggleAvatarSummonedVisibility).not.toHaveBeenCalled();
    releaseCompletion();
    await harness.bootstrapPromise;
    shortcutActions.toggleAvatarVisibility();
    trayInput.toggleAvatarVisibility();
    expect(controller.toggleAvatarSummonedVisibility).toHaveBeenCalledTimes(2);
    harness.appMock.emit("before-quit", { preventDefault: vi.fn() });
    shortcutActions.toggleAvatarVisibility();
    trayInput.toggleAvatarVisibility();
    expect(controller.toggleAvatarSummonedVisibility).toHaveBeenCalledTimes(2);
  });

  it.each(["avatar", "panel", "chat"])("%s 页面加载挂起时十秒内收口三窗口，不等待加载返回", async windowKind => {
    vi.useFakeTimers();
    const stalledWindow = new FakeElectronWindow(undefined, { loadFilePromise: new Promise(() => {}) });
    const harness = await setupBootstrapHarness({
      isPackaged: true, awaitBootstrap: false,
      ...(windowKind === "avatar" ? { avatarWindow: stalledWindow }
        : windowKind === "panel" ? { panelWindow: stalledWindow } : { chatWindow: stalledWindow })
    });
    let startupFailure: Error | null = null;
    void harness.bootstrapPromise.catch(error => { startupFailure = error; });
    await vi.waitFor(() => expect(stalledWindow.loadFile).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(10_000);
    expect(startupFailure).toEqual(expect.objectContaining({ message: expect.stringContaining("超时") }));
    expect(harness.avatarWindow.destroy).toHaveBeenCalledOnce();
    expect(harness.panelWindow.destroy).toHaveBeenCalledOnce();
    expect(harness.chatWindow.destroy).toHaveBeenCalledOnce();
    expect(progressWindow.complete).not.toHaveBeenCalled();
    expect(harness.openExternal).not.toHaveBeenCalled();
  });

  it("首次 Console 等待隐藏页面加载和 100% 显示，再关闭启动窗并放行桌宠", async () => {
    let finishLoad!: () => void;
    const avatarWindow = new FakeElectronWindow(undefined, { loadFilePromise: new Promise(resolve => { finishLoad = resolve; }) });
    const harness = await setupBootstrapHarness({ isPackaged: true, avatarWindow, controllerStartsByShowingAvatar: true, awaitBootstrap: false });
    await vi.waitFor(() => expect(avatarWindow.loadFile).toHaveBeenCalledOnce());
    expect(harness.openExternal).not.toHaveBeenCalled();
    finishLoad();
    await vi.waitFor(() => expect(progressWindow.complete).toHaveBeenCalledOnce());
    harness.appMock.emit("second-instance");
    await new Promise(resolve => setTimeout(resolve, 200));
    expect(avatarWindow.show).not.toHaveBeenCalled();
    expect(avatarWindow.showInactive).not.toHaveBeenCalled();
    expect(progressWindow.close).not.toHaveBeenCalled();
    releaseCompletion();
    await harness.bootstrapPromise;
    expect(avatarWindow.show).toHaveBeenCalledOnce();
    expect(progressWindow.close.mock.invocationCallOrder[0]).toBeLessThan(avatarWindow.show.mock.invocationCallOrder[0]);
    expect(avatarWindow.show.mock.invocationCallOrder[0]).toBeLessThan(harness.openExternal.mock.invocationCallOrder[0]);
    expect(harness.openExternal).toHaveBeenCalledOnce();
  });

  it("完成绘制期间退出，迟到完成不能再打开桌宠或 Console", async () => {
    const harness = await setupBootstrapHarness({ isPackaged: true, controllerStartsByShowingAvatar: true, awaitBootstrap: false });
    await vi.waitFor(() => expect(progressWindow.complete).toHaveBeenCalledOnce());
    harness.appMock.emit("before-quit", { preventDefault: vi.fn() });
    releaseCompletion();
    await harness.bootstrapPromise;
    expect(harness.avatarWindow.show).not.toHaveBeenCalled();
    expect(harness.openExternal).not.toHaveBeenCalled();
  });

  it("页面加载完成仍等待首帧，第二实例与档案更新不能提前显示", async () => {
    const avatarWindow = new FakeElectronWindow(undefined, { autoReadyToShow: false });
    const harness = await setupBootstrapHarness({ isPackaged: true, avatarWindow, controllerStartsByShowingAvatar: true, awaitBootstrap: false });
    await vi.waitFor(() => expect(harness.chatWindow.loadFile).toHaveBeenCalledOnce());
    expect(progressWindow.complete).not.toHaveBeenCalled();
    harness.appMock.emit("second-instance");
    harness.ipcMainMock.emit("avatar:apply-presentation-profile", { sender: avatarWindow.webContents }, { modelId: "Hiyori" });
    expect(avatarWindow.show).not.toHaveBeenCalled();
    expect(avatarWindow.showInactive).not.toHaveBeenCalled();
    avatarWindow.emit("ready-to-show");
    await vi.waitFor(() => expect(progressWindow.complete).toHaveBeenCalledOnce());
    releaseCompletion();
    await harness.bootstrapPromise;
    expect(avatarWindow.show).toHaveBeenCalledOnce();
  });

  it("后台在成功反馈期间退出，迟到反馈不会首显或打开 Console", async () => {
    let terminate!: (termination: object) => Promise<void>;
    vi.doMock("./services/backendSupervisor", () => ({
      createBackendSupervisor: vi.fn((options: { onUnexpectedTermination: typeof terminate }) => {
        terminate = options.onUnexpectedTermination;
        return { start: vi.fn(async () => undefined), stop: vi.fn(async () => undefined) };
      })
    }));
    vi.spyOn(console, "error").mockImplementation(() => {});
    const harness = await setupBootstrapHarness({ isPackaged: true, controllerStartsByShowingAvatar: true, awaitBootstrap: false });
    await vi.waitFor(() => expect(progressWindow.complete).toHaveBeenCalledOnce());
    await terminate({ kind: "exit", pid: 123, code: 1, signal: null, logPath: "backend-startup.log" });
    releaseCompletion();
    await expect(harness.bootstrapPromise).rejects.toThrow("启动交接期间停止");
    expect(harness.avatarWindow.show).not.toHaveBeenCalled();
    expect(harness.openExternal).not.toHaveBeenCalled();
    expect(progressWindow.close).toHaveBeenCalledOnce();
  });
});
