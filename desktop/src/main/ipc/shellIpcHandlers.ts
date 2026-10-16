import { ipcMain, shell } from "electron";
import { registerAvatarIpcHandlers } from "./avatarIpcHandlers";
import {
  SHELL_IPC_HANDLER_CHANNELS,
  SHELL_IPC_LISTENER_CHANNELS
} from "./shellIpcChannels";
import { registerRendererDiagnosticIpc } from "./shellIpcDiagnostics";
import { registerLiveKitSessionIpcHandler } from "./liveKitSessionIpcHandler";
import { registerPanelChatIpcHandlers } from "./panelChatIpcHandlers";
import type { RegisterShellIpcHandlersInput } from "./shellIpcTypes";
import { createAuthorizedIpcMain } from "./authorizedIpcMain";

/**
 * registerShellIpcHandlers 集中注册桌宠、侧边板、聊天窗需要的窗口壳 IPC。
 *
 * @param input IPC 处理器依赖。
 */
export function registerShellIpcHandlers(input: RegisterShellIpcHandlersInput): void {
  removeShellIpcHandlers();
  const authorizedIpcMain = createAuthorizedIpcMain(ipcMain, input.windowIdentities);

  registerRendererDiagnosticIpc(authorizedIpcMain, input.windowIdentities);
  registerAvatarIpcHandlers(authorizedIpcMain, input);
  registerPanelChatIpcHandlers(authorizedIpcMain, shell, input);
  registerLiveKitSessionIpcHandler(authorizedIpcMain, input);
}

/**
 * removeShellIpcHandlers 清理所有桌面壳 IPC 注册点。
 */
function removeShellIpcHandlers(): void {
  for (const channel of SHELL_IPC_LISTENER_CHANNELS) {
    ipcMain.removeAllListeners(channel);
  }

  for (const channel of SHELL_IPC_HANDLER_CHANNELS) {
    ipcMain.removeHandler(channel);
  }
}
