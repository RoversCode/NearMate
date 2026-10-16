import {
  type LlmConfig,
  type LlmConfigUpdatePayload,
  type SupportedLlmModelsResponse,
  isLlmConfig,
  isSupportedLlmModelsResponse
} from "../contracts/llm";
import { fetchJson, putJson } from "../httpClient";

/**
 * 读取产品内部允许用户选择的 LLM 模型列表。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   厂商凭据规格、模型接入规格和显式职责合同。
 */
export async function fetchSupportedLlmModels(baseUrl: string): Promise<SupportedLlmModelsResponse> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/llm/supported-models", "读取支持模型失败");

  // 模型列表直接决定下拉框可选项，必须拒绝不完整响应，避免用户保存后端不支持的组合。
  if (!isSupportedLlmModelsResponse(payload)) {
    throw new Error("读取支持模型失败：响应格式不符合控制台契约");
  }

  return payload;
}

/**
 * 读取当前 LLM 配置状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   不含明文 API Key 的 LLM 配置快照。
 */
export async function fetchLlmConfig(baseUrl: string): Promise<LlmConfig> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/llm/config", "读取 LLM 配置失败");

  // API Key 只能以 configured 布尔值出现，契约异常时直接阻止页面继续渲染错误状态。
  if (!isLlmConfig(payload)) {
    throw new Error("读取 LLM 配置失败：响应格式不符合控制台契约");
  }

  return payload;
}

/**
 * 保存 schema v2 厂商凭据和显式职责绑定。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   payload: 已裁剪的厂商凭据和四个职责绑定。
 *
 * Returns:
 *   后端校验并持久化后的安全配置快照。
 */
export async function updateLlmConfig(baseUrl: string, payload: LlmConfigUpdatePayload): Promise<LlmConfig> {
  const responsePayload = await putJson<unknown>(baseUrl, "/api/llm/config", payload, "保存 LLM 配置失败");

  if (!isLlmConfig(responsePayload)) {
    throw new Error("保存 LLM 配置失败：响应格式不符合控制台契约");
  }

  return responsePayload;
}
