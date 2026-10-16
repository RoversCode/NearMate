import type { BackendEvent, ReminderSettings } from "../types/events";
import type {
  TurnSubmissionState,
  TurnSubmissionsSnapshot
} from "../../../shared/turnSubmissions";
import {
  addOptimisticTurnSubmission,
  reconcileTurnSubmissions,
  rejectOptimisticTurnSubmission,
  resetTurnSubmissionsRevision
} from "./chatTurnSubmissionProjection";
import { ACTIVE_CHARACTER_NAME_FALLBACK } from "./characterName";

type ChatMessageRole = "assistant" | "user";
type ChatMessageKind = "text" | "task_result" | "agent_interaction";

type ChatTaskResult = {
  activityId: string;
  status: "completed" | "failed" | "cancelled";
  goal: string;
  contentText: string;
  sourceMaterials: string[];
  outputPaths: string[];
};

export type ChatAgentQuestion = {
  question: string;
  header: string;
  options: Array<{ label: string; description: string }>;
  multiSelect: boolean;
};

type ChatAgentInteractionBase = {
  requestId: string;
  activityId: string;
  toolName: string;
  inputData: Record<string, unknown>;
  questions: ChatAgentQuestion[];
  status: "pending" | "processing" | "resolved";
  outcome?: "allowed" | "denied" | "answered" | "cancelled";
};

export type ChatAgentInteraction = ChatAgentInteractionBase & (
  | {
      kind: "permission";
      actionSummary: string;
      grantScope: "activity";
    }
  | {
      kind: "question";
    }
);

export type ChatMessage = {
  id: string;
  requestId?: string;
  role: ChatMessageRole;
  kind?: ChatMessageKind;
  senderName: string;
  text: string;
  createdAt: number;
  taskResult?: ChatTaskResult;
  agentInteraction?: ChatAgentInteraction;
  references?: Array<{ displayName: string; text: string }>;
  submissionId?: string;
  submissionState?: ChatTurnSubmissionState;
  submissionError?: string | null;
  submissionAcknowledged?: boolean;
};

/**
 * 描述待发用户气泡在 Desktop 中可能呈现的状态。
 *
 * `awaiting_final` 只存在于本地投影：Backend 已经把文本派发出队，
 * 但正式用户事件还没有到达，不能把它误删成“发送失败”。
 */
export type ChatTurnSubmissionState = TurnSubmissionState | "awaiting_final";

export type ChatState = {
  messages: ChatMessage[];
  draftAssistantText: string;
  draftAssistantRequestId?: string;
  draftAssistantEpisodeId?: string;
  companionName: string;
  turnSubmissionsRevision: number;
  finalizedSubmissionIds: ReadonlySet<string>;
  committedUnattendedEpisodeRevisions?: ReadonlyMap<string, number>;
  assistantOutputError?: string | null;
};

export type ChatAction =
  | {
      type: "backend_event_received";
      event: BackendEvent;
    }
  | {
      type: "turn_submission_optimistically_added";
      submissionId: string;
      messageText: string;
    }
  | {
      type: "turn_submission_reservation_rejected";
      submissionId: string;
    }
  | {
      type: "turn_submissions_snapshot_received";
      snapshot: TurnSubmissionsSnapshot;
    }
  | {
      type: "turn_submissions_connection_reset";
    }
  | {
      type: "turn_submission_explicitly_deleted";
      submissionId: string;
    }
  | {
      type: "active_character_name_changed";
      characterName: string;
    };

const USER_NAME = "master";
const DEFAULT_COMPANION_DISPLAY_NAME = ACTIVE_CHARACTER_NAME_FALLBACK;

/**
 * 生成 episode 历史投影的运行时隔离键。
 *
 * @param runtimeEpoch 生成闭合历史的 Assistant Runtime 世代。
 * @param episodeId 当前未处理输入片段编号。
 * @returns 同一世代内唯一、可用于 revision 门禁的字符串键。
 */
function unattendedEpisodeProjectionKey(
  runtimeEpoch: number,
  episodeId: string
): string {
  return `${runtimeEpoch}:${episodeId}`;
}

/**
 * 创建聊天窗口的运行期内存状态。
 *
 * @param _settings 当前设置快照；预留给后续显示偏好初始化。
 * @returns 包含空消息列表、空草稿和默认伴侣名称的初始状态。
 */
