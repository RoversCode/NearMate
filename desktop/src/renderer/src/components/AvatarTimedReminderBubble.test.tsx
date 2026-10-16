// @vitest-environment jsdom

import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getAvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import type { CompanionAvatarBridge } from "../avatar/avatarBridge";
import { useAvatarInteractionBridge } from "../avatar/useAvatarInteractionBridge";
import { AvatarTimedReminderBubble } from "./AvatarTimedReminderBubble";

const reminder = {
  reminder_id: "123e4567-e89b-42d3-a456-426614174020",
  content: "该去考试啦。",
  state: "ringing" as const,
  trigger_at: "2026-09-05T10:00:00+08:00",
  created_at: "2026-09-05T09:59:00+08:00",
  updated_at: "2026-09-05T10:00:00+08:00",
  finished_at: null,
  ring_count: 3,
  next_ring_at: null,
  finish_reason: null,
  can_modify: false,
  can_cancel: false,
  can_dismiss: true
};

function createBridge(setMouseIgnored: (ignored: boolean) => void): CompanionAvatarBridge {
  return {
    applyPresentationProfile: vi.fn(),
    endDrag: vi.fn(),
    endResize: vi.fn(),
    hoverEnter: vi.fn(() => setMouseIgnored(false)),
    hoverLeave: vi.fn(() => setMouseIgnored(true)),
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
      live2d: { defaultModelPath: "", catalog: [] }
    },
    startDrag: vi.fn(),
    startResize: vi.fn()
  };
}

describe("AvatarTimedReminderBubble 交互热区", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });

  afterEach(() => {
    act(() => root?.unmount());
    container?.remove();
  });

  it("鼠标移入气泡收回窗口穿透，点击知道了触发关闭，离开后恢复穿透", () => {
    let mouseIgnored = true;
    const bridge = createBridge(ignored => { mouseIgnored = ignored; });
    const onDismiss = vi.fn();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    function Harness() {
      const dispatch = vi.fn();
      const interaction = useAvatarInteractionBridge({
        avatarBridge: bridge,
        controlsVisible: false,
        dispatch,
        panelButtonRect: null,
        petBounds: { x: 180, y: 400, width: 220, height: 300 },
        presentationProfile: getAvatarPresentationProfile("Haru"),
        resizeFrameRect: null
      });
      return createElement(
        "main",
        {
          onMouseMove: interaction.syncAvatarInteractionFromPointerEvent,
          onMouseLeave: interaction.releaseAvatarInteraction
        },
        createElement(AvatarTimedReminderBubble, {
          reminders: [reminder],
          petBounds: { x: 180, y: 400, width: 220, height: 300 },
          anchor: getAvatarPresentationProfile("Haru").layout.headAnchor,
          requesting: false,
          errorMessage: null,
          onDismiss
        })
      );
    }

    act(() => root.render(createElement(Harness)));
    const bubble = container.querySelector(".avatar-timed-reminder-bubble") as HTMLElement;
    const button = bubble.querySelector("button") as HTMLButtonElement;
    expect(bubble.dataset.avatarInteractive).toBe("true");

    act(() => button.dispatchEvent(new MouseEvent("mousemove", { bubbles: true })));
    expect(mouseIgnored).toBe(false);
    act(() => button.click());
    expect(onDismiss).toHaveBeenCalledOnce();
    act(() => container.querySelector("main")?.dispatchEvent(
      new MouseEvent("mousemove", { bubbles: true, clientX: 1, clientY: 1 })
    ));
    expect(mouseIgnored).toBe(true);
  });

  it("请求关闭期间按钮保持禁用，避免重复提交", () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const onDismiss = vi.fn();
    act(() => root.render(createElement(AvatarTimedReminderBubble, {
      reminders: [reminder],
      petBounds: { x: 180, y: 400, width: 220, height: 300 },
      anchor: getAvatarPresentationProfile("Haru").layout.headAnchor,
      requesting: true,
      errorMessage: null,
      onDismiss
    })));
    const button = container.querySelector("button") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    act(() => button.click());
    expect(onDismiss).not.toHaveBeenCalled();
  });
});
