import { useEffect, useMemo, useRef } from "react";
import { Copy, FolderOpen } from "lucide-react";
import { createDraftAssistantMessage, type ChatMessage, type ChatState } from "./chatReducer";
import { LinkifiedText } from "../components/LinkifiedText";
import { BackgroundAgentInteractionCard } from "./BackgroundAgentInteractionCard";
import type { BackgroundAgentInteractionAction } from "./backgroundAgentInteractionClient";
import { TurnSubmissionMessage } from "./TurnSubmissionMessage";

export type ChatMessageListProps = {
  state: ChatState;
  companionAvatarUrl: string;
  userAvatarUrl: string;
  onAnswerInteraction?(answer: BackgroundAgentInteractionAction): Promise<void>;
  onOpenOutputPath?(path: string): Promise<void>;
  onEditTurnSubmission?(submissionId: string, messageText: string): Promise<void>;
  onDeleteTurnSubmission?(submissionId: string): Promise<void>;
  onResumeTurnSubmission?(submissionId: string): Promise<void>;
  onRetryTurnSubmission?(submissionId: string): Promise<void>;
};

/**
 * ChatMessageList 渲染聊天消息滚动区。
 * 核心入参是聊天状态与两侧头像 URL；输出左右分列的消息气泡和空态。
 */
export function ChatMessageList({
  state,
  companionAvatarUrl,
  userAvatarUrl,
  onAnswerInteraction,
  onOpenOutputPath,
  onEditTurnSubmission,
  onDeleteTurnSubmission,
  onResumeTurnSubmission,
  onRetryTurnSubmission
}: ChatMessageListProps) {
  const messageListRef = useRef<HTMLElement | null>(null);
  const visibleMessages = useMemo(() => {
    const draftMessage = createDraftAssistantMessage(state);
    return draftMessage ? [...state.messages, draftMessage] : state.messages;
  }, [state]);

  useEffect(() => {
    /**
     * 聊天窗口只允许消息列表滚动，避免 scrollIntoView 把外层页面推到输入框下方。
     * 新消息和流式草稿都强制跟随到底部，符合即时聊天窗口的阅读预期。
     */
    if (!messageListRef.current) {
      return;
    }

    messageListRef.current.scrollTop = messageListRef.current.scrollHeight;
  }, [visibleMessages.length, state.draftAssistantText]);

  return (
    <section ref={messageListRef} className="chat-message-list" aria-label="聊天记录">
      {visibleMessages.length === 0 ? (
        <div className="chat-empty-state">
          <strong>还没有聊天记录</strong>
          <span>本次运行里的文字和语音对话会显示在这里。</span>
        </div>
      ) : (
        <div className="chat-date-divider" aria-label="消息日期">
          <span>{formatDateDivider(visibleMessages[0].createdAt)}</span>
        </div>
      )}

      {visibleMessages.map(message => (
        <ChatMessageBubble
          key={message.id}
          message={message}
          avatarUrl={message.role === "user" ? userAvatarUrl : companionAvatarUrl}
          onAnswerInteraction={onAnswerInteraction}
          onOpenOutputPath={onOpenOutputPath}
          onEditTurnSubmission={onEditTurnSubmission}
          onDeleteTurnSubmission={onDeleteTurnSubmission}
          onResumeTurnSubmission={onResumeTurnSubmission}
          onRetryTurnSubmission={onRetryTurnSubmission}
        />
      ))}
    </section>
  );
}

type ChatMessageBubbleProps = {
  message: ChatMessage;
  avatarUrl: string;
  onAnswerInteraction?: ChatMessageListProps["onAnswerInteraction"];
  onOpenOutputPath?: ChatMessageListProps["onOpenOutputPath"];
  onEditTurnSubmission?: ChatMessageListProps["onEditTurnSubmission"];
  onDeleteTurnSubmission?: ChatMessageListProps["onDeleteTurnSubmission"];
  onResumeTurnSubmission?: ChatMessageListProps["onResumeTurnSubmission"];
  onRetryTurnSubmission?: ChatMessageListProps["onRetryTurnSubmission"];
};

/**
 * ChatMessageBubble 渲染单条聊天消息。
 * 核心入参是消息对象和头像 URL；输出符合 QQ 类左右布局的头像、名字和气泡。
 */
