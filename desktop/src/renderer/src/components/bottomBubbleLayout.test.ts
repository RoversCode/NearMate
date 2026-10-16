import { describe, expect, it } from "vitest";
import { calculateBottomBubbleLayout } from "./bottomBubbleLayout";

describe("bottomBubbleLayout", () => {
  it("气泡水平居中于脚部锚点、底边贴住脚部", () => {
    const petBounds = { x: 150, y: 80, width: 200, height: 520 };
    const layout = calculateBottomBubbleLayout({
      petBounds,
      viewportWidth: 520,
      viewportHeight: 700,
      bubbleWidth: 260,
      bubbleHeight: 80,
      footAnchor: { x: 0.5, y: 1 }
    });
    const anchorX = petBounds.x + petBounds.width * 0.5;
    const footY = petBounds.y + petBounds.height * 1;

    // 水平居中于脚部锚点。
    expect(layout.left + 260 / 2).toBeCloseTo(anchorX);
    // 底边贴住脚部（top + height ≈ footY）。
    expect(layout.top + 80).toBeCloseTo(footY);
  });

  it("脚部超出窗口底部时气泡被钳制回窗口可见区内", () => {
    // 角色被向下拖动，脚部远超窗口高度，但未提供 visibleBottom。
    const petBounds = { x: 150, y: 120, width: 200, height: 900 };
    const viewportHeight = 700;
    const layout = calculateBottomBubbleLayout({
      petBounds,
      viewportWidth: 520,
      viewportHeight,
      bubbleWidth: 260,
      bubbleHeight: 80,
      footAnchor: { x: 0.5, y: 1 }
    });

    expect(layout.top).toBeGreaterThanOrEqual(18);
    expect(layout.top + 80).toBeLessThanOrEqual(viewportHeight - 18);
  });

  it("下半身被任务栏切掉时气泡贴住可见身体最底部而非窗口底边", () => {
    // 脚部 footY 落在 1020，但主进程算出可见底边只到 480（任务栏/屏幕下边缘）。
    const petBounds = { x: 150, y: 120, width: 200, height: 900 };
    const viewportHeight = 700;
    const visibleBottom = 480;
    const layout = calculateBottomBubbleLayout({
      petBounds,
      viewportWidth: 520,
      viewportHeight,
      bubbleWidth: 260,
      bubbleHeight: 80,
      footAnchor: { x: 0.5, y: 1 },
      visibleBottom
    });

    // 底边贴住可见身体最底部，而不是沉到 footY 或窗口底边。
    expect(layout.top + 80).toBeLessThanOrEqual(visibleBottom - 18);
    // 底边应尽量靠近可见底边（受 margin 约束）。
    expect(layout.top + 80).toBeGreaterThanOrEqual(visibleBottom - 18 - 1);
  });

  it("visibleBottom 大于脚部时气泡仍贴脚而非沉到可见底边", () => {
    // 角色完整可见，visibleBottom 等于窗口高度，气泡应贴脚而不是窗口底部。
    const petBounds = { x: 150, y: 80, width: 200, height: 400 };
    const footY = petBounds.y + petBounds.height * 1; // 480
    const layout = calculateBottomBubbleLayout({
      petBounds,
      viewportWidth: 520,
      viewportHeight: 700,
      bubbleWidth: 260,
      bubbleHeight: 80,
      footAnchor: { x: 0.5, y: 1 },
      visibleBottom: 700
    });

    expect(layout.top + 80).toBeCloseTo(footY);
  });

  it("气泡超出窗口右边界时被钳制回安全区内", () => {
    const petBounds = { x: 420, y: 60, width: 200, height: 480 };
    const viewportWidth = 520;
    const layout = calculateBottomBubbleLayout({
      petBounds,
      viewportWidth,
      viewportHeight: 700,
      bubbleWidth: 260,
      bubbleHeight: 80,
      footAnchor: { x: 0.5, y: 1 }
    });

    expect(layout.left).toBeGreaterThanOrEqual(18);
    expect(layout.left + 260).toBeLessThanOrEqual(viewportWidth - 18);
  });
});
