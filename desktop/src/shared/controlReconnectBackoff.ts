/** 控制 WebSocket 统一使用的重连退避边界。 */
export const CONTROL_RECONNECT_INITIAL_DELAY_MS = 500;
const CONTROL_RECONNECT_MAX_DELAY_MS = 10_000;
const CONTROL_RECONNECT_JITTER_RATIO = 0.2;
export const CONTROL_RECONNECT_STABLE_CONNECTION_MS = 10_000;

export type ControlReconnectBackoffOptions = {
  initialDelayMs?: number;
  maxDelayMs?: number;
  jitterRatio?: number;
  random?: () => number;
};

/**
 * 根据连续失败次数计算下一次控制 WebSocket 重连等待时间。
 *
 * @param attempt 已经发生的连续重连失败次数，从 0 开始。
 * @param options 可选退避边界和随机源，测试可注入固定随机值。
 * @returns 带有限抖动、且不超过上限的等待毫秒数。
 */
export function calculateControlReconnectDelayMs(
  attempt: number,
  options: ControlReconnectBackoffOptions = {}
): number {
  const initialDelayMs = normalizePositiveInteger(
    options.initialDelayMs,
    CONTROL_RECONNECT_INITIAL_DELAY_MS
  );
  const maxDelayMs = Math.max(
    initialDelayMs,
    normalizePositiveInteger(options.maxDelayMs, CONTROL_RECONNECT_MAX_DELAY_MS)
  );
  const jitterRatio = Math.min(
    1,
    Math.max(0, options.jitterRatio ?? CONTROL_RECONNECT_JITTER_RATIO)
  );
  const normalizedAttempt = Math.max(0, Math.floor(attempt));
  const baseDelayMs = Math.min(
    maxDelayMs,
    initialDelayMs * 2 ** Math.min(normalizedAttempt, 30)
  );
  const randomValue = Math.min(1, Math.max(0, options.random?.() ?? Math.random()));
  const jitterMultiplier = 1 + (randomValue * 2 - 1) * jitterRatio;
  return Math.round(Math.min(maxDelayMs, Math.max(0, baseDelayMs * jitterMultiplier)));
}

function normalizePositiveInteger(value: number | undefined, fallback: number): number {
  return Number.isInteger(value) && (value as number) > 0 ? (value as number) : fallback;
}
