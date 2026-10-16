/** 桌面实际收到的快捷键；设置水合与注册结果不会生成按键反馈。 */
export type DesktopShortcutId = "followup_toggle" | "avatar_visibility" | "selection_capture";

/** 主进程到 Avatar 的本地反馈；sequence 用于隔离快速连按的迟到回执。 */
export type ShortcutFeedback =
  | { phase: "pressed"; shortcutId: DesktopShortcutId; sequence: number }
  | { phase: "settled"; shortcutId: "followup_toggle"; sequence: number; interruptedRequestIds: string[] | null; speechInterruptedAt?: number };

export const SHORTCUT_FEEDBACK_CHANNEL = "avatar:shortcut-feedback";

/**
 * 校验主进程反馈，防止损坏的 IPC 放开助手音频。
 * @param value 未知 IPC 载荷。
 * @returns 载荷符合快捷键反馈合同则返回真。
 */
export function isShortcutFeedback(value: unknown): value is ShortcutFeedback {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const payload = value as Record<string, unknown>;
  if (!Number.isSafeInteger(payload.sequence) || Number(payload.sequence) < 1) return false;
  if (payload.phase === "pressed") {
    return Object.keys(payload).length === 3
      && ["followup_toggle", "avatar_visibility", "selection_capture"].includes(String(payload.shortcutId));
  }
  return payload.phase === "settled" && payload.shortcutId === "followup_toggle"
    && Object.keys(payload).every(key => ["phase", "shortcutId", "sequence", "interruptedRequestIds", "speechInterruptedAt"].includes(key))
    && (payload.speechInterruptedAt === undefined || (
      typeof payload.speechInterruptedAt === "number" && Number.isFinite(payload.speechInterruptedAt) && payload.speechInterruptedAt >= 0
    ))
    && (payload.interruptedRequestIds === null || (
      Array.isArray(payload.interruptedRequestIds)
      && payload.interruptedRequestIds.every(id => typeof id === "string" && id.trim().length > 0)
    ));
}
