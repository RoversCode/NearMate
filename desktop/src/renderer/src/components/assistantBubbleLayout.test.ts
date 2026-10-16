import { describe, expect, it } from "vitest";
import { calculateAssistantBubbleLayout } from "./assistantBubbleLayout";

describe("assistantBubbleLayout", () => {
  it("左侧空间充足时气泡落在头部西北侧，主体整体位于锚点左上方", () => {
    const petBounds = { x: 260, y: 60, width: 200, height: 560 };
    const layout = calculateAssistantBubbleLayout({
      petBounds,
      viewportWidth: 720,
      viewportHeight: 760,
      text: "你好呀",
      anchor: { x: 0.56, y: 0.07 }
    });

    expect(layout.placement).toBe("northwest");
    // 不压头靠的是水平分离：气泡右边缘位于头部锚点左侧（含侧向净空）。
    expect(layout.style.left + layout.style.width).toBeLessThanOrEqual(layout.anchorX);
    // 气泡顶边位于头部锚点上方，整体偏向头部左上方（西北），头顶仍留白。
    expect(layout.style.top).toBeLessThanOrEqual(layout.anchorY);
  });

  it("左侧空间不足时气泡翻转到头部东北侧", () => {
    // 头部锚点非常靠近窗口左缘，左侧放不下整块气泡。
    const petBounds = { x: 0, y: 60, width: 160, height: 560 };
    const layout = calculateAssistantBubbleLayout({
      petBounds,
      viewportWidth: 600,
      viewportHeight: 760,
      text: "你好呀",
      anchor: { x: 0.3, y: 0.07 }
    });

    expect(layout.placement).toBe("northeast");
    // 不压头靠的是水平分离：气泡左边缘位于头部锚点右侧（含侧向净空）。
    expect(layout.style.left).toBeGreaterThanOrEqual(layout.anchorX);
    // 气泡顶边位于头部锚点上方，整体偏向头部右上方（东北）。
    expect(layout.style.top).toBeLessThanOrEqual(layout.anchorY);
  });

  it("Hiyori 默认窗口下会优先把助手气泡放在头部西北侧", () => {
    const layout = calculateAssistantBubbleLayout({
      petBounds: { x: 155, y: 247, width: 344, height: 505 },
      viewportWidth: 640,
      viewportHeight: 760,
      text: "李白啊，那可是大唐最潇洒的诗人。吾可想听哪方面的呢？",
      anchor: { x: 0.5, y: 0.07 }
    });

    expect(layout.placement).toBe("northwest");
    expect(layout.style.left).toBeGreaterThanOrEqual(18);
    expect(layout.style.left + layout.style.width).toBeLessThanOrEqual(layout.anchorX);
  });

  it("左侧空间略窄时优先压缩气泡宽度，不立刻翻转到东北侧", () => {
    const layout = calculateAssistantBubbleLayout({
      petBounds: { x: 129, y: 247, width: 344, height: 505 },
      viewportWidth: 600,
      viewportHeight: 760,
      text: "我会尽量简短一点说清楚。",
      anchor: { x: 0.5, y: 0.07 }
    });

    expect(layout.placement).toBe("northwest");
    expect(layout.style.width).toBeLessThan(260);
    expect(layout.style.width).toBeGreaterThanOrEqual(190);
    expect(layout.style.left + layout.style.width).toBeLessThanOrEqual(layout.anchorX);
  });

  it("头部靠近窗口顶部时气泡向下钳制到边距，不溢出窗口顶部", () => {
    const petBounds = { x: 260, y: 0, width: 200, height: 600 };
    const layout = calculateAssistantBubbleLayout({
      petBounds,
      viewportWidth: 720,
      viewportHeight: 700,
      text: "我在听。",
      anchor: { x: 0.56, y: 0.02 }
    });

    expect(layout.style.top).toBeGreaterThanOrEqual(18);
  });

  it("西北气泡的引导线从靠头一侧底角指向头部锚点", () => {
    const petBounds = { x: 260, y: 80, width: 200, height: 540 };
    const layout = calculateAssistantBubbleLayout({
      petBounds,
      viewportWidth: 720,
      viewportHeight: 760,
      text: "好，那就先不用了。需要的时候再叫我。",
      anchor: { x: 0.56, y: 0.07 }
    });

    expect(layout.placement).toBe("northwest");
    // 西北放置从靠右底角出发。
    expect(layout.leader.startX).toBeGreaterThan(layout.style.width / 2);
    expect(layout.leader.startY).toBeCloseTo(layout.style.minHeight);
    // 终点朝向头部锚点（在气泡右下方）。
    expect(layout.leader.endX).toBeGreaterThan(layout.leader.startX);
    expect(layout.leader.endY).toBeGreaterThan(layout.leader.startY);
  });

  it("气泡被钳制远离头部时引导线长度受上限约束", () => {
    const petBounds = { x: 40, y: 600, width: 120, height: 360 };
    const layout = calculateAssistantBubbleLayout({
      petBounds,
      viewportWidth: 400,
      viewportHeight: 800,
      text: "你好",
      anchor: { x: 0.5, y: 0.05 }
    });
    const length = Math.hypot(
      layout.leader.endX - layout.leader.startX,
      layout.leader.endY - layout.leader.startY
    );

    expect(length).toBeLessThanOrEqual(110 + 0.001);
  });

  it("桌宠放大时气泡不会过宽，并保留足够的视觉高度", () => {
    const layout = calculateAssistantBubbleLayout({
      petBounds: { x: 450, y: 20, width: 360, height: 940 },
      viewportWidth: 1024,
      viewportHeight: 1024,
      text: "好，那就先不用了。需要的时候再叫我，我随时都在。",
      anchor: { x: 0.56, y: 0.07 }
    });

    expect(layout.style.width).toBeLessThanOrEqual(300);
    expect(layout.style.minHeight).toBeGreaterThanOrEqual(90);
  });

  it("窗口缩放时气泡缩放值会被限制在可读范围内", () => {
    const compactLayout = calculateAssistantBubbleLayout({
      petBounds: { x: 50, y: 80, width: 120, height: 200 },
      viewportWidth: 260,
      viewportHeight: 320,
      text: "你好",
      anchor: { x: 0.5, y: 0.07 }
    });
    const largeLayout = calculateAssistantBubbleLayout({
      petBounds: { x: 240, y: 180, width: 260, height: 480 },
      viewportWidth: 960,
      viewportHeight: 1180,
      text: "你好",
      anchor: { x: 0.52, y: 0.07 }
    });

    expect(compactLayout.scale).toBe(0.9);
    expect(largeLayout.scale).toBe(1.25);
  });
});
