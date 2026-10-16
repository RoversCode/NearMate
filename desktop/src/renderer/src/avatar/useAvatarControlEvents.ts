import {
  startTransition,
  useEffect,
  useRef,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction
} from "react";
import { ControlSocket } from "../lib/controlSocket";
import type { AudioDevicesChangeRequestedEvent, BackendEvent, ConnectionStatus, ReminderSettings } from "../types/events";
import type { ActivitySnapshot, TimedReminderSnapshot } from "../types/backendEvents";
import type { AvatarBubbleEvent, BottomAvatarBubble } from "./avatarBubbleState";
import type { PendingBackgroundAgentInteraction } from "./useAvatarRuntime";
import { AvatarPlayoutDeliveryCoordinator } from "./playoutDeliveryCoordinator";
import type { ShortcutFeedback } from "../../../shared/shortcutFeedback";
import type { AssistantAudioPlayback } from "../lib/assistantAudioPlayback";
import { ShortcutSpeechGate } from "./shortcutSpeechGate";

type CompanionAvatarRuntimeBridge = {
  reportDiagnostic(level: "info" | "warn" | "error", message: string, details?: unknown): void;
  runtime: {
    controlGateway?: {
      websocketUrl: string;
      httpBaseUrl: string;
    };
  };
  getChatMode?(): Promise<"collapsed" | "expanded" | null>;
  onChatModeChanged?(listener: (mode: "collapsed" | "expanded") => void): () => void;
  onShortcutFeedback?(listener: (feedback: ShortcutFeedback) => void): () => void;
  openBackgroundAgentInteraction?(requestId: string): void;
};

type UseAvatarControlEventsInput = {
  avatarBridge?: CompanionAvatarRuntimeBridge;
  assistantAudioPlayback?: AssistantAudioPlayback;
  audioDeviceSessionReloadRef: MutableRefObject<((payload: AudioDevicesChangeRequestedEvent["payload"]) => void) | null>;
  bottomBubbleRef: MutableRefObject<BottomAvatarBubble | null>;
  controlSocketRef: MutableRefObject<ControlSocket | null>;
  playoutDeliveryCoordinatorRef: MutableRefObject<AvatarPlayoutDeliveryCoordinator | null>;
  dispatch(action: AvatarBubbleEvent): void;
  setSettings(settings: ReminderSettings): void;
  setSettingsHydrated(value: boolean): void;
  setActivitySnapshot(snapshot: ActivitySnapshot): void;
  setTimedReminderSnapshot(snapshot: TimedReminderSnapshot): void;
  setPendingInteractions: Dispatch<SetStateAction<PendingBackgroundAgentInteraction[]>>;
  onCompanionEvent?(event: BackendEvent): void;
  onCompanionConnectionChange?(status: ConnectionStatus): void;
};

/**
 * useAvatarControlEvents 管理控制 WebSocket、设置水合和后端事件分发。
 *
 * Args:
 *   input: 控制网关 bridge、状态写入器、独立角色状态回调和音频设备重连回调引用。
 * Returns:
 *   不返回值；订阅后端事件并水合设置，角色状态不经过气泡门禁，已收到事件时忽略迟到的初始读取结果。
 */
