import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

const originalProcessArgv = [...process.argv];
const chatBootstrapArgument = `--companion-window-bootstrap=${encodeURIComponent(JSON.stringify({
  kind: "chat",
  runtime: {
    controlGateway: {
      httpBaseUrl: "http://127.0.0.1:8765",
      websocketUrl: "ws://127.0.0.1:8765/ws/control"
    }
  }
}))}`;

type ExposedChatBridge = {
  runtime: {
    controlGateway: {
      httpBaseUrl: string;
      websocketUrl: string;
    };
  };
  submitTurn(input: { submissionId: string; messageText: string }): Promise<unknown>;
  editTurnSubmission(input: { submissionId: string; messageText: string }): Promise<unknown>;
  deleteTurnSubmission(submissionId: string): Promise<unknown>;
  resumeTurnSubmission(submissionId: string): Promise<unknown>;
  retryTurnSubmission(submissionId: string): Promise<unknown>;
  openExternalUrl(url: string): Promise<void>;
  showItemInFolder(path: string): Promise<void>;
  addStagedMaterials(items: unknown[]): Promise<unknown>;
  removeStagedMaterial(materialId: string): Promise<unknown>;
  forgetRecentMaterial(path: string): Promise<unknown>;
  confirmMaterialRemoved(materialId: string, displayName: string): Promise<unknown>;
  getMaterialFailureReport(): Promise<unknown>;
  getMaterialThumbnail(path: string): Promise<string | null>;
  getPathForFile(file: File): string;
  setChatMode(mode: "collapsed" | "expanded"): void;
  getChatMode(): Promise<"collapsed" | "expanded" | null>;
  startChatDrag(point: { screenX: number; screenY: number }): void;
  moveChatDrag(point: { screenX: number; screenY: number }): void;
  endChatDrag(): void;
  startChatResize(input: { edge: "se"; screenX: number; screenY: number }): void;
  moveChatResize(point: { screenX: number; screenY: number }): void;
  endChatResize(): void;
  captureClipboardImageMaterial(): Promise<unknown>;
  createComposerTextMaterial(text: string): Promise<unknown>;
  onChatModeRequested(listener: (mode: "collapsed") => void): () => void;
  onFocusBackgroundAgentInteraction(listener: (requestId: string) => void): () => void;
  reportDiagnostic(level: "info" | "warn" | "error", message: string, details?: unknown): void;
};

