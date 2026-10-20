import type { AvatarDragRegion } from "./avatarDragRegion";
import {
  DEFAULT_AVATAR_INTERACTION_HOTSPOT,
  type AvatarInteractionHotspot
} from "../../../shared/avatarPresentationProfile";

export type AvatarInteractionRect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

type AvatarInteractionPoint = {
  x: number;
  y: number;
};

const ACTION_RAIL_WIDTH = 44;
const ACTION_RAIL_HEIGHT = 164;
// 四个 38px 按钮、三段 10px 间距和上下各 6px 内边距。
const ACTION_RAIL_REACTION_HEIGHT = 194;
const PANEL_BUTTON_EDGE_MARGIN = 16;
const PANEL_BUTTON_GAP = 12;
const PANEL_BUTTON_TOP_INSET = 8;
const RESIZE_FRAME_OUTSET = 12;
const INTENT_CORRIDOR_PADDING = 18;
const RESIZE_HANDLE_THICKNESS = 12;
const RESIZE_CORNER_HANDLE_SIZE = 18;

type AvatarActionRailRectInput = {
  petBounds: AvatarDragRegion | null;
  viewportWidth: number;
  viewportHeight: number;
  hasReactionButton?: boolean;
};

type AvatarInteractionPointInput = {
  point: AvatarInteractionPoint;
  petBounds: AvatarDragRegion | null;
  interactionHotspot?: AvatarInteractionHotspot;
  buttonRect: AvatarInteractionRect | null;
  resizeFrameRect: AvatarInteractionRect | null;
};

type AvatarActivationPointInput = {
  point: AvatarInteractionPoint;
  petBounds: AvatarDragRegion | null;
  interactionHotspot?: AvatarInteractionHotspot;
};

/**
 * resolveAvatarActionRailRect 计算右侧动作栏在头像窗口内的真实矩形。
 *
 * Args:
 *   input: 当前人物边界、窗口尺寸，以及实际绑定的模型是否提供动作按钮。
 *
 * Returns:
 *   动作栏矩形；人物边界缺失时回退到窗口右上角安全位置。
 */
export function resolveAvatarActionRailRect(
  input: AvatarActionRailRectInput
): AvatarInteractionRect {
  const height = input.hasReactionButton ? ACTION_RAIL_REACTION_HEIGHT : ACTION_RAIL_HEIGHT;
  const maxLeft = Math.max(
    PANEL_BUTTON_EDGE_MARGIN,
    input.viewportWidth - PANEL_BUTTON_EDGE_MARGIN - ACTION_RAIL_WIDTH
  );
  const maxTop = Math.max(
    PANEL_BUTTON_EDGE_MARGIN,
    input.viewportHeight - PANEL_BUTTON_EDGE_MARGIN - height
  );

  if (!input.petBounds) {
    return {
      x: maxLeft,
      y: PANEL_BUTTON_EDGE_MARGIN,
      width: ACTION_RAIL_WIDTH,
      height
    };
  }

  const preferredLeft = input.petBounds.x + input.petBounds.width + PANEL_BUTTON_GAP;
  const preferredTop = input.petBounds.y + PANEL_BUTTON_TOP_INSET;

  return {
    x: clamp(preferredLeft, PANEL_BUTTON_EDGE_MARGIN, maxLeft),
    y: clamp(preferredTop, PANEL_BUTTON_EDGE_MARGIN, maxTop),
    width: ACTION_RAIL_WIDTH,
    height
  };
}

/**
 * resolveAvatarResizeFrameRect 计算围绕人物的缩放提示框矩形。
 * @param petBounds 当前人物边界。
 * @returns 可用于绝对定位缩放框的矩形；人物边界缺失时不渲染缩放热区。
 */
export function resolveAvatarResizeFrameRect(
  petBounds: AvatarDragRegion | null
): AvatarInteractionRect | null {
  if (!petBounds) {
    return null;
  }

  return {
    x: petBounds.x - RESIZE_FRAME_OUTSET,
    y: petBounds.y - RESIZE_FRAME_OUTSET,
    width: petBounds.width + RESIZE_FRAME_OUTSET * 2,
    height: petBounds.height + RESIZE_FRAME_OUTSET * 2
  };
}

/**
 * resolveAvatarInteractionHotspotRect 计算桌宠主交互热区。
 * @param petBounds 当前人物外接交互边界。
 * @param interactionHotspot 人物档案中的热区比例配置。
 * @returns 用于首次激活和拖拽的小热区；人物边界缺失时返回 null。
 */
export function resolveAvatarInteractionHotspotRect(
  petBounds: AvatarDragRegion | null,
  interactionHotspot: AvatarInteractionHotspot = DEFAULT_AVATAR_INTERACTION_HOTSPOT
): AvatarInteractionRect | null {
  if (!petBounds) {
    return null;
  }

  const width = Math.round(petBounds.width * interactionHotspot.widthRatio);
  const height = Math.round(petBounds.height * interactionHotspot.heightRatio);

  return {
    x: Math.round(petBounds.x + petBounds.width * interactionHotspot.xRatio),
    y: Math.round(petBounds.y + petBounds.height * interactionHotspot.yRatio),
    width,
    height
  };
}

/**
 * resolveAvatarActivationRect 计算首次激活桌宠交互的核心人物热区。
 * @param petBounds 当前人物外接交互边界。
 * @param interactionHotspot 人物档案中的热区比例配置。
 * @returns 收窄后的核心激活矩形；人物边界缺失时返回 null。
 */
