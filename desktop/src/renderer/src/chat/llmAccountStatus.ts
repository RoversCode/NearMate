type LlmAccountProviderStatus = "available" | "not_configured" | "unsupported" | "error";

export type LlmAccountProvider = {
  provider_id: string;
  display_name: string;
  active: boolean;
  status: LlmAccountProviderStatus;
  balance: string | null;
  currency: string | null;
  fetched_at: string | null;
  message: string | null;
};

export type LlmAccountStatusSnapshot = {
  providers: LlmAccountProvider[];
};

/**
 * 读取 Backend 返回的厂商账户余额安全快照。
 *
 * @param httpBaseUrl Backend HTTP 根地址。
 * @returns 经过闭合字段校验的余额快照。
 */
export async function fetchLlmAccountStatus(httpBaseUrl: string): Promise<LlmAccountStatusSnapshot> {
  const response = await fetch(`${httpBaseUrl.replace(/\/+$/, "")}/api/llm/account-status`);
  if (!response.ok) {
    throw new Error(`LLM account status request failed: ${response.status}`);
  }
  return readLlmAccountStatus(await response.json());
}

/**
 * 校验余额接口载荷，避免服务端错误或迟到的半结构响应污染聊天右栏。
 *
 * @param payload 未知 HTTP JSON 载荷。
 * @returns 合法的余额快照。
 */
export function readLlmAccountStatus(payload: unknown): LlmAccountStatusSnapshot {
  if (!isRecord(payload) || !hasExactKeys(payload, ["providers"]) || !Array.isArray(payload.providers)) {
    throw new Error("LLM account status payload is invalid");
  }
  if (!payload.providers.every(isLlmAccountProvider)) {
    throw new Error("LLM account provider status is invalid");
  }
  return { providers: payload.providers };
}

function isLlmAccountProvider(payload: unknown): payload is LlmAccountProvider {
  if (!isRecord(payload) || !hasExactKeys(payload, [
    "provider_id",
    "display_name",
    "active",
    "status",
    "balance",
    "currency",
    "fetched_at",
    "message"
  ])) {
    return false;
  }
  return typeof payload.provider_id === "string"
    && payload.provider_id.trim().length > 0
    && typeof payload.display_name === "string"
    && payload.display_name.trim().length > 0
    && typeof payload.active === "boolean"
    && (payload.status === "available"
      || payload.status === "not_configured"
      || payload.status === "unsupported"
      || payload.status === "error")
    && isNullableString(payload.balance)
    && isNullableString(payload.currency)
    && isNullableString(payload.fetched_at)
    && isNullableString(payload.message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const required = [...expected].sort();
  return actual.length === required.length && actual.every((key, index) => key === required[index]);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}
