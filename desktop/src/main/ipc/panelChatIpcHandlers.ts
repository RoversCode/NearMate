import type { IpcMainInvokeEvent, Shell } from "electron";
import { app, clipboard, nativeImage, screen } from "electron";
import { isAbsolute } from "node:path";
import { resolveControlConsoleUrl } from "../../shared/controlHttpUrl";
import type { RegisterShellIpcHandlersInput } from "./shellIpcTypes";
import type { MaterialItemPayload } from "../services/materialsClient";
import { resolveMaterialThumbnailDataUrl } from "../services/materialThumbnailService";
import { createClipboardImageMaterialService } from "../services/clipboardImageMaterialService";
import { createComposerTextMaterialService } from "../services/composerTextMaterialService";
import type { ChatWindowMode } from "../windows/chatWindowMotion";
import {
  buildConsoleTargetUrl,
  readConsoleTarget
} from "../../shared/shellNavigation";
import { readExternalHttpUrl } from "./externalHttpUrl";
import type { AuthorizedIpcMain } from "./authorizedIpcMain";
import { isStandardUuid } from "../../shared/uuid";

const MATERIAL_ITEM_KINDS = new Set<MaterialItemPayload["kind"]>([
  "clipboard_text",
  "clipboard_image",
  "dragged_file",
  "dragged_folder",
  "selected_text",
  "selected_image",
  "selected_web"
]);
const CHAT_WINDOW_MODES = new Set<ChatWindowMode>(["collapsed", "expanded"]);
const AVATAR_PANEL_AND_CHAT_WINDOWS = ["avatar", "panel", "chat"] as const;
const AVATAR_AND_CHAT_WINDOWS = ["avatar", "chat"] as const;
const PANEL_WINDOW = ["panel"] as const;
const CHAT_WINDOW = ["chat"] as const;

/**
 * registerPanelChatIpcHandlers 注册 panel、chat 与控制台入口相关 IPC。
 *
 * @param ipcMain Electron IPC 主进程对象。
 * @param shell Electron shell 模块窄接口。
 * @param input IPC 处理器依赖。
 */
