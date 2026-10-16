import { describe, expect, it } from "vitest";
import { calculateResizedAvatarBounds } from "./avatarResizeBounds";

describe("calculateResizedAvatarBounds", () => {
  it("右下角缩放时，会按人物档案比例同步增大宽高", () => {
    expect(
      calculateResizedAvatarBounds({
        direction: "se",
        startBounds: {
          x: 100,
          y: 120,
          width: 420,
          height: 520
        },
        startPoint: {
          screenX: 300,
          screenY: 400
        },
        currentPoint: {
          screenX: 360,
          screenY: 470
        },
        minWidth: 360,
        minHeight: 420,
        aspectRatio: 420 / 520
      })
    ).toEqual({
      x: 100,
      y: 120,
      width: 480,
      height: 594
    });
  });

  it("东侧单边缩放也会按人物档案比例同步改变高度", () => {
    expect(
      calculateResizedAvatarBounds({
        direction: "e",
        startBounds: {
          x: 100,
          y: 120,
          width: 420,
          height: 520
        },
        startPoint: {
          screenX: 300,
          screenY: 400
        },
        currentPoint: {
          screenX: 450,
          screenY: 400
        },
        minWidth: 360,
        minHeight: 420,
        aspectRatio: 420 / 520
      })
    ).toEqual({
      x: 100,
      y: 120,
      width: 570,
      height: 706
    });
  });

  it("南侧单边缩放也会按人物档案比例同步改变宽度", () => {
    expect(
      calculateResizedAvatarBounds({
        direction: "s",
        startBounds: {
          x: 100,
          y: 120,
          width: 420,
          height: 520
        },
        startPoint: {
          screenX: 300,
          screenY: 400
        },
        currentPoint: {
          screenX: 300,
          screenY: 620
        },
        minWidth: 360,
        minHeight: 420,
        aspectRatio: 420 / 520
      })
    ).toEqual({
      x: 100,
      y: 120,
      width: 598,
      height: 740
    });
  });

  it("左上角缩小时命中等比最小尺寸后，会同步修正 x 与 y", () => {
    expect(
      calculateResizedAvatarBounds({
        direction: "nw",
        startBounds: {
          x: 100,
          y: 120,
          width: 420,
          height: 520
        },
        startPoint: {
          screenX: 500,
          screenY: 600
        },
        currentPoint: {
          screenX: 700,
          screenY: 900
        },
        minWidth: 360,
        minHeight: 420,
        aspectRatio: 420 / 520
      })
    ).toEqual({
      width: 360,
      height: 446,
      x: 160,
      y: 194
    });
  });
});
