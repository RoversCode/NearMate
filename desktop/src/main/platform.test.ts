import { beforeEach, describe, expect, it, vi } from "vitest";

describe("platform bridge", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
  });

  it("托盘图标使用仓库 assets/icon.png 并缩放到托盘友好尺寸", async () => {
    const resize = vi.fn(() => "resized-icon");
    let observedIconPath = "";
    const createFromPath = vi.fn((iconPath: string) => {
      observedIconPath = iconPath;
      return { resize, isEmpty: () => false };
    });
    const trayInstance = {
      setToolTip: vi.fn(),
      setTitle: vi.fn(),
      setContextMenu: vi.fn(),
      on: vi.fn()
    };
    vi.doMock("electron", () => ({
      app: {
        quit: vi.fn()
      },
      BrowserWindow: vi.fn(),
      Menu: {
        buildFromTemplate: vi.fn(template => template)
      },
      Tray: vi.fn(() => trayInstance),
      nativeImage: {
        createFromPath
      }
    }));

    const { createPlatformBridge } = await import("./platform");
    const bridge = createPlatformBridge({ appResourceRoot: "C:/NearMate-fixtures/assets" });

    bridge.attachTray({
      toggleAvatarVisibility: vi.fn(),
      openConsole: vi.fn()
    });

    expect(observedIconPath.replaceAll("\\", "/")).toBe(
      "C:/NearMate-fixtures/assets/icon.png"
    );
    expect(resize).toHaveBeenCalledWith({
      width: 16,
      height: 16
    });
    expect(trayInstance.setToolTip).toHaveBeenCalledWith("NearMate");
  });

  it("托盘菜单把显示收起和打开 Console 交给上层动作", async () => {
    let contextMenuTemplate: Array<{ label: string; click?: () => void }> = [];
    const toggleAvatarVisibility = vi.fn();
    const openConsole = vi.fn();
    const trayInstance = {
      setToolTip: vi.fn(),
      setTitle: vi.fn(),
      setContextMenu: vi.fn(),
      on: vi.fn()
    };

    vi.doMock("electron", () => ({
      app: {
        quit: vi.fn()
      },
      BrowserWindow: vi.fn(),
      Menu: {
        buildFromTemplate: vi.fn(template => {
          contextMenuTemplate = template as Array<{ label: string; click?: () => void }>;
          return template;
        })
      },
      Tray: vi.fn(() => trayInstance),
      nativeImage: {
        createFromPath: vi.fn(() => ({
          resize: vi.fn(() => "resized-icon"),
          isEmpty: () => false
        }))
      }
    }));

    const { createPlatformBridge } = await import("./platform");
    const bridge = createPlatformBridge({ appResourceRoot: "C:/NearMate-fixtures/assets" });

    bridge.attachTray({
      toggleAvatarVisibility,
      openConsole
    });
    contextMenuTemplate.find(item => item.label === "显示 / 收起桌宠")?.click?.();
    contextMenuTemplate.find(item => item.label === "打开 Console")?.click?.();

    expect(toggleAvatarVisibility).toHaveBeenCalledOnce();
    expect(openConsole).toHaveBeenCalledOnce();
    expect(trayInstance.on).toHaveBeenCalledWith("click", expect.any(Function));
  });
});
