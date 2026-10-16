import { describe, expect, it, vi } from "vitest";

const electronMock = vi.hoisted(() => {
  class FakeBrowserWindow {
    readonly setAlwaysOnTop = vi.fn();

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

import { createChatWindow } from "./chatWindow";

describe("createChatWindow", () => {
  it("创建聊天窗时不设置永久置顶，窗口层级由入口组编排临时处理", () => {
    const window = createChatWindow(
      "/tmp/chat-preload.mjs",
      "--companion-window-bootstrap=chat"
    ) as unknown as {
      setAlwaysOnTop: ReturnType<typeof vi.fn>;
    };

    /**
     * 聊天球的可见性属于入口组会话。
     * 创建阶段不能永久置顶，否则会污染普通聊天窗口和其他应用之间的层级关系。
     */
    expect(electronMock.BrowserWindow).toHaveBeenCalledWith(
      expect.objectContaining({
        transparent: true,
        frame: false,
        skipTaskbar: true
      })
    );
    expect(window.setAlwaysOnTop).not.toHaveBeenCalled();
  });
});
