import { describe, expect, it, vi } from "vitest";
import { createChatWindowResizeController } from "./chatWindowResize";

describe("createChatWindowResizeController", () => {
  it("东南角缩放遵守最小尺寸并记住结果", () => {
    let bounds = { x: 100, y: 80, width: 900, height: 640 };
    const rememberBounds = vi.fn();
    const window = {
      getBounds: vi.fn(() => ({ ...bounds })),
      setBounds: vi.fn(nextBounds => { bounds = { ...nextBounds }; })
    };
    const controller = createChatWindowResizeController(
      rememberBounds,
      () => ({ x: 0, y: 0, width: 1920, height: 1080 })
    );

    controller.startResize(window as never, { edge: "se", screenX: 820, screenY: 640 });
    controller.moveResize(window as never, { screenX: 650, screenY: 500 });
    controller.endResize(window as never);

    expect(bounds).toEqual({ x: 100, y: 80, width: 760, height: 520 });
    expect(rememberBounds).toHaveBeenLastCalledWith(bounds);
  });

  it("西北角缩放会移动原点并限制在工作区", () => {
    let bounds = { x: 100, y: 100, width: 900, height: 640 };
    const window = {
      getBounds: vi.fn(() => ({ ...bounds })),
      setBounds: vi.fn(nextBounds => { bounds = { ...nextBounds }; })
    };
    const controller = createChatWindowResizeController(
      vi.fn(),
      () => ({ x: 0, y: 0, width: 1280, height: 720 })
    );

    controller.startResize(window as never, { edge: "nw", screenX: 100, screenY: 100 });
    controller.moveResize(window as never, { screenX: -100, screenY: -100 });

    expect(bounds).toEqual({ x: 0, y: 0, width: 1000, height: 720 });
  });
});