export function createInitialChatState(_settings: ReminderSettings): ChatState {
  return {
    messages: [],
    draftAssistantText: "",
    companionName: DEFAULT_COMPANION_DISPLAY_NAME,
    turnSubmissionsRevision: -1,
    finalizedSubmissionIds: new Set<string>(),
    committedUnattendedEpisodeRevisions: new Map<string, number>(),
    assistantOutputError: null
  };
}

/**
 * 把控制层事件收敛成聊天窗口内存状态。
 *
 * @param state 事件到达前的聊天状态。
 * @param action 携带后端控制事件的 reducer 动作。
 * @returns 应用事件后的新状态；无关事件返回原状态引用。
 */
export function reduceChatState(state: ChatState, action: ChatAction): ChatState {
  if (action.type === "turn_submission_optimistically_added") {
    return addOptimisticTurnSubmission(state, {
      submissionId: action.submissionId,
      messageText: action.messageText,
      senderName: USER_NAME
    });
  }

  if (action.type === "turn_submission_reservation_rejected") {
    return rejectOptimisticTurnSubmission(state, action.submissionId);
  }

  if (action.type === "turn_submissions_snapshot_received") {
    return reconcileTurnSubmissions(state, action.snapshot, USER_NAME);
  }

  if (action.type === "turn_submissions_connection_reset") {
    return resetTurnSubmissionsRevision(state);
  }

  if (action.type === "turn_submission_explicitly_deleted") {
    const nextMessages = state.messages.filter(message => (
      message.submissionId !== action.submissionId
    ));
    return nextMessages.length === state.messages.length
      ? state
      : { ...state, messages: nextMessages };
  }

  if (action.type === "active_character_name_changed") {
    const characterName = action.characterName.trim();
    if (!characterName || characterName === state.companionName) {
      return state;
    }
    return {
      ...state,
      companionName: characterName,
      // 角色卡切换后，聊天窗口和右栏都应立即显示同一个当前角色名称。
      messages: state.messages.map(message => (
        message.role === "assistant"
          ? { ...message, senderName: characterName }
          : message
      ))
    };
  }

  const event = action.event;

  if (event.type === "assistant.output_status_changed") {
    return {
      ...state,
      assistantOutputError: event.payload.available
        ? null
        : event.payload.message.trim()
    };
  }

  if (event.type === "turn_submissions.snapshot_changed") {
    return reconcileTurnSubmissions(state, event.payload, USER_NAME);
  }

  if (event.type === "user.final_transcript") {
    const text = event.payload.text.trim();

    if (!text) {
      return state;
    }

    if (event.payload.history_disposition === "unattended_episode") {
      const submissionId = event.payload.submission_id?.trim();
      const finalizedSubmissionIds = new Set(state.finalizedSubmissionIds);
      if (submissionId) {
        finalizedSubmissionIds.add(submissionId);
      }
      return {
        ...state,
        messages: submissionId
          ? state.messages.filter(message => message.submissionId !== submissionId)
          : state.messages,
        draftAssistantText: state.draftAssistantEpisodeId === event.payload.episode_id
          ? ""
          : state.draftAssistantText,
        finalizedSubmissionIds
      };
    }

    const references = (event.payload.references ?? []).map(reference => ({
      displayName: reference.display_name,
      text: reference.text
    }));
    const submissionId = event.payload.submission_id?.trim();
    if (!submissionId) {
      return {
        ...state,
        messages: [
          ...state.messages,
          createChatMessage({
            role: "user",
            senderName: USER_NAME,
            text,
            requestId: event.payload.request_id,
            references
          })
        ]
      };
    }

    if (state.finalizedSubmissionIds.has(submissionId)) {
      return state;
    }

    const existingMessageIndex = state.messages.findIndex(
      message => message.submissionId === submissionId
    );
    const finalizedMessage = createChatMessage({
      id: existingMessageIndex >= 0
        ? state.messages[existingMessageIndex].id
        : undefined,
      role: "user",
      senderName: USER_NAME,
      text,
      requestId: event.payload.request_id,
      references,
      submissionId,
      createdAt: existingMessageIndex >= 0
        ? state.messages[existingMessageIndex].createdAt
        : undefined
    });
    const finalizedSubmissionIds = new Set(state.finalizedSubmissionIds);
    finalizedSubmissionIds.add(submissionId);
    return {
      ...state,
      messages: existingMessageIndex >= 0
        ? state.messages.map((message, index) => (
            index === existingMessageIndex ? finalizedMessage : message
          ))
        : [...state.messages, finalizedMessage],
      finalizedSubmissionIds
    };
  }

  if (event.type === "wake.detected") {
    const wakeText = event.payload.text?.trim() || "我在，正在听。";
    // 唤醒回复发生在正式语音轮次之前，但仍是当前角色对用户可见的表达，
    // 因此必须进入 Chat 历史；重复控制帧连续到达时只保留一条。
    const lastMessage = state.messages[state.messages.length - 1];
    if (
      lastMessage?.role === "assistant"
      && lastMessage.text === wakeText
    ) {
      return state;
    }
    return {
      ...state,
      messages: [
        ...state.messages,
        createChatMessage({
          role: "assistant",
          senderName: state.companionName,
          text: wakeText
        })
      ]
    };
  }

  if (event.type === "assistant.text_chunk") {
    const sameRequest = state.draftAssistantRequestId === event.payload.request_id;
    return {
      ...state,
      draftAssistantText: sameRequest
        ? `${state.draftAssistantText}${event.payload.text}`
        : event.payload.text,
      draftAssistantRequestId: event.payload.request_id,
      draftAssistantEpisodeId: event.payload.episode_id
    };
  }

  if (
    event.type === "assistant.text_segment_final"
    || event.type === "assistant.text_final"
  ) {
    const text = event.payload.text.trim();

    if (event.payload.request_source === "self_reflection_interruption_reply") {
      if (
        state.draftAssistantRequestId
        && state.draftAssistantRequestId !== event.payload.request_id
      ) {
        return state;
      }
      return {
        ...state,
        draftAssistantText: "",
        draftAssistantRequestId: undefined,
        draftAssistantEpisodeId: undefined
      };
    }

    if (!text) {
      return {
        ...state,
        draftAssistantText: ""
      };
    }

    return {
      ...state,
      draftAssistantText: "",
      messages: [
        ...state.messages,
        createChatMessage({
          role: "assistant",
          senderName: state.companionName,
          text
        })
      ]
    };
  }

  if (event.type === "self_reflection.episode_committed") {
    const episodeId = event.payload.episode_id;
    const episodeProjectionKey = unattendedEpisodeProjectionKey(
      event.payload.runtime_epoch,
      episodeId
    );
    const committedRevision = state.committedUnattendedEpisodeRevisions?.get(
      episodeProjectionKey
    );
    if (
      committedRevision !== undefined
      && event.payload.revision <= committedRevision
    ) {
      return state;
    }
    const committedUnattendedEpisodeRevisions = new Map(
      state.committedUnattendedEpisodeRevisions ?? []
    );
    committedUnattendedEpisodeRevisions.set(
      episodeProjectionKey,
      event.payload.revision
    );
    const episodeMessageIdPrefix = `unattended-episode-${episodeProjectionKey}-`;
    const episodeMessages = event.payload.history_block.map((item, index) => (
      createChatMessage({
        id: `${episodeMessageIdPrefix}${index}`,
        role: item.role,
        senderName: item.role === "user" ? USER_NAME : state.companionName,
        text: item.content,
        requestId: item.source_identity ?? undefined,
        createdAt: Date.parse(item.occurred_at)
      })
    ));
    return {
      ...state,
      messages: [
        ...state.messages.filter(message => (
          !message.id.startsWith(episodeMessageIdPrefix)
        )),
        ...episodeMessages
      ],
      draftAssistantText: state.draftAssistantEpisodeId === episodeId
        ? ""
        : state.draftAssistantText,
      draftAssistantRequestId: state.draftAssistantEpisodeId === episodeId
        ? undefined
        : state.draftAssistantRequestId,
      draftAssistantEpisodeId: state.draftAssistantEpisodeId === episodeId
        ? undefined
        : state.draftAssistantEpisodeId,
      committedUnattendedEpisodeRevisions
    };
  }

  if (event.type === "assistant.request_failed") {
    const requestId = event.payload.request_id;
    const failureMessageText = event.payload.message.trim();
    const finalizedSubmissionIds = new Set(state.finalizedSubmissionIds);
    const messagesWithSubmissionFailure = state.messages.map(message => {
      if (
        message.requestId !== requestId
        || !message.submissionId
        || message.submissionState !== "awaiting_final"
      ) {
        return message;
      }
      finalizedSubmissionIds.add(message.submissionId);
      return {
        ...message,
        submissionId: undefined,
        submissionState: undefined,
        submissionError: undefined,
        submissionAcknowledged: undefined
      };
    });
    const failureMessage = createChatMessage({
      id: `assistant-request-failed-${requestId}`,
      role: "assistant",
      senderName: state.companionName,
      text: failureMessageText,
      requestId
    });
    return {
      ...state,
      draftAssistantText: "",
      messages: insertRequestFailureMessage(messagesWithSubmissionFailure, failureMessage),
      finalizedSubmissionIds
    };
  }

  if (event.type === "task_result.completed") {
    const taskResultMessage = createTaskResultMessage({
      senderName: state.companionName,
      activityId: event.payload.activity_id,
      status: "completed",
      goal: event.payload.goal,
      contentText: event.payload.display_content,
      sourceMaterials: event.payload.source_materials,
      outputPaths: event.payload.output_paths ?? [],
      createdAt: event.payload.created_at
    });
    return {
      ...state,
      messages: upsertTaskResultMessage(state.messages, taskResultMessage)
    };
  }

  if (event.type === "task_result.failed") {
    const taskResultMessage = createTaskResultMessage({
      senderName: state.companionName,
      activityId: event.payload.activity_id,
      status: "failed",
      goal: event.payload.goal,
      contentText: event.payload.failure.summary,
      sourceMaterials: event.payload.source_materials,
      outputPaths: [],
      createdAt: event.payload.created_at
    });
    return {
      ...state,
      messages: upsertTaskResultMessage(state.messages, taskResultMessage)
    };
  }

  if (event.type === "task_result.cancelled") {
    const taskResultMessage = createTaskResultMessage({
      senderName: state.companionName,
      activityId: event.payload.activity_id,
      status: "cancelled",
      goal: event.payload.goal,
      contentText: event.payload.message,
      sourceMaterials: event.payload.source_materials,
      outputPaths: [],
      createdAt: event.payload.created_at
    });
    return {
      ...state,
      messages: upsertTaskResultMessage(state.messages, taskResultMessage)
    };
  }

  if (event.type === "background_agent.interaction_requested") {
    const interactionMessage = createAgentInteractionMessage(
      state.companionName,
      event.payload
    );
    return {
      ...state,
      messages: upsertMessage(state.messages, interactionMessage)
    };
  }

  if (event.type === "background_agent.interactions_snapshot") {
    return {
      ...state,
      messages: replacePendingAgentInteractions(
        state.messages,
        state.companionName,
        event.payload.items
      )
    };
  }

  if (event.type === "background_agent.interaction_submitted") {
    const messageId = `agent-interaction-${event.payload.request_id}`;
    return {
      ...state,
      messages: state.messages.map(message => {
        if (
          message.id !== messageId
          || !message.agentInteraction
          || message.agentInteraction.activityId !== event.payload.activity_id
          || message.agentInteraction.status !== "pending"
        ) {
          return message;
        }
        return {
          ...message,
          agentInteraction: {
            ...message.agentInteraction,
            status: "processing"
          }
        };
      })
    };
  }

  if (event.type === "background_agent.interaction_resolved") {
    const messageId = `agent-interaction-${event.payload.request_id}`;
    return {
      ...state,
      messages: state.messages.map(message => {
        if (message.id !== messageId || !message.agentInteraction) {
          return message;
        }
        return {
          ...message,
          agentInteraction: {
            ...message.agentInteraction,
            status: "resolved",
            outcome: event.payload.outcome
          }
        };
      })
    };
  }

  if (event.type === "barge_in.detected") {
    return {
      ...state,
      draftAssistantText: ""
    };
  }

  return state;
}

