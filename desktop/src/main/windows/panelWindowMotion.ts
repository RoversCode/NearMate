import type { BrowserWindow } from "electron";
import type { PanelPlacement } from "../services/panelPositioner";

/**
 * PanelWindowMotionOptions 描述侧边窗短缓动所需的时间参数。
 */
type PanelWindowMotionOptions = {
  durationMs?: number;
  frameMs?: number;
};

/**
 * PanelWindowMotionPort 是侧边窗移动动画依赖的最小窗口能力。
 */
type PanelWindowMotionPort = Pick<
  BrowserWindow,
  "getBounds" | "setBounds" | "show" | "isVisible"
>;

/**
 * createPanelWindowMotion 创建侧边窗位移控制器。
 *
 * @param window 需要移动的侧边窗。
 * @param options 动画时长与帧间隔配置。
 * @returns 提供 openAt 方法的位移控制器。
 */
export function createPanelWindowMotion(
  window: PanelWindowMotionPort,
  options: PanelWindowMotionOptions = {}
) {
  const durationMs = options.durationMs ?? 140;
  const frameMs = options.frameMs ?? 16;
  let animationTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * clearAnimation 清理上一轮未完成的位移动画。
   * 业务意图：用户快速拖拽或连续切换人物时，新目标应立即接管旧过渡，避免面板来回拉扯。
   */
  function clearAnimation(): void {
    if (!animationTimer) {
      return;
    }

    clearTimeout(animationTimer);
    animationTimer = null;
  }

  /**
   * readTargetBounds 把 placement 合并成真实窗口 setBounds 所需结构。
   */
  function readTargetBounds(
    placement: PanelPlacement,
    panelSize: { width: number; height: number }
  ) {
    return {
      x: placement.x,
      y: placement.y,
      width: panelSize.width,
      height: panelSize.height
    };
  }

  return {
    /**
     * openAt 将侧边窗移动到目标 placement。
     * 首次展示直接定位并显示；已经可见时使用短缓动，减轻翻边或回拢时的突兀跳变。
     */
    openAt(
      placement: PanelPlacement,
      panelSize: { width: number; height: number }
    ): void {
      clearAnimation();

      const targetBounds = readTargetBounds(placement, panelSize);

      if (!window.isVisible()) {
        window.setBounds(targetBounds);
        window.show();
        return;
      }

      const startBounds = window.getBounds();
      const startedAt = Date.now();

      const step = () => {
        const elapsedMs = Date.now() - startedAt;
        const progress = Math.min(1, elapsedMs / durationMs);
        const easedProgress = 1 - Math.pow(1 - progress, 3);

        window.setBounds({
          x: Math.round(startBounds.x + (targetBounds.x - startBounds.x) * easedProgress),
          y: Math.round(startBounds.y + (targetBounds.y - startBounds.y) * easedProgress),
          width: targetBounds.width,
          height: targetBounds.height
        });

        if (progress >= 1) {
          animationTimer = null;
          return;
        }

        animationTimer = setTimeout(step, frameMs);
      };

      step();
    },

    /**
     * stop 清理未完成动画，供窗口销毁或测试收口使用。
     */
    stop(): void {
      clearAnimation();
    }
  };
}
