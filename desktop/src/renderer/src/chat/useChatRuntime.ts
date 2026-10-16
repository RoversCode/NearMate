import { startTransition, useEffect, useReducer, useRef, useState } from "react";
import sharedDefaultSettings from "../../../../../shared/default-settings.json";
import {
  readMaterialContextSnapshot,
  reduceMaterialContextSnapshot
} from "../../../shared/materialContext";
import {
  readTurnSubmissionCommandResponse
} from "../../../shared/turnSubmissions";
import { ControlSocket } from "../lib/controlSocket";
import { fetchCurrentMaterialContext } from "../lib/materialsClient";
import type { MaterialContextSnapshot } from "../materials/materialTypes";
import type { BackendEvent, ReminderSettings } from "../types/events";
import type { ActivitySnapshot, TimedReminderSnapshot } from "../types/backendEvents";
import { cancelActivity } from "../lib/activityClient";
import {
  cancelTimedReminder,
  dismissTimedReminders,
  updateTimedReminder,
  type TimedReminderUpdate
} from "../lib/timedReminderClient";
import {
  createInitialChatState,
  reduceChatState,
  type ChatState
} from "./chatReducer";
import { readChatMaterialFeedback, type ChatMaterialFeedback } from "./chatMaterialFeedback";
import {
  submitBackgroundAgentPermissionDecision,
  type BackgroundAgentInteractionAction
} from "./backgroundAgentInteractionClient";
import {
  EMPTY_ACTIVITY_SNAPSHOT,
  EMPTY_REMINDER_SNAPSHOT,
  canAcceptWorkspaceSnapshot,
  projectEnabledSkills,
  retainActiveRequestIds,
  type ChatEnabledSkill
} from "./chatWorkspaceState";
import { fetchActiveCharacterName } from "./characterName";
import {
  fetchLlmAccountStatus,
  type LlmAccountStatusSnapshot
} from "./llmAccountStatus";
import {
  clearChatVoiceDisplay,
  createInitialChatVoiceProjection,
  reduceChatVoiceProjection,
  type ChatVoiceProjection
} from "./chatVoiceState";
import type { ChatVoiceState } from "./chatVoiceState";
import type { ConsoleTarget } from "../../../shared/shellNavigation";
import {
  createInitialCompanionStatusState,
  projectCompanionStatus,
  projectLegacyChatVoiceState,
  reduceCompanionStatusState,
  resetCompanionStatusConnection,
  type CompanionStatusPresentation
} from "../companion/companionStatus";

export type { ChatVoiceState } from "./chatVoiceState";

type CompanionChatBridge = {
  reportDiagnostic(level: "info" | "warn" | "error", message: string, details?: unknown): void;
  setChatMode?(mode: "collapsed" | "expanded"): void;
  getChatMode?(): Promise<"collapsed" | "expanded" | null>;
  onChatModeRequested?(listener: (mode: "collapsed" | "expanded") => void): () => void;
  onFocusBackgroundAgentInteraction?(listener: (requestId: string) => void): () => void;
  startChatDrag?(point: { screenX: number; screenY: number }): void;
  moveChatDrag?(point: { screenX: number; screenY: number }): void;
  endChatDrag?(): void;
  startChatResize?(input: { edge: "n" | "e" | "s" | "w" | "ne" | "nw" | "se" | "sw"; screenX: number; screenY: number }): void;
  moveChatResize?(point: { screenX: number; screenY: number }): void;
  endChatResize?(): void;
  submitTurn?(input: { submissionId: string; messageText: string }): Promise<unknown>;
  editTurnSubmission?(input: { submissionId: string; messageText: string }): Promise<unknown>;
  deleteTurnSubmission?(submissionId: string): Promise<unknown>;
  resumeTurnSubmission?(submissionId: string): Promise<unknown>;
  retryTurnSubmission?(submissionId: string): Promise<unknown>;
  prepareEnvironmentSnapshot?(): Promise<unknown>;
  openExternalUrl?(url: string): Promise<void>;
  addStagedMaterials?(items: unknown[]): Promise<unknown>;
  removeStagedMaterial?(materialId: string): Promise<unknown>;
  forgetRecentMaterial?(path: string): Promise<unknown>;
  confirmMaterialRemoved?(materialId: string, displayName: string): Promise<unknown>;
  onMaterialDeliveryFeedback?(listener: (payload: unknown) => void): () => void;
  captureClipboardImageMaterial?(): Promise<unknown>;
  createComposerTextMaterial?(text: string): Promise<unknown>;
  getPathForFile?(file: File): string;
  getMaterialThumbnail?(path: string): Promise<string | null>;
  showItemInFolder?(path: string): Promise<void>;
  openConsole?(target: ConsoleTarget): void;
  runtime: {
    controlGateway: {
      websocketUrl: string;
      httpBaseUrl: string;
    };
  };
};

