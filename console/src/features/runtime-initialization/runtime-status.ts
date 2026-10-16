import { isNonNegativeSafeInteger, isPlainObject } from "@/api/contracts/validation";
import { isStandardUuid } from "@/lib/uuid";

/**
 * 描述 Backend 必要组件的初始化进度。
 *
 * Args:
 *   不适用；字段由 Backend 的完整公开快照提供。
 * Returns:
 *   七字段状态，进程身份和单调版本共同防止旧进度覆盖新进度。
 */
export type RuntimeInitializationStatus = {
  runtime_id: string;
  operation_id: string | null;
  revision: number;
  state: "idle" | "initializing" | "ready" | "failed";
  phase: "idle" | "cleanup" | "input_models" | "assistant" | "media" | "ready" | "failed";
  message: string;
  retryable: boolean;
};

/**
 * 校验初始化身份、阶段和版本，只接收 Backend 的完整状态。
 *
 * Args:
 *   value: 未经校验的 HTTP 或 WebSocket 载荷。
 * Returns:
 *   完整初始化快照；格式不正确时抛出安全错误。
 */
export function parseRuntimeInitializationStatus(value: unknown): RuntimeInitializationStatus {
  if (!isPlainObject(value) || Object.keys(value).length !== 7 ||
      typeof value.runtime_id !== "string" || !isStandardUuid(value.runtime_id) ||
      !(value.operation_id === null || typeof value.operation_id === "string" && isStandardUuid(value.operation_id)) ||
      !isNonNegativeSafeInteger(value.revision) || typeof value.message !== "string" || !value.message.trim() ||
      typeof value.retryable !== "boolean") {
    throw new Error("本机服务返回的初始化进度不完整。");
  }
  const phases: Record<RuntimeInitializationStatus["state"], readonly string[]> = {
    idle: ["idle"], initializing: ["cleanup", "input_models", "assistant", "media"],
    ready: ["ready"], failed: ["failed"],
  };
  if (typeof value.state !== "string" || !Object.hasOwn(phases, value.state) ||
      typeof value.phase !== "string" || !phases[value.state as RuntimeInitializationStatus["state"]].includes(value.phase) ||
      (value.state === "initializing" && value.operation_id === null) ||
      (value.retryable && value.state !== "failed")) {
    throw new Error("本机服务返回的初始化阶段不正确。");
  }
  return value as RuntimeInitializationStatus;
}
