import { describe, expect, it, vi } from "vitest";
import type { AvatarSummonMode } from "../shell/shellTypes";
import { createCompanionSummonController } from "./companionSummonController";

describe("createCompanionSummonController", () => {
  it("召回桌宠时保持聊天窗隐藏且不抢焦点", () => {
    const chatWindow = {
      hide: vi.fn(),
      setAlwaysOnTop: vi.fn(),
      webContents: {
        send: vi.fn()
      }
    };
    const controller = createCompanionSummonController({
      avatarVisibilityController: {
        toggleAvatarSummonedVisibility: vi.fn<() => AvatarSummonMode>(() => "summoned_topmost")
      },
      chatWindow,
    });

    const summonMode = controller.toggleCompanionEntryVisibility();

    expect(summonMode).toBe("summoned_topmost");
    expect(chatWindow.webContents.send).toHaveBeenCalledWith("chat:mode-requested", {
      mode: "collapsed"
    });
    expect(chatWindow.hide).toHaveBeenCalledOnce();
    expect(chatWindow.setAlwaysOnTop).not.toHaveBeenCalled();
  });

  it("收起桌宠时同步隐藏聊天窗", () => {
    const chatWindow = {
      hide: vi.fn(),
      webContents: {
        send: vi.fn()
      }
    };
    const controller = createCompanionSummonController({
      avatarVisibilityController: {
        toggleAvatarSummonedVisibility: vi.fn<() => AvatarSummonMode>(() => "hidden")
      },
      chatWindow,
    });

    const summonMode = controller.toggleCompanionEntryVisibility();

    expect(summonMode).toBe("hidden");
    expect(chatWindow.webContents.send).toHaveBeenCalledWith("chat:mode-requested", {
      mode: "collapsed"
    });
    expect(chatWindow.hide).toHaveBeenCalledOnce();
  });
});
