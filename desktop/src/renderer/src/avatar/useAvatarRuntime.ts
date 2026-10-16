import { useEffect, useReducer, useRef, useState } from "react";
import { ControlSocket } from "../lib/controlSocket";
import { shouldAcceptActivitySnapshot } from "../lib/activitySnapshotAcceptance";
import { dismissTimedReminders } from "../lib/timedReminderClient";
import { shouldAcceptTimedReminderSnapshot } from "../lib/timedReminderSnapshotAcceptance";
import type { AudioDevicesChangeRequestedEvent, ReminderSettings } from "../types/events";
import type {
  ActivitySnapshot,
  TimedReminderCompact,
  TimedReminderSnapshot
} from "../types/backendEvents";
import {
  initialAvatarBubbleState,
  reduceAvatarBubbleState,
  type AvatarAssistantBubble,
  type BottomAvatarBubble
} from "./avatarBubbleState";
import type { AvatarReflectionStatus } from "./avatarBubbleTypes";
import { useAvatarControlEvents, type CompanionAvatarRuntimeBridge } from "./useAvatarControlEvents";
import { useAvatarLiveKitSession, type CompanionAvatarLiveKitBridge } from "./useAvatarLiveKitSession";
import { AvatarPlayoutDeliveryCoordinator } from "./playoutDeliveryCoordinator";
import { AssistantAudioPlayback } from "../lib/assistantAudioPlayback";
import {
  createInitialCompanionStatusState,
  projectCompanionStatus,
  reduceCompanionStatusState,
  resetCompanionStatusConnection,
  type CompanionStatusPresentation
} from "../companion/companionStatus";

type CompanionAvatarBridge = CompanionAvatarRuntimeBridge & CompanionAvatarLiveKitBridge;

/**
 * 提供虚拟形象正在显示的状态和设置操作。
 *
 * Args:
 *   不适用；这是 useAvatarRuntime 返回的运行时接口。
 * Returns:
 *   当前角色状态、气泡、提醒、设置投影，以及等待服务端确认的操作入口。
 */
export type AvatarRuntime = {
  /** 独立于聊天气泡显示门禁的当前角色状态。 */
  companionStatus: CompanionStatusPresentation;
  bottomBubble: BottomAvatarBubble | null;
  assistantBubble: AvatarAssistantBubble | null;
  reflectionStatus: AvatarReflectionStatus | null;
  settings: ReminderSettings | null;
  settingsHydrated: boolean;
  activitySnapshot: ActivitySnapshot;
  activeActivityCount: number;
  timedReminderSnapshot: TimedReminderSnapshot;
  ringingReminders: TimedReminderCompact[];
  requestingReminderIds: ReadonlySet<string>;
  reminderDismissError: string | null;
  localListeningHintActive: boolean;
  chatMode: "collapsed" | "expanded" | null;
  pendingInteraction: PendingBackgroundAgentInteraction | null;
  /**
   * 保存本次编辑的字段，完整显示状态由服务端提交快照水合。
   * @param settings 本次修改的字段，完整旧快照调用仍然接受。
   * @returns 提交完成；失败时抛错并保留原显示状态。
   */
  updateSettings(settings: Partial<ReminderSettings>): Promise<void>;
  requestDismissReminders(runtimeId: string, reminderIds: readonly string[]): Promise<void>;
};

export type PendingBackgroundAgentInteraction = {
  requestId: string;
  kind: "permission" | "question";
};

type AvatarRuntimeOptions = {
  onAssistantPlaybackElementChanged?(element: HTMLMediaElement | null): void;
  onLocalListeningHintChanged?(active: boolean): void;
};

const ASSISTANT_CAPTION_TICK_MS = 100;
const EMPTY_TIMED_REMINDER_SNAPSHOT: TimedReminderSnapshot = {
  runtime_id: "",
  revision: 0,
  active: [],
  recent: []
};

/**
 * 管理虚拟形象的控制事件、独立角色状态、气泡和权威设置状态。
 *
 * @param avatarBridge preload 提供的控制网关和虚拟形象入口。
 * @param options 音频播放元素和本地聆听提示的变化回调。
 * @returns 不受气泡门禁影响的角色状态、气泡投影及等待 Backend 确认的设置操作。
 */
