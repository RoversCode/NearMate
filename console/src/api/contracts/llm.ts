export type LlmRoleName = "foreground" | "summary" | "cognitive" | "character_authoring";

type LlmProviderCredentialStatus = {
  base_url: string;
  api_key_configured: boolean;
};

type LlmProviderCredentialInput = {
  base_url: string;
  api_key: string;
};

export type LlmRoleStatus = {
  enabled: boolean;
  is_ready: boolean;
  integration_id: string | null;
  provider_id: string | null;
  missing: string[];
  errors: string[];
};

type LlmConfigStatus = {
  is_ready: boolean;
  missing: string[];
  errors: string[];
  roles: Record<LlmRoleName, LlmRoleStatus>;
};

export type LlmConfig = {
  schema_version: 2;
  credentials: Record<string, LlmProviderCredentialStatus>;
  role_bindings: Record<LlmRoleName, string | null>;
  status: LlmConfigStatus;
};

export type LlmConfigUpdatePayload = {
  schema_version: 2;
  credentials: Record<string, LlmProviderCredentialInput>;
  role_bindings: Record<LlmRoleName, string | null>;
};

export type LlmProviderSpec = {
  provider_id: string;
  display_name: string;
  default_base_url: string;
  default_base_url_allowed: boolean;
  endpoint_normalizer: "standard_url" | "qwen_workspace";
};

export type LlmIntegrationSpec = {
  integration_id: string;
  provider_id: string;
  model: string;
  display_name: string;
  api_surface: "chat_completions";
  capabilities: string[];
};

export type LlmRoleContract = {
  role_id: LlmRoleName;
  display_name: string;
  description: string;
  required: boolean;
  default_integration_id: string;
  required_capabilities: string[];
  allowed_integration_ids: string[];
};

export type SupportedLlmModelsResponse = {
  providers: LlmProviderSpec[];
  integrations: LlmIntegrationSpec[];
  role_contracts: LlmRoleContract[];
};

const LLM_ROLE_NAMES: readonly LlmRoleName[] = [
  "foreground",
  "summary",
  "cognitive",
  "character_authoring"
];

function isPlainObject(payload: unknown): payload is Record<string, unknown> {
  return payload !== null && typeof payload === "object" && !Array.isArray(payload);
}

function isStringArray(payload: unknown): payload is string[] {
  return Array.isArray(payload) && payload.every((item) => typeof item === "string");
}

function hasExactKeys(payload: Record<string, unknown>, expectedKeys: readonly string[]): boolean {
  const actualKeys = Object.keys(payload).sort();
  const sortedExpectedKeys = [...expectedKeys].sort();
  return actualKeys.length === sortedExpectedKeys.length && actualKeys.every((key, index) => key === sortedExpectedKeys[index]);
}

function isNullableString(payload: unknown): payload is string | null {
  return payload === null || typeof payload === "string";
}

function isLlmRoleName(payload: unknown): payload is LlmRoleName {
  return typeof payload === "string" && LLM_ROLE_NAMES.includes(payload as LlmRoleName);
}

function isRoleStatus(payload: unknown): payload is LlmRoleStatus {
  if (!isPlainObject(payload)) {
    return false;
  }
  return (
    hasExactKeys(payload, ["enabled", "is_ready", "integration_id", "provider_id", "missing", "errors"]) &&
    typeof payload.enabled === "boolean" &&
    typeof payload.is_ready === "boolean" &&
    isNullableString(payload.integration_id) &&
    isNullableString(payload.provider_id) &&
    isStringArray(payload.missing) &&
    isStringArray(payload.errors)
  );
}

function isConfigStatus(payload: unknown): payload is LlmConfigStatus {
  if (!isPlainObject(payload) || !isPlainObject(payload.roles)) {
    return false;
  }
  const roles = payload.roles;
  return (
    hasExactKeys(payload, ["is_ready", "missing", "errors", "roles"]) &&
    hasExactKeys(roles, LLM_ROLE_NAMES) &&
    typeof payload.is_ready === "boolean" &&
    isStringArray(payload.missing) &&
    isStringArray(payload.errors) &&
    LLM_ROLE_NAMES.every((roleName) => isRoleStatus(roles[roleName]))
  );
}

