import type { IpcMain, IpcMainEvent, IpcMainInvokeEvent } from "electron";
import type { WindowKind } from "../contracts/windowKinds";
import type { WindowIdentityRegistry } from "./windowIdentityRegistry";

type IpcListener = (event: IpcMainEvent, ...args: unknown[]) => void;
type IpcHandler = (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown;

/**
 * AuthorizedIpcMain 在注册通道时同时声明允许调用它的窗口。
 */
export type AuthorizedIpcMain = {
  on(
    channel: string,
    allowedWindowKinds: readonly WindowKind[],
    listener: IpcListener
  ): void;
  handle(
    channel: string,
    allowedWindowKinds: readonly WindowKind[],
    handler: IpcHandler
  ): void;
};

/**
 * createAuthorizedIpcMain 为 Electron IPC 增加统一的窗口来源校验。
 *
 * Args:
 *   ipcMain: Electron 原生 IPC 注册对象。
 *   windowIdentities: 主进程在窗口创建时登记的可信身份表。
 *
 * Returns:
 *   要求每个通道显式声明允许窗口的注册器。
 */
export function createAuthorizedIpcMain(
  ipcMain: Pick<IpcMain, "on" | "handle">,
  windowIdentities: WindowIdentityRegistry
): AuthorizedIpcMain {
  return {
    on(channel, allowedWindowKinds, listener) {
      ipcMain.on(channel, (event, ...args) => {
        const senderWindowKind = windowIdentities.resolve(event.sender);

        // send/on 没有返回通道，忽略越权消息即可，避免执行任何桌面动作。
        if (!senderWindowKind || !allowedWindowKinds.includes(senderWindowKind)) {
          return;
        }

        listener(event, ...args);
      });
    },
    handle(channel, allowedWindowKinds, handler) {
      ipcMain.handle(channel, (event, ...args) => {
        const senderWindowKind = windowIdentities.resolve(event.sender);

        // invoke 调用方必须收到明确失败，不能把越权请求伪装成业务成功。
        if (!senderWindowKind || !allowedWindowKinds.includes(senderWindowKind)) {
          throw new Error(`unauthorized IPC sender for channel: ${channel}`);
        }

        return handler(event, ...args);
      });
    }
  };
}
