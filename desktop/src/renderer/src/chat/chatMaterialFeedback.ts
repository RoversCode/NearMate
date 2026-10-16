export type ChatMaterialFeedback = {
  kind: "progress" | "success" | "warning";
  text: string;
};

/**
 * 从受控 IPC 响应中读取聊天窗口可展示的材料反馈。
 *
 * Args:
 *   payload: materials:add-staged 返回的未知载荷。
 *
 * Returns:
 *   已验证的反馈；没有反馈时返回 null，协议损坏时抛错。
 */
export function readChatMaterialFeedback(payload: unknown): ChatMaterialFeedback | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("材料接纳响应无效");
  }
  const response = payload as Record<string, unknown>;
  if (response.accepted !== true) {
    throw new Error("材料接纳响应未确认成功");
  }
  if (response.feedback === null) {
    return null;
  }
  if (!response.feedback || typeof response.feedback !== "object" || Array.isArray(response.feedback)) {
    throw new Error("材料接纳反馈无效");
  }
  const feedback = response.feedback as Record<string, unknown>;
  if ((feedback.kind !== "success" && feedback.kind !== "warning") ||
      typeof feedback.text !== "string" || !feedback.text.trim()) {
    throw new Error("材料接纳反馈字段无效");
  }
  return {
    kind: feedback.kind,
    text: feedback.text.trim()
  };
}

/**
 * 校验统一反馈总线直接推送的载荷。
 *
 * @param payload 主进程广播的未知反馈对象。
 * @returns 可展示的处理中、成功或警告反馈。
 */
export function readMaterialFeedbackEvent(payload: unknown): ChatMaterialFeedback {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("材料反馈事件无效");
  }
  const feedback = payload as Record<string, unknown>;
  if ((feedback.kind !== "progress" && feedback.kind !== "success" && feedback.kind !== "warning")
    || typeof feedback.text !== "string" || !feedback.text.trim()) {
    throw new Error("材料反馈事件字段无效");
  }
  return { kind: feedback.kind, text: feedback.text.trim() };
}
