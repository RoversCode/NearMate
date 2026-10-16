import { contextBridge, ipcRenderer, webUtils } from "electron";
import {
  RENDERER_DIAGNOSTIC_CHANNEL,
  type RendererDiagnosticLevel,
  type RendererDiagnosticPayload
} from "../shared/rendererDiagnostics";
import { isStandardUuid } from "../shared/uuid";
import { readWindowBootstrapArgument } from "../shared/windowBootstrap";
import type { ConsoleTarget } from "../shared/shellNavigation";

const bootstrap = readWindowBootstrapArgument(process.argv, "chat");

/**
 * companionChat bridge 是聊天窗口唯一可见的桌面能力边界。
 * 它只暴露控制网关地址、头像资源 URL、窗口模式和诊断上报，不把 ipcRenderer 或文件系统交给 renderer。
 */
if (bootstrap) {
contextBridge.exposeInMainWorld("companionChat", {
  reportDiagnostic(level: RendererDiagnosticLevel, message: string, details?: unknown): void {
    ipcRenderer.send(RENDERER_DIAGNOSTIC_CHANNEL, {
      level,
      message,
      details
    } satisfies RendererDiagnosticPayload);
  },
  /**
   * 高级任务详情仍通过主进程白名单深链打开 Console，Chat 不直接拼接地址。
   */
  openConsole(target: ConsoleTarget): void {
    ipcRenderer.send("desktop:open-console", target);
  },
  /**
   * 聊天窗口折叠或展开时通知主进程同步原生窗口生命周期。
   */
  setChatMode(mode: "collapsed" | "expanded"): void {
    ipcRenderer.send("chat:set-mode", { mode });
  },
  /**
   * 首帧读取主进程权威模式，避免隐藏窗口 renderer 用默认值覆盖已经打开的 Chat。
   */
  getChatMode(): Promise<"collapsed" | "expanded" | null> {
    return ipcRenderer.invoke("chat:get-mode");
  },
  /**
   * 主进程可以请求聊天 renderer 回到隐藏态或展开态。
   * 这条链路只读订阅主进程事件，不允许 renderer 借此获得 ipcRenderer。
   */
  onChatModeRequested(listener: (mode: "collapsed" | "expanded") => void): () => void {
    const channelListener = (_event: unknown, payload: unknown): void => {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        return;
      }

      const mode = (payload as { mode?: unknown }).mode;
      if (mode !== "collapsed" && mode !== "expanded") {
        return;
      }

      listener(mode);
    };

    ipcRenderer.on("chat:mode-requested", channelListener);
    return () => {
      ipcRenderer.removeListener("chat:mode-requested", channelListener);
    };
  },
  /**
   * 桌宠待答气泡被点击后，接收主进程下发的目标卡片编号。
   */
  onFocusBackgroundAgentInteraction(listener: (requestId: string) => void): () => void {
    const channel = "chat:focus-background-interaction";
    const channelListener = (_event: unknown, payload: unknown): void => {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        return;
      }
      const candidate = payload as Record<string, unknown>;
      if (
        Object.keys(candidate).length === 1 &&
        typeof candidate.requestId === "string" &&
        isStandardUuid(candidate.requestId)
      ) {
        listener(candidate.requestId);
      }
    };
    ipcRenderer.on(channel, channelListener);
    return () => ipcRenderer.removeListener(channel, channelListener);
  },
  /**
   * 聊天窗口拖拽由 renderer 识别手势，主进程只接收屏幕坐标并移动原生窗口。
   */
  startChatDrag(point: { screenX: number; screenY: number }): void {
    ipcRenderer.send("chat:drag-start", point);
  },
  moveChatDrag(point: { screenX: number; screenY: number }): void {
    ipcRenderer.send("chat:drag-move", point);
  },
  endChatDrag(): void {
    ipcRenderer.send("chat:drag-end");
  },
  /**
   * 透明无边框窗口通过受控 IPC 执行八方向缩放。
   */
  startChatResize(input: { edge: "n" | "e" | "s" | "w" | "ne" | "nw" | "se" | "sw"; screenX: number; screenY: number }): void {
    ipcRenderer.send("chat:resize-start", input);
  },
  moveChatResize(point: { screenX: number; screenY: number }): void {
    ipcRenderer.send("chat:resize-move", point);
  },
  endChatResize(): void {
    ipcRenderer.send("chat:resize-end");
  },
  submitTurn(input: { submissionId: string; messageText: string }): Promise<unknown> {
    return ipcRenderer.invoke("turn-submission:reserve", input);
  },
  editTurnSubmission(input: { submissionId: string; messageText: string }): Promise<unknown> {
    return ipcRenderer.invoke("turn-submission:edit", input);
  },
  deleteTurnSubmission(submissionId: string): Promise<unknown> {
    return ipcRenderer.invoke("turn-submission:delete", submissionId);
  },
  resumeTurnSubmission(submissionId: string): Promise<unknown> {
    return ipcRenderer.invoke("turn-submission:resume", submissionId);
  },
  retryTurnSubmission(submissionId: string): Promise<unknown> {
    return ipcRenderer.invoke("turn-submission:retry", submissionId);
  },
  prepareEnvironmentSnapshot(): Promise<unknown> {
    return ipcRenderer.invoke("environment:prepare-text");
  },
  async openExternalUrl(url: string): Promise<void> {
    await ipcRenderer.invoke("chat:open-external-url", url);
  },
  async showItemInFolder(path: string): Promise<void> {
    await ipcRenderer.invoke("chat:show-item-in-folder", path);
  },
  addStagedMaterials(items: unknown[]): Promise<unknown> {
    return ipcRenderer.invoke("materials:add-staged", { items });
  },
  removeStagedMaterial(materialId: string): Promise<unknown> {
    return ipcRenderer.invoke("materials:remove-staged", { materialId });
  },
  forgetRecentMaterial(path: string): Promise<unknown> {
    return ipcRenderer.invoke("materials:forget-recent", { path });
  },
  confirmMaterialRemoved(materialId: string, displayName: string): Promise<unknown> {
    return ipcRenderer.invoke("materials:confirm-remove-applied", { materialId, displayName });
  },
  getMaterialFailureReport(): Promise<unknown> {
    return ipcRenderer.invoke("materials:get-failure-report");
  },
  onMaterialFailureReportChanged(listener: (payload: unknown) => void): () => void {
    const channel = "chat:material-failure-report-changed";
    const channelListener = (_event: unknown, payload: unknown): void => listener(payload);
    ipcRenderer.on(channel, channelListener);
    return () => ipcRenderer.removeListener(channel, channelListener);
  },
  onMaterialDeliveryFeedback(listener: (payload: unknown) => void): () => void {
    const channel = "avatar:material-delivery-feedback";
    const channelListener = (_event: unknown, payload: unknown): void => listener(payload);
    ipcRenderer.on(channel, channelListener);
    return () => ipcRenderer.removeListener(channel, channelListener);
  },
  /**
   * renderer 粘贴时通过主进程读取系统剪贴板并转换成本轮材料。
   */
  captureClipboardImageMaterial(): Promise<unknown> {
    return ipcRenderer.invoke("materials:capture-clipboard-image-material");
  },
  createComposerTextMaterial(text: string): Promise<unknown> {
    return ipcRenderer.invoke("materials:create-composer-text-material", { text });
  },
  async getMaterialThumbnail(path: string): Promise<string | null> {
    const result = await ipcRenderer.invoke("materials:get-thumbnail", { path });
    if (!result || typeof result !== "object") {
      return null;
    }

    const thumbnailDataUrl = (result as { thumbnailDataUrl?: unknown }).thumbnailDataUrl;
    return typeof thumbnailDataUrl === "string" ? thumbnailDataUrl : null;
  },
  getPathForFile(file: File): string {
    return webUtils.getPathForFile(file);
  },
  runtime: {
    controlGateway: bootstrap.runtime.controlGateway
  }
});
}
