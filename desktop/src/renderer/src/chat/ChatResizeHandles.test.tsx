// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatResizeHandles } from "./ChatResizeHandles";

describe("ChatResizeHandles", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("东南热区只向 preload 桥接转发缩放手势", () => {
    const startChatResize = vi.fn();
    const moveChatResize = vi.fn();
    const endChatResize = vi.fn();
    act(() => {
      root.render(<ChatResizeHandles bridge={{ startChatResize, moveChatResize, endChatResize }} />);
    });
    const handle = container.querySelector('[data-edge="se"]') as HTMLDivElement;
    let captured = false;
    handle.setPointerCapture = vi.fn(() => { captured = true; });
    handle.hasPointerCapture = vi.fn(() => captured);
    handle.releasePointerCapture = vi.fn(() => { captured = false; });

    act(() => {
      dispatchPointer(handle, "pointerdown", { screenX: 700, screenY: 550 });
      dispatchPointer(handle, "pointermove", { screenX: 760, screenY: 600 });
      dispatchPointer(handle, "pointerup", { screenX: 760, screenY: 600 });
    });

    expect(startChatResize).toHaveBeenCalledWith({ edge: "se", screenX: 700, screenY: 550 });
    expect(moveChatResize).toHaveBeenCalledWith({ screenX: 760, screenY: 600 });
    expect(endChatResize).toHaveBeenCalledOnce();
  });
});

function dispatchPointer(
  target: Element,
  type: "pointerdown" | "pointermove" | "pointerup",
  point: { screenX: number; screenY: number }
): void {
  target.dispatchEvent(new MouseEvent(type, {
    bubbles: true,
    button: 0,
    screenX: point.screenX,
    screenY: point.screenY
  }));
}
