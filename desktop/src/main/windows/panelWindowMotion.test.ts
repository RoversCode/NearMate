import { afterEach, describe, expect, it, vi } from "vitest";
import { createPanelWindowMotion } from "./panelWindowMotion";

function createFakePanelWindow(input?: {
  visible?: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
}) {
  let visible = input?.visible ?? false;
  let bounds = input?.bounds ?? { x: 0, y: 0, width: 380, height: 560 };

  return {
    getBounds: vi.fn(() => ({ ...bounds })),
    setBounds: vi.fn((nextBounds: typeof bounds) => {
      bounds = { ...nextBounds };
    }),
    show: vi.fn(() => {
      visible = true;
    }),
    isVisible: vi.fn(() => visible),
    readBounds: () => ({ ...bounds })
  };
}

describe("createPanelWindowMotion", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("首次打开时直接定位并显示面板", () => {
    const panelWindow = createFakePanelWindow();
    const motion = createPanelWindowMotion(panelWindow);

    motion.openAt(
      { x: 321, y: 0, side: "right" },
      { width: 380, height: 560 }
    );

    expect(panelWindow.setBounds).toHaveBeenCalledOnce();
    expect(panelWindow.setBounds).toHaveBeenCalledWith({
      x: 321,
      y: 0,
      width: 380,
      height: 560
    });
    expect(panelWindow.show).toHaveBeenCalledOnce();
  });

  it("面板已显示时使用短缓动移动到目标位置", () => {
    vi.useFakeTimers();
    const panelWindow = createFakePanelWindow({
      visible: true,
      bounds: { x: 100, y: 120, width: 380, height: 560 }
    });
    const motion = createPanelWindowMotion(panelWindow, {
      durationMs: 100,
      frameMs: 20
    });

    motion.openAt(
      { x: 320, y: 20, side: "right" },
      { width: 380, height: 560 }
    );
    vi.advanceTimersByTime(100);

    expect(panelWindow.show).not.toHaveBeenCalled();
    expect(panelWindow.readBounds()).toEqual({
      x: 320,
      y: 20,
      width: 380,
      height: 560
    });
    expect(panelWindow.setBounds.mock.calls.length).toBeGreaterThan(1);
  });

  it("新的定位请求会接管上一轮未完成动画", () => {
    vi.useFakeTimers();
    const panelWindow = createFakePanelWindow({
      visible: true,
      bounds: { x: 100, y: 120, width: 380, height: 560 }
    });
    const motion = createPanelWindowMotion(panelWindow, {
      durationMs: 100,
      frameMs: 20
    });

    motion.openAt(
      { x: 320, y: 20, side: "right" },
      { width: 380, height: 560 }
    );
    vi.advanceTimersByTime(40);
    motion.openAt(
      { x: 40, y: 60, side: "left" },
      { width: 340, height: 400 }
    );
    vi.advanceTimersByTime(100);

    // 第二个目标必须成为最终位置，避免快速拖拽结束后面板被旧动画拉回去。
    expect(panelWindow.readBounds()).toEqual({
      x: 40,
      y: 60,
      width: 340,
      height: 400
    });
  });
});
