import { isNonNegativeSafeInteger } from "./validation";

export type ShortcutId = "followup_toggle" | "avatar_visibility" | "selection_capture";
type ShortcutStatus = "registered" | "disabled" | "internal_conflict" | "unavailable" | "unknown";

export type ShortcutStatusItem = {
  shortcut_id: ShortcutId;
  accelerator: string;
  enabled: boolean;
  status: ShortcutStatus;
  settings_revision: number;
  checked_at: string;
  message: string;
};

export type ShortcutStatusPayload = {
  items: ShortcutStatusItem[];
};

/**
 * 判断未知响应是否符合快捷键状态契约。
 *
 * Args:
 *   payload: JSON 解析后的未知值。
 *
 * Returns:
 *   字段完整且枚举值合法时返回 true。
 */
export function isShortcutStatusPayload(payload: unknown): payload is ShortcutStatusPayload {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }

  const candidate = payload as Record<string, unknown>;
  return Array.isArray(candidate.items) && candidate.items.every(isShortcutStatusItem);
}

function isShortcutStatusItem(payload: unknown): payload is ShortcutStatusItem {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }

  const candidate = payload as Record<string, unknown>;
  return (
    isShortcutId(candidate.shortcut_id) &&
    typeof candidate.accelerator === "string" &&
    typeof candidate.enabled === "boolean" &&
    isShortcutStatus(candidate.status) &&
    isNonNegativeSafeInteger(candidate.settings_revision) &&
    typeof candidate.checked_at === "string" &&
    typeof candidate.message === "string"
  );
}

function isShortcutId(value: unknown): value is ShortcutId {
  return value === "followup_toggle" || value === "avatar_visibility" || value === "selection_capture";
}

function isShortcutStatus(value: unknown): value is ShortcutStatus {
  return (
    value === "registered" ||
    value === "disabled" ||
    value === "internal_conflict" ||
    value === "unavailable" ||
    value === "unknown"
  );
}
