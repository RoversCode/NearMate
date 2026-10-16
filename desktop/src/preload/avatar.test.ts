import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShortcutFeedback } from "../shared/shortcutFeedback";

const originalProcessArgv = [...process.argv];
const avatarBootstrapArgument = `--companion-window-bootstrap=${encodeURIComponent(JSON.stringify({
  kind: "avatar",
  runtime: {
    controlGateway: {
      httpBaseUrl: "http://127.0.0.1:8765",
      websocketUrl: "ws://127.0.0.1:8765/ws/control"
    },
    live2d: {
      defaultModelPath: "/live2d/resources/Haru/Haru.model3.json",
      catalog: []
    }
  }
}))}`;

type ExposedBridge = {
  runtime: {
    controlGateway: {
      httpBaseUrl: string;
    };
  };
  createLiveKitSession(): Promise<unknown>;
  openConsole(target: { section: "settings" }): void;
  openPanel(view: "models"): void;
  openChat(): void;
  startMaterialDrop(): void;
  endMaterialDrop(): void;
  startResize(direction: string, point: { screenX: number; screenY: number }): void;
  moveResize(point: { screenX: number; screenY: number }): void;
  endResize(): void;
  applyPresentationProfile(modelId: string): void;
  reportPetBounds(bounds: { x: number; y: number; width: number; height: number } | null): void;
  addStagedMaterials(items: unknown[]): Promise<unknown>;
  getPathForFile(file: File): string;
  onMaterialDeliveryFeedback(listener: (payload: unknown) => void): () => void;
  getChatMode(): Promise<"collapsed" | "expanded" | null>;
  onChatModeChanged(listener: (mode: "collapsed" | "expanded") => void): () => void;
  onShortcutFeedback(listener: (feedback: ShortcutFeedback) => void): () => void;
  openBackgroundAgentInteraction(requestId: string): void;
};