export function resolveAvatarActivationRect(
  petBounds: AvatarDragRegion | null,
  interactionHotspot?: AvatarInteractionHotspot
): AvatarInteractionRect | null {
  return resolveAvatarInteractionHotspotRect(petBounds, interactionHotspot);
}

/**
 * isAvatarActivationPoint 判断鼠标是否命中首次激活桌宠的核心人物热区。
 * @param input 鼠标点与当前人物边界。
 * @returns 命中核心人物区域时返回 true。
 */
export function isAvatarActivationPoint(input: AvatarActivationPointInput): boolean {
  const activationRect = resolveAvatarActivationRect(input.petBounds, input.interactionHotspot);

  return !!activationRect && containsPoint(activationRect, input.point);
}

/**
 * isAvatarInteractionPoint 判断窗口本地坐标是否属于桌宠真实交互区。
 * @param input 鼠标点、人物边界、按钮矩形与缩放框矩形。
 * @returns 命中人物、按钮、缩放框或按钮通行走廊时返回 true。
 */
export function isAvatarInteractionPoint(input: AvatarInteractionPointInput): boolean {
  if (
    isAvatarActivationPoint({
      point: input.point,
      petBounds: input.petBounds,
      interactionHotspot: input.interactionHotspot
    })
  ) {
    return true;
  }

  if (input.buttonRect && containsPoint(input.buttonRect, input.point)) {
    return true;
  }

  if (input.resizeFrameRect && containsResizeHandlePoint(input.resizeFrameRect, input.point)) {
    return true;
  }

  if (!input.petBounds || !input.buttonRect) {
    return false;
  }

  return containsPoint(resolvePanelButtonIntentCorridor(input.petBounds, input.buttonRect), input.point);
}

/**
 * containsResizeHandlePoint 判断点是否命中真实缩放 handle，而不是缩放框内部透明区域。
 * @param rect 缩放框外接矩形。
 * @param point 鼠标窗口本地坐标。
 * @returns 命中边缘或四角 handle 时返回 true。
 */
function containsResizeHandlePoint(
  rect: AvatarInteractionRect,
  point: AvatarInteractionPoint
): boolean {
  if (!containsPoint(rect, point)) {
    return false;
  }

  const left = point.x - rect.x;
  const right = rect.x + rect.width - point.x;
  const top = point.y - rect.y;
  const bottom = rect.y + rect.height - point.y;
  const nearLeft = left <= RESIZE_HANDLE_THICKNESS;
  const nearRight = right <= RESIZE_HANDLE_THICKNESS;
  const nearTop = top <= RESIZE_HANDLE_THICKNESS;
  const nearBottom = bottom <= RESIZE_HANDLE_THICKNESS;
  const inCornerX =
    left <= RESIZE_CORNER_HANDLE_SIZE || right <= RESIZE_CORNER_HANDLE_SIZE;
  const inCornerY =
    top <= RESIZE_CORNER_HANDLE_SIZE || bottom <= RESIZE_CORNER_HANDLE_SIZE;

  if (inCornerX && inCornerY) {
    return true;
  }

  if ((nearTop || nearBottom) && left >= RESIZE_HANDLE_THICKNESS && right >= RESIZE_HANDLE_THICKNESS) {
    return true;
  }

  return (nearLeft || nearRight) && top >= RESIZE_HANDLE_THICKNESS && bottom >= RESIZE_HANDLE_THICKNESS;
}

/**
 * resolvePanelButtonIntentCorridor 连接人物与面板入口之间的鼠标通行走廊。
 * @param petBounds 当前人物边界。
 * @param buttonRect 面板入口矩形。
 * @returns 包住两者之间缝隙的矩形，避免用户移动到按钮途中入口突然消失。
 */
function resolvePanelButtonIntentCorridor(
  petBounds: AvatarDragRegion,
  buttonRect: AvatarInteractionRect
): AvatarInteractionRect {
  const left = Math.min(petBounds.x + petBounds.width, buttonRect.x);
  const right = Math.max(petBounds.x + petBounds.width, buttonRect.x + buttonRect.width);
  const top = Math.min(petBounds.y, buttonRect.y) - INTENT_CORRIDOR_PADDING;
  const bottom = Math.max(
    petBounds.y + Math.min(petBounds.height, buttonRect.height + PANEL_BUTTON_TOP_INSET),
    buttonRect.y + buttonRect.height
  ) + INTENT_CORRIDOR_PADDING;

  return {
    x: left,
    y: top,
    width: Math.max(0, right - left),
    height: Math.max(0, bottom - top)
  };
}

/**
 * containsPoint 判断点是否位于矩形内部。
 * @param rect 目标矩形。
 * @param point 窗口本地坐标点。
 * @returns 点落在矩形闭区间内时返回 true。
 */
function containsPoint(rect: AvatarInteractionRect, point: AvatarInteractionPoint): boolean {
  return (
    point.x >= rect.x &&
    point.x <= rect.x + rect.width &&
    point.y >= rect.y &&
    point.y <= rect.y + rect.height
  );
}

/**
 * clamp 将数值限制在给定区间内。
 * @param value 待限制数值。
 * @param min 最小值。
 * @param max 最大值。
 * @returns 限制后的数值。
 */
function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
