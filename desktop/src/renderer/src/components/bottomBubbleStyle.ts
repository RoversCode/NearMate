import type { CSSProperties } from "react";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import {
  calculateBottomBubbleLayout,
  type BottomBubbleAnchor
} from "./bottomBubbleLayout";

/** 气泡左右两侧合计预留的窗口边距，与 CSS 中 calc(100% - 36px) 上限保持一致。 */
const BUBBLE_HORIZONTAL_MARGIN = 36;
/** 气泡最大宽度，与 CSS 中 min(300px, …) 上限保持一致。 */
const MAX_BUBBLE_WIDTH = 300;

export type ResolveBottomBubbleStyleInput = {
  /** 桌宠当前可见边界；为 null 时返回 undefined，让组件回退到 CSS 默认底边锚点。 */
  petBounds: AvatarDragRegion | null;
  /** 脚部锚点（与助手气泡共用同一档案），气泡定位在角色脚部附近。 */
  footAnchor: BottomBubbleAnchor;
  viewportWidth: number;
  viewportHeight: number;
  /**
   * 窗口本地坐标系下「仍位于屏幕工作区内的底边」，由主进程推送。
   * 省略时退化为窗口高度（假设窗口底边完全可见），用于主进程可见区尚未推送时的兜底。
   */
  visibleBottom?: number;
  /**
   * measureHeight 根据最终气泡宽度返回气泡估算高度。
   * 文本气泡按字数估算，初始化气泡使用浏览器测得的完整高度，包含说明和按钮。
   */
  measureHeight(bubbleWidth: number): number;
};

/**
 * resolveBottomBubbleStyle 把「锚定脚部、贴可见身体最底部」的底部气泡定位换算成内联样式。
 *
 * @param input 角色可见边界、脚部锚点、窗口尺寸、可见底边与高度估算回调。
 * @returns 覆盖 CSS 默认底边锚点的内联样式；petBounds 不可用时返回 undefined。
 *
 * 业务意图：把宽度规则、高度估算与 calculateBottomBubbleLayout 的调用收敛到一处，
 * 供 AvatarApp 的语音/权限气泡和 AvatarBootstrapShell 的引导气泡共用，避免胶水代码在两个组件间漂移。
 */
export function resolveBottomBubbleStyle(
  input: ResolveBottomBubbleStyleInput
): CSSProperties | undefined {
  if (!input.petBounds) {
    return undefined;
  }

  const viewportMaxWidth = Math.max(0, input.viewportWidth - BUBBLE_HORIZONTAL_MARGIN);
  const bubbleWidth = Math.min(MAX_BUBBLE_WIDTH, viewportMaxWidth);
  const bubbleHeight = input.measureHeight(bubbleWidth);
  const layout = calculateBottomBubbleLayout({
    petBounds: input.petBounds,
    viewportWidth: input.viewportWidth,
    viewportHeight: input.viewportHeight,
    bubbleWidth,
    bubbleHeight,
    footAnchor: input.footAnchor,
    visibleBottom: input.visibleBottom
  });

  return {
    left: `${layout.left}px`,
    top: `${layout.top}px`,
    right: "auto",
    bottom: "auto",
    width: `${bubbleWidth}px`,
    margin: 0
  };
}
