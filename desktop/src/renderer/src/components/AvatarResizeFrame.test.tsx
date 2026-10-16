// @vitest-environment jsdom

import { readFileSync } from "node:fs";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AvatarResizeFrame } from "./AvatarResizeFrame";

const rendererStyles = [
  "src/renderer/src/styles.css",
  "src/renderer/src/styles/base.css",
  "src/renderer/src/styles/avatar.css",
  "src/renderer/src/styles/avatar-stage.css",
  "src/renderer/src/styles/avatar-controls.css",
  "src/renderer/src/styles/avatar-bubbles.css",
  "src/renderer/src/styles/avatar-bootstrap.css",
  "src/renderer/src/styles/panel.css",
  "src/renderer/src/styles/chat.css",
  "src/renderer/src/styles/bootstrap-blocked.css",
  "src/renderer/src/styles/model-picker.css"
]
  .map((stylePath) => readFileSync(stylePath, "utf8"))
  .join("\n");

/**
 * readCssRuleBody 读取指定选择器的 CSS 声明体。
 * @param selector 需要检查的 CSS 选择器。
 * @returns 选择器对应的声明内容；不存在时返回空字符串。
 */
function readCssRuleBody(selector: string): string {
  const rulePattern = /(?<selectors>[^{}]+)\{(?<body>[^{}]*)\}/g;
  let match: RegExpExecArray | null;

  while ((match = rulePattern.exec(rendererStyles))) {
    const selectorList =
      match.groups?.selectors.split(",").map(ruleSelector => ruleSelector.trim()) ?? [];

    if (selectorList.includes(selector)) {
      return match.groups?.body ?? "";
    }
  }

  return "";
}

/**
 * TestPointerEvent 为 jsdom 补齐缩放热区测试所需的最小 PointerEvent 能力。
 * 核心入参沿用 MouseEventInit，并额外补充 pointerId；输出是可被 React 指针事件系统消费的事件实例。
 * 边界异常由具体测试控制，避免把浏览器实现差异混进业务断言。
 */
class TestPointerEvent extends MouseEvent {
  /**
   * pointerId 用于模拟浏览器在同一轮指针序列中的唯一标识。
   */
  pointerId: number;

  constructor(type: string, init: MouseEventInit & { pointerId?: number } = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
  }
}

describe("AvatarResizeFrame", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  const bounds = { x: 180, y: 120, width: 220, height: 500 };

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

  it("右下角热区在 pointerdown、move、up 时，会把方向与屏幕坐标交给上层", () => {
    const onResizeStart = vi.fn();
    const onResizeMove = vi.fn();
    const onResizeEnd = vi.fn();

    act(() => {
      root.render(
        <AvatarResizeFrame
          bounds={bounds}
          visible={true}
          onResizeStart={onResizeStart}
          onResizeMove={onResizeMove}
          onResizeEnd={onResizeEnd}
        />
      );
    });

    const handle = container.querySelector(
      '[data-resize-direction="se"]'
    ) as HTMLDivElement;
    handle.setPointerCapture = vi.fn();
    handle.releasePointerCapture = vi.fn();

    act(() => {
      handle.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          button: 0,
          pointerId: 11,
          screenX: 500,
          screenY: 620
        })
      );
      handle.dispatchEvent(
        new PointerEvent("pointermove", {
          bubbles: true,
          pointerId: 11,
          screenX: 530,
          screenY: 660
        })
      );
      handle.dispatchEvent(
        new PointerEvent("pointerup", {
          bubbles: true,
          pointerId: 11
        })
      );
    });

    expect(onResizeStart).toHaveBeenCalledWith("se", {
      screenX: 500,
      screenY: 620
    });
    expect(onResizeMove).toHaveBeenCalledWith({
      screenX: 530,
      screenY: 660
    });
    expect(onResizeEnd).toHaveBeenCalledOnce();
  });

  it("非左键不会触发缩放开始", () => {
    const onResizeStart = vi.fn();

    act(() => {
      root.render(
        <AvatarResizeFrame bounds={bounds} visible={true} onResizeStart={onResizeStart} />
      );
    });

    const handle = container.querySelector(
      '[data-resize-direction="se"]'
    ) as HTMLDivElement;

    act(() => {
      handle.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          button: 2,
          pointerId: 13,
          screenX: 300,
          screenY: 420
        })
      );
    });

    expect(onResizeStart).not.toHaveBeenCalled();
  });

  it("pointercancel 也会触发结束回调", () => {
    const onResizeEnd = vi.fn();

    act(() => {
      root.render(<AvatarResizeFrame bounds={bounds} visible={true} onResizeEnd={onResizeEnd} />);
    });

    const handle = container.querySelector(
      '[data-resize-direction="e"]'
    ) as HTMLDivElement;
    handle.setPointerCapture = vi.fn();
    handle.releasePointerCapture = vi.fn();

    act(() => {
      handle.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          button: 0,
          pointerId: 17,
          screenX: 320,
          screenY: 410
        })
      );
      handle.dispatchEvent(
        new PointerEvent("pointercancel", {
          bubbles: true,
          pointerId: 17
        })
      );
    });

    expect(onResizeEnd).toHaveBeenCalledOnce();
  });

  it("缩放框围绕人物边界定位，而不是覆盖整个透明窗口", () => {
    act(() => {
      root.render(<AvatarResizeFrame bounds={bounds} visible={true} />);
    });

    const frame = container.querySelector(".avatar-resize-frame") as HTMLDivElement;

    expect(frame.style.left).toBe("168px");
    expect(frame.style.top).toBe("108px");
    expect(frame.style.width).toBe("244px");
    expect(frame.style.height).toBe("524px");
    expect(frame.dataset.visible).toBe("true");
  });

  it("人物边界不可用时不渲染缩放热区，避免透明窗口拦截桌面点击", () => {
    act(() => {
      root.render(<AvatarResizeFrame bounds={null} visible={true} />);
    });

    expect(container.querySelector(".avatar-resize-frame")).toBeNull();
    expect(container.querySelector("[data-resize-direction]")).toBeNull();
  });

  it("缩放提示只显示四角角标，不给人物框绘制整圈边线", () => {
    const frameRule = readCssRuleBody(".avatar-resize-frame");
    const visibleFrameRule = readCssRuleBody('.avatar-resize-frame[data-visible="true"]');
    const cornerRule = readCssRuleBody(
      '.avatar-resize-frame[data-visible="true"] .avatar-resize-handle-ne::after'
    );

    expect(frameRule).not.toMatch(/\bborder(?:-color)?:/);
    expect(visibleFrameRule).not.toMatch(/\bborder(?:-color)?:/);
    expect(cornerRule).toMatch(/\bborder-color:/);
  });
});
