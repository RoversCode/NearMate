import { beforeEach, describe, expect, it, vi } from "vitest";
import { registerShellIpcHandlers as registerProductionShellIpcHandlers } from "./shellIpcHandlers";
import { createLiveKitSessionClient } from "../services/liveKitSessionClient";
import { createChatWindowDragController } from "../windows/chatWindowDrag";
import {
  CHAT_WINDOW_EXPANDED_SIZE,
  createChatWindowMotion
} from "../windows/chatWindowMotion";
import { createChatWindowResizeController } from "../windows/chatWindowResize";

const electronMock = vi.hoisted(() => {
  const listeners = new Map<string, Array<(...args: unknown[]) => void>>();
  const handlers = new Map<string, (...args: unknown[]) => unknown>();

  function createIpcEvent(channel: string, candidate: unknown) {
    const existingEvent = candidate && typeof candidate === "object"
      ? candidate as Record<string, unknown>
      : {};

    if (existingEvent.sender) {
      return existingEvent;
    }

    const windowKind = channel.startsWith("panel:")
      ? "panel"
      : channel.startsWith("chat:") ||
          channel.startsWith("turn-submission:") ||
          channel.startsWith("environment:") ||
          channel.startsWith("materials:")
        ? "chat"
        : "avatar";
    return {
      ...existingEvent,
      sender: { windowKind, once: vi.fn(), removeListener: vi.fn(), isDestroyed: () => false }
    };
  }

  return {
    ipcMain: {
      on: vi.fn((channel: string, listener: (...args: unknown[]) => void) => {
        const channelListeners = listeners.get(channel) ?? [];
        channelListeners.push(listener);
        listeners.set(channel, channelListeners);
      }),
      handle: vi.fn((channel: string, handler: (...args: unknown[]) => unknown) => {
        handlers.set(channel, handler);
      }),
      removeAllListeners: vi.fn((channel: string) => {
        listeners.delete(channel);
      }),
      removeHandler: vi.fn((channel: string) => {
        handlers.delete(channel);
      }),
      invoke(channel: string, ...args: unknown[]) {
        const [candidateEvent, ...payload] = args;
        return handlers.get(channel)?.(
          createIpcEvent(channel, candidateEvent),
          ...payload
        );
      },
      emit(channel: string, ...args: unknown[]) {
        const [candidateEvent, ...payload] = args;
        for (const listener of listeners.get(channel) ?? []) {
          listener(createIpcEvent(channel, candidateEvent), ...payload);
        }
      },
      reset() {
        listeners.clear();
        handlers.clear();
        this.on.mockClear();
        this.handle.mockClear();
        this.removeAllListeners.mockClear();
        this.removeHandler.mockClear();
      }
    },
    shell: {
      openExternal: vi.fn(),
      showItemInFolder: vi.fn()
    }
  };
});

vi.mock("electron", () => ({
  app: {
    getFileIcon: vi.fn(),
    getPath: vi.fn(() => "G:\\UserData")
  },
  clipboard: {
    readText: vi.fn(() => ""),
    readImage: vi.fn(() => ({
      isEmpty: () => true,
      toPNG: () => Buffer.from("")
    }))
  },
  ipcMain: electronMock.ipcMain,
  nativeImage: {
    createThumbnailFromPath: vi.fn()
  },
  screen: {
    getPrimaryDisplay: vi.fn(() => ({
      workArea: {
        x: 0,
        y: 0,
        width: 1280,
        height: 720
      }
    }))
  },
  shell: electronMock.shell
}));

vi.mock("../services/liveKitSessionClient", () => ({
  createLiveKitSessionClient: vi.fn()
}));

