import {
  type ClipboardEvent,
  type Dispatch,
  type RefObject,
  type SetStateAction,
  useLayoutEffect,
  useRef,
  useState
} from "react";

export type ComposerPasteDisposition = "inline" | "material";

type ComposerPasteTransactionOptions = {
  draft: string;
  setDraft: Dispatch<SetStateAction<string>>;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  onPasteText?(text: string): Promise<ComposerPasteDisposition>;
  onPasteImage?(): Promise<void>;
};

type FrozenPasteInput = {
  draft: string;
  selectionStart: number;
  selectionEnd: number;
  text: string;
};

/**
 * 把一次剪贴板输入从识别到落入正文或材料架作为不可并发的事务执行。
 *
 * Args:
 *   options: 当前草稿、输入框引用及文本和图片粘贴处理端口。
 *
 * Returns:
 *   粘贴事件处理器、同步占用状态和局部错误状态。
 */
export function useComposerPasteTransaction(options: ComposerPasteTransactionOptions) {
  const occupiedRef = useRef(false);
  const pendingSelectionRef = useRef<number | null>(null);
  const [state, setState] = useState<"idle" | "resolving">("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useLayoutEffect(() => {
    const cursor = pendingSelectionRef.current;
    const textarea = options.textareaRef.current;
    if (cursor === null || textarea === null) {
      return;
    }

    pendingSelectionRef.current = null;
    textarea.focus();
    textarea.setSelectionRange(cursor, cursor);
  }, [options.draft, options.textareaRef, state]);

  function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>): void {
    const text = event.clipboardData?.getData("text/plain") ?? "";
    const handler = text ? options.onPasteText : options.onPasteImage;
    if (!handler) {
      return;
    }

    event.preventDefault();
    if (occupiedRef.current) {
      return;
    }

    const frozen: FrozenPasteInput = {
      draft: options.draft,
      selectionStart: event.currentTarget.selectionStart,
      selectionEnd: event.currentTarget.selectionEnd,
      text
    };

    // ref 在任何异步工作前同步占用，阻止同一事件循环内的重复粘贴和发送。
    occupiedRef.current = true;
    setState("resolving");
    setErrorMessage(null);
    void executePaste(frozen);
  }

  async function executePaste(frozen: FrozenPasteInput): Promise<void> {
    try {
      if (!frozen.text) {
        await options.onPasteImage?.();
        return;
      }

      const disposition = await options.onPasteText?.(frozen.text);
      if (disposition === "material") {
        return;
      }
      if (disposition !== "inline") {
        throw new Error("粘贴分类结果无效");
      }

      const nextDraft = `${frozen.draft.slice(0, frozen.selectionStart)}${frozen.text}${frozen.draft.slice(frozen.selectionEnd)}`;
      pendingSelectionRef.current = frozen.selectionStart + frozen.text.length;
      options.setDraft(nextDraft);
    } catch {
      // 事务失败时恢复冻结草稿，不能让部分物化结果或异步回调改变用户原文。
      options.setDraft(frozen.draft);
      pendingSelectionRef.current = frozen.selectionEnd;
      setErrorMessage("粘贴处理失败，请重试。");
    } finally {
      occupiedRef.current = false;
      setState("idle");
    }
  }

  return {
    isResolving: state === "resolving",
    isOccupied: () => occupiedRef.current,
    errorMessage,
    clearError(): void {
      setErrorMessage(null);
    },
    handlePaste
  };
}
