import { describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => {
  class FakeBrowserWindow {
    readonly setAlwaysOnTop = vi.fn();
    readonly setIgnoreMouseEvents = vi.fn();

    constructor(readonly options: unknown) {}
  }

  return {
    FakeBrowserWindow,
    BrowserWindow: vi.fn((options: unknown) => new FakeBrowserWindow(options))
  };
});

vi.mock("electron", () => ({
  BrowserWindow: electronMock.BrowserWindow
}));

import { createAvatarWindow } from "./avatarWindow";

describe("createAvatarWindow", () => {
  it("创建角色窗时默认进入被动展示，不再使用 screen-saver 置顶层级", () => {
    const window = createAvatarWindow(
      "/tmp/avatar-preload.mjs",
      "--companion-window-bootstrap=avatar"
    ) as unknown as {
      setAlwaysOnTop: ReturnType<typeof vi.fn>;
      setIgnoreMouseEvents: ReturnType<typeof vi.fn>;
    };

    /**
     * 创建参数必须保持普通层级，初始化阶段再显式投影 passive。
     * 这样既避免构造期抢占桌面图标，也能把窗口展示策略集中到 presentation 模块。
     */
    expect(electronMock.BrowserWindow).toHaveBeenCalledWith(
      expect.objectContaining({
        alwaysOnTop: false,
        transparent: true,
        frame: false
      })
    );
    expect(window.setIgnoreMouseEvents).toHaveBeenCalledWith(true, { forward: true });
    expect(window.setAlwaysOnTop).toHaveBeenCalledWith(false);
    expect(window.setAlwaysOnTop).not.toHaveBeenCalledWith(true, "screen-saver");
  });
});
