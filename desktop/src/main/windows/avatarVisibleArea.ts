/**
 * AvatarWindowBounds 表示主进程在可见区计算时关心的最小窗口几何结构。
 * 这里保持为纯数据对象，是为了让几何算法可以脱离 Electron 单独测试。
 */
type AvatarWindowBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * WorkArea 表示当前显示器去掉任务栏/程序坞后的可用矩形（屏幕绝对坐标）。
 * 主进程从 screen.getDisplayMatching(bounds).workArea 取得，避免把气泡定位逻辑耦合到 Electron。
 */
type WorkArea = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * AvatarVisibleArea 描述「角色窗口当前有多少落在显示器工作区内」，全部以窗口本地坐标表示。
 * renderer 只认识窗口本地坐标（petBounds 也是本地坐标），所以这里把屏幕级裁剪结果换算回本地像素，
 * renderer 据此把脚部气泡钳在「可见身体最底部」之上。
 */
export type AvatarVisibleArea = {
  /** 窗口内仍位于工作区内的顶边本地 y（0 表示窗口顶边就在工作区内）。 */
  visibleTop: number;
  /** 窗口内仍位于工作区内的底边本地 y（等于窗口高度表示底边完全可见）。 */
  visibleBottom: number;
  /** 窗口内仍位于工作区内的左边本地 x。 */
  visibleLeft: number;
  /** 窗口内仍位于工作区内的右边本地 x。 */
  visibleRight: number;
};

/**
 * calculateAvatarVisibleArea 把窗口边界与显示器工作区相交，换算成窗口本地的可见矩形。
 *
 * @param windowBounds 角色窗口在屏幕上的绝对边界。
 * @param workArea 当前显示器去掉任务栏后的可用区域（屏幕绝对坐标）。
 * @returns 以窗口左上角为原点的可见区域四边本地坐标。
 *
 * 业务意图：当窗口下半部被任务栏或屏幕下边缘切掉时，visibleBottom 会小于窗口高度，
 * renderer 据此把脚部气泡上移到仍然可见的身体最底部，而不是跟着窗口底边一起被切走。
 * 边界异常：窗口与工作区完全不相交时，可见区会收敛成一条贴边的零高度/零宽度线，
 * 仍是合法的本地坐标，交给 renderer 的 clamp 兜底，不在这里抛错。
 */
export function calculateAvatarVisibleArea(
  windowBounds: AvatarWindowBounds,
  workArea: WorkArea
): AvatarVisibleArea {
  const workLeft = workArea.x;
  const workTop = workArea.y;
  const workRight = workArea.x + workArea.width;
  const workBottom = workArea.y + workArea.height;

  /**
   * 先在屏幕绝对坐标里求窗口与工作区的交集边，再减去窗口原点换算成本地坐标。
   * clamp 到 [0, 窗口尺寸] 保证即使窗口完全移出工作区，返回值也始终落在窗口本地范围内。
   */
  const visibleTop = clamp(workTop - windowBounds.y, 0, windowBounds.height);
  const visibleBottom = clamp(workBottom - windowBounds.y, 0, windowBounds.height);
  const visibleLeft = clamp(workLeft - windowBounds.x, 0, windowBounds.width);
  const visibleRight = clamp(workRight - windowBounds.x, 0, windowBounds.width);

  return {
    visibleTop,
    visibleBottom,
    visibleLeft,
    visibleRight
  };
}

/**
 * clamp 把数值限制在闭区间内。
 */
function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