declare global {
  interface Window {
    companionChat?: CompanionChatBridge;
  }
}

const fallbackSettings: ReminderSettings = {
  show_speech_bubbles: sharedDefaultSettings.show_speech_bubbles,
  followup_toggle_shortcut_enabled: sharedDefaultSettings.followup_toggle_shortcut_enabled,
  followup_toggle_shortcut: sharedDefaultSettings.followup_toggle_shortcut,
  avatar_visibility_shortcut_enabled: sharedDefaultSettings.avatar_visibility_shortcut_enabled,
  avatar_visibility_shortcut: sharedDefaultSettings.avatar_visibility_shortcut,
  selection_capture_shortcut_enabled: sharedDefaultSettings.selection_capture_shortcut_enabled,
  selection_capture_shortcut: sharedDefaultSettings.selection_capture_shortcut,
  selected_live2d_model_id: sharedDefaultSettings.selected_live2d_model_id
};

/**
 * 提供聊天、材料与任务操作，并把当前角色状态交给各处展示。
 *
 * @property companionStatus 标题、状态球与虚拟形象共用的状态展示数据。
 * @property voiceState 保留给既有调用方的语音生命周期投影。
 */
export type ChatRuntime = {
  state: ChatState;
  textSubmitError: string | null;
  materialContext: MaterialContextSnapshot | null;
  materialMutationError: string | null;
  submitText(messageText: string): Promise<void>;
  editTurnSubmission(submissionId: string, messageText: string): Promise<void>;
  deleteTurnSubmission(submissionId: string): Promise<void>;
  resumeTurnSubmission(submissionId: string): Promise<void>;
  retryTurnSubmission(submissionId: string): Promise<void>;
  addStagedMaterials(items: unknown[]): Promise<StagedMaterialMutationResult>;
  removeMaterialItem(materialId: string): Promise<void>;
  refreshMaterialContext(): Promise<void>;
  forgetRecentMaterial(path: string): Promise<void>;
  submitWorkInteractionAction(action: BackgroundAgentInteractionAction): Promise<void>;
  voiceState: ChatVoiceState;
  companionStatus: CompanionStatusPresentation;
  liveTranscript: string;
  workspace: {
    activitySnapshot: ActivitySnapshot;
    reminderSnapshot: TimedReminderSnapshot;
    requestingActivityIds: ReadonlySet<string>;
    requestingReminderIds: ReadonlySet<string>;
    errors: { activity: string | null; reminder: string | null };
    runtimeRestartMessage: string | null;
    enabledSkills: readonly ChatEnabledSkill[];
    llmAccountStatus?: LlmAccountStatusSnapshot;
    llmAccountStatusError?: string | null;
    refreshLlmAccountStatus?(): Promise<void>;
    requestCancelActivity(id: string): Promise<void>;
    requestUpdateReminder(id: string, update: TimedReminderUpdate): Promise<void>;
    requestCancelReminder(id: string): Promise<void>;
    requestDismissReminders(runtimeId: string, ids: readonly string[]): Promise<void>;
    openActivityDetail(id: string): void;
  };
};

export type StagedMaterialMutationResult = {
  feedback: ChatMaterialFeedback | null;
  materialContext: MaterialContextSnapshot;
};

/**
 * useChatRuntime 负责聊天窗口的控制链路订阅和文本提交。
 * 它只维护本次运行期间的内存消息，不读取控制台历史或数据库。
 *
 * @returns 聊天内容、统一角色状态、材料与任务操作。
 */
