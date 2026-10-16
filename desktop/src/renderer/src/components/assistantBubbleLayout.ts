import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import {
  clamp,
  clampBubbleIntoViewport,
  estimateBubbleTextHeight
} from "./bubbleLayoutShared";

export type AssistantBubbleAnchor = {
  x: number;
  y: number;
};

/** 助手气泡相对头部的方位：默认西北（左上），左侧空间不足时翻到东北（右上）。 */
type AssistantBubblePlacement = "northwest" | "northeast";

export type AssistantBubbleLayoutInput = {
  petBounds: AvatarDragRegion;
  viewportWidth: number;
  viewportHeight: number;
  text: string;
  anchor: AssistantBubbleAnchor;
};

/**
 * AssistantBubbleLeader 描述从气泡靠头一侧底角指向头部锚点的细引导线。
 * 全部坐标都相对气泡左上角，方便直接画进覆盖气泡的内联 SVG。
 */
type AssistantBubbleLeader = {
  startX: number;
  startY: number;
  controlX: number;
  controlY: number;
  endX: number;
  endY: number;
};

export type AssistantBubbleLayout = {
  placement: AssistantBubblePlacement;
  anchorX: number;
  anchorY: number;
  scale: number;
  style: {
    left: number;
    top: number;
    width: number;
    minHeight: number;
  };
  leader: AssistantBubbleLeader;
};

const BASE_VIEWPORT_WIDTH = 520;
const BASE_BUBBLE_WIDTH = 260;
const MAX_BUBBLE_WIDTH = 300;
const MIN_BUBBLE_WIDTH = 190;
const MIN_SCALE = 0.9;
const MAX_SCALE = 1.25;
const VIEWPORT_MARGIN = 18;
/** 气泡底边与头部锚点之间保留的竖直净空，让头顶留白且引导线有可见长度。 */
const HEAD_CLEARANCE = 26;
/** 气泡靠头一侧与头部锚点之间的水平净空，让气泡偏到头的斜上方而不是正上方。 */
const HEAD_SIDE_GAP = 20;
/** 引导线最大长度上限，避免气泡被迫远离头部钳制后线条横贯整窗。 */
const MAX_LEADER_LENGTH = 110;

/**
 * calculateAssistantBubbleLayout 计算「头部斜上方气泡 + 细引导线」布局。
 *
 * @param input 角色可见边界、窗口尺寸、文本和模型档案中的头部锚点。
 * @returns 气泡主体位置、缩放比例以及指向头部锚点的引导线坐标。
 *
 * 业务意图：气泡默认落在头部**西北**（左上），头顶保持留白，引导线从气泡靠头底角斜向头顶；
 * 当左侧空间不足以容纳气泡时翻转到**东北**（右上），镜像同样的构图。
 */
export function calculateAssistantBubbleLayout(
  input: AssistantBubbleLayoutInput
): AssistantBubbleLayout {
  const anchorX = input.petBounds.x + input.petBounds.width * input.anchor.x;
  const anchorY = input.petBounds.y + input.petBounds.height * input.anchor.y;
  const scale = clamp(input.viewportWidth / BASE_VIEWPORT_WIDTH, MIN_SCALE, MAX_SCALE);
  const viewportMaxWidth = Math.max(0, input.viewportWidth - VIEWPORT_MARGIN * 2);
  const defaultWidth = clamp(
    BASE_BUBBLE_WIDTH * scale,
    Math.min(MIN_BUBBLE_WIDTH, viewportMaxWidth),
    Math.min(MAX_BUBBLE_WIDTH, Math.max(160, viewportMaxWidth))
  );
  const sideGap = HEAD_SIDE_GAP * scale;
  const leftAvailableWidth = anchorX - sideGap - VIEWPORT_MARGIN;
  const minReadableWidth = Math.min(MIN_BUBBLE_WIDTH, viewportMaxWidth);
  const placement: AssistantBubblePlacement =
    leftAvailableWidth >= minReadableWidth ? "northwest" : "northeast";
  const width =
    placement === "northwest"
      ? clamp(leftAvailableWidth, minReadableWidth, defaultWidth)
      : defaultWidth;
  const minHeight = estimateBubbleTextHeight(input.text, width, scale);

  // 业务意图：优先保留西北构图，左侧略窄时先缩窄气泡，窄到不可读时才翻到东北。
  const preferredLeft =
    placement === "northwest" ? anchorX - sideGap - width : anchorX + sideGap;
  const preferredTop = anchorY - HEAD_CLEARANCE * scale - minHeight;
  const clamped = clampBubbleIntoViewport(
    { left: preferredLeft, top: preferredTop, width, height: minHeight },
    { width: input.viewportWidth, height: input.viewportHeight },
    VIEWPORT_MARGIN
  );

  const leader = calculateLeader(
    placement,
    { left: clamped.left, top: clamped.top, width, minHeight },
    anchorX,
    anchorY
  );

  return {
    placement,
    anchorX,
    anchorY,
    scale,
    style: {
      left: clamped.left,
      top: clamped.top,
      width,
      minHeight
    },
    leader
  };
}

/**
 * calculateLeader 计算从气泡靠头一侧底角弯向头部锚点的二次贝塞尔引导线。
 *
 * @param placement 气泡相对头部的方位，决定从哪个底角出发。
 * @param bubbleRect 气泡主体矩形。
 * @param anchorX 头部锚点横坐标。
 * @param anchorY 头部锚点纵坐标。
 * @returns 相对气泡左上角的起点、控制点与终点坐标。
 *
 * 终点会沿气泡到锚点方向限制在最大长度内，保证钳制后线条不会过长。
 */
function calculateLeader(
  placement: AssistantBubblePlacement,
  bubbleRect: AssistantBubbleLayout["style"],
  anchorX: number,
  anchorY: number
): AssistantBubbleLeader {
  // 西北气泡从靠右底角出发，东北气泡从靠左底角出发，让引导线始终朝头部一侧伸出。
  const startX =
    placement === "northwest" ? bubbleRect.width * 0.82 : bubbleRect.width * 0.18;
  const startY = bubbleRect.minHeight;
  const rawEndX = anchorX - bubbleRect.left;
  const rawEndY = anchorY - bubbleRect.top;
  const deltaX = rawEndX - startX;
  const deltaY = rawEndY - startY;
  const distance = Math.hypot(deltaX, deltaY);
  const ratio = distance > MAX_LEADER_LENGTH ? MAX_LEADER_LENGTH / distance : 1;
  const endX = startX + deltaX * ratio;
  const endY = startY + deltaY * ratio;

  // 控制点偏向竖直方向，让线条呈现轻微弧度而非生硬直线。
  const controlX = startX + (endX - startX) * 0.3;
  const controlY = startY + (endY - startY) * 0.72;

  return {
    startX,
    startY,
    controlX,
    controlY,
    endX,
    endY
  };
}