/**
 * 判断未知响应是否为严格 schema v2 LLM 配置。
 *
 * Args:
 *   payload: /api/llm/config 的 JSON 解析结果。
 *
 * Returns:
 *   响应只包含 v2 凭据、显式职责绑定和逐职责状态时返回 true。
 */
export function isLlmConfig(payload: unknown): payload is LlmConfig {
  if (!isPlainObject(payload) || !isPlainObject(payload.credentials) || !isPlainObject(payload.role_bindings)) {
    return false;
  }
  const credentials = payload.credentials;
  const roleBindings = payload.role_bindings;
  const credentialsAreValid = Object.values(credentials).every(
    (credential) =>
      isPlainObject(credential) &&
      hasExactKeys(credential, ["base_url", "api_key_configured"]) &&
      typeof credential.base_url === "string" &&
      typeof credential.api_key_configured === "boolean"
  );
  return (
    hasExactKeys(payload, ["schema_version", "credentials", "role_bindings", "status"]) &&
    payload.schema_version === 2 &&
    credentialsAreValid &&
    hasExactKeys(roleBindings, LLM_ROLE_NAMES) &&
    LLM_ROLE_NAMES.every((roleName) => isNullableString(roleBindings[roleName])) &&
    isConfigStatus(payload.status)
  );
}

function isProviderSpec(payload: unknown): payload is LlmProviderSpec {
  if (!isPlainObject(payload)) {
    return false;
  }
  return (
    hasExactKeys(payload, [
      "provider_id",
      "display_name",
      "default_base_url",
      "default_base_url_allowed",
      "endpoint_normalizer"
    ]) &&
    typeof payload.provider_id === "string" &&
    typeof payload.display_name === "string" &&
    typeof payload.default_base_url === "string" &&
    typeof payload.default_base_url_allowed === "boolean" &&
    (payload.endpoint_normalizer === "standard_url" || payload.endpoint_normalizer === "qwen_workspace")
  );
}

function isIntegrationSpec(payload: unknown): payload is LlmIntegrationSpec {
  if (!isPlainObject(payload)) {
    return false;
  }
  return (
    hasExactKeys(payload, [
      "integration_id",
      "provider_id",
      "model",
      "display_name",
      "api_surface",
      "capabilities"
    ]) &&
    typeof payload.integration_id === "string" &&
    typeof payload.provider_id === "string" &&
    typeof payload.model === "string" &&
    typeof payload.display_name === "string" &&
    payload.api_surface === "chat_completions" &&
    isStringArray(payload.capabilities)
  );
}

function isRoleContract(payload: unknown): payload is LlmRoleContract {
  if (!isPlainObject(payload)) {
    return false;
  }
  return (
    hasExactKeys(payload, [
      "role_id",
      "display_name",
      "description",
      "required",
      "default_integration_id",
      "required_capabilities",
      "allowed_integration_ids"
    ]) &&
    isLlmRoleName(payload.role_id) &&
    typeof payload.display_name === "string" &&
    typeof payload.description === "string" &&
    typeof payload.required === "boolean" &&
    typeof payload.default_integration_id === "string" &&
    isStringArray(payload.required_capabilities) &&
    isStringArray(payload.allowed_integration_ids)
  );
}

/**
 * 判断未知响应是否为已验证接入注册表。
 *
 * Args:
 *   payload: /api/llm/supported-models 的 JSON 解析结果。
 *
 * Returns:
 *   响应严格包含厂商、接入规格和四个职责合同时返回 true。
 */
export function isSupportedLlmModelsResponse(payload: unknown): payload is SupportedLlmModelsResponse {
  if (
    !isPlainObject(payload) ||
    !hasExactKeys(payload, ["providers", "integrations", "role_contracts"]) ||
    !Array.isArray(payload.providers) ||
    !Array.isArray(payload.integrations) ||
    !Array.isArray(payload.role_contracts)
  ) {
    return false;
  }
  const roleIds = payload.role_contracts
    .filter(isRoleContract)
    .map((contract) => contract.role_id)
    .sort();
  return (
    payload.providers.every(isProviderSpec) &&
    payload.integrations.every(isIntegrationSpec) &&
    payload.role_contracts.every(isRoleContract) &&
    roleIds.length === LLM_ROLE_NAMES.length &&
    roleIds.every((roleId, index) => roleId === [...LLM_ROLE_NAMES].sort()[index])
  );
}
