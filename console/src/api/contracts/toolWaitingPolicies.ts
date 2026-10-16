import { isPlainObject, isStringArray } from "./validation";

type ToolLatencyClass = "instant" | "external_api" | "long_running";

type ToolWaitingPolicy = {
  enabled: boolean;
  phrases: string[];
};

type EffectiveToolWaitingPolicy = ToolWaitingPolicy & {
  source: "tool_override" | "family" | "builtin" | "disabled";
};

export type ToolWaitingTool = {
  name: string;
  display_name: string;
  family_id: string;
  family_display_name: string;
  latency_class: ToolLatencyClass;
  user_override: ToolWaitingPolicy | null;
  effective_policy: EffectiveToolWaitingPolicy;
};

export type ToolWaitingFamily = {
  family_id: string;
  display_name: string;
  latency_class: ToolLatencyClass;
  default_waiting_enabled: boolean;
  default_waiting_phrases: string[];
  builtin_policy: ToolWaitingPolicy;
  user_policy: ToolWaitingPolicy | null;
  effective_policy: EffectiveToolWaitingPolicy;
  tools: ToolWaitingTool[];
};

export type ToolWaitingPoliciesPayload = {
  families: ToolWaitingFamily[];
  user_config: {
    families: Record<string, ToolWaitingPolicy>;
    tools: Record<string, ToolWaitingPolicy>;
  };
};

export type ToolWaitingFamilyPolicyPayload = {
  enabled: boolean;
  phrases: string[];
};

export type ToolWaitingToolPolicyPayload =
  | {
      inherit_family: true;
    }
  | {
      inherit_family: false;
      enabled: boolean;
      phrases: string[];
    };

export function isToolWaitingPoliciesPayload(payload: unknown): payload is ToolWaitingPoliciesPayload {
  if (!isPlainObject(payload) || !Array.isArray(payload.families) || !isPlainObject(payload.user_config)) {
    return false;
  }

  return (
    payload.families.every(isToolWaitingFamily) &&
    isToolWaitingPolicyRecord(payload.user_config.families) &&
    isToolWaitingPolicyRecord(payload.user_config.tools)
  );
}

function isToolWaitingFamily(payload: unknown): payload is ToolWaitingFamily {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.family_id === "string" &&
    typeof payload.display_name === "string" &&
    isToolLatencyClass(payload.latency_class) &&
    typeof payload.default_waiting_enabled === "boolean" &&
    isStringArray(payload.default_waiting_phrases) &&
    isToolWaitingPolicy(payload.builtin_policy) &&
    (payload.user_policy === null || isToolWaitingPolicy(payload.user_policy)) &&
    isEffectiveToolWaitingPolicy(payload.effective_policy) &&
    Array.isArray(payload.tools) &&
    payload.tools.every(isToolWaitingTool)
  );
}

function isToolWaitingTool(payload: unknown): payload is ToolWaitingTool {
  if (!isPlainObject(payload)) {
    return false;
  }

  return (
    typeof payload.name === "string" &&
    typeof payload.display_name === "string" &&
    typeof payload.family_id === "string" &&
    typeof payload.family_display_name === "string" &&
    isToolLatencyClass(payload.latency_class) &&
    (payload.user_override === null || isToolWaitingPolicy(payload.user_override)) &&
    isEffectiveToolWaitingPolicy(payload.effective_policy)
  );
}

function isToolWaitingPolicyRecord(payload: unknown): payload is Record<string, ToolWaitingPolicy> {
  return isPlainObject(payload) && Object.values(payload).every(isToolWaitingPolicy);
}

function isToolWaitingPolicy(payload: unknown): payload is ToolWaitingPolicy {
  if (!isPlainObject(payload)) {
    return false;
  }

  return typeof payload.enabled === "boolean" && isStringArray(payload.phrases);
}

function isEffectiveToolWaitingPolicy(payload: unknown): payload is EffectiveToolWaitingPolicy {
  if (!isToolWaitingPolicy(payload)) {
    return false;
  }

  const candidate = payload as Record<string, unknown>;
  return (
    candidate.source === "tool_override" ||
    candidate.source === "family" ||
    candidate.source === "builtin" ||
    candidate.source === "disabled"
  );
}

function isToolLatencyClass(value: unknown): value is ToolLatencyClass {
  return value === "instant" || value === "external_api" || value === "long_running";
}
