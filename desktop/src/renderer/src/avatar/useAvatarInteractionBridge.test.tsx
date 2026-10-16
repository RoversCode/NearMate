// @vitest-environment jsdom

import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getAvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import type { CompanionAvatarBridge } from "./avatarBridge";
import { useAvatarInteractionBridge } from "./useAvatarInteractionBridge";

type InteractionHookResult = ReturnType<typeof useAvatarInteractionBridge>;

function createBridge(): CompanionAvatarBridge {
  return {
    applyPresentationProfile: vi.fn(),
    endDrag: vi.fn(),
    endResize: vi.fn(),
    hoverEnter: vi.fn(),
    hoverLeave: vi.fn(),
    moveDrag: vi.fn(),
    moveResize: vi.fn(),
    onModelSwitchRequested: vi.fn(() => () => undefined),
    openPanel: vi.fn(),
    reportDiagnostic: vi.fn(),
    reportModelSwitchState: vi.fn(),
    runtime: {
      controlGateway: {
        httpBaseUrl: "http://127.0.0.1:8765",
        websocketUrl: "ws://127.0.0.1:8765/ws/control"
      },
      live2d: {
        defaultModelPath: "",
        catalog: []
      }
    },
    startDrag: vi.fn(),
    startResize: vi.fn()
  };
}

type TestHarnessProps = {
  avatarBridge: CompanionAvatarBridge;
  controlsVisible?: boolean;
  onHookResult(result: InteractionHookResult): void;
  onPointerAction(action: { type: "pointer.enter" } | { type: "pointer.leave" }): void;
};

function TestHarness({
  avatarBridge,
  controlsVisible = false,
  onHookResult,
  onPointerAction
}: TestHarnessProps) {
  const hookResult = useAvatarInteractionBridge({
    avatarBridge,
    controlsVisible,
    dispatch: onPointerAction,
    panelButtonRect: {
      x: 320,
      y: 40,
      width: 44,
      height: 44
    },
    petBounds: {
      x: 180,
      y: 120,
      width: 220,
      height: 500
    },
    presentationProfile: getAvatarPresentationProfile("Haru"),
    resizeFrameRect: {
      x: 180,
      y: 120,
      width: 220,
      height: 500
    }
  });

  onHookResult(hookResult);
  return createElement(
    "main",
    {
      className: "avatar-shell",
      onMouseLeave: hookResult.releaseAvatarInteraction,
      onMouseMove: hookResult.syncAvatarInteractionFromPointerEvent
    },
    createElement("button", {
      className: "interactive-button",
      "data-avatar-interactive": "true",
      type: "button"
    })
  );
}

describe("useAvatarInteractionBridge", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.useRealTimers();
  });

  it("指针命中真实交互元素时收回主进程鼠标穿透并进入悬停态", () => {
    const bridge = createBridge();
    const dispatch = vi.fn();

    act(() => {
      root.render(
        createElement(TestHarness, {
          avatarBridge: bridge,
          onHookResult: () => undefined,
          onPointerAction: dispatch
        })
      );
    });

    const interactiveButton = container.querySelector(".interactive-button");

    act(() => {
      interactiveButton?.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    });

    expect(dispatch).toHaveBeenCalledWith({ type: "pointer.enter" });
    expect(bridge.hoverEnter).toHaveBeenCalledOnce();
    expect(bridge.hoverLeave).not.toHaveBeenCalled();
  });

  it("指针从热区回到透明区域时延迟隐藏控制并恢复鼠标穿透", () => {
    vi.useFakeTimers();
    const bridge = createBridge();
    const dispatch = vi.fn();

    act(() => {
      root.render(
        createElement(TestHarness, {
          avatarBridge: bridge,
          onHookResult: () => undefined,
          onPointerAction: dispatch
        })
      );
    });

    const interactiveButton = container.querySelector(".interactive-button");
    const avatarShell = container.querySelector(".avatar-shell");

    act(() => {
      interactiveButton?.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
      avatarShell?.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          clientX: 20,
          clientY: 20
        })
      );
    });

    expect(bridge.hoverEnter).toHaveBeenCalledOnce();
    expect(bridge.hoverLeave).toHaveBeenCalledOnce();
    expect(dispatch).not.toHaveBeenCalledWith({ type: "pointer.leave" });

    act(() => {
      vi.advanceTimersByTime(1500);
    });

    expect(dispatch).toHaveBeenCalledWith({ type: "pointer.leave" });
  });

});
