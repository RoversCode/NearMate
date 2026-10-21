export type AvatarDragRegionInput = {
  x: number;
  y: number;
  scale: number;
  modelWidth: number;
  modelHeight: number;
  interactionBounds?: {
    widthRatio: number;
    heightRatio: number;
    offsetXRatio?: number;
    offsetYRatio?: number;
  };
};

export type AvatarDragRegion = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * 根据模型画布的底部中心锚点，计算身体所在的固定交互边界。
 *
 * @param input 模型锚点、缩放、画布尺寸和相对画布的身体边界；调用方保证数值有效。
 * @returns 四舍五入后的交互矩形，未配置偏移时保持原有底部对齐方式。
 */
export function calculateAvatarDragRegion(input: AvatarDragRegionInput): AvatarDragRegion {
  /**
   * Live2D 模型在舞台中使用底部中心点定位。
   * 先把原始模型尺寸乘以缩放比例，得到屏幕上真正可见的人物宽高。
   */
  const visibleWidth = input.modelWidth * input.scale;
  const visibleHeight = input.modelHeight * input.scale;
  const widthRatio = input.interactionBounds?.widthRatio ?? 1;
  const heightRatio = input.interactionBounds?.heightRatio ?? 1;
  const offsetX = visibleWidth * (input.interactionBounds?.offsetXRatio ?? 0);
  const offsetY = visibleHeight * (input.interactionBounds?.offsetYRatio ?? 0);
  const width = visibleWidth * widthRatio;
  const height = visibleHeight * heightRatio;

  /**
   * 拖拽热区以模型底部中心为锚点收缩。
   * 某些模型的透明外接盒与真实人物重心不一致，横向偏移对齐身体中心，
   * 纵向偏移则移走半身模型桌面下方的透明区域，不让留白挡住桌面操作。
   */
  return {
    x: Math.round(input.x + offsetX - width / 2),
    y: Math.round(input.y + offsetY - height),
    width: Math.round(width),
    height: Math.round(height)
  };
}
