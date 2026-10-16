import {
  type WakeResponseConfig,
  type WakeResponseGeneratedTexts,
  type WakeResponseItem,
  isWakeResponseConfig,
  isWakeResponseGeneratedTexts
} from "../contracts/wakeResponse";
import { fetchJson, fetchJsonWithInit, postJson, readHttpErrorDetail } from "../httpClient";

/**
 * 读取唤醒回复配置。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 角色卡编号。
 *
 * Returns:
 *   唤醒回复配置。
 */
export async function fetchWakeResponseConfig(baseUrl: string, cardId: string): Promise<WakeResponseConfig> {
  const payload = await fetchJson<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/wake-response`,
    "读取唤醒回复失败"
  );
  if (!isWakeResponseConfig(payload)) {
    throw new Error("读取唤醒回复失败：响应格式不符合控制台契约");
  }
  return payload;
}

/**
 * 根据当前角色卡和用户资料生成唤醒回复文本。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 角色卡编号。
 *
 * Returns:
 *   分时段生成文本。
 */
export async function generateWakeResponseTexts(baseUrl: string, cardId: string): Promise<WakeResponseGeneratedTexts> {
  const payload = await postJson<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/wake-response/generate`,
    {},
    "生成唤醒回复失败"
  );
  if (!isWakeResponseGeneratedTexts(payload)) {
    throw new Error("生成唤醒回复失败：响应格式不符合控制台契约");
  }
  return payload;
}

/**
 * 保存唤醒回复配置并触发后端自动合成。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 角色卡编号。
 *   config: 待保存配置。
 *
 * Returns:
 *   后端保存后的配置。
 */
export async function saveWakeResponseConfig(
  baseUrl: string,
  cardId: string,
  config: WakeResponseConfig
): Promise<WakeResponseConfig> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/wake-response`,
    {
      method: "PUT",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(config)
    },
    "保存唤醒回复失败"
  );
  if (!isWakeResponseConfig(payload)) {
    throw new Error("保存唤醒回复失败：响应格式不符合控制台契约");
  }
  return payload;
}

/**
 * 临时合成一条唤醒回复并返回试听音频。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 角色卡编号。
 *   text: 待试听文本。
 *
 * Returns:
 *   浏览器可播放的音频 Blob。
 */
export async function previewWakeResponseText(baseUrl: string, cardId: string, text: string): Promise<Blob> {
  const normalizedBaseUrl = baseUrl.replace(/\/$/, "");
  const response = await fetch(`${normalizedBaseUrl}/api/character-cards/${encodeURIComponent(cardId)}/wake-response/preview`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ text })
  });
  if (!response.ok) {
    throw new Error(`试听唤醒回复失败：${await readHttpErrorDetail(response)}`);
  }
  return await response.blob();
}

/**
 * 使用当前文本立即合成并保存指定唤醒回复。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 角色卡编号。
 *   itemId: 唤醒回复编号。
 *
 * Returns:
 *   已保存的单条唤醒回复。
 */
export async function synthesizeWakeResponseItem(
  baseUrl: string,
  cardId: string,
  itemId: string,
  bucket: string,
  text: string
): Promise<WakeResponseItem> {
  const payload = await postJson<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/wake-response/${encodeURIComponent(itemId)}/synthesize`,
    { bucket, text },
    "合成唤醒回复失败"
  );
  if (!isWakeResponseItem(payload)) {
    throw new Error("合成唤醒回复失败：响应格式不符合控制台契约");
  }
  return payload;
}

/**
 * 删除指定唤醒回复。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 角色卡编号。
 *   itemId: 唤醒回复编号。
 *
 * Returns:
 *   删除成功时不返回正文。
 */
export async function deleteWakeResponseItem(baseUrl: string, cardId: string, itemId: string): Promise<void> {
  const normalizedBaseUrl = baseUrl.replace(/\/$/, "");
  const response = await fetch(
    `${normalizedBaseUrl}/api/character-cards/${encodeURIComponent(cardId)}/wake-response/${encodeURIComponent(itemId)}`,
    { method: "DELETE" }
  );
  if (!response.ok) {
    throw new Error(`删除唤醒回复失败：${await readHttpErrorDetail(response)}`);
  }
}

function isWakeResponseItem(payload: unknown): payload is WakeResponseItem {
  return typeof payload === "object" && payload !== null && "id" in payload && "text" in payload &&
    typeof payload.id === "string" && typeof payload.text === "string";
}
