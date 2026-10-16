/**
 * 提取稳定错误原因。
 * 核心入参是未知异常与中文兜底；输出只包含错误 message 或固定文案，避免拼接 token 等敏感上下文。
 */
export function getLiveKitRoomErrorReason(error: unknown, fallbackReason: string): string {
  return error instanceof Error && error.message ? error.message : fallbackReason;
}
