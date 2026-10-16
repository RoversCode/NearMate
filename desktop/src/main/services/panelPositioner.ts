import type { Rect } from "../windows/avatarAnchorGeometry";

/**
 * 表示侧边面板最终应当放置的位置。
 * `side` 用于告知调用方本次布局选择了角色哪一侧，便于后续派生箭头方向等 UI 状态。
 */
export type PanelPlacement = {
  x: number;
  y: number;
  side: "left" | "right";
};

/**
 * 计算角色侧边面板的放置位置。
 * 核心入参为人物锚点区域、面板尺寸、显示器边界与间距；输出为面板左上角坐标以及左右侧标记。
 * 当右侧空间不足时会自动切换到左侧；当纵向超出显示器范围时会被夹紧在可视区域内。
 */
export function calculatePanelPlacement(input: {
  avatarAnchorBounds: Rect;
  panelSize: { width: number; height: number };
  displayBounds: Rect;
  gap: number;
}): PanelPlacement {
  const { avatarAnchorBounds, panelSize, displayBounds, gap } = input;

  // 先分别计算左右两侧的候选横坐标，后续会根据显示器剩余空间挑选最终方案。
  const rightX = avatarAnchorBounds.x + avatarAnchorBounds.width + gap;
  const leftX = avatarAnchorBounds.x - panelSize.width - gap;
  const fitsRight =
    rightX + panelSize.width <= displayBounds.x + displayBounds.width;
  const fitsLeft = leftX >= displayBounds.x;

  // 业务上默认优先右侧，只有右侧放不下且左侧能放下时才翻转到左侧。
  const side = fitsRight || !fitsLeft ? "right" : "left";
  const x = side === "right" ? rightX : leftX;

  // 面板纵向优先与角色居中对齐，但必须限制在当前显示器边界内，避免窗口跑出可视区域。
  const centeredY =
    avatarAnchorBounds.y +
    Math.round((avatarAnchorBounds.height - panelSize.height) / 2);
  const minY = displayBounds.y;
  const maxY = displayBounds.y + displayBounds.height - panelSize.height;
  const y = Math.max(minY, Math.min(centeredY, maxY));

  return { x, y, side };
}
