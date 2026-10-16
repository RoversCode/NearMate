import { shell, type Tray } from "electron";
import { resolveControlConsoleUrl } from "../shared/controlHttpUrl";
import type { PlatformBridge } from "./platform";

type TrayRuntimeInput = {
  platformBridge: PlatformBridge;
  controlHttpUrl?: string;
  toggleAvatarVisibility(): void;
};

/**
 * createTrayRuntime 创建系统托盘入口。
 *
 * Args:
 *   input: 平台桥、控制面地址和桌宠显隐命令。
 *
 * Returns:
 *   Electron Tray 实例；平台不支持时返回 null。
 */
export function createTrayRuntime(input: TrayRuntimeInput): Tray | null {
  return input.platformBridge.attachTray({
    toggleAvatarVisibility: () => input.toggleAvatarVisibility(),
    openConsole: () => {
      void shell.openExternal(resolveControlConsoleUrl(input.controlHttpUrl)).catch(error => {
        console.error("[companion-main] open-console failed", error);
      });
    }
  });
}

/**
 * destroyTrayRuntime 显式销毁旧托盘。
 *
 * Args:
 *   tray: 当前持有的托盘实例。
 */
export function destroyTrayRuntime(tray: Tray | null): void {
  tray?.destroy();
}