function ChatMessageBubble({
  message,
  avatarUrl,
  onAnswerInteraction,
  onOpenOutputPath,
  onEditTurnSubmission,
  onDeleteTurnSubmission,
  onResumeTurnSubmission,
  onRetryTurnSubmission
}: ChatMessageBubbleProps) {
  return (
    <article className="chat-message" data-role={message.role}>
      <div className="chat-message-inner">
        <img className="chat-avatar" src={avatarUrl} alt="" />
        <div className="chat-message-content">
          <div className="chat-sender-row">
            <span className="chat-sender-name">{message.senderName}</span>
            <time>{formatMessageTime(message.createdAt)}</time>
          </div>
          {message.kind === "task_result" && message.taskResult ? (
            <ChatTaskResultCard
              taskResult={message.taskResult}
              onOpenOutputPath={onOpenOutputPath}
            />
          ) : message.kind === "agent_interaction" && message.agentInteraction ? (
            <BackgroundAgentInteractionCard
              interaction={message.agentInteraction}
              onAnswer={answer => {
                if (!onAnswerInteraction) {
                  return Promise.reject(new Error("后台交互回答端口不可用"));
                }
                return onAnswerInteraction(answer);
              }}
            />
          ) : message.role === "user" && message.submissionId && message.submissionState ? (
            <TurnSubmissionMessage
              message={message}
              onEdit={onEditTurnSubmission}
              onDelete={onDeleteTurnSubmission}
              onResume={onResumeTurnSubmission}
              onRetry={onRetryTurnSubmission}
            />
          ) : (
            <>
              <p className="chat-bubble">
                {message.role === "user" ? <LinkifiedText text={message.text} /> : message.text}
              </p>
              {message.references?.map(reference => (
                <details className="chat-reference-card" key={`${reference.displayName}-${reference.text}`}>
                  <summary>{reference.displayName}</summary>
                  <p>{reference.text}</p>
                </details>
              ))}
            </>
          )}
        </div>
      </div>
    </article>
  );
}

type ChatTaskResultCardProps = {
  taskResult: NonNullable<ChatMessage["taskResult"]>;
  onOpenOutputPath?: ChatMessageListProps["onOpenOutputPath"];
};

/**
 * ChatTaskResultCard 渲染聊天窗口里的文本产物结果卡。
 * 核心入参是 taskResult 消息数据；输出可阅读、可复制、可追溯来源的卡片。
 */
function ChatTaskResultCard({ taskResult, onOpenOutputPath }: ChatTaskResultCardProps) {
  const copyResult = () => {
    void navigator.clipboard?.writeText(taskResult.contentText);
  };

  return (
    <section className="chat-task-result-card" data-status={taskResult.status}>
      <div className="chat-task-result-header">
        <h3><LinkifiedText text={taskResult.goal} /></h3>
        <button
          type="button"
          className="chat-task-result-icon-button"
          aria-label="复制结果"
          title="复制结果"
          onClick={copyResult}
        >
          <Copy size={15} aria-hidden="true" />
        </button>
      </div>
      <p className="chat-task-result-body"><LinkifiedText text={taskResult.contentText} /></p>
      {taskResult.sourceMaterials.length > 0 ? (
        <div className="chat-task-result-sources" aria-label="来源材料">
          {taskResult.sourceMaterials.map(source => (
            <span key={source}>{source}</span>
          ))}
        </div>
      ) : null}
      {taskResult.outputPaths.length > 0 ? (
        <div className="chat-task-result-outputs" aria-label="交付文件">
          {taskResult.outputPaths.map(path => (
            <div key={path} className="chat-task-result-output-row">
              <span title={path}>{displayPathName(path)}</span>
              <button
                type="button"
                className="chat-task-result-icon-button"
                aria-label={`打开 ${displayPathName(path)} 所在文件夹`}
                title="打开所在文件夹"
                onClick={() => void onOpenOutputPath?.(path)}
              >
                <FolderOpen size={15} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

/**
 * 从绝对路径提取适合结果卡展示的文件名。
 *
 * @param path Backend 校验过的交付文件绝对路径。
 * @returns 文件名；路径无法拆分时返回原字符串。
 */
function displayPathName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
}

/**
 * formatMessageTime 把时间戳格式化成聊天窗口内的短时间。
 * 核心入参是毫秒时间戳；输出 HH:mm 文案。
 */
function formatMessageTime(timestamp: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(new Date(timestamp));
}

/**
 * 为当前消息段落提供一个轻量日期分隔线，让滚动中的消息有清晰的时间锚点。
 *
 * @param timestamp 消息创建时间的毫秒时间戳。
 * @returns 今天、昨天或本地化短日期。
 */
function formatDateDivider(timestamp: number): string {
  const messageDate = new Date(timestamp);
  const now = new Date();
  const toDayKey = (date: Date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  if (toDayKey(messageDate) === toDayKey(now)) {
    return "今天";
  }

  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (toDayKey(messageDate) === toDayKey(yesterday)) {
    return "昨天";
  }

  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric"
  }).format(messageDate);
}