export function registerPanelChatIpcHandlers(
  ipcMain: AuthorizedIpcMain,
  shell: Pick<Shell, "openExternal" | "showItemInFolder">,
  input: RegisterShellIpcHandlersInput
): void {
  const pendingRemovalFeedback = new Map<string, string>();
  ipcMain.on("panel:close", PANEL_WINDOW, () => {
    input.getRuntime()?.controller.closePanelFromUserRequest();
  });
  ipcMain.on("panel:reset-avatar-layout", PANEL_WINDOW, () => {
    const runtime = input.getRuntime();

    if (!runtime || !input.avatarInteraction.resetCurrentModelLayout(runtime.avatarWindow)) {
      return;
    }

    runtime.controller.refreshPanelPlacement();
  });
  ipcMain.on("chat:set-mode", CHAT_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    const mode = readChatWindowModePayload(payload);

    if (!runtime || mode === null) {
      return;
    }

    if (runtime.chatLifecycle) {
      runtime.chatLifecycle.setRendererMode(mode);
      return;
    }

    // 兼容仍在装配中的旧 runtime；collapsed 仍然是隐藏态，不再定位或显示独立聊天浮球。
    runtime.avatarWindow.webContents.send("avatar:chat-mode-changed", { mode });
    if (mode === "collapsed") {
      runtime.chatWindow.hide();
      return;
    }
    runtime.chatWindow.setBounds(runtime.chatWindowMotion.resolveBounds(mode, screen.getPrimaryDisplay().workArea));
    if (runtime.controller.getAvatarSummonMode() !== "hidden") {
      runtime.chatWindow.show();
    }
  });
  ipcMain.on("chat:drag-start", CHAT_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    runtime?.chatWindowDrag.startDrag(runtime.chatWindow, payload);
  });
  ipcMain.on("chat:drag-move", CHAT_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    runtime?.chatWindowDrag.moveDrag(runtime.chatWindow, payload);
  });
  ipcMain.on("chat:drag-end", CHAT_WINDOW, () => {
    const runtime = input.getRuntime();
    runtime?.chatWindowDrag.endDrag(runtime.chatWindow);
  });
  ipcMain.on("chat:resize-start", CHAT_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    runtime?.chatWindowResize.startResize(runtime.chatWindow, payload);
  });
  ipcMain.on("chat:resize-move", CHAT_WINDOW, (_event, payload: unknown) => {
    const runtime = input.getRuntime();
    runtime?.chatWindowResize.moveResize(runtime.chatWindow, payload);
  });
  ipcMain.on("chat:resize-end", CHAT_WINDOW, () => {
    const runtime = input.getRuntime();
    runtime?.chatWindowResize.endResize(runtime.chatWindow);
  });
  ipcMain.on("desktop:open-console", AVATAR_PANEL_AND_CHAT_WINDOWS, (_event, payload: unknown) => {
    const target = readConsoleTarget(payload);
    if (target === null) {
      return;
    }
    void shell.openExternal(
      buildConsoleTargetUrl(
        resolveControlConsoleUrl(process.env["COMPANION_CONTROL_HTTP_URL"]),
        target
      )
    ).catch(error => {
      console.error("[companion-main] open-console failed", error);
    });
  });
  ipcMain.handle("turn-submission:reserve", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const turnInput = readTurnReservationPayload(payload);
    const turnSubmissions = await input.turnSubmission.submitTurn(turnInput);
    input.materialFeedback.clearFailureReport();
    return { accepted: true, turnSubmissions };
  });
  ipcMain.handle("turn-submission:edit", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const turnSubmissions = await input.turnSubmission.editTurn(readTurnEditPayload(payload));
    return { accepted: true, turnSubmissions };
  });
  ipcMain.handle("turn-submission:delete", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const turnSubmissions = await input.turnSubmission.deleteTurn(readTurnSubmissionId(payload));
    return { accepted: true, turnSubmissions };
  });
  ipcMain.handle("turn-submission:resume", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const turnSubmissions = await input.turnSubmission.resumeTurn(readTurnSubmissionId(payload));
    return { accepted: true, turnSubmissions };
  });
  ipcMain.handle("turn-submission:retry", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const turnSubmissions = await input.turnSubmission.retryTurn(readTurnSubmissionId(payload));
    return { accepted: true, turnSubmissions };
  });
  ipcMain.handle("environment:prepare-text", CHAT_WINDOW, () => {
    input.environmentSnapshot.prepareTextSnapshot();
    return { accepted: true };
  });
  ipcMain.handle("chat:get-mode", CHAT_WINDOW, () => input.getRuntime()?.chatLifecycle?.currentMode() ?? "collapsed");
  ipcMain.handle("chat:open-external-url", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const url = readExternalHttpUrl(payload);
    if (url === null) {
      throw new Error("external URL must use http or https");
    }
    await shell.openExternal(url);
    return { accepted: true };
  });
  ipcMain.handle("chat:show-item-in-folder", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const outputPath = readOutputPathPayload(payload);
    shell.showItemInFolder(outputPath);
    return { accepted: true };
  });
  ipcMain.handle("materials:add-staged", AVATAR_AND_CHAT_WINDOWS, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const items = readStagedMaterialItemsPayload(payload);
    const result = await input.materialCommands.add(items);
    return { accepted: true, ...result };
  });
  ipcMain.handle("materials:remove-staged", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const materialId = readMaterialIdPayload(payload);
    const result = await input.materialCommands.remove(materialId);
    pendingRemovalFeedback.set(materialId, result.removedDisplayName);
    return {
      accepted: true,
      materialContext: result.materialContext,
      removedDisplayName: result.removedDisplayName
    };
  });
  ipcMain.handle("materials:forget-recent", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const path = readRecentMaterialPathPayload(payload);
    const materialContext = await input.materialCommands.forgetRecent(path);
    return { accepted: true, materialContext };
  });
  ipcMain.handle("materials:confirm-remove-applied", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const { materialId, displayName } = readRemovedMaterialPayload(payload);
    if (pendingRemovalFeedback.get(materialId) !== displayName) {
      throw new Error("removed material feedback confirmation is stale");
    }
    pendingRemovalFeedback.delete(materialId);
    input.materialFeedback.notifyMaterialRemoved(displayName);
    return { accepted: true };
  });
  ipcMain.handle("materials:get-failure-report", CHAT_WINDOW, () => input.materialFeedback.currentFailureReport());
  ipcMain.handle("materials:get-thumbnail", CHAT_WINDOW, async (_event: IpcMainInvokeEvent, payload: unknown) => {
    const path = readThumbnailPathPayload(payload);
    if (path === null) {
      return { thumbnailDataUrl: null };
    }

    const thumbnailDataUrl = await resolveMaterialThumbnailDataUrl({
      path,
      getContentThumbnail: nativeImage.createThumbnailFromPath,
      getFileIcon: (filePath, options) => app.getFileIcon(filePath, options)
    });
    return { thumbnailDataUrl };
  });
  ipcMain.handle("materials:capture-clipboard-image-material", CHAT_WINDOW, async () => {
    const service = createClipboardImageMaterialService({
      clipboard,
      attachmentDir: input.managedMaterialAssetStore.rootDir,
      managedAssetStore: input.managedMaterialAssetStore
    });
    return await service.captureClipboardImageMaterial();
  });
  ipcMain.handle("materials:create-composer-text-material", CHAT_WINDOW, async (_event, payload: unknown) => {
    const service = createComposerTextMaterialService({
      attachmentDir: input.managedMaterialAssetStore.rootDir,
      managedAssetStore: input.managedMaterialAssetStore
    });
    return await service.createComposerTextMaterial(readTextMaterialPayload(payload));
  });
}

function readTextMaterialPayload(payload: unknown): string {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("text material payload is invalid");
  }
  const text = (payload as { text?: unknown }).text;
  if (typeof text !== "string" || !text) {
    throw new Error("text material payload is missing text");
  }
  return text;
}

/**
 * 校验 renderer 请求定位的交付文件路径。
 *
 * @param payload preload 传入的未知 IPC 载荷。
 * @returns 可交给 Electron shell 的绝对本地路径。
 */
