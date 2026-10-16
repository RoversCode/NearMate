import type { BrowserWindow } from "electron";

/**
 * applyWindowNavigationPolicy 把窗口锁定到主进程已经决定的 renderer 入口。
 *
 * Args:
 *   window: 即将加载页面的 Electron 窗口。
 *   trustedEntryUrl: main 进程解析出的唯一可信 HTML 地址。
 */
export function applyWindowNavigationPolicy(
  window: BrowserWindow,
  trustedEntryUrl: string
): void {
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));

  const preventUnknownNavigation = (
    event: { preventDefault(): void },
    navigationUrl: string
  ): void => {
    if (isTrustedRendererNavigation(navigationUrl, trustedEntryUrl)) {
      return;
    }

    event.preventDefault();
  };

  window.webContents.on("will-navigate", preventUnknownNavigation);
  window.webContents.on("will-redirect", preventUnknownNavigation);
}

/**
 * isTrustedRendererNavigation 只允许返回当前窗口自己的入口文档。
 * URL 片段可以变化，因为页内定位不会改变脚本来源；其他 Origin、路径和查询参数全部拒绝。
 */
function isTrustedRendererNavigation(candidateUrl: string, trustedEntryUrl: string): boolean {
  try {
    const candidate = new URL(candidateUrl);
    const trusted = new URL(trustedEntryUrl);
    candidate.hash = "";
    trusted.hash = "";
    return candidate.toString() === trusted.toString();
  } catch {
    return false;
  }
}