/**
 * 把流式草稿投影成可渲染但不入列的消息。
 *
 * @param state 包含当前助手流式草稿的聊天状态。
 * @returns 可渲染的助手草稿消息；草稿为空时返回空。
 */
export function createDraftAssistantMessage(state: ChatState): ChatMessage | null {
  const text = state.draftAssistantText.trim();

  if (!text) {
    return null;
  }

  return {
    id: "assistant-draft",
    role: "assistant",
    senderName: state.companionName,
    text,
    createdAt: Date.now()
  };
}

/**
 * 统一生成普通聊天消息。
 *
 * @param input 消息角色、显示名称、正文及可选引用。
 * @returns 带运行期唯一编号和创建时间的聊天消息。
 */
function createChatMessage(input: {
  id?: string;
  role: ChatMessageRole;
  senderName: string;
  text: string;
  requestId?: string;
  references?: Array<{ displayName: string; text: string }>;
  submissionId?: string;
  createdAt?: number;
}): ChatMessage {
  return {
    id: input.id ?? `${input.role}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    requestId: input.requestId,
    role: input.role,
    senderName: input.senderName,
    text: input.text,
    references: input.references,
    submissionId: input.submissionId,
    createdAt: input.createdAt ?? Date.now()
  };
}

/**
 * 把请求失败提示放回其用户消息之后，并抵御重复控制事件。
 *
 * @param messages 当前聊天消息列表。
 * @param failureMessage 已绑定 request id 的助手失败消息。
 * @returns 保持其它消息顺序的更新列表；找不到原请求时安全追加。
 */
function insertRequestFailureMessage(
  messages: ChatMessage[],
  failureMessage: ChatMessage
): ChatMessage[] {
  if (messages.some(message => message.id === failureMessage.id)) {
    return messages;
  }

  const requestMessageIndex = messages.findIndex(message => (
    message.role === "user" &&
    message.requestId === failureMessage.requestId
  ));
  if (requestMessageIndex < 0) {
    return [...messages, failureMessage];
  }

  return [
    ...messages.slice(0, requestMessageIndex + 1),
    failureMessage,
    ...messages.slice(requestMessageIndex + 1)
  ];
}

/**
 * 把产物事件转换为聊天窗口结果卡消息。
 *
 * @param input 后端结果卡的任务身份、状态、正文和来源材料。
 * @returns 独立于普通文本流的助手结果卡消息。
 */
function createTaskResultMessage(input: {
  senderName: string;
  activityId: string;
  status: ChatTaskResult["status"];
  goal: string;
  contentText: string;
  sourceMaterials: string[];
  outputPaths: string[];
  createdAt: string | number;
}): ChatMessage {
  const contentText = input.contentText.trim();
  return {
    id: `taskResult-${input.activityId}`,
    role: "assistant",
    kind: "task_result",
    senderName: input.senderName,
    text: contentText,
    createdAt: parseEventTime(input.createdAt),
    taskResult: {
      activityId: input.activityId,
      status: input.status,
      goal: input.goal.trim(),
      contentText,
      sourceMaterials: input.sourceMaterials.filter(source => source.trim()),
      outputPaths: input.outputPaths.filter(path => path.trim())
    }
  };
}

/**
 * 按任务编号原位更新结果卡，抵御控制事件重复投递。
 *
 * @param messages 当前聊天消息列表。
 * @param taskResultMessage 新到达的结果卡消息。
 * @returns 保持原顺序且同一任务最多一张结果卡的消息列表。
 */
function upsertTaskResultMessage(messages: ChatMessage[], taskResultMessage: ChatMessage): ChatMessage[] {
  return upsertMessage(messages, taskResultMessage);
}

/**
 * 把后台智能体等待请求转换为聊天交互卡。
 *
 * @param senderName 当前 Foreground 角色显示名。
 * @param payload 后端已经截断并校验基础身份的交互载荷。
 * @returns 可在同一消息流中原位收口的交互卡消息。
 */
function createAgentInteractionMessage(
  senderName: string,
  payload:
    | Extract<BackendEvent, { type: "background_agent.interaction_requested" }>["payload"]
    | Extract<BackendEvent, { type: "background_agent.interactions_snapshot" }>["payload"]["items"][number]
): ChatMessage {
  const questions = payload.questions
    .map(readAgentQuestion)
    .filter((question): question is ChatAgentQuestion => question !== null);
  const commonInteraction = {
    requestId: payload.request_id,
    activityId: payload.activity_id,
    toolName: payload.tool_name,
    inputData: payload.input_data,
    questions,
    status: "status" in payload ? payload.status : "pending"
  } satisfies ChatAgentInteractionBase;
  const agentInteraction: ChatAgentInteraction = payload.kind === "permission"
    ? {
        ...commonInteraction,
        kind: "permission",
        actionSummary: payload.action_summary,
        grantScope: payload.grant_scope
      }
    : {
        ...commonInteraction,
        kind: "question"
      };
  return {
    id: `agent-interaction-${payload.request_id}`,
    role: "assistant",
    kind: "agent_interaction",
    senderName,
    text: "",
    createdAt: Date.now(),
    agentInteraction
  };
}

/**
 * 用 Backend 完整快照替换当前仍开放的交互卡。
 *
 * @param messages 当前聊天消息列表。
 * @param senderName 当前 Foreground 角色显示名。
 * @param items Backend 当前全部 pending 或 processing 请求。
 * @returns 保留普通消息和已收口历史，只校准开放卡片的消息列表。
 */
function replacePendingAgentInteractions(
  messages: ChatMessage[],
  senderName: string,
  items: Extract<BackendEvent, { type: "background_agent.interactions_snapshot" }>["payload"]["items"]
): ChatMessage[] {
  const pendingMessages = new Map(
    items.map(item => {
      const message = createAgentInteractionMessage(senderName, item);
      return [message.id, message] as const;
    })
  );
  const synchronizedMessages = messages.flatMap(message => {
    if (
      message.agentInteraction?.status !== "pending"
      && message.agentInteraction?.status !== "processing"
    ) {
      return [message];
    }
    const replacement = pendingMessages.get(message.id);
    pendingMessages.delete(message.id);
    return replacement === undefined
      ? []
      : [{ ...replacement, createdAt: message.createdAt }];
  });
  return [...synchronizedMessages, ...pendingMessages.values()];
}

/**
 * 把 SDK AskUserQuestion 字段收敛成 renderer 稳定问题模型。
 *
 * @param value 尚未确认结构的问题对象。
 * @returns 可渲染的问题；缺少正文时返回空。
 */
function readAgentQuestion(value: Record<string, unknown>): ChatAgentQuestion | null {
  const question = typeof value.question === "string" ? value.question.trim() : "";
  if (!question) {
    return null;
  }
  const options = Array.isArray(value.options)
    ? value.options.flatMap(option => {
      if (!option || typeof option !== "object" || Array.isArray(option)) {
        return [];
      }
      const candidate = option as Record<string, unknown>;
      const label = typeof candidate.label === "string" ? candidate.label.trim() : "";
      if (!label) {
        return [];
      }
      return [{
        label,
        description: typeof candidate.description === "string"
          ? candidate.description.trim()
          : ""
      }];
    })
    : [];
  return {
    question,
    header: typeof value.header === "string" ? value.header.trim() : "",
    options,
    multiSelect: value.multiSelect === true
  };
}

/**
 * 按消息身份新增或原位替换卡片，抵御控制事件重复投递。
 *
 * @param messages 当前聊天消息列表。
 * @param nextMessage 需要写入的完整消息。
 * @returns 同一身份最多保留一条消息的列表。
 */
function upsertMessage(messages: ChatMessage[], nextMessage: ChatMessage): ChatMessage[] {
  const existingIndex = messages.findIndex(message => message.id === nextMessage.id);
  if (existingIndex < 0) {
    return [...messages, nextMessage];
  }
  return messages.map((message, index) => index === existingIndex ? nextMessage : message);
}

/**
 * 把后端事件时间统一成聊天消息时间戳。
 *
 * @param value 后端提供的 ISO 时间字符串或毫秒数。
 * @returns 可用于 Date 的毫秒时间；输入无效时返回当前时间。
 */
function parseEventTime(value: string | number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }

  if (typeof value === "string") {
    const parsedTime = Date.parse(value);
    if (Number.isFinite(parsedTime)) {
      return parsedTime;
    }
  }

  return Date.now();
}