function createRuntime(input?: { summonMode?: "passive_visible" | "summoned_topmost" | "hidden" }) {
  const chatWindowMotion = createChatWindowMotion();
  const workArea = { x: 0, y: 0, width: 1280, height: 720 };
  let chatBounds = chatWindowMotion.resolveBounds("collapsed", workArea);
  const rememberChatBounds = (bounds: typeof chatBounds) => {
    chatWindowMotion.rememberBounds(bounds);
  };

  return {
    avatarWindow: { webContents: { send: vi.fn() } },
    panelWindow: { webContents: { send: vi.fn() } },
    chatWindow: {
      webContents: { send: vi.fn() },
      hide: vi.fn(),
      isMinimized: vi.fn(() => false),
      restore: vi.fn(),
      show: vi.fn(),
      focus: vi.fn(),
      getBounds: vi.fn(() => ({ ...chatBounds })),
      setBounds: vi.fn(nextBounds => {
        chatBounds = { ...nextBounds };
      })
    },
    chatWindowMotion,
    chatWindowDrag: createChatWindowDragController(rememberChatBounds),
    chatWindowResize: createChatWindowResizeController(rememberChatBounds, () => workArea),
    controller: {
      hoverEnter: vi.fn(),
      hoverLeave: vi.fn(),
      getAvatarSummonMode: vi.fn(() => input?.summonMode ?? "passive_visible"),
      toggleAvatarSummonedVisibility: vi.fn(),
      openPanel: vi.fn(),
      startDrag: vi.fn(),
      endDrag: vi.fn(),
      startMaterialDrop: vi.fn(),
      endMaterialDrop: vi.fn(),
      closePanelFromUserRequest: vi.fn(),
      closePanelFromExternalBlur: vi.fn(),
      refreshPanelPlacement: vi.fn()
    }
  };
}

function createAvatarInteraction() {
  return {
    rememberPetBounds: vi.fn(),
    startDrag: vi.fn(),
    moveDrag: vi.fn(),
    endDrag: vi.fn(),
    startResize: vi.fn(),
    moveResize: vi.fn(),
    endResize: vi.fn(),
    requestVisibleArea: vi.fn(),
    applyPresentationProfile: vi.fn(),
    rememberPendingPresentationProfile: vi.fn(),
    applyPendingPresentationProfile: vi.fn(),
    resetCurrentModelLayout: vi.fn(),
    scheduleFirstShow: vi.fn(),
    getPetBounds: vi.fn(),
    reset: vi.fn()
  };
}

function createTurnSubmission() {
  const snapshot = { revision: 0, items: [] };
  return {
    submitTurn: vi.fn(async () => snapshot),
    editTurn: vi.fn(async () => snapshot),
    deleteTurn: vi.fn(async () => snapshot),
    resumeTurn: vi.fn(async () => snapshot),
    retryTurn: vi.fn(async () => snapshot)
  };
}

function createManagedMaterialAssetStore() {
  return {
    rootDir: "G:\\UserData\\clipboard-attachments",
    prepareRuntime: vi.fn(async () => undefined),
    registerGeneratedAsset: vi.fn(() => "managed-asset-1"),
    release: vi.fn(async () => true),
    releaseAll: vi.fn(async () => undefined),
    releaseAllSync: vi.fn()
  };
}

type ShellIpcTestInput = Omit<
  Parameters<typeof registerProductionShellIpcHandlers>[0],
  "windowIdentities" | "managedMaterialAssetStore" | "materialCommands" | "materialFeedback" | "environmentSnapshot"
> & {
  managedMaterialAssetStore?: Parameters<typeof registerProductionShellIpcHandlers>[0]["managedMaterialAssetStore"];
  materialCommands?: Parameters<typeof registerProductionShellIpcHandlers>[0]["materialCommands"];
  materialFeedback?: Parameters<typeof registerProductionShellIpcHandlers>[0]["materialFeedback"];
  environmentSnapshot?: Parameters<typeof registerProductionShellIpcHandlers>[0]["environmentSnapshot"];
};

function registerShellIpcHandlers(input: ShellIpcTestInput): void {
  registerProductionShellIpcHandlers({
    ...input,
    windowIdentities: {
      bind: vi.fn(),
      resolve: sender => {
        if (!sender || typeof sender !== "object") {
          return null;
        }

        const windowKind = (sender as unknown as { windowKind?: unknown }).windowKind;
        return windowKind === "avatar" || windowKind === "panel" || windowKind === "chat"
          ? windowKind
          : null;
      }
    },
    environmentSnapshot: input.environmentSnapshot ?? {
      prepareTextSnapshot: vi.fn()
    },
    materialCommands: input.materialCommands ?? {
      add: vi.fn(async () => null),
      remove: vi.fn(),
      clear: vi.fn(),
      forgetRecent: vi.fn()
    },
    materialFeedback: input.materialFeedback ?? {
      notifyMaterialAdmission: vi.fn(),
      notifyMaterialProgress: vi.fn(),
      notifyMaterialWarning: vi.fn(),
      notifyMaterialRemoved: vi.fn(),
      notifyMaterialPreflightFailures: vi.fn(),
      currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
      clearFailureReport: vi.fn()
    },
    managedMaterialAssetStore: input.managedMaterialAssetStore ?? createManagedMaterialAssetStore()
  });
}

