/**
 * StartupProgressState 保存真实阶段的起点、下一阶段边界和用户提示。
 * progressCeiling 只是等待动画的上限，不代表 Backend 的精确模型进度。
 */
export type StartupProgressState = {
  progress: number;
  progressCeiling?: number;
  title: string;
  detail: string;
};

/**
 * StartupProgressController 管理阶段内增长与成功显示的收口。
 * update 不显示 100%；只有启动编排完成后调用 complete 才能进入成功态。
 */
export type StartupProgressController = {
  update(state: StartupProgressState): void;
  complete(): Promise<void>;
  close(): void;
};

type StartupProgressControllerOptions = {
  render(state: StartupProgressState, waitForPaint: boolean, signal: AbortSignal): Promise<void>;
};

const PROGRESS_ANIMATION_INTERVAL_MS = 200;
// 冷启动有两分钟预算，缓慢靠近边界可以避免十几秒就耗尽可见增长空间。
const PROGRESS_EASING_STEP = 0.005;

/**
 * 创建与 Electron 无关的启动动画，关闭后取消所有等待和迟到更新。
 *
 * Args:
 *   options: 把状态交给窗口绘制的函数；成功绘制须等待页面确认。
 * Returns:
 *   可更新阶段、等待成功展示和取消动画的控制器。
 */
export function createStartupProgressController(options: StartupProgressControllerOptions): StartupProgressController {
  const cancellation = new AbortController();
  let state: StartupProgressState | null = null;
  let stageFloor = 0;
  let stageStartedAt = performance.now();
  let progressCeiling = 0;
  let completion: Promise<void> | null = null;
  let interval: ReturnType<typeof setInterval> | null = null;

  const render = (waitForPaint: boolean): Promise<void> => {
    if (!state || cancellation.signal.aborted) return Promise.resolve();
    // 系统时间校准不能让等待秒数倒退；提示从阶段原文生成，避免每次刷新重复拼接。
    const elapsedSeconds = state.progress < 100 ? Math.floor(Math.max(0, performance.now() - stageStartedAt) / 1_000) : 0;
    const detail = elapsedSeconds >= 10 ? `${state.detail} 已等待 ${elapsedSeconds} 秒。` : state.detail;
    return options.render({ ...state, detail }, waitForPaint, cancellation.signal);
  };

  const stopAnimation = (): void => {
    if (interval !== null) clearInterval(interval);
    interval = null;
  };

  return {
    update(nextState) {
      if (cancellation.signal.aborted || completion) return;
      const requestedFloor = Number.isFinite(nextState.progress) ? Math.max(0, Math.min(99, nextState.progress)) : 0;
      // 旧阶段的迟到通知不能让文字或数字回退；下一阶段只能由真实完成节点推进。
      if (requestedFloor < stageFloor) return;
      if (!state || requestedFloor > stageFloor) stageStartedAt = performance.now();
      stageFloor = requestedFloor;
      const progress = Math.max(state?.progress ?? 0, requestedFloor);
      const requestedCeiling = nextState.progressCeiling ?? requestedFloor;
      progressCeiling = Math.max(progress, Math.min(99.9, Number.isFinite(requestedCeiling) ? requestedCeiling : progress));
      state = { ...nextState, progress };
      void render(false).catch(error => console.warn("[companion-main] 启动进度绘制失败", error));
      if (interval !== null || progressCeiling <= progress) return;
      interval = setInterval(() => {
        if (!state) return;
        // 越靠近边界越慢，慢机器也不会被动画误判为下一阶段已经结束。
        state.progress += (progressCeiling - state.progress) * PROGRESS_EASING_STEP;
        void render(false).catch(error => console.warn("[companion-main] 启动进度绘制失败", error));
      }, PROGRESS_ANIMATION_INTERVAL_MS);
    },
    complete() {
      if (completion) return completion;
      stopAnimation();
      state = { progress: 100, title: "NearMate 已启动", detail: "桌面入口已准备好。" };
      completion = (async () => {
        try {
          await render(true);
          if (cancellation.signal.aborted) return;
          // 成功数字应留在屏幕上，让用户看到交接终点；退出会立即取消这段等待。
          await new Promise<void>(resolve => {
            const finish = (): void => {
              clearTimeout(timer);
              cancellation.signal.removeEventListener("abort", finish);
              resolve();
            };
            const timer = setTimeout(finish, 300);
            cancellation.signal.addEventListener("abort", finish, { once: true });
          });
        } catch (error) {
          if (!cancellation.signal.aborted) console.warn("[companion-main] 启动成功反馈未能完成", error);
        }
      })();
      return completion;
    },
    close() {
      stopAnimation();
      cancellation.abort();
    }
  };
}