export function useAvatarControlEvents({
  avatarBridge,
  assistantAudioPlayback,
  audioDeviceSessionReloadRef,
  bottomBubbleRef,
  controlSocketRef,
  playoutDeliveryCoordinatorRef,
  dispatch,
  setSettings,
  setSettingsHydrated,
  setActivitySnapshot,
  setTimedReminderSnapshot,
  setPendingInteractions,
  onCompanionEvent,
  onCompanionConnectionChange
}: UseAvatarControlEventsInput): void {
  // 状态写入回调会随 React 渲染更新，但不能因此重建控制连接或媒体交付协调器。
  const companionEventRef = useRef(onCompanionEvent);
  const companionConnectionChangeRef = useRef(onCompanionConnectionChange);
  companionEventRef.current = onCompanionEvent;
  companionConnectionChangeRef.current = onCompanionConnectionChange;

  useEffect(() => {
    const controlSocket = new ControlSocket({
      websocketUrl: avatarBridge?.runtime.controlGateway?.websocketUrl,
      httpBaseUrl: avatarBridge?.runtime.controlGateway?.httpBaseUrl,
      diagnosticsBridge: avatarBridge?.reportDiagnostic ? avatarBridge : undefined
    });
    controlSocketRef.current = controlSocket;
    const playoutDeliveryCoordinator = new AvatarPlayoutDeliveryCoordinator({
      clientInstanceId: crypto.randomUUID(),
      reportAck: ack => controlSocket.reportPlayoutDeliveryAck(ack),
      reportDiagnostic: (level, message, details) => {
        avatarBridge?.reportDiagnostic(level, message, details);
      }
    });
    playoutDeliveryCoordinatorRef.current = playoutDeliveryCoordinator;
    const speechGate = new ShortcutSpeechGate(
      muted => assistantAudioPlayback?.setMuted(muted),
      delivery => playoutDeliveryCoordinator.handleDeliveryReady(delivery)
    );
    const disposeShortcut = avatarBridge?.onShortcutFeedback?.(feedback => {
      // 同步静音不经过 React transition，也不等待短音效或 HTTP 请求。
      speechGate.handleFeedback(feedback);
      if (feedback.phase === "settled" && feedback.interruptedRequestIds === null) {
        dispatch({ type: "shell.avatar_bubble_changed", payload: {
          kind: "notice", text: "快捷键操作未完成，请重试。", ttl_ms: 6000
        } });
      }
    });
    const openedPermissionRequestIds = new Set<string>();
    let cancelled = false;
    let hasReceivedSettingsSnapshot = false;

    void controlSocket.fetchSettings().then(settingsSnapshot => {
      // 初始读取只填补还没有设置的界面，不能把已推送的新人物或字幕偏好改回旧值。
      if (cancelled || hasReceivedSettingsSnapshot) {
        return;
      }

      startTransition(() => {
        setSettings(settingsSnapshot);
        setSettingsHydrated(true);
        dispatch({
          type: "settings.updated",
          payload: settingsSnapshot
        });
      });
    });

    const dispose = controlSocket.connect({
      onEvent(event) {
        companionEventRef.current?.(event);
        // 先登记事实再更新 React，确保同一拍完成的初始 HTTP 不会抢回设置。
        if (event.type === "settings.changed") hasReceivedSettingsSnapshot = true;
        speechGate.handleBackendEvent(event);
        if (
          event.type === "background_agent.interaction_requested" &&
          event.payload.kind === "permission" &&
          !openedPermissionRequestIds.has(event.payload.request_id)
        ) {
          openedPermissionRequestIds.add(event.payload.request_id);
          avatarBridge?.openBackgroundAgentInteraction?.(event.payload.request_id);
        }

        startTransition(() => {
          dispatch(event);

          if (event.type === "settings.changed") {
            setSettings(event.payload);
            setSettingsHydrated(true);
          }
          if (event.type === "activities.snapshot_changed") {
            setActivitySnapshot(event.payload);
          }
          if (event.type === "reminders.snapshot_changed") {
            setTimedReminderSnapshot(event.payload);
          }
          if (event.type === "background_agent.interaction_requested") {
            setPendingInteractions(current => {
              if (current.some(item => item.requestId === event.payload.request_id)) {
                return current;
              }
              return [...current, {
                requestId: event.payload.request_id,
                kind: event.payload.kind
              }];
            });
          }
          if (event.type === "background_agent.interactions_snapshot") {
            setPendingInteractions(event.payload.items.flatMap(item => (
              item.status === "pending"
                ? [{ requestId: item.request_id, kind: item.kind }]
                : []
            )));
          }
          if (event.type === "background_agent.interaction_submitted") {
            setPendingInteractions(current =>
              current.filter(item => item.requestId !== event.payload.request_id)
            );
          }
          if (event.type === "background_agent.interaction_resolved") {
            setPendingInteractions(current =>
              current.filter(item => item.requestId !== event.payload.request_id)
            );
          }
          if (event.type === "assistant.playout_delivery_failed") {
            playoutDeliveryCoordinator.handleDeliveryFailed(event.payload.delivery_id);
            dispatch({
              type: "shell.avatar_bubble_changed",
              payload: {
                kind: "notice",
                text: event.payload.message,
                ttl_ms: 12000
              }
            });
          }
        });

        if (event.type === "audio.devices_change_requested") {
          audioDeviceSessionReloadRef.current?.(event.payload);
        }
      },
      onStatusChange(status, reason, source) {
        // 模型设置保存失败只影响这次操作，不能把仍在线的角色图案切成断线状态。
        if (source !== "settings") companionConnectionChangeRef.current?.(status);
        /**
         * 控制连接重建后，Backend 可能已经换过输入 Runtime，Turn 编号也可能从头开始。
         * Avatar 不展示连接文案，但必须把连接代际交给 reducer，清掉只能在旧连接内比较的 Turn 基线。
         */
        dispatch({
          type: "control.connection_changed",
          payload: {
            status,
            ...(reason ? { reason } : {})
          }
        });
      }
    });

    return () => {
      cancelled = true;
      disposeShortcut?.();
      dispose();
      controlSocketRef.current = null;
      playoutDeliveryCoordinatorRef.current = null;
      bottomBubbleRef.current = null;
    };
  }, [avatarBridge, assistantAudioPlayback]);
}

export type { CompanionAvatarRuntimeBridge };
