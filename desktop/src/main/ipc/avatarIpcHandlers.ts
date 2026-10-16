import { screen } from "electron";
import {
  readBackgroundAgentInteractionRequestId,
  readAvatarPetBounds,
  readLive2dModelSwitchRequest,
  readLive2dModelSwitchState
} from "./shellIpcPayloads";
import type { RegisterShellIpcHandlersInput } from "./shellIpcTypes";
import { readPanelView } from "../../shared/shellNavigation";
import type { AuthorizedIpcMain } from "./authorizedIpcMain";

const AVATAR_WINDOW = ["avatar"] as const;
const PANEL_WINDOW = ["panel"] as const;

/**
 * registerAvatarIpcHandlers 注册 avatar 窗口相关 IPC。
 *
 * @param ipcMain Electron IPC 主进程对象。
 * @param input IPC 处理器依赖。
 */
export function registerAvatarIpcHandlers(
  ipcMain: AuthorizedIpcMain,
  input: RegisterShellIpcHandlersInput
): void {
  ipcMain.on("avatar:hover-enter", AVATAR_WINDOW, () => {
    input.getRuntime()?.controller.hoverEnter();
  });
  ipcMain.on("avatar:hover-leave", AVATAR_WINDOW, () => {
    input.getRuntime()?.controller.hoverLeave();
  });
  ipcMain.on("avatar:open-panel", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    const view = readPanelView(payload);

    if (!runtime || view === null) {
      return;
    }

    runtime.controller.openPanel(view);
  });
  ipcMain.on("avatar:material-drop-enter", AVATAR_WINDOW, () => {
    input.getRuntime()?.controller.startMaterialDrop();
  });
  ipcMain.on("avatar:material-drop-leave", AVATAR_WINDOW, () => {
    input.getRuntime()?.controller.endMaterialDrop();
  });
  ipcMain.on("avatar:drag-start", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();

    if (!runtime || !input.avatarInteraction.startDrag(runtime.avatarWindow, payload)) {
      return;
    }

    runtime.controller.startDrag();
  });
  ipcMain.on("avatar:drag-move", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();

    if (!runtime) {
      return;
    }

    input.avatarInteraction.moveDrag(runtime.avatarWindow, payload);
  });
  ipcMain.on("avatar:drag-end", AVATAR_WINDOW, () => {
    const runtime = input.getRuntime();

    if (!runtime) {
      input.avatarInteraction.endDrag();
      return;
    }

    input.avatarInteraction.endDrag(runtime.avatarWindow);
    runtime.controller.endDrag();
    runtime.controller.refreshPanelPlacement();
  });
  ipcMain.on("avatar:resize-start", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();

    if (!runtime) {
      return;
    }

    input.avatarInteraction.startResize(runtime.avatarWindow, payload);
  });
  ipcMain.on("avatar:resize-move", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();

    if (!runtime) {
      return;
    }

    input.avatarInteraction.moveResize(runtime.avatarWindow, payload);
  });
  ipcMain.on("avatar:resize-end", AVATAR_WINDOW, () => {
    const runtime = input.getRuntime();

    if (!runtime) {
      input.avatarInteraction.endResize();
      return;
    }

    input.avatarInteraction.endResize(runtime.avatarWindow);
    runtime.controller.refreshPanelPlacement();
  });
  ipcMain.handle("avatar:request-visible-area", AVATAR_WINDOW, () => {
    const runtime = input.getRuntime();

    if (!runtime) {
      return null;
    }

    return input.avatarInteraction.requestVisibleArea(runtime.avatarWindow);
  });
  ipcMain.handle("avatar:get-chat-mode", AVATAR_WINDOW, () => {
    const runtime = input.getRuntime();
    return runtime?.chatLifecycle?.currentMode() ?? runtime?.chatWindowMotion.currentMode() ?? null;
  });
  ipcMain.on("avatar:open-background-agent-interaction", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    const requestId = readBackgroundAgentInteractionRequestId(payload);
    if (!runtime || requestId === null) {
      return;
    }

    if (runtime.chatLifecycle) {
      runtime.chatLifecycle.openChat({ focus: true, requestId });
      return;
    }

    runtime.chatWindow.setBounds(runtime.chatWindowMotion.resolveBounds(
      "expanded",
      screen.getPrimaryDisplay().workArea
    ));
    runtime.chatWindow.show();
    runtime.chatWindow.focus();
    runtime.avatarWindow.webContents.send("avatar:chat-mode-changed", { mode: "expanded" });
    runtime.chatWindow.webContents.send("chat:focus-background-interaction", { requestId });
  });
  ipcMain.on("avatar:open-chat", AVATAR_WINDOW, () => {
    const runtime = input.getRuntime();
    if (runtime?.chatLifecycle) {
      runtime.chatLifecycle.openChat({ focus: true });
    } else if (runtime) {
      runtime.chatWindow.setBounds(runtime.chatWindowMotion.resolveBounds(
        "expanded",
        screen.getPrimaryDisplay().workArea
      ));
      runtime.chatWindow.show();
      runtime.chatWindow.focus();
      runtime.avatarWindow.webContents.send("avatar:chat-mode-changed", { mode: "expanded" });
    }
  });
  ipcMain.on("avatar:apply-presentation-profile", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();

    if (!runtime) {
      input.avatarInteraction.rememberPendingPresentationProfile(payload);
      return;
    }

    input.avatarInteraction.applyPresentationProfile(runtime.avatarWindow, payload);
  });
  ipcMain.on("avatar:report-pet-bounds", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();

    if (!runtime) {
      return;
    }

    if (payload === null) {
      input.avatarInteraction.rememberPetBounds(null);
      return;
    }

    const petBounds = readAvatarPetBounds(payload);

    if (!petBounds) {
      return;
    }

    input.avatarInteraction.rememberPetBounds(petBounds);
  });
  ipcMain.on("avatar:model-switch-state-changed", AVATAR_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    const switchState = readLive2dModelSwitchState(payload);

    if (!runtime || !switchState) {
      return;
    }

    runtime.panelWindow.webContents.send("panel:model-switch-state-changed", switchState);
  });
  ipcMain.on("panel:request-model-switch", PANEL_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    const switchRequest = readLive2dModelSwitchRequest(payload);

    if (!runtime || !switchRequest) {
      return;
    }

    runtime.avatarWindow.webContents.send("avatar:model-switch-requested", switchRequest);
  });
}
