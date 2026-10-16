import type { WebContents } from "electron";
import type { WindowKind } from "../contracts/windowKinds";

/**
 * WindowIdentityRegistry 保存可信主进程创建的窗口身份。
 */
export type WindowIdentityRegistry = {
  bind(sender: WebContents, windowKind: WindowKind): void;
  resolve(sender: WebContents): WindowKind | null;
};

/**
 * createWindowIdentityRegistry 创建只按 WebContents 对象身份匹配的窗口登记表。
 *
 * Returns:
 *   可供窗口创建和 IPC 授权共同使用的进程内登记表。
 */
export function createWindowIdentityRegistry(): WindowIdentityRegistry {
  const windowKindsBySender = new WeakMap<WebContents, WindowKind>();

  return {
    bind(sender, windowKind) {
      windowKindsBySender.set(sender, windowKind);
    },
    resolve(sender) {
      return windowKindsBySender.get(sender) ?? null;
    }
  };
}
