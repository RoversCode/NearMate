import { describe, expect, it, vi } from "vitest";
import { createChatWindowLifecycleOwner } from "./chatWindowLifecycle";
import { createChatWindowMotion } from "./chatWindowMotion";

function createWindow() {
  return {
    setBounds: vi.fn(),
    show: vi.fn(),
    showInactive: vi.fn(),
    hide: vi.fn(),
    focus: vi.fn(),
    isDestroyed: vi.fn(() => false),
    webContents: { send: vi.fn() }
  };
}

describe("chatWindowLifecycle", () => {
  it("启动时隐藏 Chat，并保持 renderer 可继续存活", () => {
    const chatWindow = createWindow();
    const sendAvatarMode = vi.fn();
    const sendChatModeRequest = vi.fn();
    const owner = createChatWindowLifecycleOwner({
      chatWindow,
      chatWindowMotion: createChatWindowMotion(),
      getWorkArea: () => ({ x: 0, y: 0, width: 1440, height: 900 }),
      sendAvatarMode,
      sendChatModeRequest
    });

    owner.initializeHidden();

    expect(owner.currentMode()).toBe("collapsed");
    expect(chatWindow.hide).toHaveBeenCalledOnce();
    expect(chatWindow.show).not.toHaveBeenCalled();
    expect(sendAvatarMode).toHaveBeenLastCalledWith("collapsed");
  });

  it("展开时由主进程定位、显示、聚焦，并把模式同步给 Avatar 与 Chat", () => {
    const chatWindow = createWindow();
    const sendAvatarMode = vi.fn();
    const sendChatModeRequest = vi.fn();
    const owner = createChatWindowLifecycleOwner({
      chatWindow,
      chatWindowMotion: createChatWindowMotion(),
      getWorkArea: () => ({ x: 0, y: 0, width: 1440, height: 900 }),
      sendAvatarMode,
      sendChatModeRequest,
      getAvatarSummonMode: () => "passive_visible"
    });

    owner.openChat();
    owner.setRendererMode("expanded");

    expect(owner.currentMode()).toBe("expanded");
    expect(chatWindow.setBounds).toHaveBeenCalledOnce();
    expect(chatWindow.show).toHaveBeenCalledOnce();
    expect(chatWindow.focus).toHaveBeenCalledOnce();
    expect(sendAvatarMode).toHaveBeenLastCalledWith("expanded");
    expect(sendChatModeRequest).toHaveBeenLastCalledWith("expanded");
  });

  it("桌宠已隐藏时，打开请求不抢焦点并继续保持 Chat 隐藏", () => {
    const chatWindow = createWindow();
    const sendAvatarMode = vi.fn();
    const owner = createChatWindowLifecycleOwner({
      chatWindow,
      chatWindowMotion: createChatWindowMotion(),
      getWorkArea: () => ({ x: 0, y: 0, width: 1440, height: 900 }),
      sendAvatarMode,
      sendChatModeRequest: vi.fn(),
      getAvatarSummonMode: () => "hidden"
    });

    owner.openChat();

    expect(chatWindow.show).not.toHaveBeenCalled();
    expect(chatWindow.focus).not.toHaveBeenCalled();
    expect(owner.currentMode()).toBe("collapsed");
  });
});
