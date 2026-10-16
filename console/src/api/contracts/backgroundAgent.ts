import { isConsoleSettings, type ConsoleSettings } from "./settings";

export type BackgroundAgentPermissionMode = "acceptEdits" | "bypassPermissions";
export type BackgroundAgentCredentialType = "api_key" | "auth_token";
export type BackgroundAgentEffortLevel = "low" | "medium" | "high" | "xhigh" | "max";

export type BackgroundAgentConfig = {
  /** 后台配置读取/提交时对应的完整公开设置快照；旧 Backend 可省略。 */
  settings?: ConsoleSettings;
  environment: Record<string, string>;
  model: string;
  effort_level: BackgroundAgentEffortLevel | null;
  max_turns: number;
  permission_mode: BackgroundAgentPermissionMode;
  credential_type: BackgroundAgentCredentialType;
  credential_configured: boolean;
};

export type BackgroundAgentConfigUpdate = {
  settings: Pick<
    BackgroundAgentConfig,
    "environment" | "model" | "effort_level" | "max_turns" | "permission_mode"
  >;
  credential_type: BackgroundAgentCredentialType;
  credential_value: string;
  clear_credential: boolean;
};

/**
 * 校验后台智能体配置接口返回值。
 *
 * Args:
 *   payload: JSON 解析后的未知对象。
 *
 * Returns:
 *   所有公开字段和环境变量都符合契约时返回 true。
 */
export function isBackgroundAgentConfig(payload: unknown): payload is BackgroundAgentConfig {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const candidate = payload as Record<string, unknown>;
  const environment = candidate.environment;
  return (
    (candidate.settings === undefined || isConsoleSettings(candidate.settings)) &&
    environment !== null &&
    typeof environment === "object" &&
    !Array.isArray(environment) &&
    Object.entries(environment).every(
      ([key, value]) => key.trim().length > 0 && typeof value === "string"
    ) &&
    typeof candidate.model === "string" &&
    (candidate.effort_level === null ||
      candidate.effort_level === "low" ||
      candidate.effort_level === "medium" ||
      candidate.effort_level === "high" ||
      candidate.effort_level === "xhigh" ||
      candidate.effort_level === "max") &&
    typeof candidate.max_turns === "number" &&
    Number.isInteger(candidate.max_turns) &&
    candidate.max_turns >= 0 &&
    candidate.max_turns <= 200 &&
    (candidate.permission_mode === "acceptEdits" ||
      candidate.permission_mode === "bypassPermissions") &&
    (candidate.credential_type === "api_key" ||
      candidate.credential_type === "auth_token") &&
    typeof candidate.credential_configured === "boolean"
  );
}
