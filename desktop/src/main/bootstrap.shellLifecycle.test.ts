import { describe, expect, it, vi } from "vitest";
import type { DesktopShortcutId } from "../shared/shortcutFeedback";
import {
  FAKE_PRIMARY_DISPLAY,
  FakeBrowserWindowRegistry,
  FakeElectronWindow,
  createAppMock,
  createIpcMainMock,
  flushMicrotasks,
  mockBootstrapFileSystem,
  setupBootstrapHarness
} from "./bootstrap.testHarness";
describe("bootstrap shell lifecycle", () => {
  it("跟进快捷键先通知 Avatar 静音，慢 HTTP 完成后发送相同序号的取消名单", async () => {
    let complete!: (value: { interruptedRequestIds: string[] }) => void;
    const request = new Promise<{ interruptedRequestIds: string[] }>(resolve => { complete = resolve; });
    const toggleFollowup = vi.fn(() => request);
    let shortcut!: { onShortcutPressed(id: DesktopShortcutId): void; actions: { toggleFollowup(): Promise<void> } };
    vi.doMock("./services/followupControlClient", () => ({
      createFollowupControlClient: () => ({ toggleFollowup })
    }));
    vi.doMock("./services/shortcuts/desktopShortcutRegistry", () => ({
      createDesktopShortcutRegistryService: (options: typeof shortcut) => {
        shortcut = options;
        return { start: vi.fn(async () => undefined), stop: vi.fn() };
      }
    }));
    const { avatarWindow } = await setupBootstrapHarness();
    avatarWindow.webContents.send.mockClear();
    shortcut.onShortcutPressed("followup_toggle");
    const action = shortcut.actions.toggleFollowup();
    expect(avatarWindow.webContents.send).toHaveBeenCalledWith("avatar:shortcut-feedback", {
      phase: "pressed", shortcutId: "followup_toggle", sequence: 1
    });
    expect(avatarWindow.webContents.send.mock.invocationCallOrder[0]).toBeLessThan(toggleFollowup.mock.invocationCallOrder[0]);
    complete({ interruptedRequestIds: ["old-request"] });
    await action;
    expect(avatarWindow.webContents.send).toHaveBeenLastCalledWith("avatar:shortcut-feedback", {
      phase: "settled", shortcutId: "followup_toggle", sequence: 1, interruptedRequestIds: ["old-request"]
    });
  });
  it("头像拖拽 IPC 会按屏幕坐标差量移动角色窗，并同步控制器拖拽状态", async () => {
    const appMock = createAppMock();
    const ipcMainMock = createIpcMainMock();
    const tray = { destroy: vi.fn() };
    const avatarWindow = new FakeElectronWindow();
    const panelWindow = new FakeElectronWindow();
    const chatWindow = new FakeElectronWindow();
    const controllerMock = {
      start: vi.fn(),
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      toggleAvatarSummonedVisibility: vi.fn(),
      startDrag: vi.fn(),
      endDrag: vi.fn(),
      openPanel: vi.fn(),
      closePanelFromUserRequest: vi.fn(),
      closePanelFromExternalBlur: vi.fn(),
      refreshPanelPlacement: vi.fn()
    };

    vi.doMock("electron", () => ({
      app: appMock,
      ipcMain: ipcMainMock,
      clipboard: createClipboardMock(),
      globalShortcut: {
        register: vi.fn(() => true),
        unregister: vi.fn()
      },
      screen: {
        getDisplayMatching: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getPrimaryDisplay: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getCursorScreenPoint: vi.fn(() => ({ x: 0, y: 0 }))
      },
      shell: {
        openExternal: vi.fn()
      },
      BrowserWindow: {
        getAllWindows: () => FakeBrowserWindowRegistry.getAllWindows()
      }
    }));

    mockBootstrapFileSystem();

    vi.doMock("./windows/avatarWindow", () => ({
      createAvatarWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(avatarWindow);
        return avatarWindow;
      })
    }));

    vi.doMock("./windows/panelWindow", () => ({
      createPanelWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(panelWindow);
        return panelWindow;
      })
    }));

    vi.doMock("./windows/chatWindow", () => ({
      createChatWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(chatWindow);
        return chatWindow;
      })
    }));

    vi.doMock("./controllers/shellController", () => ({
      createShellController: vi.fn(() => controllerMock)
    }));

  vi.doMock("./platform", () => ({
    createPlatformBridge: vi.fn(() => ({
      attachTray: vi.fn(() => tray)
    }))
  }));

    const { bootstrap } = await import("./bootstrap");

    await bootstrap();

    /**
     * 从 preload 视角模拟一轮完整拖拽：
     * 起点记录窗口原始边界，移动事件只带屏幕坐标，主进程负责计算最终窗口坐标。
     */
    ipcMainMock.emit("avatar:drag-start", undefined, { screenX: 200, screenY: 220 });
    ipcMainMock.emit("avatar:drag-move", undefined, { screenX: 230, screenY: 260 });
    ipcMainMock.emit("avatar:drag-end");

    expect(controllerMock.startDrag).toHaveBeenCalledOnce();
    expect(avatarWindow.setBounds).toHaveBeenCalledWith({
      x: 150,
      y: 180,
      width: 420,
      height: 520
    });
    expect(controllerMock.endDrag).toHaveBeenCalledOnce();
  });

  it("召回桌宠入口时会同步恢复折叠态聊天按钮且不抢焦点", async () => {
    const harness = await setupBootstrapHarness();
    const trayRuntimeInput = harness.trayRuntimeInput as { toggleAvatarVisibility(): void } | null;

    harness.chatWindow.setBounds.mockClear();
    harness.chatWindow.hide.mockClear();
    harness.chatWindow.showInactive.mockClear();
    harness.chatWindow.show.mockClear();
    harness.chatWindow.focus.mockClear();
    harness.chatWindow.moveTop.mockClear();
    harness.chatWindow.webContents.send.mockClear();

    expect(trayRuntimeInput).not.toBeNull();
    trayRuntimeInput?.toggleAvatarVisibility();

    // collapsed 现在是隐藏态，桌面不再保留独立聊天浮球的几何位置。
    expect(harness.chatWindow.setBounds).not.toHaveBeenCalled();
    expect(harness.chatWindow.webContents.send).toHaveBeenCalledWith("chat:mode-requested", {
      mode: "collapsed"
    });
    expect(harness.chatWindow.hide).toHaveBeenCalledOnce();
    expect(harness.chatWindow.showInactive).not.toHaveBeenCalled();
    expect(harness.chatWindow.moveTop).not.toHaveBeenCalled();
    expect(harness.chatWindow.show).not.toHaveBeenCalled();
    expect(harness.chatWindow.focus).not.toHaveBeenCalled();
  });

  it("模型切换 IPC 会在 panel 与 avatar 之间双向转发请求与状态", async () => {
    const appMock = createAppMock();
    const ipcMainMock = createIpcMainMock();
    const tray = { destroy: vi.fn() };
    const avatarWindow = new FakeElectronWindow();
    const panelWindow = new FakeElectronWindow();
    const chatWindow = new FakeElectronWindow();

    vi.doMock("electron", () => ({
      app: appMock,
      ipcMain: ipcMainMock,
      clipboard: createClipboardMock(),
      globalShortcut: {
        register: vi.fn(() => true),
        unregister: vi.fn()
      },
      screen: {
        getDisplayMatching: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getPrimaryDisplay: vi.fn(() => FAKE_PRIMARY_DISPLAY)
      },
      shell: {
        openExternal: vi.fn()
      },
      BrowserWindow: {
        getAllWindows: () => FakeBrowserWindowRegistry.getAllWindows()
      }
    }));

    mockBootstrapFileSystem();

    vi.doMock("./windows/avatarWindow", () => ({
      createAvatarWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(avatarWindow);
        return avatarWindow;
      })
    }));

    vi.doMock("./windows/panelWindow", () => ({
      createPanelWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(panelWindow);
        return panelWindow;
      })
    }));

    vi.doMock("./windows/chatWindow", () => ({
      createChatWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(chatWindow);
        return chatWindow;
      })
    }));

    vi.doMock("./controllers/shellController", () => ({
      createShellController: vi.fn(() => ({
        start: vi.fn(),
        hoverEnter: vi.fn(),
        hoverLeave: vi.fn(),
        toggleAvatarSummonedVisibility: vi.fn(),
        startDrag: vi.fn(),
        endDrag: vi.fn(),
        openPanel: vi.fn(),
        closePanelFromUserRequest: vi.fn(),
        closePanelFromExternalBlur: vi.fn(),
        refreshPanelPlacement: vi.fn()
      }))
    }));

  vi.doMock("./platform", () => ({
    createPlatformBridge: vi.fn(() => ({
      attachTray: vi.fn(() => tray)
    }))
  }));

    const { bootstrap } = await import("./bootstrap");

    await bootstrap();

    ipcMainMock.emit("panel:request-model-switch", undefined, { modelId: "Natori" });
    expect(avatarWindow.webContents.send).toHaveBeenCalledWith(
      "avatar:model-switch-requested",
      { modelId: "Natori" }
    );

    ipcMainMock.emit("avatar:model-switch-state-changed", undefined, {
      modelId: "Natori",
      status: "switching"
    });
    expect(panelWindow.webContents.send).toHaveBeenCalledWith(
      "panel:model-switch-state-changed",
      {
        modelId: "Natori",
        status: "switching"
      }
    );

    ipcMainMock.emit("avatar:model-switch-state-changed", undefined, {
      modelId: "Natori",
      status: "failed",
      reason: "人物资源加载失败。"
    });
    expect(panelWindow.webContents.send).toHaveBeenCalledWith(
      "panel:model-switch-state-changed",
      {
        modelId: "Natori",
        status: "failed",
        reason: "人物资源加载失败。"
      }
    );

    const requestId = "a51c4163-5fab-49d6-a369-3e428ad14b22";
    ipcMainMock.emit("panel:request-model-switch", undefined, { modelId: "Natori", requestId });
    expect(avatarWindow.webContents.send).toHaveBeenLastCalledWith(
      "avatar:model-switch-requested", { modelId: "Natori", requestId }
    );
    ipcMainMock.emit("avatar:model-switch-state-changed", undefined, { modelId: "Natori", status: "succeeded", requestId });
    expect(panelWindow.webContents.send).toHaveBeenLastCalledWith(
      "panel:model-switch-state-changed", { modelId: "Natori", status: "succeeded", requestId }
    );
    avatarWindow.webContents.send.mockClear();
    panelWindow.webContents.send.mockClear();
    ipcMainMock.emit("panel:request-model-switch", undefined, { modelId: "Natori", requestId: "invalid" });
    ipcMainMock.emit("avatar:model-switch-state-changed", undefined, { modelId: "Natori", status: "switching", requestId: "invalid" });
    expect(avatarWindow.webContents.send).not.toHaveBeenCalled();
    expect(panelWindow.webContents.send).not.toHaveBeenCalled();
  });

  it("renderer diagnostics IPC 会被转发到主进程终端", async () => {
    const appMock = createAppMock();
    const ipcMainMock = createIpcMainMock();
    const tray = { destroy: vi.fn() };
    const avatarWindow = new FakeElectronWindow();
    const panelWindow = new FakeElectronWindow();
    const chatWindow = new FakeElectronWindow();
    const consoleInfoSpy = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const consoleWarnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    vi.doMock("electron", () => ({
      app: appMock,
      ipcMain: ipcMainMock,
      clipboard: createClipboardMock(),
      globalShortcut: {
        register: vi.fn(() => true),
        unregister: vi.fn()
      },
      screen: {
        getDisplayMatching: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getPrimaryDisplay: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getCursorScreenPoint: vi.fn(() => ({ x: 0, y: 0 }))
      },
      shell: {
        openExternal: vi.fn()
      },
      BrowserWindow: {
        getAllWindows: () => FakeBrowserWindowRegistry.getAllWindows()
      }
    }));

    mockBootstrapFileSystem();

    vi.doMock("./windows/avatarWindow", () => ({
      createAvatarWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(avatarWindow);
        return avatarWindow;
      })
    }));

    vi.doMock("./windows/panelWindow", () => ({
      createPanelWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(panelWindow);
        return panelWindow;
      })
    }));

    vi.doMock("./windows/chatWindow", () => ({
      createChatWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(chatWindow);
        return chatWindow;
      })
    }));

    vi.doMock("./controllers/shellController", () => ({
      createShellController: vi.fn(() => ({
        start: vi.fn(),
        hoverEnter: vi.fn(),
        hoverLeave: vi.fn(),
        toggleAvatarSummonedVisibility: vi.fn(),
        startDrag: vi.fn(),
        endDrag: vi.fn(),
        openPanel: vi.fn(),
        closePanelFromUserRequest: vi.fn(),
        closePanelFromExternalBlur: vi.fn(),
        refreshPanelPlacement: vi.fn()
      }))
    }));

  vi.doMock("./platform", () => ({
    createPlatformBridge: vi.fn(() => ({
      attachTray: vi.fn(() => tray)
    }))
  }));

    const { bootstrap } = await import("./bootstrap");

    await bootstrap();

    /**
     * 显式诊断 channel 需要按 sender 识别具体窗口来源，
     * 因此测试里直接把对应 fake webContents 塞进事件对象，模拟真实 renderer 上报。
     */
    ipcMainMock.emit(
      "renderer:diagnostic",
      { sender: avatarWindow.webContents },
      {
        level: "info",
        message: "live2d.model_loaded",
        details: { source: "avatar" }
      }
    );
    ipcMainMock.emit(
      "renderer:diagnostic",
      { sender: panelWindow.webContents },
      {
        level: "warn",
        message: "control.settings_update_unreachable"
      }
    );

    expect(consoleInfoSpy).toHaveBeenCalledWith(
      "[companion-renderer:avatar] diagnostic level=info [companion-live2d] Live2D 模型已加载",
      { source: "avatar" }
    );
    expect(consoleWarnSpy).toHaveBeenCalledWith(
      "[companion-renderer:panel] diagnostic level=warn [companion-control] 设置同步接口不可达，保留真实连接并等待用户重试。"
    );
  });

  it("任一窗口 closed 时，会 teardown 同伴窗口并销毁 tray，避免 orphan shell", async () => {
    const appMock = createAppMock();
    const ipcMainMock = createIpcMainMock();
    const tray = { destroy: vi.fn() };
    const avatarWindow = new FakeElectronWindow();
    const panelWindow = new FakeElectronWindow();
    const chatWindow = new FakeElectronWindow();
    const attachTray = vi.fn(() => tray);

    vi.doMock("electron", () => ({
      app: appMock,
      ipcMain: ipcMainMock,
      clipboard: createClipboardMock(),
      globalShortcut: {
        register: vi.fn(() => true),
        unregister: vi.fn()
      },
      screen: {
        getDisplayMatching: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getPrimaryDisplay: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getCursorScreenPoint: vi.fn(() => ({ x: 0, y: 0 }))
      },
      shell: {
        openExternal: vi.fn()
      },
      BrowserWindow: {
        getAllWindows: () => FakeBrowserWindowRegistry.getAllWindows()
      }
    }));

    mockBootstrapFileSystem();

    vi.doMock("./windows/avatarWindow", () => ({
      createAvatarWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(avatarWindow);
        return avatarWindow;
      })
    }));

    vi.doMock("./windows/panelWindow", () => ({
      createPanelWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(panelWindow);
        return panelWindow;
      })
    }));

    vi.doMock("./windows/chatWindow", () => ({
      createChatWindow: vi.fn(() => {
        FakeBrowserWindowRegistry.register(chatWindow);
        return chatWindow;
      })
    }));

    vi.doMock("./controllers/shellController", () => ({
      createShellController: vi.fn(() => ({
        start: vi.fn(),
        hoverEnter: vi.fn(),
        hoverLeave: vi.fn(),
        toggleAvatarSummonedVisibility: vi.fn(),
        startDrag: vi.fn(),
        endDrag: vi.fn(),
        openPanel: vi.fn(),
        closePanelFromUserRequest: vi.fn(),
        closePanelFromExternalBlur: vi.fn(),
        refreshPanelPlacement: vi.fn()
      }))
    }));

  vi.doMock("./platform", () => ({
    createPlatformBridge: vi.fn(() => ({
      attachTray
    }))
  }));

    const { bootstrap } = await import("./bootstrap");

    await bootstrap();
    avatarWindow.destroy();

    expect(panelWindow.destroy).toHaveBeenCalledOnce();
    expect(chatWindow.destroy).toHaveBeenCalledOnce();
    expect(tray.destroy).toHaveBeenCalledOnce();
    expect(FakeBrowserWindowRegistry.getAllWindows()).toHaveLength(0);
  });

  it("点击聊天窗关闭按钮只隐藏聊天窗，不会 teardown 桌宠与面板", async () => {
    const avatarWindow = new FakeElectronWindow();
    const panelWindow = new FakeElectronWindow();
    const chatWindow = new FakeElectronWindow();
    const { tray } = await setupBootstrapHarness({
      avatarWindow,
      panelWindow,
      chatWindow
    });

    chatWindow.hide.mockClear();
    chatWindow.requestClose();

    /**
     * 关闭聊天窗只应收起聊天窗本身。
     * 其余壳层必须保持存活，用户之后仍能从侧边板重新唤起聊天窗。
     */
    expect(chatWindow.hide).toHaveBeenCalledOnce();
    expect(chatWindow.isDestroyed()).toBe(false);
    expect(chatWindow.destroy).not.toHaveBeenCalled();
    expect(avatarWindow.destroy).not.toHaveBeenCalled();
    expect(panelWindow.destroy).not.toHaveBeenCalled();
    expect(tray.destroy).not.toHaveBeenCalled();
    expect(FakeBrowserWindowRegistry.getAllWindows()).toEqual([
      avatarWindow,
      panelWindow,
      chatWindow
    ]);
  });

  it("应用退出流程开始后，聊天窗关闭按钮不再拦截，允许窗口随应用真正销毁", async () => {
    const chatWindow = new FakeElectronWindow();
    const { appMock } = await setupBootstrapHarness({
      chatWindow
    });

    appMock.emit("before-quit", { preventDefault: vi.fn() });
    chatWindow.requestClose();

    expect(chatWindow.isDestroyed()).toBe(true);
    expect(chatWindow.destroy).toHaveBeenCalledOnce();
  });

  it("全部窗口销毁后触发 activate，会重建新的 shell runtime 并重新挂接 tray", async () => {
    const appMock = createAppMock();
    const ipcMainMock = createIpcMainMock();
    const firstTray = { destroy: vi.fn() };
    const secondTray = { destroy: vi.fn() };
    let trayIndex = 0;
    const attachTray = vi.fn(() => {
      const nextTray = [firstTray, secondTray][trayIndex] ?? null;
      trayIndex += 1;
      return nextTray;
    });
    const avatarWindows = [new FakeElectronWindow(), new FakeElectronWindow()];
    const panelWindows = [new FakeElectronWindow(), new FakeElectronWindow()];
    const chatWindows = [new FakeElectronWindow(), new FakeElectronWindow()];

    vi.doMock("electron", () => ({
      app: appMock,
      ipcMain: ipcMainMock,
      clipboard: createClipboardMock(),
      globalShortcut: {
        register: vi.fn(() => true),
        unregister: vi.fn()
      },
      screen: {
        getDisplayMatching: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getPrimaryDisplay: vi.fn(() => FAKE_PRIMARY_DISPLAY),
        getCursorScreenPoint: vi.fn(() => ({ x: 0, y: 0 }))
      },
      shell: {
        openExternal: vi.fn()
      },
      BrowserWindow: {
        getAllWindows: () => FakeBrowserWindowRegistry.getAllWindows()
      }
    }));

    mockBootstrapFileSystem();

    vi.doMock("./windows/avatarWindow", () => {
      let avatarIndex = 0;

      return {
        createAvatarWindow: vi.fn(() => {
          const window = avatarWindows[avatarIndex]!;
          avatarIndex += 1;
          FakeBrowserWindowRegistry.register(window);
          return window;
        })
      };
    });

    vi.doMock("./windows/panelWindow", () => {
      let panelIndex = 0;

      return {
        createPanelWindow: vi.fn(() => {
          const window = panelWindows[panelIndex]!;
          panelIndex += 1;
          FakeBrowserWindowRegistry.register(window);
          return window;
        })
      };
    });

    vi.doMock("./windows/chatWindow", () => {
      let chatIndex = 0;

      return {
        createChatWindow: vi.fn(() => {
          const window = chatWindows[chatIndex]!;
          chatIndex += 1;
          FakeBrowserWindowRegistry.register(window);
          return window;
        })
      };
    });

    vi.doMock("./controllers/shellController", () => ({
      createShellController: vi.fn(() => ({
        start: vi.fn(),
        hoverEnter: vi.fn(),
        hoverLeave: vi.fn(),
        toggleAvatarSummonedVisibility: vi.fn(),
        startDrag: vi.fn(),
        endDrag: vi.fn(),
        openPanel: vi.fn(),
        closePanelFromUserRequest: vi.fn(),
        closePanelFromExternalBlur: vi.fn(),
        refreshPanelPlacement: vi.fn()
      }))
    }));

  vi.doMock("./platform", () => ({
    createPlatformBridge: vi.fn(() => ({
      attachTray
    }))
  }));

    const { bootstrap } = await import("./bootstrap");

    await bootstrap();
    avatarWindows[0].destroy();
    appMock.emit("activate");
    // 重建涉及页面加载，按托盘重新挂接的真实结果等待，不依赖 Promise 链的层数。
    await vi.waitFor(() => expect(attachTray).toHaveBeenCalledTimes(2));

    expect(firstTray.destroy).toHaveBeenCalledOnce();
    expect(FakeBrowserWindowRegistry.getAllWindows()).toEqual([
      avatarWindows[1],
      panelWindows[1],
      chatWindows[1]
    ]);
    expect(secondTray.destroy).not.toHaveBeenCalled();
  });

  it("resize start/move/end 会驱动角色窗缩放，并在结束后忽略后续 move", async () => {
    const { ipcMainMock, avatarWindow } = await setupBootstrapHarness();

    ipcMainMock.emit("avatar:resize-start", undefined, {
      direction: "se",
      point: { screenX: 200, screenY: 220 }
    });
    ipcMainMock.emit("avatar:resize-move", undefined, {
      screenX: 260,
      screenY: 280
    });
    ipcMainMock.emit("avatar:resize-end");
    ipcMainMock.emit("avatar:resize-move", undefined, {
      screenX: 320,
      screenY: 340
    });

    expect(avatarWindow.setBounds).toHaveBeenCalledTimes(1);
    expect(avatarWindow.setBounds).toHaveBeenCalledWith({
      x: 120,
      y: 140,
      width: 480,
      height: 594
    });
  });

  it("侧边板短暂失焦后重新聚焦时，不会触发外部点击关闭", async () => {
    vi.useFakeTimers();

    try {
      const { panelWindow, controllerMock } = await setupBootstrapHarness({
        cursorPoint: { x: 20, y: 20 }
      });

      panelWindow.requestBlur();
      panelWindow.requestFocus();
      await vi.advanceTimersByTimeAsync(80);

      /**
       * 点击面板内部控件时 Electron 可能产生 blur -> focus 的短暂抖动。
       * 业务意图是吸收这类窗口内焦点波动，只把持续失焦解释成外部点击关闭。
       */
      expect(controllerMock!.closePanelFromExternalBlur).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("侧边板持续失焦且鼠标在外部时，会按外部点击关闭", async () => {
    vi.useFakeTimers();

    try {
      const { panelWindow, controllerMock } = await setupBootstrapHarness({
        cursorPoint: { x: 20, y: 20 }
      });

      panelWindow.requestBlur();
      await vi.advanceTimersByTimeAsync(80);

      expect(controllerMock!.closePanelFromExternalBlur).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("侧边板持续失焦但鼠标仍在面板内部时，不会关闭面板", async () => {
    vi.useFakeTimers();

    try {
      const { panelWindow, controllerMock } = await setupBootstrapHarness({
        cursorPoint: { x: 220, y: 260 }
      });

      panelWindow.requestBlur();
      await vi.advanceTimersByTimeAsync(80);

      /**
       * 面板内部按钮可能触发短暂窗口层级变化。
       * 只要鼠标仍在面板边界内，就必须把它视为内部动作而不是外部点击。
       */
      expect(controllerMock!.closePanelFromExternalBlur).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("app before-quit 会停止 AEC sidecar，避免 Python 子进程残留", async () => {
    const stopAecSidecar = vi.fn();
    const createAecSidecarSupervisor = vi.fn(() => ({
      start: vi.fn(async () => undefined),
      stop: stopAecSidecar
    }));

    /**
     * before-quit 是 Electron 退出链路的最后清理入口之一。
     * 测试直接触发 app mock 事件，确保它不依赖窗口是否仍然存在。
     */
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({
      createAecSidecarSupervisor
    }));

    const { appMock } = await setupBootstrapHarness();

    appMock.emit("before-quit", { preventDefault: vi.fn() });
    await flushMicrotasks();

    expect(stopAecSidecar).toHaveBeenCalled();
  });

  it("选区收集快捷键捕获到材料后会走统一材料采集服务", async () => {
    const calls: string[] = [];
    const selectedItems = [
      {
        kind: "selected_text" as const,
        source: "selection_capture",
        display_name: "选中文本.txt",
        path: "G:\\selected\\text.txt",
      }
    ];
    const captureSelectedContent = vi.fn(async () => {
      calls.push("capture");
      return selectedItems;
    });
    const addStagedMaterials = vi.fn(async (_items: unknown[]) => {
      calls.push("add");
    });
    const notifyMaterialProgress = vi.fn((text: string) => {
      calls.push(text);
    });
    const shortcutActionsRef: {
      current: { captureSelection(): Promise<void> } | null;
    } = { current: null };

    vi.doMock("./services/selectedContentCaptureService", () => ({
      createSelectedContentCaptureService: vi.fn(() => ({
        captureSelectedContent
      }))
    }));
    vi.doMock("./services/turnSubmissionService", () => ({
      createTurnSubmissionService: vi.fn(() => ({
        syncEnvironment: vi.fn(async () => undefined),
        submitTurn: vi.fn(async () => undefined)
      }))
    }));
    vi.doMock("./services/materialCommandService", () => ({
      createMaterialCommandService: vi.fn((input: { feedback: { notifyMaterialProgress(text: string): void } }) => ({
        add: async (items: unknown[]) => {
          input.feedback.notifyMaterialProgress(`正在检查 ${items.length} 项…`);
          await addStagedMaterials(items);
        },
        remove: vi.fn(),
        clear: vi.fn()
      }))
    }));
    vi.doMock("./services/materialDeliveryFeedbackService", () => ({
      createMaterialDeliveryFeedbackService: vi.fn(() => ({
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialProgress,
        notifyMaterialWarning: vi.fn(),
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
        clearFailureReport: vi.fn()
      }))
    }));
    vi.doMock("./services/shortcuts/desktopShortcutRegistry", () => ({
      createDesktopShortcutRegistryService: vi.fn((input: {
        actions: { captureSelection(): Promise<void> };
      }) => {
        shortcutActionsRef.current = input.actions;
        return {
          start: vi.fn(async () => undefined),
          stop: vi.fn()
        };
      })
    }));

    await setupBootstrapHarness();
    await shortcutActionsRef.current?.captureSelection();

    expect(captureSelectedContent).toHaveBeenCalledOnce();
    expect(addStagedMaterials).toHaveBeenCalledWith(selectedItems);
    expect(calls).toEqual(["正在读取所选内容…", "capture", "正在检查 1 项…", "add"]);
  });

  it("选区收集快捷键没有捕获到材料时不会显示成功反馈", async () => {
    const calls: string[] = [];
    const captureSelectedContent = vi.fn(async () => {
      calls.push("capture");
      return [];
    });
    const addStagedMaterials = vi.fn(async (_items: unknown[]) => undefined);
    const notifyMaterialProgress = vi.fn((text: string) => {
      calls.push(text);
    });
    const notifyMaterialWarning = vi.fn((text: string) => {
      calls.push(text);
    });
    const shortcutActionsRef: {
      current: { captureSelection(): Promise<void> } | null;
    } = { current: null };

    vi.doMock("./services/selectedContentCaptureService", () => ({
      createSelectedContentCaptureService: vi.fn(() => ({
        captureSelectedContent
      }))
    }));
    vi.doMock("./services/turnSubmissionService", () => ({
      createTurnSubmissionService: vi.fn(() => ({
        syncEnvironment: vi.fn(async () => undefined),
        submitTurn: vi.fn(async () => undefined)
      }))
    }));
    vi.doMock("./services/materialCommandService", () => ({
      createMaterialCommandService: vi.fn(() => ({
        add: addStagedMaterials,
        remove: vi.fn(),
        clear: vi.fn()
      }))
    }));
    vi.doMock("./services/materialDeliveryFeedbackService", () => ({
      createMaterialDeliveryFeedbackService: vi.fn(() => ({
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialProgress,
        notifyMaterialWarning,
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
        clearFailureReport: vi.fn()
      }))
    }));
    vi.doMock("./services/shortcuts/desktopShortcutRegistry", () => ({
      createDesktopShortcutRegistryService: vi.fn((input: {
        actions: { captureSelection(): Promise<void> };
      }) => {
        shortcutActionsRef.current = input.actions;
        return {
          start: vi.fn(async () => undefined),
          stop: vi.fn()
        };
      })
    }));

    await setupBootstrapHarness();
    await shortcutActionsRef.current?.captureSelection();

    expect(captureSelectedContent).toHaveBeenCalledOnce();
    expect(addStagedMaterials).not.toHaveBeenCalled();
    expect(calls).toEqual(["正在读取所选内容…", "capture", "未检测到可添加的选中内容"]);
  });

  it("窗口 closed 触发 shell teardown 时会停止 AEC sidecar", async () => {
    const stopAecSidecar = vi.fn();
    const createAecSidecarSupervisor = vi.fn(() => ({
      start: vi.fn(async () => undefined),
      stop: stopAecSidecar
    }));

    /**
     * shell teardown 是窗口生命周期清理入口。
     * 这里销毁 avatarWindow，验证 closed 事件进入 teardown 后会统一停止 sidecar。
     */
    vi.doMock("./services/aec/aecSidecarSupervisor", () => ({
      createAecSidecarSupervisor
    }));

    const { avatarWindow } = await setupBootstrapHarness();

    avatarWindow.destroy();

    expect(stopAecSidecar).toHaveBeenCalled();
  });

  it("重复 bootstrap 不会累积 activate 监听器", async () => {
    const {
      appMock,
      bootstrap,
      avatarWindow
    } = await setupBootstrapHarness();

    await bootstrap();

    /**
     * activate 只代表一次 Dock 激活。
     * 如果 bootstrap 重复注册了 activate 监听器，这里会对同一个角色窗重复 show。
     */
    appMock.emit("activate");

    expect(avatarWindow.show).toHaveBeenCalledTimes(1);
  });

});

function createClipboardMock() {
  return {
    readText: vi.fn(() => ""),
    writeText: vi.fn(),
    readImage: vi.fn(() => ({
      isEmpty: () => true,
      toPNG: () => Buffer.from("")
    })),
    writeImage: vi.fn(),
    clear: vi.fn()
  };
}
