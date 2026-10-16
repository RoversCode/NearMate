import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";
import type { AvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import type { AvatarDragPoint } from "../components/AvatarDragSurface";
import { estimateBubbleTextHeight } from "../components/bubbleLayoutShared";
import { resolveBottomBubbleStyle } from "../components/bottomBubbleStyle";
import type { AvatarDragRegion } from "./avatarDragRegion";
import type { CompanionAvatarBridge } from "./avatarBridge";
import {
  isAvatarInteractionPoint,
  resolveAvatarActionRailRect,
  resolveAvatarInteractionHotspotRect,
  resolveAvatarResizeFrameRect
} from "./avatarInteractionGeometry";
import { useAvatarVisibleArea } from "./useAvatarVisibleArea";

const AVATAR_INTERACTIVE_TARGET_SELECTOR = '[data-avatar-interactive="true"]';

type UseAvatarPetSurfaceInput = {
  avatarBridge?: Pick<
    CompanionAvatarBridge,
    | "endDrag"
    | "endResize"
    | "hoverEnter"
    | "hoverLeave"
    | "moveDrag"
    | "moveResize"
    | "onVisibleAreaChanged"
    | "reportPetBounds"
    | "requestVisibleArea"
    | "startDrag"
    | "startResize"
  >;
  bubbleText?: string;
  controlsVisible: boolean;
  measureBubbleHeight?(bubbleWidth: number): number;
  presentationProfile: AvatarPresentationProfile;
  resizeFrameInteractiveWhenHidden?: boolean;
  onInteractionEnter?(): void;
  onInteractionLeave?(): void;
};

/**
 * useAvatarPetSurface 管理桌宠窗口共用的身体边界、可见区、拖拽缩放和气泡定位输入。
 *
 * Args:
 *   input: avatar bridge、人物展示档案、气泡文本和本地控制可见性。
 *
 * Returns:
 *   Avatar ready/bootstrap 壳都可直接消费的桌宠交互 surface。
 */
export function useAvatarPetSurface(input: UseAvatarPetSurfaceInput) {
  const [petBounds, setPetBounds] = useState<AvatarDragRegion | null>(null);
  const bridgeInteractionHeldRef = useRef(false);
  const visibleArea = useAvatarVisibleArea(input.avatarBridge);
  const panelButtonRect = resolveAvatarActionRailRect({
    petBounds,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight
  });
  const resizeFrameRect = resolveAvatarResizeFrameRect(petBounds);
  const avatarInteractionHotspotBounds = resolveAvatarInteractionHotspotRect(
    petBounds,
    input.presentationProfile.layout.interactionHotspot
  );
  const bottomBubbleStyle = resolveBottomBubbleStyle({
    petBounds,
    footAnchor: input.presentationProfile.layout.footAnchor,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    visibleBottom: visibleArea?.visibleBottom,
    measureHeight: bubbleWidth =>
      input.measureBubbleHeight?.(bubbleWidth) ??
      estimateBubbleTextHeight(input.bubbleText ?? "", bubbleWidth, 1)
  });

  useEffect(() => {
    return () => {
      if (!bridgeInteractionHeldRef.current) {
        return;
      }

      bridgeInteractionHeldRef.current = false;
      input.avatarBridge?.hoverLeave();
    };
  }, [input.avatarBridge]);

  const setBridgeInteractionHeld = useCallback(
    (shouldHoldBridgeInteraction: boolean) => {
      if (bridgeInteractionHeldRef.current === shouldHoldBridgeInteraction) {
        return;
      }

      bridgeInteractionHeldRef.current = shouldHoldBridgeInteraction;

      if (shouldHoldBridgeInteraction) {
        input.avatarBridge?.hoverEnter();
        input.onInteractionEnter?.();
        return;
      }

      input.avatarBridge?.hoverLeave();
      input.onInteractionLeave?.();
    },
    [input.avatarBridge, input.onInteractionEnter, input.onInteractionLeave]
  );

  const syncInteractionFromPointerEvent = useCallback(
    (event: React.MouseEvent<HTMLElement>) => {
      const interactiveTarget = readInteractiveTarget(event.target);

      if (interactiveTarget?.closest(AVATAR_INTERACTIVE_TARGET_SELECTOR)) {
        setBridgeInteractionHeld(true);
        return;
      }

      const shouldExposeControlGeometry =
        input.controlsVisible ||
        input.resizeFrameInteractiveWhenHidden ||
        bridgeInteractionHeldRef.current;
      const isGeometryInteractive = isAvatarInteractionPoint({
        point: {
          x: event.clientX,
          y: event.clientY
        },
        petBounds,
        interactionHotspot: input.presentationProfile.layout.interactionHotspot,
        buttonRect: input.controlsVisible ? panelButtonRect : null,
        resizeFrameRect: shouldExposeControlGeometry ? resizeFrameRect : null
      });

      setBridgeInteractionHeld(isGeometryInteractive);
    },
    [
      input.controlsVisible,
      input.presentationProfile.layout.interactionHotspot,
      input.resizeFrameInteractiveWhenHidden,
      panelButtonRect,
      petBounds,
      resizeFrameRect,
      setBridgeInteractionHeld
    ]
  );

  return {
    avatarInteractionHotspotBounds,
    bottomBubbleStyle,
    panelButtonRect,
    petBounds,
    resizeFrameRect,
    setBridgeInteractionHeld,
    syncInteractionFromPointerEvent,
    visibleArea,
    updatePetBounds(bounds: AvatarDragRegion | null): void {
      setPetBounds(bounds);
      input.avatarBridge?.reportPetBounds?.(bounds);
    },
    dragHandlers: {
      onDragStart(point: AvatarDragPoint): void {
        input.avatarBridge?.startDrag(point);
      },
      onDragMove(point: AvatarDragPoint): void {
        input.avatarBridge?.moveDrag(point);
      },
      onDragEnd(): void {
        input.avatarBridge?.endDrag();
      }
    },
    resizeHandlers: {
      onResizeStart(
        direction: Parameters<NonNullable<CompanionAvatarBridge["startResize"]>>[0],
        point: AvatarDragPoint
      ): void {
        input.avatarBridge?.startResize(direction, point);
      },
      onResizeMove(point: AvatarDragPoint): void {
        input.avatarBridge?.moveResize(point);
      },
      onResizeEnd(): void {
        input.avatarBridge?.endResize();
      }
    }
  };
}

/**
 * readInteractiveTarget 把任意事件 target 安全收敛成可做 closest 查询的 Element。
 *
 * Args:
 *   target: React 鼠标事件的原始目标。
 *
 * Returns:
 *   可查询最近交互热区的 DOM 元素；无法收窄时返回 null。
 */
function readInteractiveTarget(target: EventTarget | null): Element | null {
  if (typeof Element !== "undefined" && target instanceof Element) {
    return target;
  }

  if (typeof Node !== "undefined" && target instanceof Node) {
    return target.parentElement;
  }

  return null;
}
