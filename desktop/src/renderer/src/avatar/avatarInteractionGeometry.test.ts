import { describe, expect, it } from "vitest";

import {
  isAvatarActivationPoint,
  isAvatarInteractionPoint,
  resolveAvatarActivationRect,
  resolveAvatarActionRailRect,
  resolveAvatarInteractionHotspotRect,
  resolveAvatarResizeFrameRect
} from "./avatarInteractionGeometry";

describe("avatarInteractionGeometry", () => {
  const petBounds = { x: 180, y: 120, width: 220, height: 500 };

  it("动作栏矩形锚定人物右侧，而不是透明窗口右上角", () => {
    expect(
      resolveAvatarActionRailRect({
        petBounds,
        viewportWidth: 920,
        viewportHeight: 760
      })
    ).toEqual({
      x: 412,
      y: 128,
      width: 44,
      height: 164
    });
  });

  it("主交互热区按人物边界比例落在头部和上半身中心", () => {
    expect(resolveAvatarInteractionHotspotRect(petBounds)).toEqual({
      x: 250,
      y: 210,
      width: 79,
      height: 170
    });
    expect(resolveAvatarInteractionHotspotRect(null)).toBeNull();
  });

  it("核心激活矩形复用主交互热区", () => {
    expect(resolveAvatarActivationRect(petBounds)).toEqual({
      x: 250,
      y: 210,
      width: 79,
      height: 170
    });
    expect(resolveAvatarActivationRect(null)).toBeNull();
  });

  it("人物外接边界内但小热区外不会触发首次激活，小热区内才会触发", () => {
    expect(
      isAvatarActivationPoint({
        point: { x: 220, y: 320 },
        petBounds
      })
    ).toBe(false);
    expect(
      isAvatarActivationPoint({
        point: { x: 260, y: 520 },
        petBounds
      })
    ).toBe(false);
    expect(
      isAvatarActivationPoint({
        point: { x: 280, y: 260 },
        petBounds
      })
    ).toBe(true);
  });

  it("小热区、按钮和通行走廊都算作可交互区域", () => {
    const buttonRect = resolveAvatarActionRailRect({
      petBounds,
      viewportWidth: 920,
      viewportHeight: 760
    });
    const resizeFrameRect = resolveAvatarResizeFrameRect(petBounds);

    expect(
      isAvatarInteractionPoint({
        point: { x: 280, y: 260 },
        petBounds,
        buttonRect,
        resizeFrameRect
      })
    ).toBe(true);
    expect(
      isAvatarInteractionPoint({
        point: { x: 420, y: 140 },
        petBounds,
        buttonRect,
        resizeFrameRect
      })
    ).toBe(true);
    expect(
      isAvatarInteractionPoint({
        point: { x: 405, y: 150 },
        petBounds,
        buttonRect,
        resizeFrameRect
      })
    ).toBe(true);
  });

  it("缩放框只在边缘和四角保持交互，中间透明区域不保持交互", () => {
    const resizeFrameRect = resolveAvatarResizeFrameRect(petBounds);

    expect(
      isAvatarInteractionPoint({
        point: { x: 172, y: 320 },
        petBounds: null,
        buttonRect: null,
        resizeFrameRect
      })
    ).toBe(true);
    expect(
      isAvatarInteractionPoint({
        point: { x: 190, y: 320 },
        petBounds: null,
        buttonRect: null,
        resizeFrameRect
      })
    ).toBe(false);
  });

  it("远离人物的透明窗口空白不会进入交互态", () => {
    const buttonRect = resolveAvatarActionRailRect({
      petBounds,
      viewportWidth: 920,
      viewportHeight: 760
    });
    const resizeFrameRect = resolveAvatarResizeFrameRect(petBounds);

    expect(
      isAvatarInteractionPoint({
        point: { x: 760, y: 150 },
        petBounds,
        buttonRect,
        resizeFrameRect
      })
    ).toBe(false);
    expect(
      isAvatarInteractionPoint({
        point: { x: 80, y: 520 },
        petBounds,
        buttonRect,
        resizeFrameRect
      })
    ).toBe(false);
  });

  it("缩放框只包围人物边界并留出可见提示空间", () => {
    expect(resolveAvatarResizeFrameRect(petBounds)).toEqual({
      x: 168,
      y: 108,
      width: 244,
      height: 524
    });
    expect(resolveAvatarResizeFrameRect(null)).toBeNull();
  });
});
