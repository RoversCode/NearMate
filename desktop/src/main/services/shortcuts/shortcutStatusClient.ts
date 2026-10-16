import type { ShortcutStatusClient } from "./shortcutTypes";

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

type CreateShortcutStatusClientOptions = {
  httpBaseUrl: string;
  fetch?: FetchLike;
};

/**
 * 创建桌面快捷键状态上报客户端。
 *
 * @param options 控制面 HTTP 根地址与可注入 fetch。
 * @returns 只负责把主进程注册结果上报给 backend 的客户端。
 */
export function createShortcutStatusClient(options: CreateShortcutStatusClientOptions): ShortcutStatusClient {
  const normalizedHttpBaseUrl = options.httpBaseUrl.replace(/\/+$/, "");
  const fetchImplementation = options.fetch ?? globalThis.fetch.bind(globalThis);

  return {
    async reportShortcutStatus(payload): Promise<void> {
      const response = await fetchImplementation(`${normalizedHttpBaseUrl}/api/desktop/shortcut-status`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
      });

      if (!response.ok) {
        throw new Error(`shortcut status report failed: ${response.status}`);
      }
    }
  };
}
