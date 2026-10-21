// @vitest-environment jsdom

import { createRoot } from "react-dom/client";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AvatarActionRail } from "./AvatarActionRail";

describe("AvatarActionRail", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  const setViewport = (width: number, height: number) => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: width
    });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      value: height
    });
  };

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setViewport(640, 760);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.restoreAllMocks();
  });

  it("有人物边界时，动作栏锚定人物右侧并渲染三个功能按钮", () => {
    act(() => {
      root.render(
        <AvatarActionRail
          visible={true}
          petBounds={{ x: 180, y: 120, width: 220, height: 500 }}
          onOpenChat={vi.fn()}
          onOpenModels={vi.fn()}
          onOpenConsole={vi.fn()}
        />
      );
    });

    const rail = container.querySelector(".avatar-action-rail") as HTMLElement;
    const buttons = Array.from(container.querySelectorAll("button"));

    expect(rail.style.left).toBe("412px");
    expect(rail.style.top).toBe("128px");
    expect(buttons).toHaveLength(3);
    expect(buttons.map(button => button.getAttribute("aria-label"))).toEqual([
      "打开聊天",
      "切换人物形象",
      "打开 Console"
    ]);
    expect(container.textContent).not.toContain("任务");
  });

  it("点击三个按钮时分别调用对应动作", () => {
    const onOpenChat = vi.fn();
    const onOpenModels = vi.fn();
    const onOpenConsole = vi.fn();

    act(() => {
      root.render(
        <AvatarActionRail
          visible={true}
          petBounds={{ x: 180, y: 120, width: 220, height: 500 }}
          onOpenChat={onOpenChat}
          onOpenModels={onOpenModels}
          onOpenConsole={onOpenConsole}
        />
      );
    });

    const buttons = Array.from(container.querySelectorAll("button"));
    act(() => {
      buttons[0].click();
      buttons[1].click();
      buttons[2].click();
    });

    expect(onOpenChat).toHaveBeenCalledOnce();
    expect(onOpenModels).toHaveBeenCalledOnce();
    expect(onOpenConsole).toHaveBeenCalledOnce();
  });

  it("人物贴近窗口边缘时，动作栏会被限制在窗口内", () => {
    act(() => {
      root.render(
        <AvatarActionRail
          visible={true}
          petBounds={{ x: 610, y: -10, width: 80, height: 420 }}
          onOpenChat={vi.fn()}
          onOpenModels={vi.fn()}
          onOpenConsole={vi.fn()}
        />
      );
    });

    const rail = container.querySelector(".avatar-action-rail") as HTMLElement;

    expect(rail.style.left).toBe("580px");
    expect(rail.style.top).toBe("16px");
  });

  it("支持动作的模型增加逗一逗按钮，完整四按钮矩形仍留在窗口内", () => {
    setViewport(360, 240);
    const onPlayReaction = vi.fn();
    act(() => {
      root.render(
        <AvatarActionRail
          visible={true}
          petBounds={{ x: 100, y: 180, width: 160, height: 220 }}
          onOpenChat={vi.fn()}
          onOpenModels={vi.fn()}
          onOpenConsole={vi.fn()}
          onPlayReaction={onPlayReaction}
        />
      );
    });

    const rail = container.querySelector(".avatar-action-rail") as HTMLElement;
    const buttons = Array.from(container.querySelectorAll("button"));
    expect(buttons).toHaveLength(4);
    expect(rail.style.height).toBe("194px");
    expect(rail.style.top).toBe("30px");
    expect(buttons[3].getAttribute("aria-label")).toBe("逗一逗");
    act(() => buttons[3].click());
    expect(onPlayReaction).toHaveBeenCalledOnce();
  });

  it("不可见时不渲染，避免隐藏按钮截获桌面点击", () => {
    act(() => {
      root.render(
        <AvatarActionRail
          visible={false}
          petBounds={{ x: 180, y: 120, width: 220, height: 500 }}
          onOpenChat={vi.fn()}
          onOpenModels={vi.fn()}
          onOpenConsole={vi.fn()}
        />
      );
    });

    expect(container.querySelector(".avatar-action-rail")).toBeNull();
  });
});
