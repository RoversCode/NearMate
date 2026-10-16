/**
 * 解析 renderer 请求交给系统浏览器打开的 HTTP(S) 地址。
 *
 * Args:
 *   value: 尚未信任的 IPC 参数。
 *
 * Returns:
 *   经过 URL 标准化的 HTTP(S) 地址；协议或结构无效时返回空。
 */
export function readExternalHttpUrl(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }

  try {
    const parsed = new URL(value.trim());
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return null;
    }
    return parsed.href;
  } catch {
    return null;
  }
}
