import {
  type ToolWaitingFamilyPolicyPayload,
  type ToolWaitingPoliciesPayload,
  type ToolWaitingToolPolicyPayload,
  isToolWaitingPoliciesPayload
} from "../contracts/toolWaitingPolicies";
import { fetchJson, putJson } from "../httpClient";

/**
 * 读取工具等待反馈策略和后端工具目录。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   后端工具目录和当前等待策略。
 */
export async function fetchToolWaitingPolicies(baseUrl: string): Promise<ToolWaitingPoliciesPayload> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/tool-waiting-policies", "读取工具等待策略失败");
  if (!isToolWaitingPoliciesPayload(payload)) {
    throw new Error("读取工具等待策略失败：响应格式不符合控制台契约");
  }
  return payload;
}

/**
 * 保存 family 级工具等待反馈策略。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   familyId: 后端工具族编号。
 *   payload: 等待反馈策略。
 *
 * Returns:
 *   最新等待策略视图。
 */
export async function updateToolWaitingFamilyPolicy(
  baseUrl: string,
  familyId: string,
  payload: ToolWaitingFamilyPolicyPayload
): Promise<ToolWaitingPoliciesPayload> {
  const responsePayload = await putJson<unknown>(
    baseUrl,
    `/api/tool-waiting-policies/families/${encodeURIComponent(familyId)}`,
    payload,
    "保存工具族等待策略失败"
  );
  if (!isToolWaitingPoliciesPayload(responsePayload)) {
    throw new Error("保存工具族等待策略失败：响应格式不符合控制台契约");
  }
  return responsePayload;
}

/**
 * 保存或清除单工具等待反馈 override。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   toolName: 后端工具名。
 *   payload: 单工具覆盖策略。
 *
 * Returns:
 *   最新等待策略视图。
 */
export async function updateToolWaitingToolPolicy(
  baseUrl: string,
  toolName: string,
  payload: ToolWaitingToolPolicyPayload
): Promise<ToolWaitingPoliciesPayload> {
  const responsePayload = await putJson<unknown>(
    baseUrl,
    `/api/tool-waiting-policies/tools/${encodeURIComponent(toolName)}`,
    payload,
    "保存工具等待策略失败"
  );
  if (!isToolWaitingPoliciesPayload(responsePayload)) {
    throw new Error("保存工具等待策略失败：响应格式不符合控制台契约");
  }
  return responsePayload;
}
