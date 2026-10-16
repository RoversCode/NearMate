import { describe, expect, it } from "vitest";
import { resolveBottomBubbleStyle } from "./bottomBubbleStyle";

describe("resolveBottomBubbleStyle", () => {
  it("petBounds 不可用时返回 undefined，让组件回退到 CSS 默认锚点", () => {
    const style = resolveBottomBubbleStyle({
      petBounds: null,
      footAnchor: { x: 0.5, y: 1 },
      viewportWidth: 520,
      viewportHeight: 700,
      measureHeight: () => 80
    });

    expect(style).toBeUndefined();
  });

  it("petBounds 可用时返回覆盖 CSS 底边锚点的绝对定位", () => {
    const style = resolveBottomBubbleStyle({
      petBounds: { x: 150, y: 80, width: 200, height: 520 },
      footAnchor: { x: 0.5, y: 1 },
      viewportWidth: 520,
      viewportHeight: 700,
      measureHeight: () => 80
    });

    expect(style).toBeDefined();
    // 显式覆盖 right/bottom/margin，避免与 CSS 中 left/right/bottom 默认锚点叠加。
    expect(style?.right).toBe("auto");
    expect(style?.bottom).toBe("auto");
    expect(style?.margin).toBe(0);
    expect(style?.left).toMatch(/px$/);
    expect(style?.top).toMatch(/px$/);
  });

  it("visibleBottom 上移时气泡顶边随之上移（贴可见身体最底部）", () => {
    const base = {
      petBounds: { x: 150, y: 120, width: 200, height: 900 },
      footAnchor: { x: 0.5, y: 1 } as const,
      viewportWidth: 520,
      viewportHeight: 700,
      measureHeight: () => 80
    };
    const fullyVisible = resolveBottomBubbleStyle(base);
    const clipped = resolveBottomBubbleStyle({ ...base, visibleBottom: 420 });

    const fullyTop = Number.parseFloat(String(fullyVisible?.top));
    const clippedTop = Number.parseFloat(String(clipped?.top));

    // 可见底边上移后，气泡顶边应更靠上。
    expect(clippedTop).toBeLessThan(fullyTop);
  });

  it("气泡宽度受窗口边距与最大宽度上限共同约束", () => {
    const narrow = resolveBottomBubbleStyle({
      petBounds: { x: 40, y: 60, width: 120, height: 400 },
      footAnchor: { x: 0.5, y: 1 },
      viewportWidth: 260,
      viewportHeight: 600,
      measureHeight: () => 80
    });
    const wide = resolveBottomBubbleStyle({
      petBounds: { x: 200, y: 60, width: 200, height: 480 },
      footAnchor: { x: 0.5, y: 1 },
      viewportWidth: 1024,
      viewportHeight: 800,
      measureHeight: () => 80
    });

    // 窄窗口宽度被 (viewportWidth - 36) 约束。
    expect(narrow?.width).toBe(`${260 - 36}px`);
    // 宽窗口宽度被 300px 上限约束。
    expect(wide?.width).toBe("300px");
  });

  it("measureHeight 会收到最终气泡宽度用于估算高度", () => {
    let receivedWidth = -1;
    resolveBottomBubbleStyle({
      petBounds: { x: 200, y: 60, width: 200, height: 480 },
      footAnchor: { x: 0.5, y: 1 },
      viewportWidth: 1024,
      viewportHeight: 800,
      measureHeight: bubbleWidth => {
        receivedWidth = bubbleWidth;
        return 80;
      }
    });

    expect(receivedWidth).toBe(300);
  });
});
