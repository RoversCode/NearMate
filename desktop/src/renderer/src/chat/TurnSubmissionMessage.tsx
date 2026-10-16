import { useEffect, useState, type ReactNode } from "react";
import {
  Check,
  CircleAlert,
  Clock3,
  Pause,
  Pencil,
  Play,
  RotateCcw,
  Trash2,
  X
} from "lucide-react";
import { LinkifiedText } from "../components/LinkifiedText";
import type { ChatMessage } from "./chatReducer";

type TurnSubmissionMessageProps = {
  message: ChatMessage;
  onEdit?(submissionId: string, messageText: string): Promise<void>;
  onDelete?(submissionId: string): Promise<void>;
  onResume?(submissionId: string): Promise<void>;
  onRetry?(submissionId: string): Promise<void>;
};

type SubmissionOperation = "edit" | "delete" | "resume" | "retry";

const SUBMISSION_STATUS_LABELS = {
  preparing: "正在准备",
  queued: "等待发送",
  paused: "语音输入优先，已暂停",
  failed: "发送准备失败",
  awaiting_final: "正在确认发送"
} as const;

/**
 * 渲染一条尚未正式派发的用户文本。
 *
 * Args:
 *   props: 待发消息及 Backend 队列命令回调。
 *
 * Returns:
 *   带状态、原位编辑和恢复操作的用户气泡。
 */
export function TurnSubmissionMessage({
  message,
  onEdit,
  onDelete,
  onResume,
  onRetry
}: TurnSubmissionMessageProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editingText, setEditingText] = useState(message.text);
  const [activeOperation, setActiveOperation] = useState<SubmissionOperation | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const isAcknowledged = message.submissionAcknowledged === true;
  const trimmedEditingText = editingText.trim();
  const submissionId = message.submissionId;
  const submissionState = message.submissionState;

  useEffect(() => {
    if (!isEditing) {
      setEditingText(message.text);
    }
  }, [isEditing, message.text]);

  /**
   * 执行单条队列命令，并把瞬时失败留在当前气泡内。
   *
   * Args:
   *   operation: 当前操作类型，用于锁定对应按钮和编辑状态。
   *   callback: 调用 Backend 队列命令的异步函数。
   *
   * Returns:
   *   命令结束且本地操作状态完成收口后的 Promise。
   */
  async function runOperation(
    operation: SubmissionOperation,
    callback: (() => Promise<void>) | undefined
  ): Promise<void> {
    if (!callback || activeOperation !== null) {
      return;
    }
    setActiveOperation(operation);
    setOperationError(null);
    try {
      await callback();
      if (operation === "edit") {
        setIsEditing(false);
      }
    } catch {
      setOperationError("操作失败，请重试。");
    } finally {
      setActiveOperation(null);
    }
  }

  if (!submissionId || !submissionState) {
    return null;
  }

  const StatusIcon = submissionState === "failed"
    ? CircleAlert
    : submissionState === "paused"
      ? Pause
      : Clock3;
  const statusText = submissionState === "failed" && message.submissionError
    ? message.submissionError
    : SUBMISSION_STATUS_LABELS[submissionState];

  return (
    <div className="chat-turn-submission" data-state={submissionState}>
      {isEditing ? (
        <div className="chat-turn-submission-editor">
          <textarea
            aria-label="编辑待发文本"
            value={editingText}
            disabled={activeOperation !== null}
            rows={2}
            onChange={event => setEditingText(event.currentTarget.value)}
          />
          <div className="chat-turn-submission-editor-actions">
            <IconButton
              label="保存修改"
              disabled={!trimmedEditingText || activeOperation !== null}
              onClick={() => void runOperation(
                "edit",
                onEdit
                  ? () => onEdit(submissionId, trimmedEditingText)
                  : undefined
              )}
            >
              <Check size={15} aria-hidden="true" />
            </IconButton>
            <IconButton
              label="取消编辑"
              disabled={activeOperation !== null}
              onClick={() => {
                setEditingText(message.text);
                setIsEditing(false);
                setOperationError(null);
              }}
            >
              <X size={15} aria-hidden="true" />
            </IconButton>
          </div>
        </div>
      ) : (
        <p className="chat-bubble"><LinkifiedText text={message.text} /></p>
      )}

      <div className="chat-turn-submission-footer">
        <span className="chat-turn-submission-status" role="status">
          <StatusIcon size={13} aria-hidden="true" />
          <span>{statusText}</span>
        </span>
        {isAcknowledged && submissionState !== "awaiting_final" && !isEditing ? (
          <div className="chat-turn-submission-actions">
            {submissionState === "paused" ? (
              <IconButton
                label="恢复发送"
                disabled={activeOperation !== null}
                onClick={() => void runOperation(
                  "resume",
                  onResume ? () => onResume(submissionId) : undefined
                )}
              >
                <Play size={14} aria-hidden="true" />
              </IconButton>
            ) : null}
            {submissionState === "failed" ? (
              <IconButton
                label="重试发送"
                disabled={activeOperation !== null}
                onClick={() => void runOperation(
                  "retry",
                  onRetry ? () => onRetry(submissionId) : undefined
                )}
              >
                <RotateCcw size={14} aria-hidden="true" />
              </IconButton>
            ) : null}
            <IconButton
              label="编辑待发文本"
              disabled={activeOperation !== null}
              onClick={() => {
                setOperationError(null);
                setIsEditing(true);
              }}
            >
              <Pencil size={14} aria-hidden="true" />
            </IconButton>
            <IconButton
              label="删除待发文本"
              disabled={activeOperation !== null}
              tone="danger"
              onClick={() => void runOperation(
                "delete",
                onDelete ? () => onDelete(submissionId) : undefined
              )}
            >
              <Trash2 size={14} aria-hidden="true" />
            </IconButton>
          </div>
        ) : null}
      </div>
      {operationError ? (
        <p className="chat-turn-submission-error" role="alert">{operationError}</p>
      ) : null}
    </div>
  );
}

/**
 * 渲染统一尺寸的队列图标按钮。
 *
 * Args:
 *   props: 无障碍名称、禁用状态、颜色语义、点击动作和图标内容。
 *
 * Returns:
 *   带原生 tooltip 和稳定尺寸的按钮。
 */
function IconButton({
  label,
  disabled,
  tone = "default",
  onClick,
  children
}: {
  label: string;
  disabled: boolean;
  tone?: "default" | "danger";
  onClick(): void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="chat-turn-submission-icon-button"
      data-tone={tone}
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