function createLiveKitDesktopSession() {
  return {
    url: "wss://livekit.example.test",
    roomName: "desktop-room",
    desktopUi: {
      identity: "desktop-ui-identity",
      token: "desktop-ui-token"
    },
    desktopAec: {
      identity: "desktop-aec-identity",
      token: "desktop-aec-token"
    },
    aecSidecarDebugAudio: {
      enabled: false,
      outputDir: "C:\\NearMate-fixtures\\backend\\data\\debug-audio\\aec-sidecar",
      maxRuns: 7
    },
    audioDeviceSelection: {
      mode: "system_default" as const,
      microphoneId: null,
      microphoneName: null,
      speakerLoopbackId: null,
      speakerLoopbackName: null
    }
  };
}

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((innerResolve, innerReject) => {
    resolve = innerResolve;
    reject = innerReject;
  });

  return {
    promise,
    resolve,
    reject
  };
}

describe("registerShellIpcHandlers", () => {
  beforeEach(() => {
    electronMock.ipcMain.reset();
    electronMock.shell.openExternal.mockReset();
    electronMock.shell.openExternal.mockResolvedValue(undefined);
    vi.mocked(createLiveKitSessionClient).mockReset();
  });

  it("拒绝跨窗口调用，并保留每个通道获准窗口的正常行为", async () => {
    const runtime = createRuntime();
    const submitTurn = vi.fn(async () => ({ revision: 0, items: [] }));
    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: {
        ...createTurnSubmission(),
        submitTurn
      }
    });

    electronMock.ipcMain.emit(
      "desktop:open-console",
      { sender: { windowKind: "chat" } },
      { section: "settings" }
    );
    expect(electronMock.shell.openExternal).toHaveBeenCalledOnce();

    electronMock.ipcMain.emit(
      "desktop:open-console",
      { sender: { windowKind: "panel" } },
      { section: "settings" }
    );
    expect(electronMock.shell.openExternal).toHaveBeenCalledTimes(2);

    expect(() => electronMock.ipcMain.invoke(
      "turn-submission:reserve",
      { sender: { windowKind: "avatar" } },
      {
        submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
        messageText: "越权消息"
      }
    )).toThrow("unauthorized IPC sender for channel: turn-submission:reserve");

    await electronMock.ipcMain.invoke(
      "turn-submission:reserve",
      { sender: { windowKind: "chat" } },
      {
        submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
        messageText: "正常消息"
      }
    );
    expect(submitTurn).toHaveBeenCalledWith({
      submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
      messageText: "正常消息"
    });
  });

  it("materials:get-thumbnail 通过主进程生成受控缩略图 data URL", async () => {
    const { nativeImage } = await import("electron");
    vi.mocked(nativeImage.createThumbnailFromPath).mockResolvedValueOnce({
      isEmpty: () => false,
      toDataURL: () => "data:image/png;base64,thumbnail"
    } as never);

    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    const result = await electronMock.ipcMain.invoke("materials:get-thumbnail", undefined, {
      path: "G:\\docs\\report.md"
    });

    expect(nativeImage.createThumbnailFromPath).toHaveBeenCalledWith("G:\\docs\\report.md", {
      width: 96,
      height: 96
    });
    expect(result).toEqual({
      thumbnailDataUrl: "data:image/png;base64,thumbnail"
    });
  });

  it("materials:get-thumbnail 内容缩略图为空时返回系统文件图标", async () => {
    const { app, nativeImage } = await import("electron");
    vi.mocked(nativeImage.createThumbnailFromPath).mockResolvedValueOnce({
      isEmpty: () => true,
      toDataURL: () => "data:image/png;base64,empty"
    } as never);
    vi.mocked(app.getFileIcon).mockResolvedValueOnce({
      isEmpty: () => false,
      toDataURL: () => "data:image/png;base64,file-icon"
    } as never);

    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    await expect(
      electronMock.ipcMain.invoke("materials:get-thumbnail", undefined, {
        path: ""
      })
    ).resolves.toEqual({ thumbnailDataUrl: null });
    await expect(
      electronMock.ipcMain.invoke("materials:get-thumbnail", undefined, {
        path: "G:\\docs\\note.txt"
      })
    ).resolves.toEqual({ thumbnailDataUrl: "data:image/png;base64,file-icon" });
    expect(app.getFileIcon).toHaveBeenCalledWith("G:\\docs\\note.txt", {
      size: "normal"
    });
  });

  it("materials:get-thumbnail 内容缩略图和系统图标都失败时返回 null", async () => {
    const { app, nativeImage } = await import("electron");
    vi.mocked(nativeImage.createThumbnailFromPath).mockResolvedValueOnce({
      isEmpty: () => true,
      toDataURL: () => "data:image/png;base64,empty"
    } as never);
    vi.mocked(app.getFileIcon).mockRejectedValueOnce(new Error("icon failed"));

    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    await expect(
      electronMock.ipcMain.invoke("materials:get-thumbnail", undefined, {
        path: "G:\\docs\\unknown.bin"
      })
    ).resolves.toEqual({ thumbnailDataUrl: null });
  });

  it("avatar:report-pet-bounds 会接受有效人物边界并允许 null 清除缓存", () => {
    const runtime = createRuntime();
    const rememberPetBounds = vi.fn();

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: {
        rememberPetBounds,
        startDrag: vi.fn(),
        moveDrag: vi.fn(),
        endDrag: vi.fn(),
        startResize: vi.fn(),
        moveResize: vi.fn(),
        endResize: vi.fn(),
        requestVisibleArea: vi.fn(),
        applyPresentationProfile: vi.fn(),
        rememberPendingPresentationProfile: vi.fn(),
        applyPendingPresentationProfile: vi.fn(),
        resetCurrentModelLayout: vi.fn(),
        scheduleFirstShow: vi.fn(),
        getPetBounds: vi.fn(),
        reset: vi.fn()
      },
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("avatar:report-pet-bounds", undefined, {
      x: 142,
      y: 36,
      width: 163,
      height: 479
    });
    electronMock.ipcMain.emit("avatar:report-pet-bounds", undefined, null);
    electronMock.ipcMain.emit("avatar:report-pet-bounds", undefined, {
      x: Number.NaN,
      y: 36,
      width: 163,
      height: 479
    });

    // null 是模型卸载或无可见身体时的明确清除信号，非法对象才应被丢弃。
    expect(rememberPetBounds).toHaveBeenCalledTimes(2);
    expect(rememberPetBounds).toHaveBeenNthCalledWith(1, {
      x: 142,
      y: 36,
      width: 163,
      height: 479
    });
    expect(rememberPetBounds).toHaveBeenNthCalledWith(2, null);
    expect(rememberPetBounds).not.toHaveBeenCalledWith({
      x: Number.NaN,
      y: 36,
      width: 163,
      height: 479
    });
  });

  it("avatar material drop IPC 会临时切换头像窗口接收材料投递", () => {
    const runtime = createRuntime();

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("avatar:material-drop-enter");
    electronMock.ipcMain.emit("avatar:material-drop-leave");

    expect(runtime.controller.startMaterialDrop).toHaveBeenCalledOnce();
    expect(runtime.controller.endMaterialDrop).toHaveBeenCalledOnce();
  });

  it("avatar:open-panel 会打开人物侧边板", () => {
    const runtime = createRuntime();

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("avatar:open-panel", undefined, { view: "models" });

    expect(runtime.controller.openPanel).toHaveBeenCalledWith("models");
  });

  it("panel:reset-avatar-layout 会重置当前人物布局并刷新侧边板位置", () => {
    const runtime = createRuntime();
    const resetCurrentModelLayout = vi.fn(() => true);

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: {
        rememberPetBounds: vi.fn(),
        startDrag: vi.fn(),
        moveDrag: vi.fn(),
        endDrag: vi.fn(),
        startResize: vi.fn(),
        moveResize: vi.fn(),
        endResize: vi.fn(),
        requestVisibleArea: vi.fn(),
        applyPresentationProfile: vi.fn(),
        rememberPendingPresentationProfile: vi.fn(),
        applyPendingPresentationProfile: vi.fn(),
        resetCurrentModelLayout,
        scheduleFirstShow: vi.fn(),
        getPetBounds: vi.fn(),
        reset: vi.fn()
      },
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("panel:reset-avatar-layout");

    expect(resetCurrentModelLayout).toHaveBeenCalledWith(runtime.avatarWindow);
    expect(runtime.controller.refreshPanelPlacement).toHaveBeenCalledOnce();
    expect(runtime.controller.closePanelFromUserRequest).not.toHaveBeenCalled();
    expect(runtime.controller.closePanelFromExternalBlur).not.toHaveBeenCalled();
  });

  it("panel:close 只代表用户主动关闭侧边板", () => {
    const runtime = createRuntime();

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: {
        rememberPetBounds: vi.fn(),
        startDrag: vi.fn(),
        moveDrag: vi.fn(),
        endDrag: vi.fn(),
        startResize: vi.fn(),
        moveResize: vi.fn(),
        endResize: vi.fn(),
        requestVisibleArea: vi.fn(),
        applyPresentationProfile: vi.fn(),
        rememberPendingPresentationProfile: vi.fn(),
        applyPendingPresentationProfile: vi.fn(),
        resetCurrentModelLayout: vi.fn(),
        scheduleFirstShow: vi.fn(),
        getPetBounds: vi.fn(),
        reset: vi.fn()
      },
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("panel:close");

    expect(runtime.controller.closePanelFromUserRequest).toHaveBeenCalledOnce();
    expect(runtime.controller.closePanelFromExternalBlur).not.toHaveBeenCalled();
  });

  it("打开控制台不会隐式关闭侧边板", async () => {
    const runtime = createRuntime();
    electronMock.shell.openExternal.mockResolvedValueOnce(undefined);

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: {
        rememberPetBounds: vi.fn(),
        startDrag: vi.fn(),
        moveDrag: vi.fn(),
        endDrag: vi.fn(),
        startResize: vi.fn(),
        moveResize: vi.fn(),
        endResize: vi.fn(),
        requestVisibleArea: vi.fn(),
        applyPresentationProfile: vi.fn(),
        rememberPendingPresentationProfile: vi.fn(),
        applyPendingPresentationProfile: vi.fn(),
        resetCurrentModelLayout: vi.fn(),
        scheduleFirstShow: vi.fn(),
        getPetBounds: vi.fn(),
        reset: vi.fn()
      },
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("desktop:open-console", undefined, {
      section: "settings"
    });
    await Promise.resolve();

    expect(electronMock.shell.openExternal).toHaveBeenCalledOnce();
    expect(runtime.controller.closePanelFromUserRequest).not.toHaveBeenCalled();
    expect(runtime.controller.closePanelFromExternalBlur).not.toHaveBeenCalled();
  });

  it("chat:open-external-url 只允许主进程重新解析后的 HTTP(S) 地址", async () => {
    electronMock.shell.openExternal.mockResolvedValue(undefined);
    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission: createTurnSubmission()
    });

    await expect(
      electronMock.ipcMain.invoke(
        "chat:open-external-url",
        undefined,
        "https://example.com/report?id=1"
      )
    ).resolves.toEqual({ accepted: true });
    expect(electronMock.shell.openExternal).toHaveBeenCalledWith(
      "https://example.com/report?id=1"
    );

    await expect(
      electronMock.ipcMain.invoke("chat:open-external-url", undefined, "file:///C:/secret.txt")
    ).rejects.toThrow("external URL must use http or https");
    expect(electronMock.shell.openExternal).toHaveBeenCalledTimes(1);
  });

  it("chat:show-item-in-folder 只接受绝对本地路径", async () => {
    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission: createTurnSubmission()
    });

    await expect(electronMock.ipcMain.invoke(
      "chat:show-item-in-folder",
      undefined,
      "G:\\NearMate Workspace\\report.md"
    )).resolves.toEqual({ accepted: true });
    expect(electronMock.shell.showItemInFolder).toHaveBeenCalledWith(
      "G:\\NearMate Workspace\\report.md"
    );

    await expect(electronMock.ipcMain.invoke(
      "chat:show-item-in-folder",
      undefined,
      "..\\report.md"
    )).rejects.toThrow("absolute local path");
    expect(electronMock.shell.showItemInFolder).toHaveBeenCalledTimes(1);
  });

  it("chat:set-mode 会按模式重新定位聊天浮层且不影响桌宠和侧边板", async () => {
    const runtime = createRuntime();

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("chat:set-mode", undefined, { mode: "expanded" });

    expect(runtime.chatWindow.setBounds).toHaveBeenCalledWith({
      x: 24,
      y: 44,
      ...CHAT_WINDOW_EXPANDED_SIZE
    });
    expect(runtime.chatWindow.show).toHaveBeenCalledOnce();
    expect(runtime.avatarWindow.webContents.send).toHaveBeenCalledWith(
      "avatar:chat-mode-changed",
      { mode: "expanded" }
    );
    expect(runtime.controller.openPanel).not.toHaveBeenCalled();
    expect(runtime.controller.closePanelFromUserRequest).not.toHaveBeenCalled();
  });

  it("桌宠待答气泡只接受标准请求编号，并展开聊天窗定位对应卡片", async () => {
    const runtime = createRuntime();
    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission: createTurnSubmission()
    });

    expect(electronMock.ipcMain.invoke("avatar:get-chat-mode")).toBe("collapsed");
    electronMock.ipcMain.emit(
      "avatar:open-background-agent-interaction",
      undefined,
      { requestId: "123e4567-e89b-42d3-a456-426614174000" }
    );

    expect(runtime.chatWindow.setBounds).toHaveBeenLastCalledWith({
      x: 24,
      y: 44,
      ...CHAT_WINDOW_EXPANDED_SIZE
    });
    expect(runtime.chatWindow.show).toHaveBeenCalledOnce();
    expect(runtime.chatWindow.focus).toHaveBeenCalledOnce();
    expect(runtime.avatarWindow.webContents.send).toHaveBeenCalledWith(
      "avatar:chat-mode-changed",
      { mode: "expanded" }
    );
    expect(runtime.chatWindow.webContents.send).toHaveBeenCalledWith(
      "chat:focus-background-interaction",
      { requestId: "123e4567-e89b-42d3-a456-426614174000" }
    );
    expect(electronMock.ipcMain.invoke("avatar:get-chat-mode")).toBe("expanded");

    electronMock.ipcMain.emit(
      "avatar:open-background-agent-interaction",
      undefined,
      { requestId: "not-a-uuid" }
    );
    expect(runtime.chatWindow.focus).toHaveBeenCalledOnce();
  });

  it("入口组隐藏后，renderer 回流的 collapsed 模式不应重新显示聊天按钮", async () => {
    const runtime = createRuntime({ summonMode: "hidden" });

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("chat:set-mode", undefined, { mode: "collapsed" });

    expect(runtime.chatWindow.setBounds).not.toHaveBeenCalled();
    expect(runtime.chatWindow.hide).toHaveBeenCalledOnce();
    expect(runtime.chatWindow.show).not.toHaveBeenCalled();
  });

  it("chat drag IPC 只按屏幕坐标差量移动聊天窗并记住新位置", async () => {
    const runtime = createRuntime();

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("chat:set-mode", undefined, { mode: "expanded" });
    runtime.chatWindow.setBounds.mockClear();
    electronMock.ipcMain.emit("chat:drag-start", undefined, { screenX: 300, screenY: 400 });
    electronMock.ipcMain.emit("chat:drag-move", undefined, { screenX: 340, screenY: 430 });
    electronMock.ipcMain.emit("chat:drag-end");
    electronMock.ipcMain.emit("chat:set-mode", undefined, { mode: "collapsed" });

    expect(runtime.chatWindow.setBounds).toHaveBeenNthCalledWith(1, {
      x: 64,
      y: 74,
      ...CHAT_WINDOW_EXPANDED_SIZE
    });
    expect(runtime.chatWindow.setBounds).toHaveBeenCalledTimes(1);
    expect(runtime.chatWindow.hide).toHaveBeenCalledOnce();
    expect(runtime.controller.openPanel).not.toHaveBeenCalled();
    expect(runtime.avatarWindow.webContents.send).toHaveBeenCalledTimes(2);
  });

  it("chat resize IPC 只在主进程调整展开窗口边界", async () => {
    const runtime = createRuntime();

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission: createTurnSubmission()
    });

    electronMock.ipcMain.emit("chat:set-mode", undefined, { mode: "expanded" });
    runtime.chatWindow.setBounds.mockClear();
    electronMock.ipcMain.emit("chat:resize-start", undefined, {
      edge: "se",
      screenX: 744,
      screenY: 684
    });
    electronMock.ipcMain.emit("chat:resize-move", undefined, { screenX: 824, screenY: 704 });
    electronMock.ipcMain.emit("chat:resize-end");

    expect(runtime.chatWindow.setBounds).toHaveBeenCalledWith({
      x: 24,
      y: 44,
      width: 980,
      height: 660
    });
  });

  it("materials:capture-clipboard-image-material 会把剪贴板图片转换成路径材料", async () => {
    const { clipboard } = await import("electron");
    const managedMaterialAssetStore = createManagedMaterialAssetStore();
    vi.mocked(clipboard.readImage).mockReturnValueOnce({
      isEmpty: () => false,
      toPNG: () => Buffer.from("png-bytes")
    } as never);

    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission: createTurnSubmission(),
      managedMaterialAssetStore
    });

    const result = await electronMock.ipcMain.invoke("materials:capture-clipboard-image-material");

    expect(result).toMatchObject({
      kind: "clipboard_image",
      source: "clipboard",
      display_name: expect.stringContaining("剪贴板图片"),
      managed_asset_id: "managed-asset-1"
    });
    expect(String((result as { path: string }).path)).toContain("clipboard-attachments");
    expect(managedMaterialAssetStore.registerGeneratedAsset).toHaveBeenCalledOnce();
  });

  it("materials:add-staged 会追加本轮材料且不提交 turn", async () => {
    const turnSubmission = createTurnSubmission();
    const add = vi.fn(async () => null);
    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: vi.fn()
      },
      turnSubmission,
      materialCommands: { add, remove: vi.fn(), clear: vi.fn(), forgetRecent: vi.fn() }
    });

    await expect(electronMock.ipcMain.invoke("materials:add-staged", undefined, {
      items: [
        {
          kind: "selected_text",
          source: "selection_capture",
          display_name: "选中文本.txt",
          path: "G:\\materials\\selected-text.txt",
        }
      ]
    })).resolves.toEqual({ accepted: true });

    expect(add).toHaveBeenCalledWith([
      {
        kind: "selected_text",
        source: "selection_capture",
        display_name: "选中文本.txt",
        path: "G:\\materials\\selected-text.txt",
      }
    ]);
    expect(turnSubmission.submitTurn).not.toHaveBeenCalled();
  });

  it("turn 成功提交后清除运行期材料失败报告", async () => {
    const turnSubmission = createTurnSubmission();
    const clearFailureReport = vi.fn();
    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission,
      materialFeedback: {
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialProgress: vi.fn(),
        notifyMaterialWarning: vi.fn(),
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 1, report: null })),
        clearFailureReport
      }
    });

    await electronMock.ipcMain.invoke("turn-submission:reserve", undefined, {
      submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
      messageText: "整理材料"
    });

    expect(turnSubmission.submitTurn).toHaveBeenCalledOnce();
    expect(clearFailureReport).toHaveBeenCalledOnce();
  });

  it("turn 提交失败时保留材料失败报告", async () => {
    const turnSubmission = createTurnSubmission();
    turnSubmission.submitTurn.mockRejectedValue(new Error("network"));
    const clearFailureReport = vi.fn();
    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission,
      materialFeedback: {
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialProgress: vi.fn(),
        notifyMaterialWarning: vi.fn(),
        notifyMaterialRemoved: vi.fn(),
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 1, report: null })),
        clearFailureReport
      }
    });

    await expect(electronMock.ipcMain.invoke("turn-submission:reserve", undefined, {
      submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
      messageText: "整理材料"
    })).rejects.toThrow("network");

    expect(clearFailureReport).not.toHaveBeenCalled();
  });

  it("材料删除反馈只在 renderer 应用权威快照后发布一次", async () => {
    const notifyMaterialRemoved = vi.fn();
    const materialContext = {
      revision: 2,
      turn_material_context: { staged_materials: [], active: null },
      recent_materials: []
    };
    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission: createTurnSubmission(),
      materialCommands: {
        add: vi.fn(),
        remove: vi.fn(async () => ({ materialContext, removedDisplayName: "报告.pdf" })),
        clear: vi.fn(),
        forgetRecent: vi.fn()
      },
      materialFeedback: {
        notifyMaterialAdmission: vi.fn(),
        notifyMaterialProgress: vi.fn(),
        notifyMaterialWarning: vi.fn(),
        notifyMaterialRemoved,
        notifyMaterialPreflightFailures: vi.fn(),
        currentFailureReport: vi.fn(() => ({ revision: 0, report: null })),
        clearFailureReport: vi.fn()
      }
    });

    await expect(electronMock.ipcMain.invoke(
      "materials:remove-staged",
      undefined,
      { materialId: "material-1" }
    )).resolves.toEqual({
      accepted: true,
      materialContext,
      removedDisplayName: "报告.pdf"
    });
    expect(notifyMaterialRemoved).not.toHaveBeenCalled();

    await electronMock.ipcMain.invoke("materials:confirm-remove-applied", undefined, {
      materialId: "material-1",
      displayName: "报告.pdf"
    });

    expect(notifyMaterialRemoved).toHaveBeenCalledOnce();
    await expect(electronMock.ipcMain.invoke("materials:confirm-remove-applied", undefined, {
      materialId: "material-1",
      displayName: "报告.pdf"
    })).rejects.toThrow("confirmation is stale");
  });

  it("近期材料忘记命令只接受 JSON body 中的路径", async () => {
    const materialContext = {
      revision: 3,
      turn_material_context: { staged_materials: [], active: null },
      recent_materials: []
    };
    const forgetRecent = vi.fn(async () => materialContext);
    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission: createTurnSubmission(),
      materialCommands: {
        add: vi.fn(),
        remove: vi.fn(),
        clear: vi.fn(),
        forgetRecent
      }
    });

    await expect(electronMock.ipcMain.invoke(
      "materials:forget-recent",
      undefined,
      { path: "G:\\docs\\report.md" }
    )).resolves.toEqual({ accepted: true, materialContext });
    expect(forgetRecent).toHaveBeenCalledWith("G:\\docs\\report.md");
    await expect(electronMock.ipcMain.invoke(
      "materials:forget-recent",
      undefined,
      { path: "G:\\docs\\report.md", extra: true }
    )).rejects.toThrow("payload is invalid");
  });

  it("并发 livekit:create-session 会复用同一个启动任务，避免重复替换 AEC sidecar", async () => {
    const runtime = createRuntime();
    const startSidecar = vi.fn(async () => undefined);
    const deferredSession = createDeferred<ReturnType<typeof createLiveKitDesktopSession>>();
    const createSession = vi.fn(() => deferredSession.promise);
    vi.mocked(createLiveKitSessionClient).mockReturnValue({
      createSession
    });

    registerShellIpcHandlers({
      getRuntime: () => runtime as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: {
        start: startSidecar
      },
      turnSubmission: createTurnSubmission()
    });

    const firstRequest = electronMock.ipcMain.invoke("livekit:create-session");
    const secondRequest = electronMock.ipcMain.invoke("livekit:create-session");
    await Promise.resolve();

    deferredSession.resolve(createLiveKitDesktopSession());
    const [firstPayload, secondPayload] = await Promise.all([firstRequest, secondRequest]);

    expect(firstPayload).toEqual(secondPayload);
    expect(createLiveKitSessionClient).toHaveBeenCalledOnce();
    expect(createSession).toHaveBeenCalledOnce();
    expect(startSidecar).toHaveBeenCalledOnce();
  });

  it("environment:prepare-text 只触发后台预采集", async () => {
    const prepareTextSnapshot = vi.fn();
    registerShellIpcHandlers({
      getRuntime: () => createRuntime() as never,
      avatarInteraction: createAvatarInteraction() as never,
      aecSidecarSupervisor: { start: vi.fn() },
      turnSubmission: createTurnSubmission(),
      environmentSnapshot: { prepareTextSnapshot }
    });

    expect(electronMock.ipcMain.invoke("environment:prepare-text")).toEqual({ accepted: true });

    expect(prepareTextSnapshot).toHaveBeenCalledOnce();
  });
});
