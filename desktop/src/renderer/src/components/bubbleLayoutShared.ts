/**
 * Rect 是气泡布局通用的轴对齐矩形。
 * 统一用 left/top/width/height 描述，方便与 DOM 绝对定位直接对接。
 */
export type Rect = {
  left: number;
  top: number;
  width: number;
  height: number;
};

/**
 * Viewport 描述当前角色窗口的可见尺寸。
 * 所有气泡钳制都以窗口内坐标为基准，不涉及物理屏幕坐标。
 */
export type Viewport = {
  width: number;
  height: number;
};

/**
 * clampBubbleIntoViewport 把任意气泡矩形钳制进窗口可见区域内。
 *
 * @param rect 气泡期望的位置与尺寸。
 * @param viewport 当前角色窗口可见尺寸。
 * @param margin 气泡与窗口边缘之间必须保留的安全边距。
 * @returns 左上角被钳制后的新位置；宽高保持不变。
 *
 * 边界异常：当气泡比可用空间还大时，优先保证 left/top 不小于 margin，
 * 这样即使溢出也只会从右/下方向溢出，左上角文字始终可读。
 */
export function clampBubbleIntoViewport(
  rect: Rect,
  viewport: Viewport,
  margin: number
): Rect {
  const maxLeft = Math.max(margin, viewport.width - margin - rect.width);
  const maxTop = Math.max(margin, viewport.height - margin - rect.height);

  return {
    ...rect,
    left: clamp(rect.left, margin, maxLeft),
    top: clamp(rect.top, margin, maxTop)
  };
}

/**
 * estimateBubbleTextHeight 根据文本长度估算气泡主体高度。
 *
 * @param text 气泡文本。
 * @param width 气泡最终宽度。
 * @param scale 当前视觉缩放。
 * @returns 用于布局避让与 CSS 最小高度的估算高度。
 *
 * 该估算被助手气泡与底部气泡共享，保证两类气泡的换行高度规则一致。
 */
export function estimateBubbleTextHeight(text: string, width: number, scale: number): number {
  const fontSize = 13 * scale;
  const lineHeight = fontSize * 1.55;
  const horizontalPadding = 34 * scale;
  const verticalPadding = 28 * scale;
  const contentWidth = Math.max(fontSize * 8, width - horizontalPadding);
  const charsPerLine = Math.max(8, Math.floor(contentWidth / (fontSize * 0.92)));
  const lineCount = Math.max(1, Math.ceil(text.trim().length / charsPerLine));
  const estimatedHeight = verticalPadding + lineCount * lineHeight;

  return Math.max(72 * scale, estimatedHeight);
}

/**
 * clamp 把数值限制在闭区间内。
 *
 * @param value 原始数值。
 * @param min 最小值。
 * @param max 最大值。
 * @returns 被限制在闭区间内的数值。
 */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
