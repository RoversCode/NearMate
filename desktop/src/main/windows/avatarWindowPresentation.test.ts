import { describe, expect, it, vi } from "vitest";
import { applyAvatarWindowPresentationMode } from "./avatarWindowPresentation";

function createFakeAvatarWindow() {
  return {
    setAlwaysOnTop: vi.fn(),
    setIgnoreMouseEvents: vi.fn(),
    hide: vi.fn(),
    show: vi.fn()
  };
}

describe("applyAvatarWindowPresentationMode", () => {
  it("被动展示模式先恢复鼠标穿透，再关闭置顶", () => {
    const window = createFakeAvatarWindow();

    applyAvatarWindowPresentationMode(window, "passive");

    expect(window.setIgnoreMouseEvents).toHaveBeenCalledWith(true, { forward: true });
    expect(window.setAlwaysOnTop).toHaveBeenCalledWith(false);
    expect(window.setIgnoreMouseEvents.mock.invocationCallOrder[0]).toBeLessThan(
      window.setAlwaysOnTop.mock.invocationCallOrder[0]
    );
  });

  it("交互展示模式先临时提升到 floating 层级，再收回鼠标穿透", () => {
    const window = createFakeAvatarWindow();

    applyAvatarWindowPresentationMode(window, "interactive");

    expect(window.setAlwaysOnTop).toHaveBeenCalledWith(true, "floating");
    expect(window.setIgnoreMouseEvents).toHaveBeenCalledWith(false, { forward: true });
    expect(window.setAlwaysOnTop.mock.invocationCallOrder[0]).toBeLessThan(
      window.setIgnoreMouseEvents.mock.invocationCallOrder[0]
    );
  });

  it("召回展示模式会显示角色窗、置顶并保持鼠标穿透", () => {
    const window = createFakeAvatarWindow();

    applyAvatarWindowPresentationMode(window, "summoned");

    expect(window.show).toHaveBeenCalledOnce();
    expect(window.setAlwaysOnTop).toHaveBeenCalledWith(true, "floating");
    expect(window.setIgnoreMouseEvents).toHaveBeenCalledWith(true, { forward: true });
  });

  it("隐藏展示模式会先恢复穿透再隐藏角色窗", () => {
    const window = createFakeAvatarWindow();

    applyAvatarWindowPresentationMode(window, "hidden");

    expect(window.setIgnoreMouseEvents).toHaveBeenCalledWith(true, { forward: true });
    expect(window.setAlwaysOnTop).toHaveBeenCalledWith(false);
    expect(window.hide).toHaveBeenCalledOnce();
  });

  it("面板共存交互模式只收回穿透，不把角色窗提升到侧边板上方", () => {
    const window = createFakeAvatarWindow();

    applyAvatarWindowPresentationMode(window, "panel-adjacent-interactive");

    expect(window.setAlwaysOnTop).toHaveBeenCalledWith(false);
    expect(window.setIgnoreMouseEvents).toHaveBeenCalledWith(false, { forward: true });
    expect(window.setAlwaysOnTop.mock.invocationCallOrder[0]).toBeLessThan(
      window.setIgnoreMouseEvents.mock.invocationCallOrder[0]
    );
  });
});
