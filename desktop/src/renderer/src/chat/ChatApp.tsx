import { useEffect, useState, type DragEvent } from "react";
import companionAvatarUrl from "../../../../../assets/icon.png";
import userAvatarUrl from "../../../../../assets/user_icon.png";
import { BootstrapBlockedView } from "../components/BootstrapBlockedView";
import { useBootstrapStatus } from "../bootstrap/useBootstrapStatus";
import { MaterialShelf } from "../materials/MaterialShelf";
import { RecentMaterialShelf } from "../materials/RecentMaterialShelf";
import { MaterialFailureReportCard } from "./MaterialFailureReportCard";
import { ChatDockShell, type ChatDockMode } from "./ChatDockShell";
import {
  hasStagedManagedAsset,
  materialItemsFromFiles,
  normalizeCapturedMaterial
} from "./chatAttachments";
import { ChatComposer, type ChatComposerAttachment } from "./ChatComposer";
import { classifyComposerPaste } from "./chatPasteClient";
import { ChatMessageList } from "./ChatMessageList";
import { ChatStatusRail } from "./ChatStatusRail";
import { readMaterialFeedbackEvent } from "./chatMaterialFeedback";
import { ChatResizeHandles } from "./ChatResizeHandles";
import { useChatRuntime } from "./useChatRuntime";
import { createInitialCompanionStatusState, projectCompanionStatus } from "../companion/companionStatus";
import type { StagedMaterialMutationResult } from "./useChatRuntime";
import { useMaterialFeedback } from "./useMaterialFeedback";
import { useMaterialFailureReport } from "./useMaterialFailureReport";
import { useChatWindowDragGesture } from "./useChatWindowDragGesture";

type BootstrapChatBridge = {
  reportDiagnostic?(level: "info" | "warn" | "error", message: string, details?: unknown): void;
  setChatMode?(mode: ChatDockMode): void;
  getChatMode?(): Promise<ChatDockMode | null>;
  onChatModeRequested?(listener: (mode: ChatDockMode) => void): () => void;
  onFocusBackgroundAgentInteraction?(listener: (requestId: string) => void): () => void;
  startChatDrag?(point: { screenX: number; screenY: number }): void;
  moveChatDrag?(point: { screenX: number; screenY: number }): void;
  endChatDrag?(): void;
  startChatResize?(input: { edge: "n" | "e" | "s" | "w" | "ne" | "nw" | "se" | "sw"; screenX: number; screenY: number }): void;
  moveChatResize?(point: { screenX: number; screenY: number }): void;
  endChatResize?(): void;
  addStagedMaterials?(items: unknown[]): Promise<unknown>;
  prepareEnvironmentSnapshot?(): Promise<unknown>;
  openExternalUrl?(url: string): Promise<void>;
  showItemInFolder?(path: string): Promise<void>;
  onMaterialDeliveryFeedback?(listener: (payload: unknown) => void): () => void;
  getMaterialFailureReport?(): Promise<unknown>;
  onMaterialFailureReportChanged?(listener: (payload: unknown) => void): () => void;
  captureClipboardImageMaterial?(): Promise<unknown>;
  createComposerTextMaterial?(text: string): Promise<unknown>;
  getPathForFile?(file: File): string;
  getMaterialThumbnail?(path: string): Promise<string | null>;
  runtime: {
    controlGateway: {
      httpBaseUrl: string;
    };
  };
};

/**
 * ChatApp 是独立聊天窗口的 bootstrap gate。
 * 核心输入来自 window.companionChat；后端 ready 后才启动聊天 runtime，避免提前创建控制连接。
 *
 * @returns 未就绪时返回配置提示；就绪后返回聊天界面。
 */
export function ChatApp() {
  const chatBridge = window.companionChat as BootstrapChatBridge | undefined;
  const bootstrapStatus = useBootstrapStatus(chatBridge?.runtime.controlGateway.httpBaseUrl);

  if (bootstrapStatus.status !== "ready") {
    return (
      <BootstrapBlockedView
        reason={bootstrapStatus.reason ?? "backend_unavailable"}
        windowLabel="聊天窗口"
        onRetry={bootstrapStatus.retry}
      />
    );
  }

  return <ChatReadyApp />;
}

