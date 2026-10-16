import type { TtsModelDescriptor, TtsSetupTask } from "./bootstrap";

/** 用户明确确认的本地 ONNX 与 llama.cpp 推理链执行方式。 */
export type TtsExecutionSelection = { onnx_provider: "cuda" | "directml" | "cpu"; llama_backend: "cuda" | "vulkan" | "cpu" };
type TtsBackendCapability = { kind: "onnx" | "llama"; backend: string; available: boolean; reason?: string | null; evidence?: unknown };
export type TtsCapabilities = { backends: TtsBackendCapability[]; recommendation: TtsExecutionSelection };
export type TtsConfiguration = { confirmed: boolean; selection: TtsExecutionSelection | null; model_directory: string; capabilities: TtsCapabilities | null };
export type TtsConfigurationInput = { schema_version: 1; selection: TtsExecutionSelection; model_directory: string };

/**
 * 保存模型文件目录和独立下载进度，便于页面同时展示当前语音和其他模型下载。
 *
 * Args:
 *   不适用；字段来自 Backend 模型目录响应。
 * Returns:
 *   download_task 缺少时使用 null，旧 Backend 响应仍可读取。
 */
export type TtsModelCatalog = { provider: string; models: TtsModelDescriptor[]; configured_model: string | null; download_task: TtsSetupTask | null };

/**
 * 校验执行组合，拒绝旧的自动选择和未知的执行后端。
 *
 * Args:
 *   value: 接口返回的执行选择。
 * Returns:
 *   是否符合固定两条执行链契约。
 */
function isTtsExecutionSelection(value: unknown): value is TtsExecutionSelection {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return ["cuda", "directml", "cpu"].includes(String(item.onnx_provider)) && ["cuda", "vulkan", "cpu"].includes(String(item.llama_backend));
}

/**
 * 校验本机检测结果，错误响应不能把不可用设备显示为可选择。
 *
 * Args:
 *   value: 检测接口的未知响应。
 * Returns:
 *   有明确可用性和推荐组合的检测结果。
 */
export function parseTtsCapabilities(value: unknown): TtsCapabilities {
  if (!value || typeof value !== "object") throw new Error("执行方式检测响应无效");
  const item = value as Record<string, unknown>;
  if (!Array.isArray(item.backends) || !isTtsExecutionSelection(item.recommendation) || !item.backends.every(backend =>
    backend && ["onnx", "llama"].includes(backend.kind) && typeof backend.backend === "string" && typeof backend.available === "boolean" &&
    (backend.reason === undefined || backend.reason === null || typeof backend.reason === "string"))) throw new Error("执行方式检测响应无效");
  return value as TtsCapabilities;
}

/**
 * 读取持久配置时检查用户确认事实，不把字符串或缺失字段当成已确认。
 *
 * Args:
 *   value: 配置接口的未知响应。
 * Returns:
 *   合法的配置快照。
 */
export function parseTtsConfiguration(value: unknown): TtsConfiguration {
  if (!value || typeof value !== "object") throw new Error("语音执行配置响应无效");
  const item = value as Record<string, unknown>;
  if (typeof item.confirmed !== "boolean" || typeof item.model_directory !== "string" ||
      !(item.selection === null || isTtsExecutionSelection(item.selection)) || (item.confirmed && item.selection === null)) throw new Error("语音执行配置响应无效");
  if (item.capabilities !== null) parseTtsCapabilities(item.capabilities);
  return value as TtsConfiguration;
}

/**
 * 检查模型目录的来源与下载资格，缺少资格字段时不能启动下载。
 * Args:
 *   value: 模型目录接口返回的未知值。
 * Returns:
 *   五模型目录或合法的空目录。
 */