export function useChatRuntime(): ChatRuntime {
  const chatBridge = window.companionChat;
  const [state, dispatch] = useReducer(reduceChatState, fallbackSettings, createInitialChatState);
  const [textSubmitError, setTextSubmitError] = useState<string | null>(null);
  const [materialContext, setMaterialContext] = useState<MaterialContextSnapshot | null>(null);
  const [materialMutationError, setMaterialMutationError] = useState<string | null>(null);
  const [activitySnapshot, setActivitySnapshot] = useState<ActivitySnapshot>(EMPTY_ACTIVITY_SNAPSHOT);
  const [reminderSnapshot, setReminderSnapshot] = useState<TimedReminderSnapshot>(EMPTY_REMINDER_SNAPSHOT);
  const [requestingActivityIds, setRequestingActivityIds] = useState<Set<string>>(new Set());
  const [requestingReminderIds, setRequestingReminderIds] = useState<Set<string>>(new Set());
  const [workspaceErrors, setWorkspaceErrors] = useState<{ activity: string | null; reminder: string | null }>({ activity: null, reminder: null });
  const [runtimeRestartMessage, setRuntimeRestartMessage] = useState<string | null>(null);
  const [enabledSkills, setEnabledSkills] = useState<ChatEnabledSkill[]>([]);
  const [llmAccountStatus, setLlmAccountStatus] = useState<LlmAccountStatusSnapshot>();
  const [llmAccountStatusError, setLlmAccountStatusError] = useState<string | null>(null);
  const [companionStatusState, setCompanionStatusState] = useState(createInitialCompanionStatusState);
  const [liveTranscript, setLiveTranscript] = useState("");
  const activityRef = useRef<ActivitySnapshot | null>(null);
  const reminderRef = useRef<TimedReminderSnapshot | null>(null);
  const activityRequests = useRef(new Set<string>());
  const reminderRequests = useRef(new Set<string>());
  const chatVoiceProjectionRef = useRef<ChatVoiceProjection>(createInitialChatVoiceProjection());
  const disposedRef = useRef(false);
  const materialContextGenerationRef = useRef(0);
  const skillFetchGenerationRef = useRef(0);
  const characterNameFetchGenerationRef = useRef(0);
  const llmAccountFetchGenerationRef = useRef(0);
  const httpBaseUrl = chatBridge?.runtime.controlGateway.httpBaseUrl ?? "http://127.0.0.1:8765";

  /**
   * 重新拉取 Backend 权威材料快照，并只接纳更高 revision。
   *
   * @returns 快照刷新完成后结束；失败时抛出原始错误供调用方决定是否静默。
   */
  async function refreshMaterialContext(): Promise<void> {
    const requestGeneration = materialContextGenerationRef.current;
    setMaterialMutationError(null);
    try {
      const nextContext = await fetchCurrentMaterialContext(httpBaseUrl);
      if (
        !disposedRef.current
        && requestGeneration === materialContextGenerationRef.current
      ) {
        setMaterialContext(current => reduceMaterialContextSnapshot(current, nextContext));
      }
    } catch (error) {
      if (
        !disposedRef.current
        && requestGeneration === materialContextGenerationRef.current
      ) {
        setMaterialMutationError("材料状态刷新失败，请重试");
      }
      chatBridge?.reportDiagnostic("warn", "materials.context_fetch_failed", error);
      throw error;
    }
  }

  /**
   * 读取受管 Skill 的安全元数据，并与 Settings 快照投影成右栏只读列表。
   * 竞态请求只允许最后一次结果生效，避免旧设置回包覆盖新设置。
   */
  async function refreshEnabledSkills(preferences: Readonly<Record<string, boolean>>): Promise<void> {
    const requestGeneration = ++skillFetchGenerationRef.current;
    try {
      const response = await fetch(`${httpBaseUrl.replace(/\/$/, "")}/api/console/skills`);
      if (!response.ok) {
        throw new Error(`skills fetch request failed: ${response.status}`);
      }
      const payload: unknown = await response.json();
      if (
        disposedRef.current
        || requestGeneration !== skillFetchGenerationRef.current
      ) {
        return;
      }
      setEnabledSkills(projectEnabledSkills(payload, preferences));
    } catch (error) {
      if (
        !disposedRef.current
        && requestGeneration === skillFetchGenerationRef.current
      ) {
        setEnabledSkills([]);
        chatBridge?.reportDiagnostic("warn", "skills.metadata_fetch_failed", error);
      }
    }
  }

  /**
   * 读取当前激活角色卡的展示名称，并把结果投影到聊天状态。
   *
   * 角色卡请求只是展示数据，失败时保留已有名称；请求代际门禁保证重连或卸载后的迟到响应不会污染新运行期。
   */
  async function refreshActiveCharacterName(): Promise<void> {
    const requestGeneration = ++characterNameFetchGenerationRef.current;
    const characterName = await fetchActiveCharacterName(httpBaseUrl);
    if (
      disposedRef.current
      || requestGeneration !== characterNameFetchGenerationRef.current
      || !characterName
    ) {
      return;
    }
    dispatch({
      type: "active_character_name_changed",
      characterName
    });
  }

  /**
   * 读取当前已配置厂商的余额安全快照；请求代际变化时丢弃迟到响应。
   *
   * @returns 查询完成后结束；失败由工作区错误提示承接，允许用户重试。
   */
  async function refreshLlmAccountStatus(): Promise<void> {
    const requestGeneration = ++llmAccountFetchGenerationRef.current;
    // 配置事件可能来自换 Key，查询完成前不能继续把旧账户余额当作当前余额。
    setLlmAccountStatus(undefined);
    setLlmAccountStatusError(null);
    try {
      const snapshot = await fetchLlmAccountStatus(httpBaseUrl);
      if (
        disposedRef.current
        || requestGeneration !== llmAccountFetchGenerationRef.current
      ) {
        return;
      }
      setLlmAccountStatus(snapshot);
    } catch (error) {
      if (
        !disposedRef.current
        && requestGeneration === llmAccountFetchGenerationRef.current
      ) {
        setLlmAccountStatusError("余额读取失败，请重试");
        chatBridge?.reportDiagnostic("warn", "llm.account_status_fetch_failed", error);
      }
    }
  }

  /**
   * 把输入侧 Backend 事件交给纯投影模块，只同步当前轮次的临时转写。
   *
   * @param event 已通过控制 WebSocket 协议解析的 Backend 事件。
   * @returns 无返回值；有变化时刷新实时转写。
   */
  function applyChatVoiceEvent(event: BackendEvent): void {
    const previousProjection = chatVoiceProjectionRef.current;
    const nextProjection = reduceChatVoiceProjection(previousProjection, event);
    if (nextProjection === previousProjection) {
      return;
    }
    chatVoiceProjectionRef.current = nextProjection;
    setLiveTranscript(nextProjection.liveTranscript);
  }

  useEffect(() => {
    disposedRef.current = false;
    // 每次重新装配 Chat 都开启新的 Skill 请求世代，旧请求即使随后完成也不能回填新运行期。
    skillFetchGenerationRef.current += 1;
    llmAccountFetchGenerationRef.current += 1;
    setEnabledSkills([]);
    setLlmAccountStatus(undefined);
    setLlmAccountStatusError(null);
    const controlSocket = new ControlSocket({
      websocketUrl: chatBridge?.runtime.controlGateway.websocketUrl,
      httpBaseUrl,
      diagnosticsBridge: chatBridge
    });
    const disposeSocket = controlSocket.connect({
      onEvent(event) {
        setCompanionStatusState(current => reduceCompanionStatusState(current, event));
        if (event.type === "materials.contexts_changed") {
          try {
            const nextContext = readMaterialContextSnapshot(event.payload);
            setMaterialContext(current => reduceMaterialContextSnapshot(current, nextContext));
          } catch (error) {
            chatBridge?.reportDiagnostic("warn", "materials.context_event_invalid", error);
          }
          return;
        }
        if (event.type === "activities.snapshot_changed") {
          const current = activityRef.current;
          if (!canAcceptWorkspaceSnapshot({
            currentActivity: current,
            nextActivity: event.payload,
            currentReminder: reminderRef.current
          })) {
            return;
          }
          if (current && current.runtime_id !== event.payload.runtime_id) {
            // 新运行期的 ID 可能与旧进程重复；旧请求不能继续锁住新任务卡片。
            activityRequests.current.clear();
            setRequestingActivityIds(new Set());
            setRuntimeRestartMessage("Backend 已重启，上一运行期的任务已清空。");
          }
          activityRef.current = event.payload;
          setActivitySnapshot(event.payload);
          activityRequests.current = retainActiveRequestIds(
            activityRequests.current,
            new Set(event.payload.active.map(activity => activity.activity_id))
          );
          setRequestingActivityIds(new Set(activityRequests.current));
          setWorkspaceErrors(currentErrors => ({ ...currentErrors, activity: null }));
          return;
        }
        if (event.type === "reminders.snapshot_changed") {
          const current = reminderRef.current;
          if (!canAcceptWorkspaceSnapshot({
            currentActivity: activityRef.current,
            currentReminder: current,
            nextReminder: event.payload
          })) {
            return;
          }
          if (current && current.runtime_id !== event.payload.runtime_id) {
            setRuntimeRestartMessage("Backend 已重启，上一运行期的提醒已清空。");
            reminderRequests.current.clear();
          }
          reminderRef.current = event.payload;
          setReminderSnapshot(event.payload);
          reminderRequests.current = retainActiveRequestIds(
            reminderRequests.current,
            new Set(event.payload.active.map(reminder => reminder.reminder_id))
          );
          setRequestingReminderIds(new Set(reminderRequests.current));
          setWorkspaceErrors(currentErrors => ({ ...currentErrors, reminder: null }));
          return;
        }
        if (event.type === "settings.changed") {
          void refreshEnabledSkills(event.payload.background_agent.skills);
          // 角色卡激活会广播 Settings 快照；此时重新读取 meta.name，确保标题和消息署名同步。
          void refreshActiveCharacterName();
        }
        if (event.type === "bootstrap.status_changed") {
          // 保存 LLM 配置会广播 Bootstrap 状态；余额查询跟随该事实刷新。
          void refreshLlmAccountStatus();
        }

        applyChatVoiceEvent(event);

        startTransition(() => {
          dispatch({
            type: "backend_event_received",
            event
          });
        });
      },
      onStatusChange(status, _reason, source) {
        if (source !== "settings") setCompanionStatusState(current => resetCompanionStatusConnection(current, status));
        if (status === "connecting") {
          // Backend 重启后 revision 会从零开始。这里先进入新世代并清空旧门禁，
          // 同时让上一世代仍在途的 HTTP 响应无法回填新进程状态。
          materialContextGenerationRef.current += 1;
          activityRef.current = null;
          reminderRef.current = null;
          activityRequests.current.clear();
          reminderRequests.current.clear();
          setActivitySnapshot(EMPTY_ACTIVITY_SNAPSHOT);
          setReminderSnapshot(EMPTY_REMINDER_SNAPSHOT);
          setRequestingActivityIds(new Set());
          setRequestingReminderIds(new Set());
          setRuntimeRestartMessage(null);
          // Settings 事件会重新触发安全 Skill 投影；在此之前不保留上一运行期的名称。
          skillFetchGenerationRef.current += 1;
          characterNameFetchGenerationRef.current += 1;
          llmAccountFetchGenerationRef.current += 1;
          setEnabledSkills([]);
          setLlmAccountStatus(undefined);
          setLlmAccountStatusError(null);
          chatVoiceProjectionRef.current = createInitialChatVoiceProjection();
          setLiveTranscript("");
          setMaterialContext(null);
          dispatch({ type: "turn_submissions_connection_reset" });
        }
        if (status === "disconnected") {
          const nextProjection = clearChatVoiceDisplay(chatVoiceProjectionRef.current);
          chatVoiceProjectionRef.current = nextProjection;
          setLiveTranscript(nextProjection.liveTranscript);
        }
        if (status === "connected") {
          void refreshMaterialContext().catch(() => {
            // 失败已经进入统一材料错误区；连接状态回调不再制造未处理的 Promise rejection。
          });
          void refreshActiveCharacterName();
        }
        // 状态区只显示“同步中/暂不可用”等用户文案，连接细节仍留在诊断中。
      }
    });
    // 控制连接首帧会带 bootstrap.status_changed；余额随其它设备消费时再低频刷新。
    const accountStatusTimer = window.setInterval(() => {
      void refreshLlmAccountStatus();
    }, 5 * 60 * 1000);

    return () => {
      disposedRef.current = true;
      skillFetchGenerationRef.current += 1;
      characterNameFetchGenerationRef.current += 1;
      llmAccountFetchGenerationRef.current += 1;
      window.clearInterval(accountStatusTimer);
      disposeSocket();
    };
  }, [chatBridge, httpBaseUrl]);

  async function requestCancelActivity(activityId: string): Promise<void> {
    const current = activityRef.current;
    const activity = current?.active.find(item => item.activity_id === activityId);
    if (!current || !activity?.can_cancel || activityRequests.current.has(activityId)) {
      return;
    }
    const requestRuntimeId = current.runtime_id;
    activityRequests.current.add(activityId);
    setRequestingActivityIds(new Set(activityRequests.current));
    try {
      await cancelActivity(httpBaseUrl, activityId);
    } catch (error) {
      if (activityRef.current?.runtime_id === requestRuntimeId) {
        activityRequests.current.delete(activityId);
        setRequestingActivityIds(new Set(activityRequests.current));
        setWorkspaceErrors(currentErrors => ({
          ...currentErrors,
          activity: error instanceof Error ? error.message : "取消任务失败。"
        }));
      }
    }
  }

  async function requestUpdateReminder(
    reminderId: string,
    update: TimedReminderUpdate
  ): Promise<void> {
    const current = reminderRef.current;
    const reminder = current?.active.find(item => item.reminder_id === reminderId);
    if (
      !current
      || !reminder
      || reminder.state !== "scheduled"
      || !reminder.can_modify
      || reminderRequests.current.has(reminderId)
    ) {
      return;
    }
    const requestRuntimeId = current.runtime_id;
    reminderRequests.current.add(reminderId);
    setRequestingReminderIds(new Set(reminderRequests.current));
    try {
      await updateTimedReminder(httpBaseUrl, reminderId, update);
    } catch (error) {
      if (reminderRef.current?.runtime_id === requestRuntimeId) {
        reminderRequests.current.delete(reminderId);
        setRequestingReminderIds(new Set(reminderRequests.current));
        setWorkspaceErrors(current => ({
          ...current,
          reminder: error instanceof Error ? error.message : "修改提醒失败。"
        }));
      }
      throw error;
    }
  }

  async function requestCancelReminder(reminderId: string): Promise<void> {
    const current = reminderRef.current;
    const reminder = current?.active.find(item => item.reminder_id === reminderId);
    if (
      !current
      || !reminder
      || reminder.state !== "scheduled"
      || !reminder.can_cancel
      || reminderRequests.current.has(reminderId)
    ) {
      return;
    }
    const requestRuntimeId = current.runtime_id;
    reminderRequests.current.add(reminderId);
    setRequestingReminderIds(new Set(reminderRequests.current));
    try {
      await cancelTimedReminder(httpBaseUrl, reminderId);
    } catch (error) {
      if (reminderRef.current?.runtime_id === requestRuntimeId) {
        reminderRequests.current.delete(reminderId);
        setRequestingReminderIds(new Set(reminderRequests.current));
        setWorkspaceErrors(current => ({
          ...current,
          reminder: error instanceof Error ? error.message : "取消提醒失败。"
        }));
      }
    }
  }

  async function requestDismissReminders(
    runtimeId: string,
    reminderIds: readonly string[]
  ): Promise<void> {
    const current = reminderRef.current;
    if (
      !current
      || current.runtime_id !== runtimeId
      || reminderIds.length === 0
      || reminderIds.length > 5
      || new Set(reminderIds).size !== reminderIds.length
      || reminderIds.some(id => reminderRequests.current.has(id))
      || reminderIds.some(id => {
        const reminder = current.active.find(item => item.reminder_id === id);
        return !reminder || reminder.state !== "ringing" || !reminder.can_dismiss;
      })
    ) {
      return;
    }
    reminderIds.forEach(id => reminderRequests.current.add(id));
    setRequestingReminderIds(new Set(reminderRequests.current));
    try {
      await dismissTimedReminders(httpBaseUrl, runtimeId, reminderIds);
    } catch (error) {
      if (reminderRef.current?.runtime_id === runtimeId) {
        reminderIds.forEach(id => reminderRequests.current.delete(id));
        setRequestingReminderIds(new Set(reminderRequests.current));
        setWorkspaceErrors(currentErrors => ({
          ...currentErrors,
          reminder: error instanceof Error ? error.message : "关闭提醒失败。"
        }));
      }
    }
  }

  /**
   * 把 IPC 命令返回的完整快照送入同一个 revision 对账入口。
   *
   * Args:
   *   payload: Main 进程返回的未知 IPC 载荷。
   *
   * Returns:
   *   无；合法快照会同步投递给聊天 reducer。
   */
  function applyTurnSubmissionCommandResponse(payload: unknown): void {
    const response = readTurnSubmissionCommandResponse(payload);
    dispatch({
      type: "turn_submissions_snapshot_received",
      snapshot: response.turnSubmissions
    });
  }

  /**
   * 调用一条只携带 submission id 的队列命令。
   *
   * Args:
   *   operation: preload 暴露的受控队列操作。
   *   submissionId: 目标待发文本的稳定编号。
   *
   * Returns:
   *   Backend 快照完成对账后结束的 Promise。
   */
  async function runSubmissionIdCommand(
    operation: ((submissionId: string) => Promise<unknown>) | undefined,
    submissionId: string
  ): Promise<void> {
    if (!operation) {
      throw new Error("Turn 队列操作桥接不可用");
    }
    applyTurnSubmissionCommandResponse(await operation(submissionId));
  }

  async function submitText(messageText: string): Promise<void> {
    if (state.assistantOutputError != null) {
      throw new Error("助手输出当前不可用");
    }
    const trimmedText = messageText.trim();

    if (!trimmedText) {
      return;
    }

    const submissionId = crypto.randomUUID();
    setTextSubmitError(null);
    setMaterialMutationError(null);
    dispatch({
      type: "turn_submission_optimistically_added",
      submissionId,
      messageText: trimmedText
    });

    try {
      if (!chatBridge?.submitTurn) {
        throw new Error("Turn reservation 桥接不可用");
      }
      applyTurnSubmissionCommandResponse(await chatBridge.submitTurn({
        submissionId,
        messageText: trimmedText
      }));
    } catch (error) {
      dispatch({
        type: "turn_submission_reservation_rejected",
        submissionId
      });
      if (!disposedRef.current) {
        setTextSubmitError("文本发送失败，请稍后重试。");
      }
      throw error;
    }
  }

  return {
    state,
    textSubmitError,
    materialContext,
    materialMutationError,
    voiceState: projectLegacyChatVoiceState(companionStatusState),
    companionStatus: projectCompanionStatus(companionStatusState, activitySnapshot.active),
    liveTranscript,
    workspace: {
      activitySnapshot,
      reminderSnapshot,
      requestingActivityIds,
      requestingReminderIds,
      errors: workspaceErrors,
      runtimeRestartMessage,
      enabledSkills,
      llmAccountStatus,
      llmAccountStatusError,
      refreshLlmAccountStatus,
      requestCancelActivity,
      requestUpdateReminder,
      requestCancelReminder,
      requestDismissReminders,
      openActivityDetail(activityId) {
        chatBridge?.openConsole?.({ section: "tasks", activityId });
      }
    },
    submitText,
    async editTurnSubmission(submissionId, messageText) {
      const trimmedText = messageText.trim();
      if (!trimmedText) {
        throw new Error("待发文本正文不能为空");
      }
      if (!chatBridge?.editTurnSubmission) {
        throw new Error("Turn 编辑桥接不可用");
      }
      applyTurnSubmissionCommandResponse(await chatBridge.editTurnSubmission({
        submissionId,
        messageText: trimmedText
      }));
    },
    async deleteTurnSubmission(submissionId) {
      await runSubmissionIdCommand(chatBridge?.deleteTurnSubmission, submissionId);
      dispatch({
        type: "turn_submission_explicitly_deleted",
        submissionId
      });
    },
    resumeTurnSubmission(submissionId) {
      return runSubmissionIdCommand(chatBridge?.resumeTurnSubmission, submissionId);
    },
    retryTurnSubmission(submissionId) {
      return runSubmissionIdCommand(chatBridge?.retryTurnSubmission, submissionId);
    },
    async addStagedMaterials(items) {
      if (items.length === 0) {
        throw new Error("追加 staged 材料至少需要一项输入");
      }

      setMaterialMutationError(null);
      try {
        if (!chatBridge?.addStagedMaterials) {
          throw new Error("材料接纳桥接不可用");
        }
        const response = await chatBridge.addStagedMaterials(items);
        const feedback = readChatMaterialFeedback(response);
        const nextContext = readCommandMaterialContext(response);
        if (!disposedRef.current) {
          setMaterialContext(current => reduceMaterialContextSnapshot(current, nextContext));
        }
        return {
          feedback,
          materialContext: nextContext
        };
      } catch (error) {
        chatBridge?.reportDiagnostic("warn", "materials.staged_append_failed", error);
        throw error;
      }
    },
    async removeMaterialItem(materialId) {
      setMaterialMutationError(null);
      try {
        if (!chatBridge?.removeStagedMaterial) {
          throw new Error("材料删除桥接不可用");
        }
        const response = await chatBridge.removeStagedMaterial(materialId);
        const nextContext = readCommandMaterialContext(response);
        const removedDisplayName = readRemovedDisplayName(response);
        const stillExists = nextContext.turn_material_context.staged_materials.some(
          item => item.material_id === materialId
        );
        if (stillExists) {
          throw new Error("Backend 删除响应仍包含目标材料");
        }
        if (!disposedRef.current) {
          setMaterialContext(current => reduceMaterialContextSnapshot(current, nextContext));
        }
        try {
          await chatBridge.confirmMaterialRemoved?.(materialId, removedDisplayName);
        } catch (error) {
          chatBridge.reportDiagnostic("warn", "materials.remove_feedback_failed", error);
        }
      } catch (error) {
        if (!disposedRef.current) {
          setMaterialMutationError("材料删除失败，请重试");
        }
        chatBridge?.reportDiagnostic("warn", "materials.staged_delete_failed", error);
        throw error;
      }
    },
    refreshMaterialContext,
    async forgetRecentMaterial(path) {
      setMaterialMutationError(null);
      try {
        if (!chatBridge?.forgetRecentMaterial) {
          throw new Error("近期材料忘记桥接不可用");
        }
        const response = await chatBridge.forgetRecentMaterial(path);
        const nextContext = readCommandMaterialContext(response);
        if (nextContext.recent_materials.some(item => item.path === path)) {
          throw new Error("Backend 忘记响应仍包含目标近期材料");
        }
        if (!disposedRef.current) {
          setMaterialContext(current => reduceMaterialContextSnapshot(current, nextContext));
        }
      } catch (error) {
        if (!disposedRef.current) {
          setMaterialMutationError("忘记材料失败，请重试");
        }
        chatBridge?.reportDiagnostic("warn", "materials.recent_forget_failed", error);
        throw error;
      }
    },
    async submitWorkInteractionAction(action) {
      try {
        if (action.kind === "question") {
          await submitText(action.messageText);
          return;
        }
        await submitBackgroundAgentPermissionDecision(httpBaseUrl, action);
      } catch (error) {
        chatBridge?.reportDiagnostic("warn", "background_agent.interaction_answer_failed", {
          requestId: action.requestId,
          error
        });
        throw error;
      }
    }
  };
}

function readCommandMaterialContext(payload: unknown): MaterialContextSnapshot {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("material command response is invalid");
  }
  const response = payload as Record<string, unknown>;
  if (response.accepted !== true) {
    throw new Error("material command response was not accepted");
  }
  return readMaterialContextSnapshot(response.materialContext);
}

function readRemovedDisplayName(payload: unknown): string {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("material remove response is invalid");
  }
  const displayName = (payload as { removedDisplayName?: unknown }).removedDisplayName;
  if (typeof displayName !== "string" || !displayName.trim()) {
    throw new Error("material remove response is missing display name");
  }
  return displayName.trim();
}