describe("chat preload bridge", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
    process.argv = [...originalProcessArgv, chatBootstrapArgument];
  });

  afterAll(() => {
    process.argv = originalProcessArgv;
  });

  it("向 renderer 暴露聊天窗口控制能力", async () => {
    const send = vi.fn();
    let exposedBridge: ExposedChatBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedChatBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send,
        invoke: vi.fn()
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./chat");

    exposedBridge!.reportDiagnostic("info", "chat.ready", { source: "chat" });

    expect(exposedBridge!.runtime.controlGateway.httpBaseUrl).toBe("http://127.0.0.1:8765");
    expect(exposedBridge!.runtime.controlGateway.websocketUrl).toBe("ws://127.0.0.1:8765/ws/control");
    expect(exposedBridge).not.toHaveProperty("assets");
    expect(send).toHaveBeenNthCalledWith(1, "renderer:diagnostic", {
      level: "info",
      message: "chat.ready",
      details: { source: "chat" }
    });
  });

  it("turn 输入提交和本轮材料追加分别通过受控 IPC invoke 进入主进程", async () => {
    const invoke = vi.fn(async () => ({ accepted: true }));
    let exposedBridge: ExposedChatBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedChatBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn(),
        invoke
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./chat");

    await exposedBridge!.submitTurn({
      submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
      messageText: "你好"
    });
    await exposedBridge!.editTurnSubmission({
      submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
      messageText: "你好呀"
    });
    await exposedBridge!.deleteTurnSubmission("6d77fa4e-177c-49b6-a4e3-42df9b46211d");
    await exposedBridge!.resumeTurnSubmission("6d77fa4e-177c-49b6-a4e3-42df9b46211d");
    await exposedBridge!.retryTurnSubmission("6d77fa4e-177c-49b6-a4e3-42df9b46211d");
    await exposedBridge!.openExternalUrl("https://example.com/report?id=1");
    await exposedBridge!.showItemInFolder("G:\\NearMate Workspace\\report.md");
    await exposedBridge!.addStagedMaterials([{ path: "G:\\docs\\report.md" }]);
    await exposedBridge!.removeStagedMaterial("material-1");
    await exposedBridge!.forgetRecentMaterial("G:\\docs\\report.md");
    await exposedBridge!.confirmMaterialRemoved("material-1", "report.md");
    await exposedBridge!.getMaterialFailureReport();
    await exposedBridge!.getMaterialThumbnail("G:\\docs\\report.md");

    expect(invoke).toHaveBeenNthCalledWith(1, "turn-submission:reserve", {
      submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
      messageText: "你好"
    });
    expect(invoke).toHaveBeenNthCalledWith(2, "turn-submission:edit", {
      submissionId: "6d77fa4e-177c-49b6-a4e3-42df9b46211d",
      messageText: "你好呀"
    });
    expect(invoke).toHaveBeenNthCalledWith(
      3,
      "turn-submission:delete",
      "6d77fa4e-177c-49b6-a4e3-42df9b46211d"
    );
    expect(invoke).toHaveBeenNthCalledWith(
      4,
      "turn-submission:resume",
      "6d77fa4e-177c-49b6-a4e3-42df9b46211d"
    );
    expect(invoke).toHaveBeenNthCalledWith(
      5,
      "turn-submission:retry",
      "6d77fa4e-177c-49b6-a4e3-42df9b46211d"
    );
    expect(invoke).toHaveBeenNthCalledWith(6, "chat:open-external-url", "https://example.com/report?id=1");
    expect(invoke).toHaveBeenNthCalledWith(
      7,
      "chat:show-item-in-folder",
      "G:\\NearMate Workspace\\report.md"
    );
    expect(invoke).toHaveBeenNthCalledWith(8, "materials:add-staged", {
      items: [{ path: "G:\\docs\\report.md" }]
    });
    expect(invoke).toHaveBeenNthCalledWith(9, "materials:remove-staged", { materialId: "material-1" });
    expect(invoke).toHaveBeenNthCalledWith(10, "materials:forget-recent", {
      path: "G:\\docs\\report.md"
    });
    expect(invoke).toHaveBeenNthCalledWith(11, "materials:confirm-remove-applied", {
      materialId: "material-1",
      displayName: "report.md"
    });
    expect(invoke).toHaveBeenNthCalledWith(12, "materials:get-failure-report");
    expect(invoke).toHaveBeenNthCalledWith(13, "materials:get-thumbnail", {
      path: "G:\\docs\\report.md"
    });
  });

  it("聊天浮层模式和剪贴板材料只通过受控 IPC 进入主进程", async () => {
    const send = vi.fn();
    const invoke = vi.fn(async () => ({
      kind: "clipboard_image",
      source: "clipboard",
      display_name: "剪贴板图片",
      path: "G:\\Temp\\clipboard.png",
    }));
    let exposedBridge: ExposedChatBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedChatBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send,
        invoke
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./chat");

    exposedBridge!.setChatMode("expanded");
    exposedBridge!.startChatDrag({ screenX: 100, screenY: 120 });
    exposedBridge!.moveChatDrag({ screenX: 130, screenY: 160 });
    exposedBridge!.endChatDrag();
    exposedBridge!.startChatResize({ edge: "se", screenX: 600, screenY: 500 });
    exposedBridge!.moveChatResize({ screenX: 680, screenY: 560 });
    exposedBridge!.endChatResize();
    await expect(exposedBridge!.captureClipboardImageMaterial()).resolves.toEqual({
      kind: "clipboard_image",
      source: "clipboard",
      display_name: "剪贴板图片",
      path: "G:\\Temp\\clipboard.png",
    });

    expect(send).toHaveBeenCalledWith("chat:set-mode", { mode: "expanded" });
    expect(send).toHaveBeenCalledWith("chat:drag-start", { screenX: 100, screenY: 120 });
    expect(send).toHaveBeenCalledWith("chat:drag-move", { screenX: 130, screenY: 160 });
    expect(send).toHaveBeenCalledWith("chat:drag-end");
    expect(send).toHaveBeenCalledWith("chat:resize-start", { edge: "se", screenX: 600, screenY: 500 });
    expect(send).toHaveBeenCalledWith("chat:resize-move", { screenX: 680, screenY: 560 });
    expect(send).toHaveBeenCalledWith("chat:resize-end");
    expect(invoke).toHaveBeenCalledWith("materials:capture-clipboard-image-material");
  });

  it("主进程请求聊天窗回到按钮态时，会通过只读订阅通知 renderer", async () => {
    const registeredListeners: Array<(_event: unknown, payload: unknown) => void> = [];
    const removeListener = vi.fn();
    let exposedBridge: ExposedChatBridge | null = null;
    const modeListener = vi.fn();

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedChatBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn(),
        invoke: vi.fn(),
        on: vi.fn((_channel: string, listener: (_event: unknown, payload: unknown) => void) => {
          registeredListeners.push(listener);
        }),
        removeListener
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./chat");

    const unsubscribe = exposedBridge!.onChatModeRequested(modeListener);
    const listener = registeredListeners[0];
    expect(listener).toBeDefined();
    listener(undefined, { mode: "collapsed" });
    listener(undefined, { mode: "expanded" });
    unsubscribe();

    expect(modeListener).toHaveBeenCalledTimes(2);
    expect(modeListener).toHaveBeenNthCalledWith(1, "collapsed");
    expect(modeListener).toHaveBeenNthCalledWith(2, "expanded");
    expect(removeListener).toHaveBeenCalledWith("chat:mode-requested", listener);
  });

  it("待答卡片定位订阅只接受精确字段和标准 UUID", async () => {
    const registeredListeners = new Map<string, (_event: unknown, payload: unknown) => void>();
    const removeListener = vi.fn();
    let exposedBridge: ExposedChatBridge | null = null;
    const focusListener = vi.fn();

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedChatBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn(),
        invoke: vi.fn(),
        on: vi.fn((channel: string, listener: (_event: unknown, payload: unknown) => void) => {
          registeredListeners.set(channel, listener);
        }),
        removeListener
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./chat");

    const unsubscribe = exposedBridge!.onFocusBackgroundAgentInteraction(focusListener);
    const channelListener = registeredListeners.get("chat:focus-background-interaction")!;
    channelListener(undefined, { requestId: "123e4567-e89b-42d3-a456-426614174000" });
    channelListener(undefined, { requestId: "not-a-uuid" });
    channelListener(undefined, {
      requestId: "123e4567-e89b-42d3-a456-426614174000",
      extra: true
    });
    unsubscribe();

    expect(focusListener).toHaveBeenCalledOnce();
    expect(focusListener).toHaveBeenCalledWith("123e4567-e89b-42d3-a456-426614174000");
    expect(removeListener).toHaveBeenCalledWith("chat:focus-background-interaction", channelListener);
  });

  it("文件路径解析只通过 Electron webUtils 暴露给 renderer", async () => {
    const getPathForFile = vi.fn(() => "G:\\docs\\report.md");
    let exposedBridge: ExposedChatBridge | null = null;
    const file = new File(["hello"], "report.md");

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedChatBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn(),
        invoke: vi.fn()
      },
      webUtils: {
        getPathForFile
      }
    }));

    await import("./chat");

    expect(exposedBridge!.getPathForFile(file)).toBe("G:\\docs\\report.md");
    expect(getPathForFile).toHaveBeenCalledWith(file);
  });

  it("不向 renderer 透传由 Chat 组件自己拥有的头像资源", async () => {
    let exposedBridge: ExposedChatBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedChatBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn()
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./chat");

    expect(exposedBridge).not.toHaveProperty("assets");
  });
});
