export type ShortcutId = "followup_toggle" | "avatar_visibility" | "selection_capture";

export type ShortcutStatus = "registered" | "disabled" | "internal_conflict" | "unavailable" | "unknown";

export type ShortcutStatusItemPayload = {
  shortcut_id: ShortcutId;
  accelerator: string;
  enabled: boolean;
  status: ShortcutStatus;
  settings_revision: number;
  checked_at: string;
  message: string;
};

type ShortcutStatusReportPayload = {
  settings_revision: number;
  items: ShortcutStatusItemPayload[];
};

/**
 * 快捷键状态上报端口，由注册器依赖、HTTP 客户端实现。
 */
export type ShortcutStatusClient = {
  reportShortcutStatus(payload: ShortcutStatusReportPayload): Promise<void>;
};
