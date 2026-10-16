import { describe, expect, it, vi } from "vitest";
import { getAvatarPresentationProfile } from "../shared/avatarPresentationProfile";
import {
  FakeElectronWindow,
  flushMicrotasks,
  setupBootstrapHarness
} from "./bootstrap.testHarness";
describe("bootstrap avatar presentation", () => {
  it("非法 resize 与人物展示档案 payload 会被忽略", async () => {
    const { ipcMainMock, avatarWindow } = await setupBootstrapHarness({
      controllerStartsByShowingAvatar: true
    });

    ipcMainMock.emit("avatar:resize-start", undefined, {
      direction: "bad-direction",
      point: { screenX: 200, screenY: 220 }
    });
    ipcMainMock.emit("avatar:resize-move", undefined, {
      screenX: 260,
      screenY: 280
    });
    ipcMainMock.emit("avatar:apply-presentation-profile", undefined, {
      modelId: "   "
    });

    expect(avatarWindow.setBounds).not.toHaveBeenCalled();
    expect(avatarWindow.show).not.toHaveBeenCalled();
  });

  it("applyPresentationProfile 会按底部中心点重置到新人物默认尺寸", async () => {
    const avatarWindow = new FakeElectronWindow({
      x: 100,
      y: 200,
      width: 420,
      height: 520
    });
    const { ipcMainMock } = await setupBootstrapHarness({
      avatarWindow
    });
    const hiyoriProfile = getAvatarPresentationProfile("Hiyori");

    ipcMainMock.emit("avatar:apply-presentation-profile", undefined, {
      modelId: "Hiyori"
    });

    expect(avatarWindow.setBounds).toHaveBeenCalledWith({
      x: -10,
      y: -40,
      width: hiyoriProfile.windowWidth,
      height: hiyoriProfile.windowHeight
    });
  });

  it("首次收到 applyPresentationProfile 后才真正显示角色窗，且后续不会重复首次 show", async () => {
    vi.useFakeTimers();

    try {
      const { ipcMainMock, avatarWindow } = await setupBootstrapHarness({
        controllerStartsByShowingAvatar: true
      });

      expect(avatarWindow.show).not.toHaveBeenCalled();

      ipcMainMock.emit("avatar:apply-presentation-profile", undefined, {
        modelId: "Hiyori"
      });

      expect(avatarWindow.show).toHaveBeenCalledTimes(1);

      ipcMainMock.emit("avatar:apply-presentation-profile", undefined, {
        modelId: "Hiyori"
      });
      await vi.runOnlyPendingTimersAsync();

      expect(avatarWindow.show).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("首显兜底策略有效，如果一直没有人物展示档案请求也不会无限隐藏", async () => {
    vi.useFakeTimers();

    try {
      const { avatarWindow } = await setupBootstrapHarness({
        controllerStartsByShowingAvatar: true
      });

      expect(avatarWindow.show).not.toHaveBeenCalled();

      await vi.runOnlyPendingTimersAsync();

      expect(avatarWindow.show).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("resize move 会使用当前人物档案的最小尺寸，而不是永远使用默认值", async () => {
    const { ipcMainMock, avatarWindow } = await setupBootstrapHarness();

    ipcMainMock.emit("avatar:apply-presentation-profile", undefined, {
      modelId: "Hiyori"
    });
    ipcMainMock.emit("avatar:resize-start", undefined, {
      direction: "nw",
      point: { screenX: 500, screenY: 600 }
    });
    ipcMainMock.emit("avatar:resize-move", undefined, {
      screenX: 700,
      screenY: 900
    });

    expect(avatarWindow.setBounds).toHaveBeenLastCalledWith({
      x: 130,
      y: 42,
      width: 520,
      height: 618
    });
  });

  it("bootstrap 尚未完成时收到 applyPresentationProfile，也会在首次 show 前应用人物默认尺寸", async () => {
    vi.useFakeTimers();

    try {
      let resolveAvatarLoad: (() => void) | undefined;
      const avatarWindow = new FakeElectronWindow(
        undefined,
        {
          loadFilePromise: new Promise<void>(resolve => {
            resolveAvatarLoad = resolve;
          })
        }
      );

      const {
        ipcMainMock,
        bootstrapPromise
      } = await setupBootstrapHarness({
        avatarWindow,
        controllerStartsByShowingAvatar: true,
        awaitBootstrap: false
      });

      await flushMicrotasks();

      ipcMainMock.emit("avatar:apply-presentation-profile", undefined, {
        modelId: "Hiyori"
      });

      resolveAvatarLoad?.();
      await bootstrapPromise;

      const hiyoriProfile = getAvatarPresentationProfile("Hiyori");
      expect(avatarWindow.setBounds).toHaveBeenCalledWith({
        x: 10,
        y: -100,
        width: hiyoriProfile.windowWidth,
        height: hiyoriProfile.windowHeight
      });
      expect(avatarWindow.show).toHaveBeenCalledOnce();
      expect(avatarWindow.setBounds.mock.invocationCallOrder[0]).toBeLessThan(
        avatarWindow.show.mock.invocationCallOrder[0]!
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("窗口已显示后再次收到相同 modelId 的 applyPresentationProfile，不会重置本次运行内手调尺寸", async () => {
    const { ipcMainMock, avatarWindow } = await setupBootstrapHarness({
      controllerStartsByShowingAvatar: true
    });

    ipcMainMock.emit("avatar:apply-presentation-profile", undefined, {
      modelId: "Hiyori"
    });
    avatarWindow.setBounds.mockClear();

    ipcMainMock.emit("avatar:resize-start", undefined, {
      direction: "se",
      point: { screenX: 100, screenY: 100 }
    });
    ipcMainMock.emit("avatar:resize-move", undefined, {
      screenX: 140,
      screenY: 160
    });

    expect(avatarWindow.setBounds).toHaveBeenCalledTimes(1);

    ipcMainMock.emit("avatar:apply-presentation-profile", undefined, {
      modelId: "Hiyori"
    });

    expect(avatarWindow.setBounds).toHaveBeenCalledTimes(1);
  });
});
