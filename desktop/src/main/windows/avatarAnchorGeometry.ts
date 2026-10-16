/**
 * Rect 表示一个屏幕或窗口内的矩形区域。
 * 这里仅保存定位所需的最小几何字段，避免和 Electron 具体窗口对象耦合。
 */
export type Rect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * AvatarAnchorRegion 表示用于侧边窗定位的人物锚点区域。
 * 它既可以直接等于人物可见身体边界，也可以是从人物边界派生出来的锚点包围盒。
 */
export type AvatarAnchorRegion = Rect;

/**
 * resolveAvatarAnchorRegion 把人物可见边界转成侧边窗定位所需的锚点区域。
 * @param petBounds 人物在窗口本地坐标中的可见边界。
 * @returns 可直接送入面板定位算法的锚点区域。
 */
export function resolveAvatarAnchorRegion(petBounds: Rect): AvatarAnchorRegion {
  const anchorWidth = Math.max(1, Math.round(petBounds.width * 0.58));
  const anchorHeight = Math.max(1, Math.round(petBounds.height * 0.88));
  const anchorX = Math.round(petBounds.x + petBounds.width - anchorWidth);
  const anchorY = Math.round(petBounds.y + petBounds.height * 0.06);

  return {
    x: anchorX,
    y: anchorY,
    width: anchorWidth,
    height: anchorHeight
  };
}
