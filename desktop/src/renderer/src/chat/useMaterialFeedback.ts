import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMaterialFeedback } from "./chatMaterialFeedback";

const MATERIAL_FEEDBACK_DURATION_MS = 3000;

/**
 * 管理聊天材料反馈的替换、自动清理和卸载清理。
 *
 * Returns:
 *   当前反馈、展示反馈和主动清理函数。
 */
export function useMaterialFeedback() {
  const [feedback, setFeedback] = useState<ChatMaterialFeedback | null>(null);
  const timerRef = useRef<number | null>(null);

  const clear = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setFeedback(null);
  }, []);

  const show = useCallback((nextFeedback: ChatMaterialFeedback, autoDismiss = true) => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setFeedback(nextFeedback);
    if (!autoDismiss) {
      return;
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      setFeedback(null);
    }, MATERIAL_FEEDBACK_DURATION_MS);
  }, []);

  useEffect(() => () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  return { feedback, show, clear };
}
