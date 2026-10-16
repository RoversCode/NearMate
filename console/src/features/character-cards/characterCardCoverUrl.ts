import { type CharacterCard } from "../../api/contracts/characterCards";

/**
 * 构建角色卡封面资产 URL。
 *
 * Args:
 *   card: 当前角色卡。
 *
 * Returns:
 *   有封面时返回同源资产路径，否则返回 null。
 */
export function buildCharacterCardCoverUrl(card: CharacterCard): string | null {
  if (card.meta.avatar_path === null || !card.meta.avatar_path.trim()) {
    return null;
  }
  return `/api/character-cards/${encodeURIComponent(card.meta.card_id)}/asset/${card.meta.avatar_path}`;
}

/**
 * 释放封面裁剪预览对象 URL。
 *
 * Args:
 *   objectUrl: 浏览器创建的对象 URL。
 */
export function revokeCoverObjectUrl(objectUrl: string): void {
  if (typeof URL.revokeObjectURL !== "function") {
    return;
  }
  URL.revokeObjectURL(objectUrl);
}
