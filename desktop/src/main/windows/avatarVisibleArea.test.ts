import { describe, expect, it } from "vitest";
import { calculateAvatarVisibleArea } from "./avatarVisibleArea";

describe("calculateAvatarVisibleArea", () => {
  it("窗口完全在工作区内时可见区等于整个窗口", () => {
    const area = calculateAvatarVisibleArea(
      { x: 200, y: 100, width: 400, height: 600 },
      { x: 0, y: 0, width: 1920, height: 1040 }
    );

    expect(area.visibleTop).toBe(0);
    expect(area.visibleBottom).toBe(600);
    expect(area.visibleLeft).toBe(0);
    expect(area.visibleRight).toBe(400);
  });

  it("窗口下半部被任务栏切掉时 visibleBottom 小于窗口高度", () => {
    // 工作区底边在 y=1040（任务栏占 40px）。窗口 y=600、高 600，底边到 1200，被切掉 160px。
    const area = calculateAvatarVisibleArea(
      { x: 200, y: 600, width: 400, height: 600 },
      { x: 0, y: 0, width: 1920, height: 1040 }
    );

    // 工作区底边 1040 - 窗口顶 600 = 440 本地像素处。
    expect(area.visibleBottom).toBe(440);
    expect(area.visibleTop).toBe(0);
  });

  it("窗口顶部被工作区上边缘切掉时 visibleTop 大于 0", () => {
    // 工作区顶边在 y=48（顶部菜单栏）。窗口 y=0，顶部 48px 被切掉。
    const area = calculateAvatarVisibleArea(
      { x: 200, y: 0, width: 400, height: 600 },
      { x: 0, y: 48, width: 1920, height: 992 }
    );

    expect(area.visibleTop).toBe(48);
    expect(area.visibleBottom).toBe(600);
  });

  it("窗口完全移出工作区下方时可见区收敛成贴底零高度线", () => {
    // 窗口整体在工作区底边之下。
    const area = calculateAvatarVisibleArea(
      { x: 200, y: 1200, width: 400, height: 600 },
      { x: 0, y: 0, width: 1920, height: 1040 }
    );

    expect(area.visibleTop).toBe(0);
    expect(area.visibleBottom).toBe(0);
  });

  it("窗口左右被工作区切掉时 visibleLeft/right 同步收缩", () => {
    // 工作区右边 1920。窗口 x=1800、宽 400，右边到 2200，被切掉 280px。
    const area = calculateAvatarVisibleArea(
      { x: 1800, y: 100, width: 400, height: 600 },
      { x: 0, y: 0, width: 1920, height: 1040 }
    );

    expect(area.visibleLeft).toBe(0);
    expect(area.visibleRight).toBe(120);
  });
});
