import { useEffect, useReducer, useRef, useState } from "react";
import { AvatarDragSurface } from "../components/AvatarDragSurface";
import { AvatarResizeFrame } from "../components/AvatarResizeFrame";
import { AvatarActionRail } from "../components/AvatarActionRail";
import { AvatarAssistantBubble } from "../components/AvatarAssistantBubble";
import { AvatarTimedReminderBubble } from "../components/AvatarTimedReminderBubble";
import { AvatarSpeechBubble } from "../components/AvatarSpeechBubble";
import { AvatarStage } from "../components/AvatarStage";
import { AvatarListeningIndicator } from "../components/AvatarListeningIndicator";
import { AvatarStatusIndicator } from "../components/AvatarStatusIndicator";
import { useBootstrapStatus } from "../bootstrap/useBootstrapStatus";
import {
  Live2dLipSyncController,
  type Live2dLipSyncModelBinding
} from "../lib/live2dLipSync";
import { initialAvatarViewState, reduceAvatarViewState } from "./avatarViewState";
import { AvatarBootstrapShell } from "./AvatarBootstrapShell";
import { useAvatarRuntime } from "./useAvatarRuntime";
import type { CompanionAvatarBridge } from "./avatarBridge";
import { resolveAvatarModelSelection } from "./avatarModelSelection";
import {
  createMaterialDeliverySoundPlayer,
  readAvatarMaterialDeliveryFeedback
} from "./avatarMaterialDeliveryFeedback";
import { useAvatarTimedReminderFeedback } from "./useAvatarTimedReminderFeedback";
import { useAvatarInteractionBridge } from "./useAvatarInteractionBridge";
import { useAvatarModelSwitch } from "./useAvatarModelSwitch";
import { useAvatarPetSurface } from "./useAvatarPetSurface";
import { AvatarMaterialFeedbackToast, type AvatarMaterialFeedback } from "./AvatarMaterialFeedbackToast";
import { ShortcutSound } from "../lib/shortcutSound";
import { DeepSeekAvatarController } from "../lib/deepSeekAvatarController";

declare global {
  interface Window {
    companionAvatar?: CompanionAvatarBridge;
  }
}

/**
 * AvatarApp 是头像窗口的 bootstrap gate。
 * 核心输入来自 window.companionAvatar 的 HTTP 基址；预期输出是在 bootstrap ready 前展示无能力桌宠壳，ready 后才挂载头像运行时；边界异常是桥接缺失或后端不可用时不启动 runtime。
 */
export function AvatarApp() {
  /**
   * preload 在浏览器预览里可能不存在，因此所有桥接能力都要允许缺失。
   * 业务意图是只在这里读取最小 bootstrap 探测地址，避免 blocked/loading 时启动头像运行时副作用。
   */
  const avatarBridge = window.companionAvatar;
  const bootstrapStatus = useBootstrapStatus(avatarBridge?.runtime.controlGateway?.httpBaseUrl);

  useEffect(() => {
    // 快捷键早于首次初始化就能注册；短提示属于本地壳层，不依赖语音或业务门禁。
    const sound = new ShortcutSound();
    const unsubscribe = avatarBridge?.onShortcutFeedback?.(feedback => {
      if (feedback.phase !== "pressed") return;
      void sound.play().catch(() => {
        avatarBridge.reportDiagnostic("warn", "avatar.shortcut_sound_failed");
      });
    });
    return () => {
      unsubscribe?.();
      sound.dispose();
    };
  }, [avatarBridge]);

  /**
   *
   * 新逻辑仍然阻止 socket、麦克风、设置读取和运行时能力启动，但不再把头像窗口替换成居中阻断页。
   * 业务意图是先展示桌宠资源与持久气泡，让用户能直接从桌宠气泡进入 console 完成初始化。
   */
  if (bootstrapStatus.status !== "ready") {
    return (
      <AvatarBootstrapShell
        avatarBridge={avatarBridge}
        mode={bootstrapStatus.reason ?? "loading"}
        message={bootstrapStatus.message}
        onRetry={bootstrapStatus.retry}
      />
    );
  }

  return <AvatarReadyApp avatarBridge={avatarBridge} />;
}

