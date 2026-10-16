import {
  logRendererDiagnostic,
  readRendererDiagnosticPayload
} from "../diagnostics/rendererDiagnostics";
import { RENDERER_DIAGNOSTIC_CHANNEL } from "../../shared/rendererDiagnostics";
import type { AuthorizedIpcMain } from "./authorizedIpcMain";
import type { WindowIdentityRegistry } from "./windowIdentityRegistry";

const ALL_RENDERER_WINDOWS = ["avatar", "panel", "chat"] as const;

/**
 * registerRendererDiagnosticIpc 注册 renderer 诊断上报通道。
 *
 * @param ipcMain Electron IPC 主进程对象。
 * @param getRuntime 当前 shell runtime 读取器。
 */
export function registerRendererDiagnosticIpc(
  ipcMain: Pick<AuthorizedIpcMain, "on">,
  windowIdentities: WindowIdentityRegistry
): void {
  ipcMain.on(RENDERER_DIAGNOSTIC_CHANNEL, ALL_RENDERER_WINDOWS, (event, payload: unknown) => {
    const diagnosticPayload = readRendererDiagnosticPayload(payload);

    if (!diagnosticPayload) {
      return;
    }

    const windowKind = windowIdentities.resolve(event.sender);

    if (!windowKind) {
      return;
    }

    logRendererDiagnostic(windowKind, diagnosticPayload);
  });
}
