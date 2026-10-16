/**
 * isReadyEventForSession 判断 stdout 单行是否是本次 sidecar 的 ready 事件。
 *
 * @param line stdout 单行文本。
 * @param expectedIdentity 后端签发的 AEC identity。
 * @returns 当前行能否放行 start。
 */
export function isReadyEventForSession(line: string, expectedIdentity: string): boolean {
  const normalizedLine = line.trim();

  if (!normalizedLine) {
    return false;
  }

  try {
    const payload = JSON.parse(normalizedLine) as {
      event?: unknown;
      identity?: unknown;
      track?: unknown;
    };

    return (
      payload.event === "aec-sidecar-ready" &&
      payload.identity === expectedIdentity &&
      payload.track === "clean-mic"
    );
  } catch {
    /**
     * stdout 可能包含第三方库或调试日志。
     * 解析失败时静默忽略，避免无关输出让 sidecar 启动失败。
     */
    return false;
  }
}
