import { resolveControlConsoleUrl, resolveControlHttpRootUrl } from "../../shared/controlHttpUrl";

type BootstrapConsoleLauncherOptions = {
  controlUrl: string;
  consoleSection?: string;
  openExternal(url: string): Promise<unknown>;
  isAppQuitting(): boolean;
  onFailure(error: unknown): void;
  fetchImpl?: typeof fetch;
};

/**
 * 安装版启动后只检查一次首次初始化状态，需要配置时自动打开本地控制台。
 *
 * Args:
 *   options: 已就绪的控制面地址、浏览器入口、退出状态和错误记录函数。
 * Returns:
 *   可重复调用的一次性启动函数；错误留给现有手动入口处理。
 */
export function createBootstrapConsoleLauncher(options: BootstrapConsoleLauncherOptions) {
  let hasAttempted = false;
  const controlRoot = resolveControlHttpRootUrl(options.controlUrl);
  const fetchImpl = options.fetchImpl ?? fetch;
  return async function openBootstrapConsoleOnce(): Promise<void> {
    if (hasAttempted || options.isAppQuitting()) return;
    // 请求开始前就记录尝试，避免窗口重建或并发调用反复弹出浏览器。
    hasAttempted = true;
    try {
      const response = await fetchImpl(`${controlRoot}/api/bootstrap/status`, { signal: AbortSignal.timeout(5_000) });
      if (!response.ok) throw new Error(`首次初始化状态读取失败：${response.status}`);
      const status: unknown = await response.json();
      if (!status || typeof status !== "object" || Array.isArray(status) || typeof (status as { is_initialized?: unknown }).is_initialized !== "boolean") {
        throw new Error("首次初始化状态响应无效");
      }
      if ((status as { is_initialized: boolean }).is_initialized || options.isAppQuitting()) return;
      const consoleUrl = new URL(resolveControlConsoleUrl(controlRoot));
      if (options.consoleSection) {
        consoleUrl.searchParams.set("section", options.consoleSection);
      }
      await options.openExternal(consoleUrl.toString());
    } catch (error) {
      // 自动入口失败不改变前端授权门禁，用户仍可从头像或托盘主动打开控制台。
      options.onFailure(error);
    }
  };
}
