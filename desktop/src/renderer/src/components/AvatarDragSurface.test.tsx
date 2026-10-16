// @vitest-environment jsdom

import { createRoot } from "react-dom/client";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AvatarDragSurface } from "./AvatarDragSurface";

/**
 * TestPointerEvent 为 jsdom 补齐头像拖拽测试需要的最小 PointerEvent 能力。
 * 核心入参沿用 MouseEventInit，并额外接收 pointerId；预期输出是可被 React 指针事件监听识别的事件对象。
 * 边界异常由具体测试用例控制，避免在测试环境里引入浏览器实现差异。
 */
class TestPointerEvent extends MouseEvent {
  /**
   * pointerId 用于模拟浏览器指针捕获链路，保证拖拽开始和结束使用同一根指针。
   */
  pointerId: number;

  constructor(type: string, init: MouseEventInit & { pointerId?: number } = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
  }
}

describe("AvatarDragSurface", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.stubGlobal("PointerEvent", TestPointerEvent);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.unstubAllGlobals();
  });

  it("鼠标左键拖拽时，把屏幕坐标连续交给头像窗口拖拽回调", () => {
    const onDragStart = vi.fn();
    const onDragMove = vi.fn();
    const onDragEnd = vi.fn();
    const onTap = vi.fn();

    act(() => {
      root.render(
        <AvatarDragSurface
          bounds={{ x: 10, y: 20, width: 120, height: 180 }}
          onDragStart={onDragStart}
          onDragMove={onDragMove}
          onDragEnd={onDragEnd}
          onTap={onTap}
        />
      );
    });

    const dragSurface = container.querySelector(".avatar-drag-surface") as HTMLDivElement;
    dragSurface.setPointerCapture = vi.fn();
    dragSurface.releasePointerCapture = vi.fn();

    act(() => {
      dragSurface.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          button: 0,
          pointerId: 7,
          screenX: 300,
          screenY: 340
        })
      );
      dragSurface.dispatchEvent(
        new PointerEvent("pointermove", {
          bubbles: true,
          pointerId: 7,
          screenX: 330,
          screenY: 370
        })
      );
      dragSurface.dispatchEvent(
        new PointerEvent("pointerup", {
          bubbles: true,
          pointerId: 7
        })
      );
      dragSurface.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onDragStart).toHaveBeenCalledWith({ screenX: 300, screenY: 340 });
    expect(onDragMove).toHaveBeenCalledWith({ screenX: 330, screenY: 370 });
    expect(onDragEnd).toHaveBeenCalledOnce();
    expect(onTap).not.toHaveBeenCalled();
  });

  it("没有产生明显位移时，把按下抬起解释为头像点击", () => {
    const onDragStart = vi.fn();
    const onDragEnd = vi.fn();
    const onTap = vi.fn();

    act(() => {
      root.render(
        <AvatarDragSurface
          bounds={{ x: 10, y: 20, width: 120, height: 180 }}
          onDragStart={onDragStart}
          onDragEnd={onDragEnd}
          onTap={onTap}
        />
      );
    });

    const dragSurface = container.querySelector(".avatar-drag-surface") as HTMLDivElement;
    dragSurface.setPointerCapture = vi.fn();
    dragSurface.releasePointerCapture = vi.fn();

    act(() => {
      dragSurface.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          button: 0,
          pointerId: 8,
          screenX: 300,
          screenY: 340
        })
      );
      dragSurface.dispatchEvent(
        new PointerEvent("pointerup", {
          bubbles: true,
          button: 0,
          pointerId: 8,
          screenX: 300,
          screenY: 340
        })
      );
      dragSurface.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onDragStart).toHaveBeenCalledWith({ screenX: 300, screenY: 340 });
    expect(onDragEnd).toHaveBeenCalledOnce();
    expect(onTap).toHaveBeenCalledOnce();
  });

  it("辅助输入只产生 click 时仍可触发点击回调", () => {
    const onTap = vi.fn();

    act(() => {
      root.render(
        <AvatarDragSurface
          bounds={{ x: 10, y: 20, width: 120, height: 180 }}
          onTap={onTap}
        />
      );
    });

    const dragSurface = container.querySelector(".avatar-drag-surface") as HTMLDivElement;
    act(() => dragSurface.dispatchEvent(new MouseEvent("click", { bubbles: true })));

    expect(onTap).toHaveBeenCalledOnce();
  });

  it("没有 pointermove 但抬起点已越过阈值时不触发轻点", () => {
    const onTap = vi.fn();

    act(() => {
      root.render(
        <AvatarDragSurface
          bounds={{ x: 10, y: 20, width: 120, height: 180 }}
          onTap={onTap}
        />
      );
    });

    const dragSurface = container.querySelector(".avatar-drag-surface") as HTMLDivElement;
    dragSurface.setPointerCapture = vi.fn();
    dragSurface.releasePointerCapture = vi.fn();

    act(() => {
      dragSurface.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          button: 0,
          pointerId: 9,
          screenX: 300,
          screenY: 340
        })
      );
      dragSurface.dispatchEvent(
        new PointerEvent("pointerup", {
          bubbles: true,
          button: 0,
          pointerId: 9,
          screenX: 330,
          screenY: 370
        })
      );
      dragSurface.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onTap).not.toHaveBeenCalled();
  });
});
