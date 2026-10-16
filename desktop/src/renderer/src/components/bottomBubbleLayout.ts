import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import { clampBubbleIntoViewport } from "./bubbleLayoutShared";

export type BottomBubbleAnchor = {
  x: number;
  y: number;
};

export type BottomBubbleLayoutInput = {
  petBounds: AvatarDragRegion;
  viewportWidth: number;
  viewportHeight: number;
  /** 气泡估算宽度，与 CSS 中的 min(300px, 100% - 36px) 上限保持一致。 */
  bubbleWidth: number;
  /** 气泡估算高度，用于把底边钳制进可见区。 */
  bubbleHeight: number;
  /** 脚部锚点（与助手气泡共用同一档案），气泡定位在角色脚部附近。 */
  footAnchor: BottomBubbleAnchor;
  /**
   * 窗口本地坐标系下「仍位于屏幕工作区内的底边」。
   * 由主进程根据窗口屏幕位置与显示器工作区算出；renderer 不知道物理屏幕坐标，必须依赖它。
   * 省略时退化为窗口高度（即假设窗口底边完全可见），仅用于主进程可见区尚未推送时的兜底。
   */
  visibleBottom?: number;
};

export type BottomBubbleLayout = {
  left: number;
  top: number;
};

const VIEWPORT_MARGIN = 18;

/**
 * calculateBottomBubbleLayout 计算用户语音/权限气泡在窗口内的位置。
 *
 * @param input 角色可见边界、窗口尺寸、气泡估算尺寸、脚部锚点与可见底边。
 * @returns 气泡左上角在窗口内坐标的绝对定位。
 *
 * 业务意图：气泡水平居中于脚部锚点，底边贴住「脚部」与「可见身体最底部」中更靠上的一条。
 * 角色完整可见时气泡落在脚部；下半身被任务栏/屏幕下边缘切掉时，visibleBottom 上移，
 * 气泡随之贴到仍然可见的身体最底部，而不是跟着窗口底边一起被切走。
 */
export function calculateBottomBubbleLayout(
  input: BottomBubbleLayoutInput
): BottomBubbleLayout {
  const anchorX = input.petBounds.x + input.petBounds.width * input.footAnchor.x;
  const footY = input.petBounds.y + input.petBounds.height * input.footAnchor.y;

  /**
   * effectiveBottom 是气泡底边允许到达的最低本地 y。
   * 取「窗口高度」与「主进程推送的可见底边」中更小的一条，保证气泡永远落在屏幕工作区内；
   * 再与脚部 footY 取更小，保证角色完整可见时气泡贴脚而不是沉到窗口最底部。
   */
  const screenVisibleBottom = Math.min(
    input.viewportHeight,
    input.visibleBottom ?? input.viewportHeight
  );
  const desiredBottom = Math.min(footY, screenVisibleBottom);
  const preferredLeft = anchorX - input.bubbleWidth / 2;
  const preferredTop = desiredBottom - input.bubbleHeight;

  /**
   * 钳制用的有效视口高度收敛到 screenVisibleBottom。
   * 这样 clamp 的底部边界变成 screenVisibleBottom - margin，气泡底边不会越过可见身体最底部。
   */
  const clamped = clampBubbleIntoViewport(
    {
      left: preferredLeft,
      top: preferredTop,
      width: input.bubbleWidth,
      height: input.bubbleHeight
    },
    { width: input.viewportWidth, height: screenVisibleBottom },
    VIEWPORT_MARGIN
  );

  return {
    left: clamped.left,
    top: clamped.top
  };
}
