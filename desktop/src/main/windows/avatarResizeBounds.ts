import type { AvatarResizeDirection } from "../../shared/live2dModelTypes";

/**
 * AvatarWindowBounds 表示主进程在缩放计算时关心的最小窗口几何结构。
 * 这里保持为纯数据对象，是为了让几何算法可以脱离 Electron 单独测试。
 */
type AvatarWindowBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * AvatarResizePoint 表示一次缩放会话中的屏幕绝对坐标。
 * 主进程通过起点与当前点的差量推导新窗口边界，不依赖 renderer 本地坐标系。
 */
type AvatarResizePoint = {
  screenX: number;
  screenY: number;
};

/**
 * CalculateResizedAvatarBoundsInput 描述一次缩放计算需要的全部输入。
 * 核心入参包括起始窗口边界、缩放方向、起点坐标、当前坐标、最小尺寸限制与人物窗口比例。
 */
type CalculateResizedAvatarBoundsInput = {
  direction: AvatarResizeDirection;
  startBounds: AvatarWindowBounds;
  startPoint: AvatarResizePoint;
  currentPoint: AvatarResizePoint;
  minWidth: number;
  minHeight: number;
  aspectRatio: number;
};

/**
 * calculateResizedAvatarBounds 根据八方向缩放手柄计算新的桌宠窗口边界。
 * 核心目标是让窗口缩放始终保持人物档案比例，使窗口变大和 Live2D 等比放大具备一致手感。
 */
export function calculateResizedAvatarBounds(
  input: CalculateResizedAvatarBoundsInput
): AvatarWindowBounds {
  const deltaX = input.currentPoint.screenX - input.startPoint.screenX;
  const deltaY = input.currentPoint.screenY - input.startPoint.screenY;
  const aspectRatio = readSafeAspectRatio(input);
  const candidateWidth = readCandidateWidth(input, deltaX);
  const candidateHeight = readCandidateHeight(input, deltaY);
  const scale = Math.max(
    candidateWidth / input.startBounds.width,
    candidateHeight / input.startBounds.height,
    input.minWidth / input.startBounds.width,
    input.minHeight / input.startBounds.height
  );
  const nextWidth = Math.max(input.minWidth, Math.round(input.startBounds.width * scale));
  const nextHeight = Math.max(
    input.minHeight,
    Math.round(nextWidth / aspectRatio)
  );
  const fixedRight = input.startBounds.x + input.startBounds.width;
  const fixedBottom = input.startBounds.y + input.startBounds.height;

  /**
   * 左/上方向缩放要固定对边；右/下方向缩放保留左上锚点。
   * 这让命中最小尺寸或等比补齐另一轴时，窗口不会在鼠标反方向产生额外漂移。
   */
  const nextX = input.direction.includes("w")
    ? fixedRight - nextWidth
    : input.startBounds.x;
  const nextY = input.direction.includes("n")
    ? fixedBottom - nextHeight
    : input.startBounds.y;

  return {
    x: nextX,
    y: nextY,
    width: nextWidth,
    height: nextHeight
  };
}

/**
 * readCandidateWidth 根据缩放方向读取当前鼠标意图对应的候选宽度。
 */
function readCandidateWidth(
  input: CalculateResizedAvatarBoundsInput,
  deltaX: number
): number {
  if (input.direction.includes("e")) {
    return input.startBounds.width + deltaX;
  }

  if (input.direction.includes("w")) {
    return input.startBounds.width - deltaX;
  }

  return input.startBounds.width;
}

/**
 * readCandidateHeight 根据缩放方向读取当前鼠标意图对应的候选高度。
 */
function readCandidateHeight(
  input: CalculateResizedAvatarBoundsInput,
  deltaY: number
): number {
  if (input.direction.includes("s")) {
    return input.startBounds.height + deltaY;
  }

  if (input.direction.includes("n")) {
    return input.startBounds.height - deltaY;
  }

  return input.startBounds.height;
}

/**
 * readSafeAspectRatio 过滤非法比例，避免异常档案让窗口几何产生 NaN。
 */
function readSafeAspectRatio(input: CalculateResizedAvatarBoundsInput): number {
  if (Number.isFinite(input.aspectRatio) && input.aspectRatio > 0) {
    return input.aspectRatio;
  }

  return input.startBounds.width / input.startBounds.height;
}