export function parseTtsModelCatalog(value: unknown): TtsModelCatalog {
  if (!value || typeof value !== "object") throw new Error("语音模型目录响应无效");
  const item = value as Record<string, unknown>;
  if (typeof item.provider !== "string" || !(item.configured_model === null || typeof item.configured_model === "string") ||
      !Array.isArray(item.models) || !item.models.every(model => model && typeof model.model_id === "string" && typeof model.display_name === "string" &&
      (model.repository === null || typeof model.repository === "string") && (model.estimated_size_bytes === null || Number.isSafeInteger(model.estimated_size_bytes) && model.estimated_size_bytes >= 0) &&
      typeof model.verification_status === "string" && typeof model.download_available === "boolean")) throw new Error("语音模型目录响应无效");
  const models = (item.models as TtsModelDescriptor[]).map(model => {
    if ((model.local_state !== undefined && !["unknown", "not_downloaded", "incomplete", "downloaded", "unavailable"].includes(model.local_state)) ||
        (model.is_current !== undefined && typeof model.is_current !== "boolean") ||
        (model.integrity_status !== undefined && !["verified", "not_verified"].includes(model.integrity_status)) ||
        ![model.installed_size_bytes, model.total_size_bytes].every(size => size === undefined || size === null || Number.isSafeInteger(size) && size >= 0)) {
      throw new Error("语音模型文件状态响应无效");
    }
    // 旧接口只有发行目录。缺少磁盘证据时保留未知，不能替用户猜成“未下载”。
    return { ...model, local_state: model.local_state ?? "unknown", installed_size_bytes: model.installed_size_bytes ?? null,
      total_size_bytes: model.total_size_bytes ?? null, is_current: model.is_current ?? model.model_id === item.configured_model,
      integrity_status: model.integrity_status ?? "not_verified" };
  });
  return { provider: item.provider, models, configured_model: item.configured_model,
    download_task: item.download_task === undefined || item.download_task === null ? null : parseTtsModelDownloadStatus(item.download_task) } as TtsModelCatalog;
}

/**
 * 校验只下载文件的任务，拒绝把下载完成误报为可以确认角色语音。
 *
 * Args:
 *   value: 下载接口返回的未知快照。
 * Returns:
 *   没有试听确认资格的下载任务状态。
 */
export function parseTtsModelDownloadStatus(value: unknown): TtsSetupTask {
  const task = parseTtsSetupStatus(value);
  if (task.can_confirm || task.is_ready === true ||
      !["idle", "queued", "download", "copy", "verify", "publish", "release", "completed", "failed", "cancelled", "interrupted"].includes(task.phase)) {
    throw new Error("模型下载状态响应无效");
  }
  return task;
}

/**
 * 检查任务身份和动作资格，避免错误响应让页面误报可确认或可重试。
 * Args:
 *   value: 语音任务接口返回的未知值。
 * Returns:
 *   可安全展示的任务状态。
 */
export function parseTtsSetupStatus(value: unknown): TtsSetupTask {
  if (!value || typeof value !== "object") throw new Error("语音任务状态响应无效");
  const item = value as Record<string, unknown>;
  if (typeof item.phase !== "string" || !(item.task_id === null || typeof item.task_id === "string") || !(item.model_id === null || typeof item.model_id === "string") ||
      !(item.error === null || typeof item.error === "string") || typeof item.can_cancel !== "boolean" || typeof item.can_confirm !== "boolean" || typeof item.can_retry !== "boolean") throw new Error("语音任务状态响应无效");
  if (item.progress !== null) {
    if (!item.progress || typeof item.progress !== "object") throw new Error("语音任务进度响应无效");
    const progress = item.progress as Record<string, unknown>;
    if (typeof progress.phase !== "string" || typeof progress.completed_bytes !== "number" || !Number.isFinite(progress.completed_bytes) || progress.completed_bytes < 0 ||
        !(progress.total_bytes === null || typeof progress.total_bytes === "number" && Number.isFinite(progress.total_bytes) && progress.total_bytes >= 0)) throw new Error("语音任务进度响应无效");
  }
  return value as TtsSetupTask;
}