describe("avatar preload bridge", () => {
  it("快捷键只转发合法反馈并在卸载时移除同一 IPC 监听", async () => {
    let bridge!: ExposedBridge;
    let received!: (_event: unknown, payload: unknown) => void;
    const removeListener = vi.fn();
    vi.doMock("electron", () => ({
      contextBridge: { exposeInMainWorld: (_name: string, exposed: ExposedBridge) => { bridge = exposed; } },
      ipcRenderer: { on: (_channel: string, listener: typeof received) => { received = listener; }, removeListener },
      webUtils: {}
    }));
    await import("./avatar");
    const listener = vi.fn();
    const dispose = bridge.onShortcutFeedback(listener);
    const valid = { phase: "pressed", shortcutId: "followup_toggle", sequence: 1 };
    received(null, valid);
    received(null, { ...valid, sequence: 0 });
    received(null, { ...valid, shortcutId: "unknown" });
    received(null, { ...valid, phase: "settled", interruptedRequestIds: [42] });
    expect(listener).toHaveBeenCalledOnce();
    expect(listener).toHaveBeenCalledWith(valid);
    dispose();
    expect(removeListener).toHaveBeenCalledWith("avatar:shortcut-feedback", received);
  });
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
    process.argv = [...originalProcessArgv, avatarBootstrapArgument];
  });

  afterAll(() => {
    process.argv = originalProcessArgv;
  });

  it("新增 resize 与 presentation profile bridge 会发出稳定的 channel 与 payload 契约", async () => {
    const send = vi.fn();
    let exposedBridge: ExposedBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send,
        on: vi.fn(),
        removeListener: vi.fn()
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./avatar");

    exposedBridge!.openConsole({ section: "settings" });
    exposedBridge!.openChat();
    exposedBridge!.startMaterialDrop();
    exposedBridge!.endMaterialDrop();
    exposedBridge!.startResize("se", { screenX: 300, screenY: 400 });
    exposedBridge!.moveResize({ screenX: 320, screenY: 430 });
    exposedBridge!.endResize();
    exposedBridge!.applyPresentationProfile("Hiyori");
    exposedBridge!.reportPetBounds({ x: 142, y: 36, width: 163, height: 479 });

    expect(send).toHaveBeenNthCalledWith(1, "desktop:open-console", {
      section: "settings"
    });
    expect(send).toHaveBeenNthCalledWith(2, "avatar:open-chat");
    expect(send).toHaveBeenNthCalledWith(3, "avatar:material-drop-enter");
    expect(send).toHaveBeenNthCalledWith(4, "avatar:material-drop-leave");
    expect(send).toHaveBeenNthCalledWith(5, "avatar:resize-start", {
      direction: "se",
      point: {
        screenX: 300,
        screenY: 400
      }
    });
    expect(send).toHaveBeenNthCalledWith(6, "avatar:resize-move", {
      screenX: 320,
      screenY: 430
    });
    expect(send).toHaveBeenNthCalledWith(7, "avatar:resize-end");
    expect(send).toHaveBeenNthCalledWith(8, "avatar:apply-presentation-profile", {
      modelId: "Hiyori"
    });
    expect(send).toHaveBeenNthCalledWith(9, "avatar:report-pet-bounds", {
      x: 142,
      y: 36,
      width: 163,
      height: 479
    });
  });

  it("只向 renderer 暴露 main 进程准备好的本机控制地址", async () => {
    const send = vi.fn();
    let exposedBridge: ExposedBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send,
        on: vi.fn(),
        removeListener: vi.fn()
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./avatar");

    expect(exposedBridge!.runtime.controlGateway.httpBaseUrl).toBe("http://127.0.0.1:8765");
  });

  it("聊天模式和待答卡片定位通过受控 bridge 往返主进程", async () => {
    const send = vi.fn();
    const invoke = vi.fn(async () => "collapsed" as const);
    const listeners = new Map<string, (_event: unknown, payload: unknown) => void>();
    const removeListener = vi.fn();
    let exposedBridge: ExposedBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send,
        invoke,
        on: vi.fn((channel: string, listener: (_event: unknown, payload: unknown) => void) => {
          listeners.set(channel, listener);
        }),
        removeListener
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));
    await import("./avatar");

    await expect(exposedBridge!.getChatMode()).resolves.toBe("collapsed");
    const modeListener = vi.fn();
    const unsubscribe = exposedBridge!.onChatModeChanged(modeListener);
    listeners.get("avatar:chat-mode-changed")?.(undefined, { mode: "expanded" });
    listeners.get("avatar:chat-mode-changed")?.(undefined, { mode: "invalid" });
    exposedBridge!.openBackgroundAgentInteraction("123e4567-e89b-42d3-a456-426614174000");
    unsubscribe();

    expect(invoke).toHaveBeenCalledWith("avatar:get-chat-mode");
    expect(modeListener).toHaveBeenCalledOnce();
    expect(modeListener).toHaveBeenCalledWith("expanded");
    expect(send).toHaveBeenCalledWith("avatar:open-background-agent-interaction", {
      requestId: "123e4567-e89b-42d3-a456-426614174000"
    });
    expect(removeListener).toHaveBeenCalledWith(
      "avatar:chat-mode-changed",
      listeners.get("avatar:chat-mode-changed")
    );
  });

  it("createLiveKitSession 会通过稳定的 IPC invoke channel 请求主进程创建会话", async () => {
    const invoke = vi.fn(async () => ({
      url: "wss://livekit.example.test",
      room_name: "desktop-room"
    }));
    let exposedBridge: ExposedBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn(),
        invoke,
        on: vi.fn(),
        removeListener: vi.fn()
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./avatar");

    await expect(exposedBridge!.createLiveKitSession()).resolves.toEqual({
      url: "wss://livekit.example.test",
      room_name: "desktop-room"
    });
    expect(invoke).toHaveBeenCalledWith("livekit:create-session");
  });

  it("createLiveKitSession 会把主进程 invoke 拒绝原样传播给 renderer 调用方", async () => {
    const sessionError = new Error("livekit session request failed: 503");
    const invoke = vi.fn(async () => {
      throw sessionError;
    });
    let exposedBridge: ExposedBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn(),
        invoke,
        on: vi.fn(),
        removeListener: vi.fn()
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./avatar");

    await expect(exposedBridge!.createLiveKitSession()).rejects.toBe(sessionError);
    expect(invoke).toHaveBeenCalledWith("livekit:create-session");
  });

  it("材料追加和文件路径解析通过受控 preload 能力进入主进程", async () => {
    const invoke = vi.fn(async () => ({ accepted: true }));
    const getPathForFile = vi.fn(() => "G:\\docs\\report.md");
    let exposedBridge: ExposedBridge | null = null;
    const file = new File(["hello"], "report.md");

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn(),
        invoke,
        on: vi.fn(),
        removeListener: vi.fn()
      },
      webUtils: {
        getPathForFile
      }
    }));

    await import("./avatar");

    expect(exposedBridge!.getPathForFile(file)).toBe("G:\\docs\\report.md");
    await exposedBridge!.addStagedMaterials([{ path: "G:\\docs\\report.md" }]);

    expect(getPathForFile).toHaveBeenCalledWith(file);
    expect(invoke).toHaveBeenCalledWith("materials:add-staged", {
      items: [{ path: "G:\\docs\\report.md" }]
    });
  });

  it("材料交付反馈订阅会监听稳定 IPC channel 并返回取消订阅函数", async () => {
    const on = vi.fn();
    const removeListener = vi.fn();
    let exposedBridge: ExposedBridge | null = null;
    const listener = vi.fn();

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send: vi.fn(),
        invoke: vi.fn(),
        on,
        removeListener
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./avatar");

    const unsubscribe = exposedBridge!.onMaterialDeliveryFeedback(listener);
    const wrappedListener = on.mock.calls[0]?.[1] as (_event: unknown, payload: unknown) => void;
    wrappedListener({}, {
      itemCount: 2,
      text: "已加入本轮材料 2 项",
      sound: "upload"
    });
    unsubscribe();

    expect(on).toHaveBeenCalledWith("avatar:material-delivery-feedback", expect.any(Function));
    expect(listener).toHaveBeenCalledWith({
      itemCount: 2,
      text: "已加入本轮材料 2 项",
      sound: "upload"
    });
    expect(removeListener).toHaveBeenCalledWith("avatar:material-delivery-feedback", wrappedListener);
  });
});
