import { isShortcutStatusPayload, type ShortcutStatusPayload } from "../contracts/shortcutStatus";
import { fetchJson } from "../httpClient";

/**
 * 读取桌面全局快捷键注册状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   三类全局快捷键在当前设备上的运行期状态。
 */
export async function fetchShortcutStatus(baseUrl: string): Promise<ShortcutStatusPayload> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/settings/shortcut-status", "读取快捷键状态失败");
  if (!isShortcutStatusPayload(payload)) {
    throw new Error("读取快捷键状态失败：响应格式不符合控制台契约");
  }

  return payload;
}
