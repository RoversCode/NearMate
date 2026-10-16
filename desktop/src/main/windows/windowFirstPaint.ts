import type { BrowserWindow } from "electron";

/**
 * 在加载页面之前登记首帧事件，等待页面和首帧一起就绪，避免加载卡住时超时失效。
 *
 * Args:
 *   window: 尚未开始加载页面的隐藏窗口。
 *   contentLoaded: 可选的页面加载完成通知；与首帧共用十秒预算，省略时只等待首帧。
 * Returns:
 *   首帧完成的 Promise 和取消等待的收口函数；窗口销毁、取消或超时会拒绝等待。
 */
export function watchWindowFirstPaint(window: BrowserWindow, contentLoaded: Promise<void> = Promise.resolve()): { ready: Promise<void>; dispose(): void } {
  let settle!: (error?: Error) => void;
  let isSettled = false;
  let hasPainted = false;
  let hasLoadedContent = false;
  const ready = new Promise<void>((resolve, reject) => {
    settle = error => {
      if (isSettled) return;
      isSettled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };
  });
  const finishIfReady = (): void => {
    if (hasPainted && hasLoadedContent) settle();
  };
  const onReady = (): void => {
    hasPainted = true;
    finishIfReady();
  };
  const onClosed = (): void => settle(new Error("桌面窗口在页面加载或首次绘制完成前关闭"));
  const timer = setTimeout(() => settle(new Error("桌面窗口加载或首次绘制超时")), 10_000);
  const cleanup = (): void => {
    clearTimeout(timer);
    window.removeListener("ready-to-show", onReady);
    window.removeListener("closed", onClosed);
  };
  window.once("ready-to-show", onReady);
  window.once("closed", onClosed);
  // 首帧可以先于其他页面完成；加载失败或迟到结果仍须被接住，不能留下无人处理的拒绝。
  void contentLoaded.then(() => {
    hasLoadedContent = true;
    finishIfReady();
  }, error => settle(error instanceof Error ? error : new Error("桌面页面加载失败")));
  // 页面加载本身可能先失败；提前接住拒绝，真正的错误仍由 shell 装配等待统一收口。
  void ready.catch(() => {});
  return { ready, dispose: () => settle(new Error("桌面窗口首次绘制等待已取消")) };
}
