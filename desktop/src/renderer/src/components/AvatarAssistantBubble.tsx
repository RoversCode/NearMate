import type { CSSProperties } from "react";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import {
  calculateAssistantBubbleLayout,
  type AssistantBubbleAnchor
} from "./assistantBubbleLayout";

type AvatarAssistantBubbleProps = {
  text: string;
  petBounds: AvatarDragRegion | null;
  anchor: AssistantBubbleAnchor;
};

/** 引导线 SVG 在气泡四周额外预留的画布边距，用于容纳超出气泡边界（含负坐标）的引导线终点。 */
const LEADER_SVG_PADDING = 8;

/**
 * AvatarAssistantBubble 渲染从桌宠头部斜上方浮出的助手回复气泡。
 * 核心入参是助手文本、桌宠可见边界和头部锚点；预期输出是位于头部西北/东北、用细引导线指向头部的对话气泡。
 * 边界异常：模型边界尚未可用时不渲染，避免把气泡错误放在窗口中心。
 */
export function AvatarAssistantBubble({ text, petBounds, anchor }: AvatarAssistantBubbleProps) {
  if (!petBounds) {
    return null;
  }

  const layout = calculateAssistantBubbleLayout({
    petBounds,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    text,
    anchor
  });
  const bubbleStyle = {
    left: `${layout.style.left}px`,
    top: `${layout.style.top}px`,
    width: `${layout.style.width}px`,
    minHeight: `${layout.style.minHeight}px`,
    "--avatar-assistant-bubble-scale": layout.scale
  } as CSSProperties;
  const { leader } = layout;
  /**
   * 引导线终点可能落在气泡左侧（东北放置时 endX 为负）或下方。
   * 这里用包含起点、终点的包围盒撑开 SVG，并允许它相对气泡左上角负向偏移，
   * 使同一套相对坐标在西北/东北两种镜像构图下都能完整画出。
   */
  const minX = Math.min(0, leader.startX, leader.endX) - LEADER_SVG_PADDING;
  const minY = Math.min(0, leader.startY, leader.endY) - LEADER_SVG_PADDING;
  const maxX = Math.max(layout.style.width, leader.startX, leader.endX) + LEADER_SVG_PADDING;
  const maxY = Math.max(layout.style.minHeight, leader.startY, leader.endY) + LEADER_SVG_PADDING;
  const svgWidth = maxX - minX;
  const svgHeight = maxY - minY;
  const leaderStyle = {
    left: `${minX}px`,
    top: `${minY}px`
  } as CSSProperties;

  return (
    <div
      className="avatar-assistant-bubble"
      data-placement={layout.placement}
      style={bubbleStyle}
      role="status"
      aria-live="polite"
    >
      <svg
        className="avatar-assistant-bubble-leader"
        width={svgWidth}
        height={svgHeight}
        viewBox={`${minX} ${minY} ${svgWidth} ${svgHeight}`}
        style={leaderStyle}
        aria-hidden="true"
      >
        <path
          className="avatar-assistant-bubble-leader-line"
          d={`M ${leader.startX} ${leader.startY} Q ${leader.controlX} ${leader.controlY} ${leader.endX} ${leader.endY}`}
          fill="none"
        />
        <circle
          className="avatar-assistant-bubble-leader-dot"
          cx={leader.endX}
          cy={leader.endY}
          r={3}
        />
      </svg>
      <p>{text}</p>
    </div>
  );
}
