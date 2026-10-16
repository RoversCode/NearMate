/**
 * RendererDiagnosticBridge 是 renderer 主动向主进程终端上报诊断时使用的最小桥接契约。
 * avatar 与 panel 当前都复用同一组方法签名，避免每个窗口各自定义一套日志入口。
 */
export type RendererDiagnosticBridge = {
  reportDiagnostic(
    level: "info" | "warn" | "error",
    message: string,
    details?: unknown
  ): void;
};

/**
 * reportRendererDiagnostic 负责把 renderer 内部中文诊断稳定送到 preload 暴露的显式 IPC。
 * 核心入参是可选 bridge、日志级别、消息与附加详情；输出为空。
 * 边界异常：bridge 缺失时回退到原生 console，保证浏览器预览和单测环境仍有可见诊断。
 */
export function reportRendererDiagnostic(
  bridge: RendererDiagnosticBridge | undefined,
  level: "info" | "warn" | "error",
  message: string,
  details?: unknown
): void {
  if (bridge) {
    bridge.reportDiagnostic(level, message, details);
    return;
  }

  /**
   * 浏览器预览、单测或 preload 缺失时，仍然保留本地 console 输出。
   * 这样不会因为显式 IPC 不存在，就把调试信息整个吞掉。
   */
  const consoleMethod =
    level === "error" ? console.error : level === "warn" ? console.warn : console.info;

  if (details === undefined) {
    consoleMethod(message);
    return;
  }

  consoleMethod(message, details);
}
