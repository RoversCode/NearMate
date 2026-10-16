/**
 * RENDERER_DIAGNOSTIC_CHANNEL 是 renderer 主动上报诊断的唯一 IPC 通道。
 */
export const RENDERER_DIAGNOSTIC_CHANNEL = "renderer:diagnostic";

/**
 * RendererDiagnosticLevel 约束 renderer 可以上报的日志等级。
 */
export type RendererDiagnosticLevel = "info" | "warn" | "error";

/**
 * RendererDiagnosticPayload 是 renderer 交给主进程的最小诊断载荷。
 */
export type RendererDiagnosticPayload = {
  level: RendererDiagnosticLevel;
  message: string;
  details?: unknown;
};
