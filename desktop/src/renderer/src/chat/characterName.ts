/**
 * 当前角色名称的安全默认值。
 * 角色卡尚未加载或后端暂时不可用时，界面仍需有中性称呼，不能冒用某个具体角色。
 */
export const ACTIVE_CHARACTER_NAME_FALLBACK = "当前角色";

type UnknownRecord = Record<string, unknown>;

/**
 * 从角色卡接口响应中提取可展示的当前角色名称。
 *
 * @param payload `/api/character-cards/active` 返回的 JSON 值。
 * @returns 去掉首尾空白的角色卡名称；载荷缺失或名称为空时返回 null。
 */
export function readActiveCharacterName(payload: unknown): string | null {
  if (!isRecord(payload) || !isRecord(payload.card) || !isRecord(payload.card.meta)) {
    return null;
  }

  const name = payload.card.meta.name;
  if (typeof name !== "string") {
    return null;
  }

  const normalizedName = name.trim();
  return normalizedName ? normalizedName : null;
}

/**
 * 读取 Backend 当前激活角色卡名称。
 *
 * @param httpBaseUrl 控制网关 HTTP 根地址。
 * @param request 可替换的 fetch 实现，便于测试和宿主环境接入。
 * @returns 当前角色卡名称；请求失败或响应不完整时返回 null。
 */
export async function fetchActiveCharacterName(
  httpBaseUrl: string,
  request: typeof fetch = fetch
): Promise<string | null> {
  try {
    const response = await request(
      `${httpBaseUrl.replace(/\/$/, "")}/api/character-cards/active`
    );
    if (!response.ok) {
      return null;
    }
    return readActiveCharacterName(await response.json());
  } catch {
    // 角色名只是展示投影；读取失败时由调用方保留当前值或使用中性默认值。
    return null;
  }
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
