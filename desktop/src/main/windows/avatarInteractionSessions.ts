import type { BrowserWindow } from "electron";
import type { AvatarResizeDirection } from "../../shared/live2dModelTypes";
import type { DragPoint } from "./avatarWindowInteractionPayloads";

/**
 * AvatarDragState 保存一轮拖拽开始时的鼠标坐标和窗口边界。
 */
export type AvatarDragState = {
  startPoint: DragPoint;
  startBounds: ReturnType<BrowserWindow["getBounds"]>;
};

/**
 * AvatarResizeState 保存一轮缩放会话的起始方向、鼠标坐标与窗口边界。
 */
export type AvatarResizeState = {
  direction: AvatarResizeDirection;
  startPoint: DragPoint;
  startBounds: ReturnType<BrowserWindow["getBounds"]>;
};

/**
 * createAvatarDragState 创建拖拽会话状态。
 *
 * @param avatarWindow 当前角色窗口。
 * @param startPoint 拖拽起始屏幕坐标。
 * @returns 拖拽会话状态。
 */
export function createAvatarDragState(
  avatarWindow: BrowserWindow,
  startPoint: DragPoint
): AvatarDragState {
  return {
    startPoint,
    startBounds: avatarWindow.getBounds()
  };
}

/**
 * createAvatarResizeState 创建缩放会话状态。
 *
 * @param avatarWindow 当前角色窗口。
 * @param direction 缩放方向。
 * @param startPoint 缩放起始屏幕坐标。
 * @returns 缩放会话状态。
 */
export function createAvatarResizeState(
  avatarWindow: BrowserWindow,
  direction: AvatarResizeDirection,
  startPoint: DragPoint
): AvatarResizeState {
  return {
    direction,
    startPoint,
    startBounds: avatarWindow.getBounds()
  };
}
