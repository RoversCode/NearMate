import type { BrowserWindow, screen as electronScreen } from "electron";
import type { AvatarPetBounds } from "../../shared/live2dModelTypes";
import type { AvatarModelLayoutStore } from "../stores/avatarModelLayoutStore";
import { createAvatarFirstShowGate } from "./avatarFirstShowGate";
import {
  createAvatarDragState,
  createAvatarResizeState,
  type AvatarDragState,
  type AvatarResizeState
} from "./avatarInteractionSessions";
import { createAvatarPetBoundsCache } from "./avatarPetBoundsState";
import { createAvatarPresentationController } from "./avatarPresentationState";
import { calculateResizedAvatarBounds } from "./avatarResizeBounds";
import { calculateAvatarVisibleArea } from "./avatarVisibleArea";
import {
  readAvatarResizeStartPayload,
  readDragPoint
} from "./avatarWindowInteractionPayloads";

/**
 * AvatarWindowInteractionController 描述主进程 avatar 交互模块的公开能力。
 */
export type AvatarWindowInteractionController = {
  /** 暂停首次显示，展示档案与计时兜底仍可登记显示请求；返回值为空。 */
  holdFirstShow(): void;
  /** 成功反馈结束后显示待交接的角色窗；返回值为空。 */
  releaseFirstShow(): void;
  scheduleFirstShow(avatarWindow: BrowserWindow): void;
  startDrag(avatarWindow: BrowserWindow, payload: unknown): boolean;
  moveDrag(avatarWindow: BrowserWindow, payload: unknown): boolean;
  endDrag(avatarWindow?: BrowserWindow): void;
  startResize(avatarWindow: BrowserWindow, payload: unknown): boolean;
  moveResize(avatarWindow: BrowserWindow, payload: unknown): boolean;
  endResize(avatarWindow?: BrowserWindow): void;
  requestVisibleArea(avatarWindow: BrowserWindow): ReturnType<typeof calculateAvatarVisibleArea>;
  applyPresentationProfile(avatarWindow: BrowserWindow, payload: unknown): boolean;
  rememberPendingPresentationProfile(payload: unknown): boolean;
  applyPendingPresentationProfile(avatarWindow: BrowserWindow): void;
  resetCurrentModelLayout(avatarWindow: BrowserWindow): boolean;
  rememberPetBounds(payload: unknown): boolean;
  getPetBounds(avatarWindow: BrowserWindow): AvatarPetBounds | null;
  reset(): void;
};

/**
 * createAvatarWindowInteractionController 创建 avatar 窗口交互控制器。
 *
 * @param screen Electron screen 模块的窄接口。
 * @param layoutStore 按人物保存窗口几何的仓储。
 * @returns 封装拖拽、缩放、可见区、首显档案和启动交接门闩的控制器。
 */
export function createAvatarWindowInteractionController(
  screen: Pick<typeof electronScreen, "getDisplayMatching">,
  layoutStore?: AvatarModelLayoutStore
): AvatarWindowInteractionController {
  let dragState: AvatarDragState | null = null;
  let resizeState: AvatarResizeState | null = null;

  const firstShowGate = createAvatarFirstShowGate();
  const petBoundsCache = createAvatarPetBoundsCache();

  /**
   * pushVisibleArea 计算角色窗口当前在显示器工作区内的可见矩形，并推送给 renderer。
   */
  function pushVisibleArea(avatarWindow: BrowserWindow): void {
    if (avatarWindow.isDestroyed() || avatarWindow.webContents.isDestroyed()) {
      return;
    }

    avatarWindow.webContents.send(
      "avatar:visible-area-changed",
      readVisibleArea(avatarWindow)
    );
  }

  /**
   * readVisibleArea 读取角色窗在当前显示器工作区内的可见范围。
   */
  function readVisibleArea(avatarWindow: BrowserWindow) {
    const bounds = avatarWindow.getBounds();
    const workArea = screen.getDisplayMatching(bounds).workArea;
    return calculateAvatarVisibleArea(bounds, workArea);
  }

  const presentationController = createAvatarPresentationController(
    screen,
    layoutStore,
    avatarWindow => firstShowGate.showIfNeeded(avatarWindow),
    pushVisibleArea
  );

  return {
    holdFirstShow() {
      firstShowGate.hold();
    },
    releaseFirstShow() {
      firstShowGate.release();
    },
    scheduleFirstShow(avatarWindow) {
      firstShowGate.schedule(avatarWindow);
    },
    startDrag(avatarWindow, payload) {
      const dragPoint = readDragPoint(payload);

      if (!dragPoint) {
        return false;
      }

      dragState = createAvatarDragState(avatarWindow, dragPoint);
      return true;
    },
    moveDrag(avatarWindow, payload) {
      const dragPoint = readDragPoint(payload);

      if (!dragState || !dragPoint) {
        return false;
      }

      const deltaX = dragPoint.screenX - dragState.startPoint.screenX;
      const deltaY = dragPoint.screenY - dragState.startPoint.screenY;

      avatarWindow.setBounds({
        x: dragState.startBounds.x + deltaX,
        y: dragState.startBounds.y + deltaY,
        width: dragState.startBounds.width,
        height: dragState.startBounds.height
      });
      pushVisibleArea(avatarWindow);
      return true;
    },
    endDrag(avatarWindow) {
      presentationController.saveCurrentLayout(avatarWindow);
      dragState = null;
    },
    startResize(avatarWindow, payload) {
      const resizeStartPayload = readAvatarResizeStartPayload(payload);

      if (!resizeStartPayload) {
        return false;
      }

      resizeState = createAvatarResizeState(
        avatarWindow,
        resizeStartPayload.direction,
        resizeStartPayload.point
      );
      return true;
    },
    moveResize(avatarWindow, payload) {
      const dragPoint = readDragPoint(payload);

      if (!resizeState || !dragPoint) {
        return false;
      }

      const currentPresentationProfile = presentationController.currentProfile();
      avatarWindow.setBounds(
        calculateResizedAvatarBounds({
          direction: resizeState.direction,
          startBounds: resizeState.startBounds,
          startPoint: resizeState.startPoint,
          currentPoint: dragPoint,
          minWidth: currentPresentationProfile.minWidth,
          minHeight: currentPresentationProfile.minHeight,
          aspectRatio:
            currentPresentationProfile.windowWidth /
            currentPresentationProfile.windowHeight
        })
      );
      pushVisibleArea(avatarWindow);
      return true;
    },
    endResize(avatarWindow) {
      presentationController.saveCurrentLayout(avatarWindow);
      resizeState = null;
    },
    requestVisibleArea(avatarWindow) {
      return readVisibleArea(avatarWindow);
    },
    applyPresentationProfile(avatarWindow, payload) {
      return presentationController.apply(avatarWindow, payload);
    },
    rememberPetBounds(payload) {
      return petBoundsCache.remember(payload);
    },
    getPetBounds(avatarWindow) {
      return petBoundsCache.readInScreen(avatarWindow);
    },
    rememberPendingPresentationProfile(payload) {
      return presentationController.rememberPending(payload);
    },
    applyPendingPresentationProfile(avatarWindow) {
      presentationController.applyPending(avatarWindow);
    },
    resetCurrentModelLayout(avatarWindow) {
      return presentationController.resetCurrentLayout(avatarWindow);
    },
    reset() {
      dragState = null;
      resizeState = null;
      firstShowGate.reset();
      presentationController.reset();
      petBoundsCache.reset();
    }
  };
}
