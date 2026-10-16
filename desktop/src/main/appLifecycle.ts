import { app, BrowserWindow } from "electron";
import type { ShellRuntime } from "./shellRuntime";

type AppLifecycleInput = {
  getRuntime(): ShellRuntime | null;
  recreateRuntime(): Promise<void>;
  teardownBeforeQuit(): void | Promise<void>;
};

export type AppLifecycleRuntime = {
  isAppQuitting(): boolean;
};

let hasRegisteredLifecycleHandlers = false;
let isAppQuitting = false;

/**
 * registerAppLifecycleHandlers 挂接 Electron 应用级生命周期。
 *
 * Args:
 *   input: 当前 shell runtime 读取、重建与退出清理命令。
 *
 * Returns:
 *   可供窗口运行时读取的退出状态。
 */
export function registerAppLifecycleHandlers(input: AppLifecycleInput): AppLifecycleRuntime {
  if (hasRegisteredLifecycleHandlers) {
    return {
      isAppQuitting: () => isAppQuitting
    };
  }

  hasRegisteredLifecycleHandlers = true;

  /**
   * 关闭全部窗口后的默认行为保持与旧入口一致：
   * 非 macOS 平台直接退出应用，macOS 继续常驻等待再次激活。
   */
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") {
      app.quit();
    }
  });

  /**
   * 应用退出前主动停止长生命周期服务，避免主进程退出后留下后台资源。
   * 清理本身失败时也必须继续退出，否则用户只能强杀进程。
   */
  let hasFinishedAsyncTeardown = false;
  let isAsyncTeardownInProgress = false;
  app.on("before-quit", event => {
    if (hasFinishedAsyncTeardown) return;
    isAppQuitting = true;
    if (isAsyncTeardownInProgress) {
      // 用户在收口期间再次触发退出时继续拦住默认流程，但绝不能并发执行第二套清理。
      event.preventDefault();
      return;
    }
    let teardown: void | Promise<void>;
    try {
      teardown = input.teardownBeforeQuit();
    } catch (error) {
      // 同步清理抛错不拦截退出：不 preventDefault，让 Electron 按原流程退出。
      console.error("[companion-main] 退出清理失败，继续退出", error);
      return;
    }
    if (teardown) {
      isAsyncTeardownInProgress = true;
      event.preventDefault();
      void teardown.then(
        () => {
          isAsyncTeardownInProgress = false;
          hasFinishedAsyncTeardown = true;
          app.quit();
        },
        error => {
          console.error("[companion-main] 运行组件未能退出", error);
          isAsyncTeardownInProgress = false;
          hasFinishedAsyncTeardown = true;
          app.quit();
        }
      );
    }
  });

  /**
   * macOS Dock 激活时，如果窗口壳已完全销毁则重建；否则只显示角色窗。
   */
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      void input.recreateRuntime();
      return;
    }

    input.getRuntime()?.avatarWindow.show();
  });

  return {
    isAppQuitting: () => isAppQuitting
  };
}
