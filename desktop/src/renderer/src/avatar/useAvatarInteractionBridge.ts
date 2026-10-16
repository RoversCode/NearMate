import { useEffect, useRef } from "react";
import type React from "react";
import type { AvatarDragRegion } from "./avatarDragRegion";
import type { CompanionAvatarBridge } from "./avatarBridge";
import {
  isAvatarActivationPoint,
  isAvatarInteractionPoint
} from "./avatarInteractionGeometry";
import type { AvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";

/**
 * AVATAR_INTERACTIVE_TARGET_SELECTOR 定义“哪些 DOM 才算角色窗真实可交互热区”。
 * 只有命中这些元素时，renderer 才会要求主进程收回鼠标穿透；
 * 如果鼠标只是落在放大后新增的透明空白区，就应继续保持 click-through。
 */
const AVATAR_INTERACTIVE_TARGET_SELECTOR = '[data-avatar-interactive="true"]';
const AVATAR_CONTROLS_HIDE_DELAY_MS = 1500;

type UseAvatarInteractionBridgeInput = {
  avatarBridge?: CompanionAvatarBridge;
  controlsVisible: boolean;
  dispatch(action: { type: "pointer.enter" } | { type: "pointer.leave" }): void;
  panelButtonRect: DOMRect | AvatarDragRegion | null;
  petBounds: AvatarDragRegion | null;
  presentationProfile: AvatarPresentationProfile;
  resizeFrameRect: DOMRect | AvatarDragRegion | null;
};

/**
 * useAvatarInteractionBridge 管理头像窗口悬停态、鼠标穿透和本地控制隐藏计时器。
 *
 * Args:
 *   input: 当前人物边界、控制几何、展示档案和 preload bridge。
 *
 * Returns:
 *   交给根节点、按钮和舞台回调用的交互函数。
 */
export function useAvatarInteractionBridge({
  avatarBridge,
  controlsVisible,
  dispatch,
  panelButtonRect,
  petBounds,
  presentationProfile,
  resizeFrameRect
}: UseAvatarInteractionBridgeInput) {
  const interactionEngagedRef = useRef(false);
  const bridgeInteractionHeldRef = useRef(false);
  const controlsHideTimerRef = useRef<number | null>(null);

  /**
   * readInteractiveTarget 把任意事件 target 安全收敛成可做 closest 查询的 Element。
   * 这样无论命中的是按钮本身、其子节点还是文本包装节点，都能统一落回最近的交互热区元素判断。
   */
  const readInteractiveTarget = (target: EventTarget | null): Element | null => {
    if (typeof Element !== "undefined" && target instanceof Element) {
      return target;
    }

    if (typeof Node !== "undefined" && target instanceof Node) {
      return target.parentElement;
    }

    return null;
  };

  /**
   * isInteractivePointerTarget 负责判断当前鼠标是否命中了真实交互热区。
   * 透明空白区域不会命中这个选择器，因此不会再错误触发整窗悬停与整窗拦截点击。
   */
  const isInteractivePointerTarget = (target: EventTarget | null): boolean => {
    const interactiveTarget = readInteractiveTarget(target);
    return !!interactiveTarget?.closest(AVATAR_INTERACTIVE_TARGET_SELECTOR);
  };

  /**
   * syncBridgeInteractionState 统一收敛主进程的鼠标穿透开关。
   * 指针命中真实交互区时暂时收回穿透；离开后明确恢复，避免透明放大区长期挡住桌面。
   */
  const syncBridgeInteractionState = () => {
    const shouldHoldBridgeInteraction = interactionEngagedRef.current;

    if (shouldHoldBridgeInteraction === bridgeInteractionHeldRef.current) {
      return;
    }

    bridgeInteractionHeldRef.current = shouldHoldBridgeInteraction;

    if (shouldHoldBridgeInteraction) {
      avatarBridge?.hoverEnter();
      return;
    }

    avatarBridge?.hoverLeave();
  };

  /**
   * clearControlsHideTimer 清理按钮延迟隐藏计时器。
   */
  const clearControlsHideTimer = () => {
    if (controlsHideTimerRef.current === null) {
      return;
    }

    window.clearTimeout(controlsHideTimerRef.current);
    controlsHideTimerRef.current = null;
  };

  /**
   * hideAvatarControlsImmediately 立即收起本地悬浮控制。
   */
  const hideAvatarControlsImmediately = () => {
    clearControlsHideTimer();
    dispatch({ type: "pointer.leave" });
  };

  /**
   * scheduleControlsHide 在用户离开真实热区后保留短暂点击窗口。
   */
  const scheduleControlsHide = () => {
    clearControlsHideTimer();
    controlsHideTimerRef.current = window.setTimeout(() => {
      controlsHideTimerRef.current = null;
      dispatch({ type: "pointer.leave" });
    }, AVATAR_CONTROLS_HIDE_DELAY_MS);
  };

  /**
   * Electron 的 forward 模式对 mousemove 比 mouseenter 更稳定。
   * 这里一旦收到头像窗内的鼠标移动，就立刻通知主进程收回鼠标穿透，保证后续点击权限气泡和模型拖拽都能命中当前窗口。
   */
  const engageAvatarInteraction = () => {
    clearControlsHideTimer();

    if (!interactionEngagedRef.current) {
      interactionEngagedRef.current = true;
      dispatch({ type: "pointer.enter" });
    }

    syncBridgeInteractionState();
  };

  /**
   * 指针真正离开头像窗后，再把本地悬停态和主进程穿透态一起收回。
   * 这样既能保持原有 pass-through 设计，也不会在窗口外残留交互占用。
   */
  const releaseAvatarInteraction = () => {
    if (interactionEngagedRef.current) {
      interactionEngagedRef.current = false;
      scheduleControlsHide();
    }

    syncBridgeInteractionState();
  };

  /**
   * syncAvatarInteractionFromPointerEvent 会在每次 mouse enter / move 时重新判断当前命中的真实热区。
   * 一旦鼠标从模型或边框移回窗口里的透明空白区，就立刻释放交互并恢复 click-through。
   */
  const syncAvatarInteractionFromPointerEvent = (event: React.MouseEvent<HTMLElement>) => {
    if (isInteractivePointerTarget(event.target)) {
      engageAvatarInteraction();
      return;
    }

    const pointerPoint = {
      x: event.clientX,
      y: event.clientY
    };

    if (!interactionEngagedRef.current && !controlsVisible) {
      if (
        isAvatarActivationPoint({
          point: pointerPoint,
          petBounds,
          interactionHotspot: presentationProfile.layout.interactionHotspot
        })
      ) {
        engageAvatarInteraction();
        return;
      }

      releaseAvatarInteraction();
      return;
    }

    const shouldKeepControlGeometry =
      interactionEngagedRef.current ||
      controlsVisible;
    const isGeometryInteractive = isAvatarInteractionPoint({
      point: pointerPoint,
      petBounds,
      interactionHotspot: presentationProfile.layout.interactionHotspot,
      buttonRect: shouldKeepControlGeometry ? panelButtonRect : null,
      resizeFrameRect: shouldKeepControlGeometry ? resizeFrameRect : null
    });

    if (isGeometryInteractive) {
      engageAvatarInteraction();
      return;
    }

    releaseAvatarInteraction();
  };

  useEffect(() => {
    return () => {
      clearControlsHideTimer();
    };
  }, []);

  return {
    hideAvatarControlsImmediately,
    releaseAvatarInteraction,
    syncAvatarInteractionFromPointerEvent
  };
}
