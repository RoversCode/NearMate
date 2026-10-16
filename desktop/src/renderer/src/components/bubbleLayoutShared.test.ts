import { describe, expect, it } from "vitest";
import {
  clamp,
  clampBubbleIntoViewport,
  estimateBubbleTextHeight
} from "./bubbleLayoutShared";

describe("clampBubbleIntoViewport", () => {
  it("气泡完全在窗口内时保持原位", () => {
    const rect = { left: 40, top: 50, width: 200, height: 100 };
    const result = clampBubbleIntoViewport(rect, { width: 500, height: 400 }, 18);

    expect(result.left).toBe(40);
    expect(result.top).toBe(50);
    expect(result.width).toBe(200);
    expect(result.height).toBe(100);
  });

  it("气泡超出右下边界时被钳制回安全区内", () => {
    const rect = { left: 480, top: 360, width: 200, height: 100 };
    const result = clampBubbleIntoViewport(rect, { width: 500, height: 400 }, 18);

    expect(result.left).toBe(500 - 18 - 200);
    expect(result.top).toBe(400 - 18 - 100);
  });

  it("气泡超出左上边界时被钳制到边距处", () => {
    const rect = { left: -40, top: -10, width: 200, height: 100 };
    const result = clampBubbleIntoViewport(rect, { width: 500, height: 400 }, 18);

    expect(result.left).toBe(18);
    expect(result.top).toBe(18);
  });

  it("气泡比可用空间更大时优先保住左上角可读", () => {
    const rect = { left: 0, top: 0, width: 600, height: 500 };
    const result = clampBubbleIntoViewport(rect, { width: 400, height: 300 }, 18);

    expect(result.left).toBe(18);
    expect(result.top).toBe(18);
  });
});

describe("estimateBubbleTextHeight", () => {
  it("短文本返回最小高度", () => {
    const height = estimateBubbleTextHeight("你好", 260, 1);

    expect(height).toBe(72);
  });

  it("长文本随行数增长返回更高高度", () => {
    const shortHeight = estimateBubbleTextHeight("你好", 260, 1);
    const longHeight = estimateBubbleTextHeight(
      "好呀，哥哥想听李白的哪方面呢？是他小时候的传说，还是他游历天下的经历，或者他那些传奇的朋友圈？",
      260,
      1
    );

    expect(longHeight).toBeGreaterThan(shortHeight);
  });
});

describe("clamp", () => {
  it("把数值限制在闭区间内", () => {
    expect(clamp(5, 0, 10)).toBe(5);
    expect(clamp(-3, 0, 10)).toBe(0);
    expect(clamp(99, 0, 10)).toBe(10);
  });
});
