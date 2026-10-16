import type { AecSidecarSession } from "./aecSidecarTypes";
import { stopChildForReplacement, type SidecarChild } from "./aecSidecarProcess";

export const DATA_DISCONTINUITY_RESTART_COOLDOWN_MS = 60_000;
const UNEXPECTED_EXIT_RESTART_BASE_DELAY_MS = 1_000;
const UNEXPECTED_EXIT_RESTART_MAX_DELAY_MS = 30_000;
export const UNEXPECTED_EXIT_STABLE_RUNTIME_MS = 10_000;

/**
 * getUnexpectedExitRestartDelay 根据连续崩溃次数计算下一次 sidecar 重启等待时间。
 *
 * @param attempt 已经发生的连续重启尝试次数，从零开始计算。
 * @returns 下一次重启前需要等待的毫秒数。
 */
export function getUnexpectedExitRestartDelay(attempt: number): number {
  const normalizedAttempt = Math.max(0, Math.floor(attempt));
  const exponent = Math.min(normalizedAttempt, 5);
  return Math.min(
    UNEXPECTED_EXIT_RESTART_BASE_DELAY_MS * (2 ** exponent),
    UNEXPECTED_EXIT_RESTART_MAX_DELAY_MS
  );
}

/**
 * restartCurrentSidecarAfterDataDiscontinuity 以正常替换路径重启当前 sidecar。
 *
 * @param child 被判定为采集异常的当前子进程。
 * @param session 最近一次 sidecar 启动参数。
 * @param replacementExitGraceMs 等待旧进程退出的最长时间。
 * @param canRestart 重启前的代际校验。
 * @param start supervisor 的启动入口。
 */
export async function restartCurrentSidecarAfterDataDiscontinuity(
  child: SidecarChild,
  session: AecSidecarSession,
  replacementExitGraceMs: number,
  canRestart: () => boolean,
  start: (session: AecSidecarSession) => Promise<void>
): Promise<void> {
  await stopChildForReplacement(child, replacementExitGraceMs);
  if (!canRestart()) {
    return;
  }

  await start(session);
}
