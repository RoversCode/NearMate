import type {
  AvatarPresentationProfileApplyRequest,
  AvatarPetBounds,
  AvatarResizeDirection
} from "../../shared/live2dModelTypes";

/**
 * DragPoint 表示 renderer 通过 preload 上报的屏幕坐标。
 */
export type DragPoint = {
  screenX: number;
  screenY: number;
};

/**
 * AvatarResizeStartPayload 表示 renderer 发起缩放时上报的方向与起点。
 */
export type AvatarResizeStartPayload = {
  direction: AvatarResizeDirection;
  point: DragPoint;
};

/**
 * readDragPoint 从未知 IPC payload 中读取安全的屏幕坐标。
 *
 * @param payload renderer 上报的未知 payload。
 * @returns 有限数字坐标，非法时返回 null。
 */
export function readDragPoint(payload: unknown): DragPoint | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<DragPoint>;

  if (
    typeof candidate.screenX !== "number" ||
    typeof candidate.screenY !== "number" ||
    !Number.isFinite(candidate.screenX) ||
    !Number.isFinite(candidate.screenY)
  ) {
    return null;
  }

  return {
    screenX: candidate.screenX,
    screenY: candidate.screenY
  };
}

/**
 * readAvatarResizeStartPayload 从未知 IPC payload 中读取缩放起点数据。
 *
 * @param payload renderer 上报的未知 payload。
 * @returns 合法缩放起点，非法时返回 null。
 */
export function readAvatarResizeStartPayload(payload: unknown): AvatarResizeStartPayload | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<AvatarResizeStartPayload>;

  if (!isAvatarResizeDirection(candidate.direction)) {
    return null;
  }

  const point = readDragPoint(candidate.point);

  if (!point) {
    return null;
  }

  return {
    direction: candidate.direction,
    point
  };
}

/**
 * isAvatarResizeDirection 用显式字面量白名单校验缩放方向。
 *
 * @param direction 未知方向值。
 * @returns 是否是受支持的缩放方向。
 */
function isAvatarResizeDirection(
  direction: unknown
): direction is AvatarResizeDirection {
  return (
    direction === "n" ||
    direction === "s" ||
    direction === "e" ||
    direction === "w" ||
    direction === "ne" ||
    direction === "nw" ||
    direction === "se" ||
    direction === "sw"
  );
}

/**
 * readAvatarPresentationProfileApplyRequest 读取合法的人物展示档案应用请求。
 *
 * @param payload renderer 上报的未知 payload。
 * @returns 合法人物档案应用请求，非法时返回 null。
 */
export function readAvatarPresentationProfileApplyRequest(
  payload: unknown
): AvatarPresentationProfileApplyRequest | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<AvatarPresentationProfileApplyRequest>;
  const normalizedModelId = candidate.modelId?.trim();

  if (!normalizedModelId) {
    return null;
  }

  return {
    modelId: normalizedModelId
  };
}

/**
 * readAvatarPetBounds 从未知 IPC payload 中读取人物可见边界。
 *
 * @param payload renderer 上报的人物边界，null 表示清除缓存。
 * @returns 有限数字边界，非法时返回 null。
 */
export function readAvatarPetBounds(payload: unknown): AvatarPetBounds | null {
  if (payload === null) {
    return null;
  }

  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<AvatarPetBounds>;

  if (
    typeof candidate.x !== "number" ||
    typeof candidate.y !== "number" ||
    typeof candidate.width !== "number" ||
    typeof candidate.height !== "number" ||
    !Number.isFinite(candidate.x) ||
    !Number.isFinite(candidate.y) ||
    !Number.isFinite(candidate.width) ||
    !Number.isFinite(candidate.height)
  ) {
    return null;
  }

  return {
    x: candidate.x,
    y: candidate.y,
    width: candidate.width,
    height: candidate.height
  };
}