/**
 * ChatReadyApp 渲染真正的聊天界面。
 * 运行时副作用由 useChatRuntime 托管，组件层只组合标题、消息区和输入区。
 */
function ChatReadyApp() {
  const runtime = useChatRuntime();
  const companionStatus = runtime.companionStatus ?? projectCompanionStatus(createInitialCompanionStatusState(), []);
  const assistantOutputError = runtime.state.assistantOutputError ?? null;
  const chatBridge = window.companionChat;
  const [isDragActive, setIsDragActive] = useState(false);
  // 主进程创建窗口时已经保持隐藏；首帧使用展开布局可以避免 renderer 水合期间丢失输入区。
  // 真正可见前由 getChatMode/onChatModeRequested 写入主进程权威模式。
  const [chatMode, setChatMode] = useState<ChatDockMode>("expanded");
  const [chatModeHydrated, setChatModeHydrated] = useState(false);
  const [interactionFocusRequestId, setInteractionFocusRequestId] = useState<string | null>(null);
  const materialFeedback = useMaterialFeedback();
  const materialFailureReport = useMaterialFailureReport(chatBridge);
  const collapseGesture = useChatWindowDragGesture({
    bridge: chatBridge,
    onClick() {
      return;
    }
  });

  useEffect(() => {
    if (chatModeHydrated) {
      chatBridge?.setChatMode?.(chatMode);
    }
  }, [chatBridge, chatMode, chatModeHydrated]);

  useEffect(() => {
    let cancelled = false;
    const dispose = chatBridge?.onChatModeRequested?.((mode) => {
      setChatMode(mode);
      setChatModeHydrated(true);
    });
    const getChatMode = chatBridge?.getChatMode;
    if (getChatMode) {
      void getChatMode().then(mode => {
        if (!cancelled && mode) {
          setChatMode(mode);
          setChatModeHydrated(true);
        }
      });
    }
    return () => {
      cancelled = true;
      dispose?.();
    };
  }, [chatBridge]);

  function collapseChat(): void {
    setChatModeHydrated(true);
    setChatMode("collapsed");
    // 关闭是明确的用户动作，首帧水合尚未完成时也必须立即交给主进程隐藏窗口。
    chatBridge?.setChatMode?.("collapsed");
  }

  useEffect(() => {
    return chatBridge?.onFocusBackgroundAgentInteraction?.((requestId) => {
      setInteractionFocusRequestId(requestId);
      setChatMode("expanded");
    });
  }, [chatBridge]);

  useEffect(() => {
    if (chatMode !== "expanded" || interactionFocusRequestId === null) {
      return;
    }

    const interactionCard = Array.from(
      document.querySelectorAll<HTMLElement>("[data-background-interaction-id]")
    ).find(element => element.dataset.backgroundInteractionId === interactionFocusRequestId);
    if (!interactionCard) {
      return;
    }

    interactionCard.scrollIntoView({ behavior: "smooth", block: "center" });
    interactionCard.focus({ preventScroll: true });
    setInteractionFocusRequestId(null);
  }, [chatMode, interactionFocusRequestId, runtime.state.messages]);

  useEffect(() => {
    return chatBridge?.onMaterialDeliveryFeedback?.((payload) => {
      try {
        const feedback = readMaterialFeedbackEvent(payload);
        materialFeedback.show(feedback, feedback.kind !== "progress");
      } catch (error) {
        chatBridge?.reportDiagnostic?.("warn", "materials.feedback_event_invalid", error);
      }
    });
  }, [chatBridge, materialFeedback.show]);

  useEffect(() => {
    function clearDragActive(): void {
      setIsDragActive(false);
    }

    window.addEventListener("dragend", clearDragActive);
    window.addEventListener("drop", clearDragActive);
    window.addEventListener("blur", clearDragActive);
    return () => {
      window.removeEventListener("dragend", clearDragActive);
      window.removeEventListener("drop", clearDragActive);
      window.removeEventListener("blur", clearDragActive);
    };
  }, []);

  async function addStagedMaterials(items: ChatComposerAttachment[]): Promise<StagedMaterialMutationResult> {
    if (items.length === 0) {
      throw new Error("追加 staged 材料至少需要一项输入");
    }

    materialFeedback.show({ kind: "progress", text: `正在检查 ${items.length} 项…` }, false);
    try {
      const result = await runtime.addStagedMaterials(items);
      if (result.feedback) {
        materialFeedback.show(result.feedback);
      } else {
        materialFeedback.clear();
      }
      return result;
    } catch (error) {
      materialFeedback.show({ kind: "warning", text: "材料添加失败，请稍后重试。" });
      throw error;
    }
  }

  async function handlePasteText(text: string): Promise<"inline" | "material"> {
    const disposition = await classifyComposerPaste(
      chatBridge?.runtime.controlGateway.httpBaseUrl ?? "",
      text
    );
    if (disposition === "inline") {
      return "inline";
    }
    const attachment = normalizeCapturedMaterial(await chatBridge?.createComposerTextMaterial?.(text));
    if (!attachment?.managed_asset_id) {
      throw new Error("长文本材料创建失败");
    }
    const result = await addStagedMaterials([attachment]);
    if (!hasStagedManagedAsset(result.materialContext, attachment.managed_asset_id)) {
      throw new Error("长文本材料未进入 Backend 权威快照");
    }
    return "material";
  }

  async function handlePasteImage(): Promise<void> {
    const attachment = normalizeCapturedMaterial(await chatBridge?.captureClipboardImageMaterial?.());
    if (!attachment?.managed_asset_id) {
      throw new Error("剪贴板图片材料创建失败");
    }
    const result = await addStagedMaterials([attachment]);
    if (!hasStagedManagedAsset(result.materialContext, attachment.managed_asset_id)) {
      throw new Error("剪贴板图片未进入 Backend 权威快照");
    }
  }

  async function handleSelectFiles(files: File[]): Promise<void> {
    const items = materialItemsFromFiles(
      files,
      file => chatBridge?.getPathForFile?.(file) ?? ""
    );
    if (items.length === 0) {
      materialFeedback.show({ kind: "warning", text: "没有拿到文件路径，请重新选择本地文件。" });
      return;
    }
    await addStagedMaterials(items);
  }

  const dropZoneHandlers = {
    onDragOver(event: DragEvent<HTMLElement>) {
      event.preventDefault();
      setIsDragActive(true);
    },
    onDragLeave(event: DragEvent<HTMLElement>) {
      if (event.currentTarget === event.target) {
        setIsDragActive(false);
      }
    },
    onDrop(event: DragEvent<HTMLElement>) {
      event.preventDefault();
      setIsDragActive(false);
      const items = materialItemsFromFiles(event.dataTransfer.files, file => chatBridge?.getPathForFile?.(file) ?? "");
      if (items.length > 0) {
        void addStagedMaterials(items)
          .catch(() => {
            return;
          });
        return;
      }

      materialFeedback.show({ kind: "warning", text: "没有拿到文件路径，请从资源管理器拖入本地文件。" });
    }
  };

  return (
    <ChatDockShell
      mode={chatMode}
    >
      <main
        className="chat-shell"
        data-mode="expanded"
        data-window="chat"
        {...dropZoneHandlers}
      >
        <ChatResizeHandles bridge={chatBridge} />
        {isDragActive ? <div className="chat-drop-overlay">松开以添加到本轮材料</div> : null}
        <header className="chat-titlebar">
          <div className="chat-title-drag-region" {...collapseGesture}>
            <span className="chat-brand">NearMate</span>
            <span className="chat-title-divider" aria-hidden="true" />
            <img className="chat-title-avatar" src={companionAvatarUrl} alt="" />
            <div className="chat-title-identity">
              <strong>{runtime.state.companionName}</strong>
              <span>你的桌面陪伴</span>
            </div>
            <span
              className={`chat-title-status chat-title-status--${companionStatus.state}`}
              data-voice-state={companionStatus.state}
              role="status"
            >
              <span className="chat-title-status-dot" aria-hidden="true" />
              {companionStatus.label}
            </span>
            {companionStatus.wakeMode !== "unknown" && companionStatus.state !== "waiting_wake" && companionStatus.state !== "available" ? (
              <span className="chat-title-wake-mode">{companionStatus.wakeLabel}</span>
            ) : null}
          </div>
          <div className="chat-title-tagline" aria-label="陪伴寄语">
            <span>有我在，一切都会更好</span>
            <span aria-hidden="true">♡</span>
          </div>
          <div className="chat-window-actions">
            <button
              type="button"
              aria-label="收起聊天窗口"
              title="收起"
              onClick={collapseChat}
            >
              <span className="chat-minimize-icon" aria-hidden="true" />
            </button>
            <button
              type="button"
              aria-label="关闭聊天窗口"
              title="关闭"
              onClick={collapseChat}
            >
              <span aria-hidden="true">×</span>
            </button>
          </div>
        </header>
        <div className="chat-layout">
          <section className="chat-main-column">
            <section className="chat-history-surface" aria-label="聊天记录">
              <ChatMessageList
                state={runtime.state}
                companionAvatarUrl={companionAvatarUrl}
                userAvatarUrl={userAvatarUrl}
                onAnswerInteraction={runtime.submitWorkInteractionAction}
                onEditTurnSubmission={runtime.editTurnSubmission}
                onDeleteTurnSubmission={runtime.deleteTurnSubmission}
                onResumeTurnSubmission={runtime.resumeTurnSubmission}
                onRetryTurnSubmission={runtime.retryTurnSubmission}
                onOpenOutputPath={async path => {
                  if (!chatBridge?.showItemInFolder) {
                    throw new Error("交付文件定位能力不可用");
                  }
                  await chatBridge.showItemInFolder(path);
                }}
              />
              <MaterialShelf
                pool={runtime.materialContext}
                removeMaterialItem={runtime.removeMaterialItem}
                getMaterialThumbnail={chatBridge?.getMaterialThumbnail}
                onMaterialRemoved={materialFeedback.clear}
              />
              <RecentMaterialShelf
                materials={runtime.materialContext?.recent_materials ?? []}
                refresh={runtime.refreshMaterialContext}
                forget={runtime.forgetRecentMaterial}
              />
              {materialFailureReport ? <MaterialFailureReportCard report={materialFailureReport} /> : null}
              {materialFeedback.feedback || runtime.materialMutationError ? (
                <div
                  className="chat-material-feedback"
                  data-kind={runtime.materialMutationError ? "warning" : materialFeedback.feedback?.kind}
                  role="status"
                >
                  {runtime.materialMutationError ?? materialFeedback.feedback?.text}
                </div>
              ) : null}
            </section>
            {runtime.liveTranscript ? (
              <div className="chat-live-transcript" role="status" aria-live="polite">
                <span>实时转写</span>
                <p>{runtime.liveTranscript}</p>
              </div>
            ) : null}
            <section className="chat-composer-surface" aria-label="聊天输入区">
              <ChatComposer
                errorMessage={assistantOutputError ?? runtime.textSubmitError}
                isSubmissionDisabled={assistantOutputError !== null}
                onPasteText={handlePasteText}
                onPasteImage={handlePasteImage}
                onSelectFiles={handleSelectFiles}
                onFocus={() => {
                  void chatBridge?.prepareEnvironmentSnapshot?.().catch(error => {
                    chatBridge?.reportDiagnostic?.("warn", "environment.prepare_failed", error);
                  });
                }}
                onSubmit={async ({ messageText }) => {
                  materialFeedback.clear();
                  await runtime.submitText(messageText);
                }}
              />
            </section>
          </section>
          <ChatStatusRail
            companionName={runtime.state.companionName}
            voiceState={runtime.voiceState}
            companionStatus={companionStatus}
            workspace={runtime.workspace}
            llmAccountStatus={runtime.workspace.llmAccountStatus}
          />
        </div>
      </main>
    </ChatDockShell>
  );
}
