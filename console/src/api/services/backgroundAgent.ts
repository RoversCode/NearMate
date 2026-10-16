import {
  type BackgroundAgentConfig,
  type BackgroundAgentConfigUpdate,
  isBackgroundAgentConfig
} from "../contracts/backgroundAgent";
import { normalizeConsoleSettings } from "../contracts/settings";
import { fetchJson, putJson } from "../httpClient";

/**
 * 读取后台智能体 Claude Code 配置。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *
 * Returns:
 *   不含凭据明文的安全配置。
 */
export async function fetchBackgroundAgentConfig(baseUrl: string): Promise<BackgroundAgentConfig> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/background-agent/config", "读取后台工作配置失败");
  if (!isBackgroundAgentConfig(payload)) {
    throw new Error("读取后台工作配置失败：响应格式不符合契约");
  }
  return normalizeBackgroundAgentConfig(payload);
}

/**
 * 保存后台智能体配置。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *   payload: 非密钥设置和独立凭据提交信息。
 *
 * Returns:
 *   Backend 保存后的安全配置。
 */
export async function updateBackgroundAgentConfig(
  baseUrl: string,
  payload: BackgroundAgentConfigUpdate
): Promise<BackgroundAgentConfig> {
  const savedPayload = await putJson<unknown>(baseUrl, "/api/background-agent/config", payload, "保存后台工作配置失败");
  if (!isBackgroundAgentConfig(savedPayload)) {
    throw new Error("保存后台工作配置失败：响应格式不符合契约");
  }
  return normalizeBackgroundAgentConfig(savedPayload);
}

/**
 * 规范化后台配置中的可选设置快照，避免把后端额外字段原样带进 React 状态。
 *
 * Args:
 *   payload: 已通过后台配置契约校验的响应。
 *
 * Returns:
 *   保留安全配置字段，并复制完整设置快照的规范化对象。
 */
function normalizeBackgroundAgentConfig(payload: BackgroundAgentConfig): BackgroundAgentConfig {
  return payload.settings === undefined
    ? payload
    : {
        ...payload,
        settings: normalizeConsoleSettings(payload.settings)
      };
}
