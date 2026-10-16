/**
 * 把“写入已经成功、但随后读取页面快照失败”的情况转换成明确提示。
 *
 * 配置类接口通常先完成持久化，再由控制台读取附属列表或状态。后一个读取
 * 失败时不能把已经成功的写入说成失败，否则用户很容易重复提交同一操作。
 *
 * Args:
 *   error: 快照读取阶段捕获到的异常。
 *   fallback: 异常不是 Error 时使用的稳定说明。
 *
 * Returns:
 *   可以直接展示给用户的中文提示。
 */
export function formatSavedRefreshWarning(error: unknown, fallback: string): string {
  const detail = error instanceof Error && error.message.trim().length > 0
    ? error.message
    : fallback;
  return `已保存，但页面状态刷新失败：${detail}`;
}
