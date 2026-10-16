import {
  type BootstrapInitializePayload,
  type BootstrapStatus,
  type TtsSetupTask,
  type QWeatherApiKeyPayload,
  isBootstrapStatus
} from "../contracts/bootstrap";
import { fetchJson, postJson, putJson, readHttpErrorDetail } from "../httpClient";
import {
  parseTtsCapabilities,
  parseTtsConfiguration,
  parseTtsModelCatalog,
  parseTtsModelDownloadStatus,
  parseTtsSetupStatus,
  type TtsCapabilities,
  type TtsConfiguration,
  type TtsConfigurationInput,
  type TtsModelCatalog
} from "../contracts/ttsSetup";

/**
 * 读取本地控制台首次初始化状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址，不包含尾随接口路径。
 *
 * Returns:
 *   必需密钥与可选天气密钥是否存在，以及控制台是否已经满足首次初始化条件。
 */
export async function fetchBootstrapStatus(baseUrl: string): Promise<BootstrapStatus> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/bootstrap/status", "读取初始化状态失败");

  // bootstrap 状态决定是否允许进入普通控制台，必须严格校验布尔字段，避免异常响应被误判为已初始化。
  if (!isBootstrapStatus(payload)) {
    throw new Error("读取初始化状态失败：响应格式不符合控制台契约");
  }

  return payload;
}

/**
 * 读取首次初始化与 StreamVox 设置共用的受管 TTS 模型目录。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址，不包含尾随接口路径。
 *
 * Returns:
 *   提供商、可选模型列表和当前已配置的模型标识。
 */
export async function fetchTtsModels(baseUrl: string): Promise<TtsModelCatalog> {
  return parseTtsModelCatalog(await fetchJson(baseUrl, "/api/bootstrap/tts/models", "读取 TTS 模型目录失败"));
}

/**
 * 读取模型文件下载任务，与角色语音准备任务分别展示。
 *
 * Args:
 *   baseUrl: Backend 控制面地址。
 * Returns:
 *   只包含文件获取进度的任务状态。
 */
export async function fetchTtsModelDownloadStatus(baseUrl: string): Promise<TtsSetupTask> {
  return parseTtsModelDownloadStatus(await fetchJson(baseUrl, "/api/bootstrap/tts/downloads", "读取模型下载状态失败"));
}

/**
 * 只下载模型权重，保留当前角色已确认的语音。
 *
 * Args:
 *   baseUrl: Backend 控制面地址。
 *   modelId: 发行固定模型编号。
 * Returns:
 *   新文件下载任务状态。
 */
export async function startTtsModelDownload(baseUrl: string, modelId: string): Promise<TtsSetupTask> {
  return parseTtsModelDownloadStatus(await postJson(baseUrl, "/api/bootstrap/tts/downloads", { model_id: modelId }, "启动模型下载失败"));
}

/**
 * 取消文件下载并保留已下载缓存。
 *
 * Args:
 *   baseUrl: Backend 控制面地址。
 *   taskId: 当前文件下载任务编号。
 * Returns:
 *   取消后的权威任务状态。
 */
export async function cancelTtsModelDownload(baseUrl: string, taskId: string): Promise<TtsSetupTask> {
  return parseTtsModelDownloadStatus(await postJson(baseUrl, `/api/bootstrap/tts/downloads/${encodeURIComponent(taskId)}/cancel`, {}, "取消模型下载失败"));
}

/**
 * 从缓存继续失败或中断的文件下载。
 *
 * Args:
 *   baseUrl: Backend 控制面地址。
 *   taskId: 当前可重试下载任务编号。
 * Returns:
 *   新下载任务状态。
 */
export async function retryTtsModelDownload(baseUrl: string, taskId: string): Promise<TtsSetupTask> {
  return parseTtsModelDownloadStatus(await postJson(baseUrl, `/api/bootstrap/tts/downloads/${encodeURIComponent(taskId)}/retry`, {}, "重试模型下载失败"));
}