type AvatarReadyAppProps = {
  avatarBridge?: CompanionAvatarBridge;
};

/**
 * AvatarApp 只负责头像窗口本身的展示与交互。
 * 核心输入是已通过 bootstrap gate 的 avatar bridge；预期输出是头像舞台、交互热区、气泡和面板按钮；边界异常由运行时 hook 与各交互 guard 内部处理。
 * 业务控制面板、任务流和连接状态都不在这里展开，避免再次回到旧的单窗口大壳。
 */
function AvatarReadyApp({ avatarBridge }: AvatarReadyAppProps) {
  const [viewState, dispatch] = useReducer(reduceAvatarViewState, initialAvatarViewState);
  const [pendingModelId, setPendingModelId] = useState<string | null>(null);
  const [materialFeedback, setMaterialFeedback] = useState<AvatarMaterialFeedback>(null);
  const [supportsAvatarReactions, setSupportsAvatarReactions] = useState(false);
  const materialFeedbackTimerRef = useRef<number | null>(null);
  const appliedPresentationProfileModelIdRef = useRef<string | null>(null);
  const lipSyncControllerRef = useRef<Live2dLipSyncController | null>(null);
  const avatarActionControllerRef = useRef<DeepSeekAvatarController | null>(null);
  const materialSoundPlayerRef = useRef<ReturnType<typeof createMaterialDeliverySoundPlayer> | null>(null);

  /**
   * preload 在浏览器预览里可能不存在，因此所有桥接能力都要允许缺失。
   */
  if (lipSyncControllerRef.current === null) {
    lipSyncControllerRef.current = new Live2dLipSyncController({
      reportDiagnostic(level, message, details) {
        avatarBridge?.reportDiagnostic(level, message, details);
      }
    });
  }
  if (avatarActionControllerRef.current === null) {
    avatarActionControllerRef.current = new DeepSeekAvatarController({
      reportDiagnostic(level, message, details) {
        avatarBridge?.reportDiagnostic(level, message, details);
      }
    });
  }
  if (materialSoundPlayerRef.current === null) {
    materialSoundPlayerRef.current = createMaterialDeliverySoundPlayer({
      reportPlaybackError(error) {
        avatarBridge?.reportDiagnostic("warn", "avatar.material_delivery_sound_failed", error);
      }
    });
  }

  const avatarRuntime = useAvatarRuntime(avatarBridge, {
    onAssistantPlaybackElementChanged(element) {
      lipSyncControllerRef.current?.setAudioElement(element);
    }
  });
  const companionDisplayState = avatarRuntime.companionStatus?.state ?? "syncing";
  useEffect(() => {
    avatarActionControllerRef.current?.setState(companionDisplayState);
  }, [companionDisplayState]);
  const { feedbackActive: reminderFeedbackActive, stopFeedback: stopReminderFeedback } =
    useAvatarTimedReminderFeedback(
      avatarRuntime.timedReminderSnapshot,
      avatarRuntime.requestingReminderIds,
      avatarBridge
    );
  const {
    committedPresentationModelId,
    committedSelectedModelId,
    modelCatalog,
    presentationProfile,
    rememberedModelUnavailable,
    resolvedModelPath,
    targetModelId
  } = resolveAvatarModelSelection({
    avatarBridge,
    avatarRuntime,
    pendingModelId
  });
  const {
    handleModelLoadFailed,
    handleModelReady
  } = useAvatarModelSwitch({
    appliedPresentationProfileModelIdRef,
    avatarBridge,
    avatarRuntime,
    committedPresentationModelId,
    committedSelectedModelId,
    modelCatalog,
    pendingModelId,
    rememberedModelUnavailable,
    resolvedModelPath,
    setPendingModelId,
    targetModelId
  });

  useEffect(() => {
    return () => {
      clearMaterialFeedbackTimer();
      // StrictMode 会在同一组件上重放副作用；释放外部资源后保留可重新绑定的控制器，
      // 避免重放期间没有 render 重建 ref，导致后续模型和音频回调丢失接收方。
      lipSyncControllerRef.current?.destroy();
      avatarActionControllerRef.current?.destroy();
    };
  }, []);

  useEffect(() => {
    return avatarBridge?.onMaterialDeliveryFeedback?.((payload) => {
      const feedback = readAvatarMaterialDeliveryFeedback(payload);
      if (!feedback) {
        return;
      }

      if ("sound" in feedback && feedback.sound) {
        materialSoundPlayerRef.current?.play(feedback.sound);
      }

      showMaterialFeedback({
        kind: feedback.kind,
        text: feedback.text
      }, feedback.kind !== "progress");
    });
  }, [avatarBridge]);

  function clearMaterialFeedbackTimer(): void {
    if (materialFeedbackTimerRef.current === null) {
      return;
    }

    window.clearTimeout(materialFeedbackTimerRef.current);
    materialFeedbackTimerRef.current = null;
  }

  function showMaterialFeedback(
    feedback: NonNullable<AvatarMaterialFeedback>,
    autoDismiss = true
  ): void {
    clearMaterialFeedbackTimer();
    setMaterialFeedback(feedback);
    if (!autoDismiss) {
      return;
    }
    materialFeedbackTimerRef.current = window.setTimeout(() => {
      materialFeedbackTimerRef.current = null;
      setMaterialFeedback(null);
    }, 2000);
  }

  /**
   * bottomBubbleStyle 把底部气泡从“钉死窗口底边”改为“锚定脚部 + 贴可见身体最底部”。
   * 业务意图：petBounds 实时反映模型在窗口内的位置，气泡据此落在脚部附近；
   * 当下半身被任务栏/屏幕下边缘切掉时，主进程推送的 visibleBottom 上移，气泡随之贴到仍可见的身体最底部。
   * 边界异常：petBounds 尚不可用时返回 undefined，组件回退到 CSS 中 bottom:92px 的默认锚点；
   * visibleArea 尚未推送时 visibleBottom 传 undefined，layout 退化为按窗口高度兜底。
   */
  const chatExpanded = avatarRuntime.chatMode === "expanded";
  const pendingInteraction = !chatExpanded
    ? avatarRuntime.pendingInteraction
    : null;
  const pendingInteractionBubbleText = pendingInteraction?.kind === "permission"
    ? "后台任务需要你的授权，点击处理"
    : pendingInteraction?.kind === "question"
      ? "后台任务需要补充信息，点击处理"
      : "";
  const bottomBubbleText = pendingInteractionBubbleText || avatarRuntime.bottomBubble?.text || "";
  const petSurface = useAvatarPetSurface({
    avatarBridge,
    bubbleText: bottomBubbleText,
    controlsVisible: viewState.controlsVisible,
    hasReactionButton: supportsAvatarReactions,
    presentationProfile
  });
  const {
    hideAvatarControlsImmediately,
    releaseAvatarInteraction,
    syncAvatarInteractionFromPointerEvent
  } = useAvatarInteractionBridge({
    avatarBridge,
    controlsVisible: viewState.controlsVisible,
    dispatch,
    panelButtonRect: petSurface.panelButtonRect,
    petBounds: petSurface.petBounds,
    presentationProfile,
    resizeFrameRect: petSurface.resizeFrameRect
  });

  const openChat = () => {
    releaseAvatarInteraction();
    hideAvatarControlsImmediately();
    avatarBridge?.openChat?.();
  };
  const reactToAvatarTap = () => {
    avatarActionControllerRef.current?.reactToTap();
    openChat();
  };

  return (
    <main
      className={reminderFeedbackActive ? "avatar-shell avatar-shell--reminder-feedback" : "avatar-shell"}
      data-window="avatar"
      onMouseMove={syncAvatarInteractionFromPointerEvent}
      onMouseLeave={releaseAvatarInteraction}
      onDragEnter={event => {
        if (!isFileDragEvent(event.dataTransfer)) {
          return;
        }

        event.preventDefault();
        avatarBridge?.startMaterialDrop?.();
      }}
      onDragOver={event => {
        if (!isFileDragEvent(event.dataTransfer)) {
          return;
        }

        event.preventDefault();
        avatarBridge?.startMaterialDrop?.();
      }}
      onDragLeave={event => {
        if (event.currentTarget.contains(event.relatedTarget as Node | null)) {
          return;
        }

        avatarBridge?.endMaterialDrop?.();
      }}
      onDragEnd={() => {
        avatarBridge?.endMaterialDrop?.();
      }}
      onDrop={event => {
        if (!isFileDragEvent(event.dataTransfer)) {
          return;
        }

        event.preventDefault();
        avatarBridge?.endMaterialDrop?.();
        const items = materialItemsFromDrop(event.dataTransfer.files, file => avatarBridge?.getPathForFile?.(file) ?? "");
        if (items.length === 0) {
          showMaterialFeedback({
            kind: "error",
            text: "没有拿到文件路径"
          });
          return;
        }

        void submitAvatarMaterialDrop({
          avatarBridge,
          items,
          showMaterialFeedback
        });
      }}
    >
      <AvatarStage
        diagnosticsBridge={avatarBridge}
        modelPath={avatarRuntime.settingsHydrated ? resolvedModelPath || null : null}
        presentationProfile={presentationProfile}
        assistantStateLabel="待机中"
        controlStatusLabel="悬停后可展开面板"
        mediaStatusLabel="拖拽可移动桌宠"
        mode="pet"
        onModelReady={handleModelReady}
        onModelLoadFailed={handleModelLoadFailed}
        onAvatarTap={reactToAvatarTap}
        onPetBoundsChange={bounds => {
          petSurface.updatePetBounds(bounds);
        }}
        onActiveLive2dModelChanged={(binding: Live2dLipSyncModelBinding | null) => {
          lipSyncControllerRef.current?.setModelBinding(binding);
          // 只有已经提交到舞台的模型才能启用动作，切换中的目标不能改变旧人物的交互。
          setSupportsAvatarReactions(
            avatarActionControllerRef.current?.setModelBinding(binding) ?? false
          );
        }}
      />
      <AvatarResizeFrame
        bounds={petSurface.petBounds}
        visible={viewState.controlsVisible}
        onResizeStart={petSurface.resizeHandlers.onResizeStart}
        onResizeMove={petSurface.resizeHandlers.onResizeMove}
        onResizeEnd={petSurface.resizeHandlers.onResizeEnd}
      />
      <AvatarListeningIndicator
        active={avatarRuntime.localListeningHintActive}
        petBounds={petSurface.petBounds}
      />
      {avatarRuntime.companionStatus ? (
        <AvatarStatusIndicator
          status={avatarRuntime.companionStatus}
          petBounds={petSurface.petBounds}
          visibleArea={petSurface.visibleArea}
        />
      ) : null}

      <AvatarDragSurface
        bounds={petSurface.avatarInteractionHotspotBounds}
        onDragStart={petSurface.dragHandlers.onDragStart}
        onDragMove={petSurface.dragHandlers.onDragMove}
        onDragEnd={petSurface.dragHandlers.onDragEnd}
        onTap={reactToAvatarTap}
      />
      {/* Chat 展开后，所有对话与提醒只在 Chat 主场投影；桌宠仍保留动作和媒体播放。 */}
      {!chatExpanded && avatarRuntime.reflectionStatus ? (
        <AvatarAssistantBubble
          text={avatarRuntime.reflectionStatus.text || "\u00a0"}
          petBounds={petSurface.petBounds}
          anchor={presentationProfile.layout.headAnchor}
        />
      ) : !chatExpanded && avatarRuntime.ringingReminders?.length > 0 ? (
        <AvatarTimedReminderBubble
          reminders={avatarRuntime.ringingReminders}
          feedbackActive={reminderFeedbackActive}
          petBounds={petSurface.petBounds}
          anchor={presentationProfile.layout.headAnchor}
          requesting={avatarRuntime.ringingReminders.some(reminder =>
            avatarRuntime.requestingReminderIds?.has(reminder.reminder_id)
          )}
          errorMessage={avatarRuntime.reminderDismissError ?? null}
          onDismiss={() => {
            stopReminderFeedback();
            void avatarRuntime.requestDismissReminders?.(
              avatarRuntime.timedReminderSnapshot.runtime_id,
              avatarRuntime.ringingReminders.map(reminder => reminder.reminder_id)
            );
          }}
        />
      ) : !chatExpanded && avatarRuntime.assistantBubble ? (
        <AvatarAssistantBubble
          text={avatarRuntime.assistantBubble.visibleText}
          petBounds={petSurface.petBounds}
          anchor={presentationProfile.layout.headAnchor}
        />
      ) : null}
      {!chatExpanded && pendingInteraction ? (
        <AvatarSpeechBubble
          text={pendingInteractionBubbleText}
          style={petSurface.bottomBubbleStyle}
          onClick={() => {
            avatarBridge?.openBackgroundAgentInteraction?.(pendingInteraction.requestId);
          }}
        />
      ) : !chatExpanded && (avatarRuntime.bottomBubble?.kind === "speech" ||
        avatarRuntime.bottomBubble?.kind === "notice" ||
        avatarRuntime.bottomBubble?.kind === "smart_turn_waiting") ? (
        <AvatarSpeechBubble text={avatarRuntime.bottomBubble.text} style={petSurface.bottomBubbleStyle} />
      ) : null}
      <AvatarMaterialFeedbackToast feedback={materialFeedback} style={petSurface.bottomBubbleStyle} />
      <AvatarActionRail
        visible={viewState.controlsVisible}
        petBounds={petSurface.petBounds}
        onOpenChat={openChat}
        onPlayReaction={supportsAvatarReactions
          ? () => avatarActionControllerRef.current?.playNextReaction()
          : undefined}
        onOpenModels={() => {
          releaseAvatarInteraction();
          hideAvatarControlsImmediately();
          avatarBridge?.openPanel("models");
        }}
        onOpenConsole={() => {
          releaseAvatarInteraction();
          hideAvatarControlsImmediately();
          avatarBridge?.openConsole?.({ section: "settings" });
        }}
      />
    </main>
  );
}

async function submitAvatarMaterialDrop(input: {
  avatarBridge?: CompanionAvatarBridge;
  items: ReturnType<typeof materialItemsFromDrop>;
  showMaterialFeedback(feedback: NonNullable<AvatarMaterialFeedback>): void;
}): Promise<void> {
  try {
    await input.avatarBridge?.addStagedMaterials?.(input.items);
  } catch {
    input.showMaterialFeedback({
      kind: "error",
      text: "材料添加失败"
    });
  }
}

function isFileDragEvent(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) {
    return false;
  }

  if (!dataTransfer.types) {
    return dataTransfer.files.length > 0;
  }

  return Array.from(dataTransfer.types).includes("Files");
}

function materialItemsFromDrop(files: FileList, resolvePath: (file: File) => string): Array<{
  kind: "dragged_file";
  source: "drag_drop";
  display_name: string;
  path: string;
}> {
  return Array.from(files)
    .map(file => {
      const path = resolvePath(file).trim();
      if (!path) {
        return null;
      }

      return {
        kind: "dragged_file" as const,
        source: "drag_drop" as const,
        display_name: file.name || path,
        path
      };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null);
}
