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
  };
};

export type AvatarDragRegion = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * calculateAvatarDragRegion 根据 Live2D 模型的底部中心锚点计算真实可拖拽区域。
 * 核心入参是模型锚点坐标、缩放比例和模型原始宽高，输出为四舍五入后的矩形边界。
 * 边界异常：本函数只做确定性几何换算，不吞掉异常数值，调用方应在模型布局阶段保证输入有效。
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
  const width = visibleWidth * widthRatio;
  const height = visibleHeight * heightRatio;

  /**
   * 拖拽热区以模型底部中心为锚点收缩。
   * 某些模型的透明外接盒与真实人物重心不一致，offsetX 用于把热区对齐到视觉身体中心。
   */
  return {
    x: Math.round(input.x + offsetX - width / 2),
    y: Math.round(input.y - height),
    width: Math.round(width),
    height: Math.round(height)
  };
}
