import { describe, expect, it } from "vitest";
import { DEFAULT_AVATAR_PRESENTATION_PROFILE } from "../../shared/avatarPresentationProfile";
import {
  CHAT_WINDOW_SIZE,
  SAFE_DEFAULT_AVATAR_WINDOW_SIZE,
  buildAvatarWindowOptions,
  buildChatWindowOptions,
  buildPanelWindowOptions
} from "./windowOptions";

describe("window options", () => {
  it("角色窗继续使用安全默认窗口尺寸，并保持透明、无边框、跳过任务栏", () => {
    const options = buildAvatarWindowOptions(
      "/tmp/avatar-preload.mjs",
      "--companion-window-bootstrap=avatar"
    );

    /**
     * 主进程创建 avatar window 时还不知道最终人物是谁，
     * 所以这里只能坚持使用默认展示档案提供的安全窗口尺寸，后续再由 renderer 链路告知 main 做调整。
     */
    expect(SAFE_DEFAULT_AVATAR_WINDOW_SIZE).toEqual({
      width: DEFAULT_AVATAR_PRESENTATION_PROFILE.windowWidth,
      height: DEFAULT_AVATAR_PRESENTATION_PROFILE.windowHeight
    });
    expect(options.width).toBe(SAFE_DEFAULT_AVATAR_WINDOW_SIZE.width);
    expect(options.height).toBe(SAFE_DEFAULT_AVATAR_WINDOW_SIZE.height);
    expect(options.transparent).toBe(true);
    expect(options.frame).toBe(false);
    expect(options.skipTaskbar).toBe(true);
    expect(options.alwaysOnTop).toBe(false);
    expect(options.webPreferences).toEqual(expect.objectContaining({
      additionalArguments: ["--companion-window-bootstrap=avatar"],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }));
  });

  it("面板窗默认隐藏并可获得焦点", () => {
    const options = buildPanelWindowOptions(
      "/tmp/panel-preload.mjs",
      "--companion-window-bootstrap=panel"
    );

    expect(options.show).toBe(false);
    expect(options.focusable).toBe(true);
    expect(options.transparent).toBe(false);
  });

  it("聊天窗是透明无边框的桌面浮层窗口", () => {
    const options = buildChatWindowOptions(
      "/tmp/chat-preload.mjs",
      "--companion-window-bootstrap=chat"
    );

    expect(options.width).toBe(CHAT_WINDOW_SIZE.width);
    expect(options.height).toBe(CHAT_WINDOW_SIZE.height);
    expect(options.show).toBe(false);
    expect(options.focusable).toBe(true);
    expect(options.resizable).toBe(true);
    expect(options.minWidth).toBe(760);
    expect(options.minHeight).toBe(520);
    expect(options.skipTaskbar).toBe(true);
    expect(options.frame).toBe(false);
    expect(options.transparent).toBe(true);
  });
});
