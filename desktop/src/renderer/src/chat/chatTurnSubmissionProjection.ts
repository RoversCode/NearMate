import type {
  TurnSubmissionItem,
  TurnSubmissionsSnapshot
} from "../../../shared/turnSubmissions";
import type {
  ChatMessage,
  ChatState,
  ChatTurnSubmissionState
} from "./chatReducer";

type OptimisticTurnSubmission = {
  submissionId: string;
  messageText: string;
  senderName: string;
};

/**
 * 在 reservation 发出前立即投影用户消息。
 *
 * Args:
 *   state: 当前聊天状态。
 *   input: Desktop 创建的稳定提交编号、正文和用户显示名。
 *
 * Returns:
 *   已包含准备中用户气泡的新状态。
 */
export function addOptimisticTurnSubmission(
  state: ChatState,
  input: OptimisticTurnSubmission
): ChatState {
  if (
    state.finalizedSubmissionIds.has(input.submissionId)
    || state.messages.some(message => message.submissionId === input.submissionId)
  ) {
    return state;
  }

  return {
    ...state,
    messages: [
      ...state.messages,
      createPendingUserMessage({
        submissionId: input.submissionId,
        messageText: input.messageText,
        senderName: input.senderName,
        state: "preparing",
        errorMessage: null,
        acknowledged: false
      })
    ]
  };
}

/**
 * reservation 明确失败时撤销仍未被 Backend 确认的乐观气泡。
 *
 * Args:
 *   state: 当前聊天状态。
 *   submissionId: 失败提交的稳定编号。
 *
 * Returns:
 *   已撤销纯本地气泡的新状态；若 WebSocket 已确认该提交则保持原状。
 */
export function rejectOptimisticTurnSubmission(
  state: ChatState,
  submissionId: string
): ChatState {
  const nextMessages = state.messages.filter(message => (
    message.submissionId !== submissionId || message.submissionAcknowledged === true
  ));
  return nextMessages.length === state.messages.length
    ? state
    : { ...state, messages: nextMessages };
}

/**
 * 用 Backend 权威快照校准全部待发文本。
 *
 * Args:
 *   state: 当前聊天状态。
 *   snapshot: HTTP 或 WebSocket 返回的完整队列快照。
 *   senderName: 用户消息显示名。
 *
 * Returns:
 *   忽略旧 revision、原位更新已知气泡并补齐恢复项后的状态。
 */
export function reconcileTurnSubmissions(
  state: ChatState,
  snapshot: TurnSubmissionsSnapshot,
  senderName: string
): ChatState {
  if (snapshot.revision < state.turnSubmissionsRevision) {
    return state;
  }

  const remainingItems = new Map(
    snapshot.items
      .filter(item => !state.finalizedSubmissionIds.has(item.submissionId))
      .map(item => [item.submissionId, item] as const)
  );
  const nextMessages = state.messages.flatMap(message => {
    if (!message.submissionId || state.finalizedSubmissionIds.has(message.submissionId)) {
      return [message];
    }

    const item = remainingItems.get(message.submissionId);
    if (item) {
      remainingItems.delete(message.submissionId);
      return [applySnapshotItem(message, item)];
    }

    // Backend 派发后会立即把记录从待发快照移除；在正式用户事件丢失的窗口里，
    // 仍要保留已确认的气泡，避免用户看到自己刚发出的文本凭空消失。
    return message.submissionAcknowledged === true
      ? [markAwaitingFinalization(message)]
      : [message];
  });

  for (const item of remainingItems.values()) {
    nextMessages.push(createPendingUserMessage({
      submissionId: item.submissionId,
      messageText: item.messageText,
      senderName,
      state: item.state,
      errorMessage: item.errorMessage,
      acknowledged: true
    }));
  }

  return {
    ...state,
    messages: nextMessages,
    turnSubmissionsRevision: snapshot.revision
  };
}

/**
 * 新控制连接建立前放开 revision 门禁。
 *
 * Args:
 *   state: 当前聊天状态。
 *
 * Returns:
 *   保留当前气泡、但允许新 Backend 从较小 revision 开始恢复的状态。
 */
export function resetTurnSubmissionsRevision(state: ChatState): ChatState {
  return state.turnSubmissionsRevision === -1
    ? state
    : { ...state, turnSubmissionsRevision: -1 };
}

/**
 * 把一条权威快照记录原位覆盖到已有用户气泡。
 *
 * Args:
 *   message: 当前本地用户气泡。
 *   item: Backend 返回的同编号待发文本。
 *
 * Returns:
 *   保留气泡身份和时间、更新正文与队列状态后的消息。
 */
function applySnapshotItem(
  message: ChatMessage,
  item: TurnSubmissionItem
): ChatMessage {
  return {
    ...message,
    text: item.messageText,
    submissionState: item.state,
    submissionError: item.errorMessage,
    submissionAcknowledged: true
  };
}

/**
 * 将已离队但还没有正式用户事件的消息标记为等待收口。
 *
 * Args:
 *   message: 已被 Backend 确认、但不再出现在待发快照中的用户气泡。
 *
 * Returns:
 *   保留正文和消息身份、等待 `user.final_transcript` 或明确失败事件的气泡。
 */
function markAwaitingFinalization(message: ChatMessage): ChatMessage {
  if (message.submissionState === "failed") {
    return message;
  }
  return {
    ...message,
    submissionState: "awaiting_final" as ChatTurnSubmissionState,
    submissionError: null
  };
}

/**
 * 创建一条尚未正式进入聊天历史的用户气泡。
 *
 * Args:
 *   input: 提交编号、正文、显示名和当前队列状态。
 *
 * Returns:
 *   可原位正式化的稳定用户消息。
 */
function createPendingUserMessage(input: {
  submissionId: string;
  messageText: string;
  senderName: string;
  state: TurnSubmissionItem["state"];
  errorMessage: string | null;
  acknowledged: boolean;
}): ChatMessage {
  return {
    id: `turn-submission-${input.submissionId}`,
    role: "user",
    senderName: input.senderName,
    text: input.messageText,
    createdAt: Date.now(),
    requestId: `text-input-${input.submissionId}`,
    submissionId: input.submissionId,
    submissionState: input.state,
    submissionError: input.errorMessage,
    submissionAcknowledged: input.acknowledged
  };
}
