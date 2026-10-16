const CONTROL_RECONNECT_INITIAL_DELAY_MS = 500;
const CONTROL_RECONNECT_MAX_DELAY_MS = 10_000;
const CONTROL_RECONNECT_JITTER_RATIO = 0.2;
export const CONTROL_RECONNECT_STABLE_CONNECTION_MS = 10_000;

/**
 * 计算控制 WebSocket 下一次重连等待时间。
 *
 * @param attempt 连续失败次数，从 0 开始。
 * @returns 带抖动且不超过上限的等待毫秒数。
 */
export function calculateControlReconnectDelayMs(attempt: number): number {
  const baseDelayMs = Math.min(
    CONTROL_RECONNECT_MAX_DELAY_MS,
    CONTROL_RECONNECT_INITIAL_DELAY_MS * 2 ** Math.min(Math.max(0, Math.floor(attempt)), 30)
  );
  const jitterMultiplier = 1 + (Math.random() * 2 - 1) * CONTROL_RECONNECT_JITTER_RATIO;
  return Math.round(Math.min(
    CONTROL_RECONNECT_MAX_DELAY_MS,
    Math.max(0, baseDelayMs * jitterMultiplier)
  ));
}