function readOutputPathPayload(payload: unknown): string {
  if (typeof payload !== "string") {
    throw new Error("output path payload is invalid");
  }
  const outputPath = payload.trim();
  if (!outputPath || outputPath.includes("\0") || !isAbsolute(outputPath)) {
    throw new Error("output path must be an absolute local path");
  }
  return outputPath;
}

function readMaterialIdPayload(payload: unknown): string {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("material id payload is invalid");
  }
  return readRequiredString((payload as { materialId?: unknown }).materialId, "materialId");
}

/**
 * 读取 renderer 请求忘记的近期材料路径。
 *
 * @param payload preload 传入的未知 IPC 载荷。
 * @returns 已确认存在且非空的本地路径字符串。
 */
function readRecentMaterialPathPayload(payload: unknown): string {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("recent material forget payload is invalid");
  }
  const candidate = payload as Record<string, unknown>;
  if (Object.keys(candidate).length !== 1 || !Object.hasOwn(candidate, "path")) {
    throw new Error("recent material forget payload is invalid");
  }
  return readRequiredString(candidate.path, "path");
}

function readRemovedMaterialPayload(payload: unknown): { materialId: string; displayName: string } {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("removed material payload is invalid");
  }
  const candidate = payload as { materialId?: unknown; displayName?: unknown };
  return {
    materialId: readRequiredString(candidate.materialId, "materialId"),
    displayName: readRequiredString(candidate.displayName, "displayName")
  };
}

function readStagedMaterialItemsPayload(payload: unknown): MaterialItemPayload[] {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("staged material payload is invalid");
  }

  const items = (payload as { items?: unknown }).items;
  if (!Array.isArray(items) || items.length === 0) {
    throw new Error("staged material payload requires items");
  }
  return items.map(item => readMaterialItemPayload(item));
}

/**
 * 读取 renderer 发起的文本 reservation。
 *
 * Args:
 *   payload: preload 传入的未知 IPC 载荷。
 *
 * Returns:
 *   已校验标准 UUID 和非空正文的提交参数。
 */
function readTurnReservationPayload(payload: unknown): {
  submissionId: string;
  messageText: string;
} {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("turn reservation payload is invalid");
  }
  const candidate = payload as Record<string, unknown>;
  return {
    submissionId: readTurnSubmissionId(candidate.submissionId),
    messageText: readRequiredString(candidate.messageText, "messageText")
  };
}

/**
 * 读取一条尚未派发文本的原位编辑命令。
 *
 * Args:
 *   payload: preload 传入的未知 IPC 载荷。
 *
 * Returns:
 *   已校验提交编号和替换正文的编辑参数。
 */
function readTurnEditPayload(payload: unknown): {
  submissionId: string;
  messageText: string;
} {
  return readTurnReservationPayload(payload);
}

/**
 * 读取标准 UUID 文本提交编号。
 *
 * Args:
 *   payload: preload 传入的未知编号。
 *
 * Returns:
 *   去除首尾空白后的标准 UUID。
 */
function readTurnSubmissionId(payload: unknown): string {
  if (typeof payload !== "string") {
    throw new Error("turn submission id is invalid");
  }
  const submissionId = payload.trim();
  if (!isStandardUuid(submissionId)) {
    throw new Error("turn submission id is invalid");
  }
  return submissionId;
}

function readMaterialItemPayload(payload: unknown): MaterialItemPayload {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("material item payload is invalid");
  }

  const candidate = payload as Record<string, unknown>;
  const materialItem: MaterialItemPayload = {
    kind: readMaterialKind(candidate.kind),
    source: readRequiredString(candidate.source, "source"),
    display_name: readRequiredString(candidate.display_name, "display_name"),
    path: readRequiredString(candidate.path, "path")
  };
  const managedAssetId = readOptionalString(candidate.managed_asset_id);
  if (managedAssetId !== null) {
    materialItem.managed_asset_id = managedAssetId;
  }
  return materialItem;
}

function readMaterialKind(value: unknown): MaterialItemPayload["kind"] {
  const kind = readRequiredString(value, "kind");
  if (!MATERIAL_ITEM_KINDS.has(kind as MaterialItemPayload["kind"])) {
    throw new Error("material item payload has unsupported kind");
  }

  return kind as MaterialItemPayload["kind"];
}

function readRequiredString(value: unknown, fieldName: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`material item payload is missing ${fieldName}`);
  }

  return value.trim();
}

function readOptionalString(value: unknown): string | null {
  if (value == null) {
    return null;
  }

  if (typeof value !== "string") {
    return null;
  }

  return value.trim() || null;
}

function readThumbnailPathPayload(payload: unknown): string | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }

  return readOptionalString((payload as { path?: unknown }).path);
}

function readChatWindowModePayload(payload: unknown): ChatWindowMode | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }

  const mode = (payload as { mode?: unknown }).mode;
  if (typeof mode !== "string" || !CHAT_WINDOW_MODES.has(mode as ChatWindowMode)) {
    return null;
  }

  return mode as ChatWindowMode;
}
