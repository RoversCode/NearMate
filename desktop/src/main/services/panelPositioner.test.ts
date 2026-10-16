import { describe, expect, it } from "vitest";
import { calculatePanelPlacement } from "./panelPositioner";

describe("calculatePanelPlacement", () => {
  it("默认优先把面板放在人物锚点右侧", () => {
    const placement = calculatePanelPlacement({
      avatarAnchorBounds: { x: 100, y: 120, width: 180, height: 320 },
      panelSize: { width: 360, height: 520 },
      displayBounds: { x: 0, y: 0, width: 1440, height: 900 },
      gap: 16
    });

    // 这里同时固定横向选择与正常纵向居中结果，避免后续改动只保住 side/x 而破坏未夹紧时的 y 计算。
    expect(placement).toEqual({ side: "right", x: 296, y: 20 });
  });

  it("右侧放不下时自动切到角色左侧", () => {
    const placement = calculatePanelPlacement({
      avatarAnchorBounds: { x: 1120, y: 140, width: 180, height: 320 },
      panelSize: { width: 360, height: 520 },
      displayBounds: { x: 0, y: 0, width: 1440, height: 900 },
      gap: 16
    });

    expect(placement.side).toBe("left");
    expect(placement.x).toBe(744);
  });

  it("纵向位置会被夹在当前显示器边界内", () => {
    const placement = calculatePanelPlacement({
      avatarAnchorBounds: { x: 120, y: 760, width: 180, height: 220 },
      panelSize: { width: 360, height: 420 },
      displayBounds: { x: 0, y: 0, width: 1440, height: 900 },
      gap: 16
    });

    expect(placement.y).toBe(480);
  });

  it("人物变大但透明窗口更宽时，面板仍贴近人物锚点而不是透明窗口右边", () => {
    const placement = calculatePanelPlacement({
      avatarAnchorBounds: { x: 142, y: 36, width: 163, height: 479 },
      panelSize: { width: 380, height: 560 },
      displayBounds: { x: 0, y: 0, width: 1440, height: 900 },
      gap: 16
    });

    expect(placement).toEqual({
      side: "right",
      x: 321,
      y: 0
    });
  });
});