export async function fetchTtsSetupStatus(baseUrl: string): Promise<TtsSetupTask> {
  return parseTtsSetupStatus(await fetchJson(baseUrl, "/api/bootstrap/tts/status", "读取语音配置状态失败"));
}

export async function startTtsSetup(baseUrl: string, model_id: string): Promise<TtsSetupTask> {
  return parseTtsSetupStatus(await postJson(baseUrl, "/api/bootstrap/tts/tasks", { model_id }, "启动 TTS 配置失败"));
}

export async function cancelTtsSetup(baseUrl: string, taskId: string): Promise<TtsSetupTask> {
  return parseTtsSetupStatus(await postJson(baseUrl, `/api/bootstrap/tts/tasks/${encodeURIComponent(taskId)}/cancel`, {}, "取消 TTS 配置失败"));
}

export async function retryTtsSetup(baseUrl: string, taskId: string): Promise<TtsSetupTask> {
  return parseTtsSetupStatus(await postJson(baseUrl, `/api/bootstrap/tts/tasks/${encodeURIComponent(taskId)}/retry`, {}, "重试 TTS 配置失败"));
}

export async function confirmTtsSetup(baseUrl: string, taskId: string): Promise<BootstrapStatus> {
  const status = await postJson<unknown>(baseUrl, `/api/bootstrap/tts/tasks/${encodeURIComponent(taskId)}/confirm`, {}, "确认 TTS 音色失败");
  if (!isBootstrapStatus(status)) throw new Error("确认语音失败：响应缺少完整初始化状态");
  return status;
}

export async function fetchTtsConfiguration(baseUrl: string): Promise<TtsConfiguration> {
  return parseTtsConfiguration(await fetchJson(baseUrl, "/api/bootstrap/tts/configuration", "读取执行配置失败"));
}

export async function probeTtsCapabilities(baseUrl: string): Promise<TtsCapabilities> {
  return parseTtsCapabilities(await postJson(baseUrl, "/api/bootstrap/tts/probe", {}, "检测执行方式失败"));
}

export async function saveTtsConfiguration(baseUrl: string, payload: TtsConfigurationInput): Promise<TtsConfiguration> {
  return parseTtsConfiguration(await putJson(baseUrl, "/api/bootstrap/tts/configuration", payload, "保存执行配置失败"));
}

export async function fetchTtsPreview(baseUrl: string, taskId: string): Promise<Blob> {
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/api/bootstrap/tts/tasks/${encodeURIComponent(taskId)}/preview`);
  if (!response.ok) throw new Error(`读取试听失败：${await readHttpErrorDetail(response)}`);
  return response.blob();
}

/**
 * 提交本地控制台启动密钥。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   payload: 已由页面去除首尾空白的服务密钥。
 *
 * Returns:
 *   后端接受密钥后正常返回。
 */
export async function initializeBootstrap(baseUrl: string, payload: BootstrapInitializePayload): Promise<void> {
  const normalizedBaseUrl = baseUrl.replace(/\/$/, "");

  // 启动密钥接口的历史 Backend 版本可能返回空的 202；这里只关心提交是否成功，
  // 但失败时仍读取 FastAPI detail，不能退化成没有行动建议的 HTTP 状态码。
  const response = await fetch(`${normalizedBaseUrl}/api/bootstrap/initialize`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    throw new Error(`提交初始化信息失败：${await readHttpErrorDetail(response)}`);
  }
}

/**
 * 验证并保存和风天气 API Host 与 API Key。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   payload: 已裁剪的和风天气 API Host 与 API Key。
 *
 * Returns:
 *   最新服务密钥状态。
 */
export async function updateQWeatherApiKey(baseUrl: string, payload: QWeatherApiKeyPayload): Promise<BootstrapStatus> {
  const responsePayload = await postJson<unknown>(
    baseUrl,
    "/api/service-keys/qweather",
    payload,
    "保存和风天气密钥失败"
  );
  if (!isBootstrapStatus(responsePayload)) {
    throw new Error("保存和风天气密钥失败：响应格式不符合控制台契约");
  }
  return responsePayload;
}
