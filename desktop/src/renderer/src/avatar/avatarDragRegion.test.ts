import { describe, expect, it } from "vitest";
import { calculateAvatarDragRegion } from "./avatarDragRegion";

describe("calculateAvatarDragRegion", () => {
  it("根据模型底部中心点和缩放比例计算可拖拽的可见模型边界", () => {
    expect(
      calculateAvatarDragRegion({
        x: 180,
        y: 420,
        scale: 0.5,
        modelWidth: 300,
        modelHeight: 600
      })
    ).toEqual({
      x: 105,
      y: 120,
      width: 150,
      height: 300
    });
  });

  it("传入交互热区比例时，会围绕底部中心点收窄透明拖拽层", () => {
    expect(
      calculateAvatarDragRegion({
        x: 180,
        y: 420,
        scale: 0.5,
        modelWidth: 300,
        modelHeight: 600,
        interactionBounds: {
          widthRatio: 0.64,
          heightRatio: 0.94
        }
      })
    ).toEqual({
      x: 132,
      y: 138,
      width: 96,
      height: 282
    });
  });

  it("交互热区可按模型可见宽度水平偏移，用于补偿 Live2D 透明外接盒", () => {
    expect(
      calculateAvatarDragRegion({
        x: 370,
        y: 752,
        scale: 268.8,
        modelWidth: 2,
        modelHeight: 2,
        interactionBounds: {
          widthRatio: 0.64,
          heightRatio: 0.94,
          offsetXRatio: -0.08
        }
      })
    ).toEqual({
      x: 155,
      y: 247,
      width: 344,
      height: 505
    });
  });
});
