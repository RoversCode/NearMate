import { describe, expect, it } from "vitest";
import type { BrowserWindow } from "electron";
import type { AvatarSummonMode } from "./shell/shellTypes";
import { createShellRuntime, teardownShellRuntime } from "./shellRuntime";

describe("shellRuntime", () => {
  it("导出 shell runtime 创建与销毁入口，供 bootstrap 保持纯编排", () => {
    expect(typeof createShellRuntime).toBe("function");
    expect(typeof teardownShellRuntime).toBe("function");
  });

  it("teardown 时会销毁非 closingWindow 的窗口并保留正在关闭的窗口", () => {
    const destroyed: string[] = [];
    const avatarWindow = createWindowStub("avatar", destroyed);
    const panelWindow = createWindowStub("panel", destroyed);
    const chatWindow = createWindowStub("chat", destroyed);
    const runtime = {
      avatarWindow,
      panelWindow,
      chatWindow,
      chatWindowMotion: {
        currentMode: () => "collapsed" as const,
        resolveBounds: () => ({ x: 0, y: 0, width: 76, height: 76 }),
        rememberBounds() {},
        reset() {}
      },
      chatWindowDrag: {
        startDrag: () => false,
        moveDrag: () => false,
        endDrag() {},
        reset() {}
      },
      chatWindowResize: {
        startResize: () => false,
        moveResize: () => false,
        endResize() {},
        reset() {}
      },
      controller: createControllerStub(),
      companionSummonController: {
        toggleCompanionEntryVisibility: (): AvatarSummonMode => "summoned_topmost"
      },
      stopPanelBlurCloseGuard() {}
    };

    teardownShellRuntime(runtime, {
      closingWindow: panelWindow,
      destroyTray() {},
      stopAecSidecar() {},
      resetAvatarInteraction() {}
    });

    expect(destroyed).toEqual(["avatar", "chat"]);
  });
});

function createControllerStub() {
  return {
    start() {},
    hoverEnter() {},
    hoverLeave() {},
    getAvatarSummonMode(): AvatarSummonMode {
      return "summoned_topmost";
    },
    toggleAvatarSummonedVisibility(): AvatarSummonMode {
      return "summoned_topmost";
    },
    startDrag() {},
    endDrag() {},
    startMaterialDrop() {},
    endMaterialDrop() {},
    openPanel() {},
    closePanelFromUserRequest() {},
    closePanelFromExternalBlur() {},
    refreshPanelPlacement() {}
  };
}

function createWindowStub(name: string, destroyed: string[]): BrowserWindow {
  return {
    isDestroyed: () => false,
    destroy: () => {
      destroyed.push(name);
    }
  } as unknown as BrowserWindow;
}