export function useAvatarRuntime(
  avatarBridge?: CompanionAvatarBridge,
  options: AvatarRuntimeOptions = {}
): AvatarRuntime {
  const [state, dispatch] = useReducer(reduceAvatarBubbleState, initialAvatarBubbleState);
  const [companionStatusState, setCompanionStatusState] = useState(createInitialCompanionStatusState);
  const [settings, setSettings] = useState<ReminderSettings | null>(null);
  const [settingsHydrated, setSettingsHydrated] = useState(false);
  const [localListeningHintActive, setLocalListeningHintActive] = useState(false);
  const [chatMode, setChatMode] = useState<"collapsed" | "expanded" | null>(null);
  const [pendingInteractions, setPendingInteractions] = useState<PendingBackgroundAgentInteraction[]>([]);
  const [activitySnapshot, setActivitySnapshotState] = useState<ActivitySnapshot>({
    revision: 0,
    runtime_id: "",
    active: [],
    recent: []
  });
  const [timedReminderSnapshot, setTimedReminderSnapshotState] =
    useState<TimedReminderSnapshot>(EMPTY_TIMED_REMINDER_SNAPSHOT);
  const [requestingReminderIds, setRequestingReminderIds] = useState<Set<string>>(
    () => new Set()
  );
  const [reminderDismissError, setReminderDismissError] = useState<string | null>(null);
  const controlSocketRef = useRef<ControlSocket | null>(null);
  const playoutDeliveryCoordinatorRef = useRef<AvatarPlayoutDeliveryCoordinator | null>(null);
  const bottomBubbleRef = useRef<BottomAvatarBubble | null>(state.bottomBubble);
  const onAssistantPlaybackElementChangedRef = useRef(options.onAssistantPlaybackElementChanged);
  const onLocalListeningHintChangedRef = useRef(options.onLocalListeningHintChanged);
  const audioDeviceSessionReloadRef = useRef<((payload: AudioDevicesChangeRequestedEvent["payload"]) => void) | null>(null);
  const latestAudioDeviceChangeRequestIdRef = useRef<string | null>(null);
  const timedReminderSnapshotRef = useRef<TimedReminderSnapshot | null>(null);
  const requestingReminderIdsRef = useRef<Set<string>>(new Set());
  const chatModeRef = useRef<"collapsed" | "expanded" | null>(null);
  const assistantAudioPlaybackRef = useRef<AssistantAudioPlayback | null>(null);
  if (assistantAudioPlaybackRef.current === null) {
    assistantAudioPlaybackRef.current = new AssistantAudioPlayback(document, {
      onPlaybackElementChanged: element => onAssistantPlaybackElementChangedRef.current?.(element)
    });
  }

  useEffect(() => {
    bottomBubbleRef.current = state.bottomBubble;
  }, [state.bottomBubble]);

  useEffect(() => {
    onAssistantPlaybackElementChangedRef.current = options.onAssistantPlaybackElementChanged;
  }, [options.onAssistantPlaybackElementChanged]);

  useEffect(() => {
    onLocalListeningHintChangedRef.current = active => {
      setLocalListeningHintActive(active);
      options.onLocalListeningHintChanged?.(active);
    };
  }, [options.onLocalListeningHintChanged]);

  useAvatarControlEvents({
    avatarBridge,
    assistantAudioPlayback: assistantAudioPlaybackRef.current,
    audioDeviceSessionReloadRef,
    bottomBubbleRef,
    controlSocketRef,
    playoutDeliveryCoordinatorRef,
    dispatch,
    setSettings,
    setSettingsHydrated,
    setPendingInteractions,
    // 状态图案始终消费后端事实；Chat 展开或关闭气泡偏好都不能让它失去当前状态。
    onCompanionEvent: event => {
      setCompanionStatusState(current => reduceCompanionStatusState(current, event));
    },
    onCompanionConnectionChange: status => {
      setCompanionStatusState(current => resetCompanionStatusConnection(current, status));
    },
    // runtime 改变无条件替换，同一 runtime 只接受更高 revision；与 Panel 共用同一接受规则。
    setActivitySnapshot: (nextSnapshot) => {
      setActivitySnapshotState(current =>
        shouldAcceptActivitySnapshot(current, nextSnapshot) ? nextSnapshot : current
      );
    },
    setTimedReminderSnapshot: (nextSnapshot) => {
      const currentSnapshot = timedReminderSnapshotRef.current;
      if (!shouldAcceptTimedReminderSnapshot(currentSnapshot, nextSnapshot)) {
        return;
      }

      // Backend 换运行期后，旧气泡上的编号和迟到 HTTP 结果都不再有资格影响新状态。
      if (currentSnapshot !== null && currentSnapshot.runtime_id !== nextSnapshot.runtime_id) {
        requestingReminderIdsRef.current = new Set();
        setRequestingReminderIds(new Set());
        setReminderDismissError(null);
      }

      timedReminderSnapshotRef.current = nextSnapshot;
      setTimedReminderSnapshotState(nextSnapshot);
      setReminderDismissError(null);
      setRequestingReminderIds(current => {
        const activeIds = new Set(nextSnapshot.active.map(reminder => reminder.reminder_id));
        const next = new Set([...current].filter(reminderId => activeIds.has(reminderId)));
        requestingReminderIdsRef.current = next;
        return next;
      });
    }
  });

  useEffect(() => {
    let cancelled = false;
    let receivedModeChange = false;

    // 先把模式送进气泡 reducer，再更新 React 展示状态。主进程事件和语音事件
    // 共用不同的异步通道；如果只 setState，wake.detected 可能在下一次 effect 前
    // 抵达，被旧的 expanded 门禁误丢掉。
    const applyChatMode = (mode: "collapsed" | "expanded"): void => {
      if (chatModeRef.current === mode) {
        return;
      }
      chatModeRef.current = mode;
      dispatch({ type: "avatar.chat_mode_changed", mode });
      setChatMode(mode);
      if (mode === "expanded") {
        // 待答请求也属于 Chat 的主场；关闭后只等待后端新的请求事件。
        setPendingInteractions([]);
        bottomBubbleRef.current = null;
      }
    };

    const dispose = avatarBridge?.onChatModeChanged?.(mode => {
      receivedModeChange = true;
      applyChatMode(mode);
    });

    const getChatMode = avatarBridge?.getChatMode;
    if (getChatMode) {
      void getChatMode().then(mode => {
        if (!cancelled && !receivedModeChange && mode !== null) {
          applyChatMode(mode);
        }
      });
    }

    return () => {
      cancelled = true;
      dispose?.();
    };
  }, [avatarBridge]);

  useAvatarLiveKitSession({
    audioDeviceSessionReloadRef,
    avatarBridge,
    assistantAudioPlayback: assistantAudioPlaybackRef.current,
    dispatch,
    latestAudioDeviceChangeRequestIdRef,
    onAssistantPlaybackElementChangedRef,
    onLocalListeningHintChangedRef,
    playoutDeliveryCoordinatorRef
  });

  useEffect(() => {
    if (
      !state.bottomBubble ||
      state.bottomBubble.kind === "smart_turn_waiting"
    ) {
      return;
    }

    const timeoutId = window.setTimeout(() => {
      dispatch({ type: "avatar.bubble_timeout" });
    }, state.bottomBubble.ttlMs);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [state.bottomBubble]);

  useEffect(() => {
    if (!state.assistantBubble?.ttlMs) {
      return;
    }

    const timeoutId = window.setTimeout(() => {
      dispatch({ type: "avatar.assistant_bubble_timeout" });
    }, state.assistantBubble.ttlMs);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [state.assistantBubble]);

  const activeAssistantCaptionGenerationId =
    state.assistantBubble !== null && state.assistantBubble.ttlMs === null
      ? state.assistantBubble.generationId ?? "__assistant_caption_without_generation__"
      : null;

  useEffect(() => {
    if (activeAssistantCaptionGenerationId === null) {
      return;
    }

    let lastTickAt = Date.now();
    const intervalId = window.setInterval(() => {
      const now = Date.now();
      const deltaMs = now - lastTickAt;
      lastTickAt = now;
      dispatch({
        type: "avatar.assistant_caption_tick",
        payload: {
          delta_ms: deltaMs
        }
      });
    }, ASSISTANT_CAPTION_TICK_MS);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [activeAssistantCaptionGenerationId]);

  const thinkingBubbleActive = state.reflectionStatus?.mode === "thinking";
  useEffect(() => {
    if (!thinkingBubbleActive) {
      return;
    }

    let lastTickAt = Date.now();
    const intervalId = window.setInterval(() => {
      const now = Date.now();
      const deltaMs = now - lastTickAt;
      lastTickAt = now;
      dispatch({
        type: "avatar.self_reflection_thinking_tick",
        payload: { delta_ms: deltaMs }
      });
    }, 50);

    return () => window.clearInterval(intervalId);
  }, [thinkingBubbleActive]);

  return {
    companionStatus: projectCompanionStatus(companionStatusState, activitySnapshot.active),
    bottomBubble: state.bottomBubble,
    assistantBubble: state.assistantBubble,
    reflectionStatus: state.reflectionStatus ?? null,
    settings,
    settingsHydrated,
    activitySnapshot,
    activeActivityCount: activitySnapshot.active.length,
    timedReminderSnapshot,
    ringingReminders: timedReminderSnapshot.active.filter(
      reminder => reminder.state === "ringing"
    ),
    requestingReminderIds,
    reminderDismissError,
    localListeningHintActive,
    chatMode,
    pendingInteraction: pendingInteractions[0] ?? null,
    async requestDismissReminders(runtimeId, reminderIds) {
      const normalizedRuntimeId = runtimeId.trim();
      const normalizedReminderIds = reminderIds.map(reminderId => reminderId.trim());
      const currentSnapshot = timedReminderSnapshotRef.current;
      const currentRingingIds = new Set(
        currentSnapshot?.active
          .filter(reminder => reminder.state === "ringing")
          .map(reminder => reminder.reminder_id) ?? []
      );
      if (
        !avatarBridge?.runtime.controlGateway
        || currentSnapshot?.runtime_id !== normalizedRuntimeId
        || normalizedReminderIds.length === 0
        || normalizedReminderIds.length > 5
        || normalizedReminderIds.some(reminderId => !currentRingingIds.has(reminderId))
        || new Set(normalizedReminderIds).size !== normalizedReminderIds.length
        || normalizedReminderIds.some(reminderId => requestingReminderIdsRef.current.has(reminderId))
      ) {
        return;
      }

      setReminderDismissError(null);
      const requesting = new Set(requestingReminderIdsRef.current);
      normalizedReminderIds.forEach(reminderId => requesting.add(reminderId));
      requestingReminderIdsRef.current = requesting;
      setRequestingReminderIds(requesting);
      try {
        await dismissTimedReminders(
          avatarBridge.runtime.controlGateway.httpBaseUrl,
          normalizedRuntimeId,
          normalizedReminderIds
        );
      } catch (error) {
        const latestSnapshot = timedReminderSnapshotRef.current;
        if (latestSnapshot?.runtime_id !== normalizedRuntimeId) {
          return;
        }

        const next = new Set(requestingReminderIdsRef.current);
        normalizedReminderIds.forEach(reminderId => next.delete(reminderId));
        requestingReminderIdsRef.current = next;
        setRequestingReminderIds(next);
        const stillRinging = latestSnapshot.active.some(
          reminder => reminder.state === "ringing"
            && normalizedReminderIds.includes(reminder.reminder_id)
        );
        if (stillRinging) {
          setReminderDismissError(
            error instanceof Error ? error.message : "关闭提醒失败，请重试。"
          );
        }
      }
    },
    async updateSettings(nextSettings) {
      const controlSocket = controlSocketRef.current;
      if (!controlSocket) throw new Error("人物设置连接尚未建立，请稍后重试。");
      // HTTP 提交后的权威快照走既有 settings.changed 处理，避免把旧快捷键等字段写回本地。
      await controlSocket.updateSettings(nextSettings);
    }
  };
}
