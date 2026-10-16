import { type ChangeEvent, type FormEvent, type KeyboardEvent, useRef, useState } from "react";
import { Paperclip } from "lucide-react";
import {
  type ComposerPasteDisposition,
  useComposerPasteTransaction
} from "./useComposerPasteTransaction";

export type ChatComposerAttachment = {
  kind:
    | "clipboard_text"
    | "clipboard_image"
    | "dragged_file"
    | "dragged_folder"
    | "selected_text"
    | "selected_image"
    | "selected_web";
  source: string;
  display_name: string;
  path: string;
  managed_asset_id?: string;
};

export type ChatComposerProps = {
  errorMessage: string | null;
  isSubmissionDisabled?: boolean;
  onPasteText?(text: string): Promise<ComposerPasteDisposition>;
  onPasteImage?(): Promise<void>;
  onSelectFiles?(files: File[]): Promise<void>;
  onFocus?(): void;
  onSubmit(input: { messageText: string }): Promise<void>;
};

/**
 * ChatComposer 渲染聊天窗口底部输入区。
 * 核心入参是提交状态、错误文案和异步提交回调；输出为可换行输入和发送按钮。
 */
export function ChatComposer({
  errorMessage,
  isSubmissionDisabled = false,
  onPasteText,
  onPasteImage,
  onSelectFiles,
  onFocus,
  onSubmit
}: ChatComposerProps) {
  const [draft, setDraft] = useState("");
  const [localIsSubmitting, setLocalIsSubmitting] = useState(false);
  const submitInFlightRef = useRef(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const pasteTransaction = useComposerPasteTransaction({
    draft,
    setDraft,
    textareaRef,
    onPasteText,
    onPasteImage
  });
  const trimmedDraft = draft.trim();
  const isSubmitLocked = localIsSubmitting || pasteTransaction.isResolving;
  const canSubmit = !isSubmissionDisabled && !isSubmitLocked && trimmedDraft.length > 0;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (!canSubmit || submitInFlightRef.current || pasteTransaction.isOccupied()) {
      return;
    }

    submitInFlightRef.current = true;
    setLocalIsSubmitting(true);

    try {
      await onSubmit({ messageText: draft.trim() });
      setDraft("");
      if (textareaRef.current) {
        textareaRef.current.style.height = "44px";
      }
    } catch {
      /**
       * 失败文案由 runtime 统一提供。
       * 这里保留草稿，方便用户在错误提示下直接重试。
       */
    } finally {
      submitInFlightRef.current = false;
      setLocalIsSubmitting(false);
    }
  }

  /**
   * handleComposerKeyDown 让 Enter 符合聊天软件直觉：直接发送；Shift+Enter 留给换行。
   * 提交仍复用表单 submit 逻辑，保证按钮发送和键盘发送共享同一套锁与错误处理。
   */
  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }

    event.preventDefault();
    if (pasteTransaction.isOccupied()) {
      return;
    }
    event.currentTarget.form?.requestSubmit();
  }

  async function handleFileSelection(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";
    if (files.length === 0 || !onSelectFiles) {
      return;
    }
    await onSelectFiles(files);
  }

  return (
    <form className="chat-composer" onSubmit={handleSubmit}>
      <div className="chat-composer-input-row">
        <div className="chat-composer-tools" aria-label="输入材料操作">
          <button
            type="button"
            className="chat-composer-icon-button"
            aria-label="添加文件"
            title="添加文件"
            disabled={localIsSubmitting || !onSelectFiles}
            onClick={() => fileInputRef.current?.click()}
          >
            <Paperclip size={21} strokeWidth={1.8} aria-hidden="true" />
          </button>
        </div>
        <textarea
          ref={textareaRef}
          value={draft}
          disabled={localIsSubmitting}
          readOnly={pasteTransaction.isResolving}
          rows={1}
          placeholder="输入消息..."
          onFocus={onFocus}
          onInput={event => {
            pasteTransaction.clearError();
            setDraft(event.currentTarget.value);
            event.currentTarget.style.height = "auto";
            event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 112)}px`;
          }}
          onKeyDown={handleComposerKeyDown}
          onPaste={pasteTransaction.handlePaste}
        />
        <input
          ref={fileInputRef}
          className="chat-composer-file-input"
          type="file"
          multiple
          tabIndex={-1}
          aria-hidden="true"
          onChange={event => void handleFileSelection(event)}
        />
      </div>
      <div className="chat-composer-footer">
        {pasteTransaction.errorMessage || errorMessage ? (
          <p className="chat-composer-error" role="alert">
            {pasteTransaction.errorMessage ?? errorMessage}
          </p>
        ) : (
          <span />
        )}
        <button type="submit" disabled={!canSubmit}>
          {pasteTransaction.isResolving ? "处理中" : isSubmitLocked ? "发送中" : "发送"}
        </button>
      </div>
    </form>
  );
}
