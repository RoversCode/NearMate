import { describe, expect, it, vi } from "vitest";
import { getAvatarPresentationProfile } from "../../shared/avatarPresentationProfile";
import { createAvatarWindowInteractionController } from "./avatarWindowInteraction";

type FakeBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * createFakeAvatarWindow 构造 avatar 交互控制器需要的最小窗口能力。
 * 业务意图是让测试直接观察窗口几何读写，而不启动真实 Electron 窗口。
 */
function createFakeAvatarWindow(initialBounds: FakeBounds) {
  let bounds = { ...initialBounds };

  return {
    isDestroyed: vi.fn(() => false),
    webContents: {
      isDestroyed: vi.fn(() => false),
      send: vi.fn()
    },
    show: vi.fn(),
    getBounds: vi.fn(() => ({ ...bounds })),
    setBounds: vi.fn((nextBounds: FakeBounds) => {
      bounds = { ...nextBounds };
    })
  };
}

describe("createAvatarWindowInteractionController", () => {
  it("应用人物展示档案时优先恢复该 modelId 保存过的窗口几何", () => {
    const layoutStore = {
      read: vi.fn(() => ({ x: 320, y: 180, width: 520, height: 620 })),
      save: vi.fn(),
      reset: vi.fn()
    };
    const controller = createAvatarWindowInteractionController(
      {
        getDisplayMatching: vi.fn(() => ({
          workArea: { x: 0, y: 0, width: 1440, height: 860 }
        }))
      } as never,
      layoutStore
    );
    const avatarWindow = createFakeAvatarWindow({ x: 120, y: 140, width: 420, height: 520 });

    controller.applyPresentationProfile(avatarWindow as never, { modelId: "Hiyori" });

    expect(layoutStore.read).toHaveBeenCalledWith("Hiyori", {
      x: 0,
      y: 0,
      width: 1440,
      height: 860
    });
    expect(avatarWindow.setBounds).toHaveBeenCalledWith({
      x: 320,
      y: 180,
      width: 520,
      height: 620
    });
  });

  it("拖拽结束时把当前人物窗口几何写入布局仓储", () => {
    const layoutStore = {
      read: vi.fn(() => null),
      save: vi.fn(),
      reset: vi.fn()
    };
    const controller = createAvatarWindowInteractionController(
      {
        getDisplayMatching: vi.fn(() => ({
          workArea: { x: 0, y: 0, width: 1440, height: 860 }
        }))
      } as never,
      layoutStore
    );
    const avatarWindow = createFakeAvatarWindow({ x: 120, y: 140, width: 420, height: 520 });

    controller.applyPresentationProfile(avatarWindow as never, { modelId: "Haru" });
    controller.startDrag(avatarWindow as never, { screenX: 200, screenY: 220 });
    controller.moveDrag(avatarWindow as never, { screenX: 250, screenY: 260 });
    controller.endDrag(avatarWindow as never);

    expect(layoutStore.save).toHaveBeenCalledWith("Haru", {
      x: 170,
      y: 180,
      width: 420,
      height: 520
    });
  });

  it("重置当前人物布局时清除记录，并强制回到该人物默认展示档案", () => {
    const layoutStore = {
      read: vi.fn(() => ({ x: 320, y: 180, width: 520, height: 620 })),
      save: vi.fn(),
      reset: vi.fn()
    };
    const controller = createAvatarWindowInteractionController(
      {
        getDisplayMatching: vi.fn(() => ({
          workArea: { x: 0, y: 0, width: 1440, height: 860 }
        }))
      } as never,
      layoutStore
    );
    const avatarWindow = createFakeAvatarWindow({ x: 120, y: 140, width: 420, height: 520 });
    const hiyoriProfile = getAvatarPresentationProfile("Hiyori");

    controller.applyPresentationProfile(avatarWindow as never, { modelId: "Hiyori" });
    avatarWindow.setBounds.mockClear();
    controller.resetCurrentModelLayout(avatarWindow as never);

    expect(layoutStore.reset).toHaveBeenCalledWith("Hiyori");
    expect(avatarWindow.setBounds).toHaveBeenCalledWith({
      x: Math.round(320 + 520 / 2 - hiyoriProfile.windowWidth / 2),
      y: Math.round(180 + 620 - hiyoriProfile.windowHeight),
      width: hiyoriProfile.windowWidth,
      height: hiyoriProfile.windowHeight
    });
  });
});
