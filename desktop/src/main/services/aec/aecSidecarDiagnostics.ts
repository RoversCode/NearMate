import type { AecSidecarDiagnostic } from "./aecSidecarTypes";
import type { SidecarChild } from "./aecSidecarProcess";

const DATA_DISCONTINUITY_SUMMARY_MS = 10_000;
const DATA_DISCONTINUITY_RESTART_WINDOW_MS = 30_000;
const DATA_DISCONTINUITY_RESTART_THRESHOLD = 100;

type DataDiscontinuityState = {
  warningTimestamps: number[];
  summaryWindowStartedAt: number | null;
  suppressedWarnings: number;
  restartRequested: boolean;
};

/**
 * attachSidecarStderrDiagnostics 把 sidecar stderr 行转换为脱敏诊断事件。
 *
 * @param child 当前 sidecar 子进程。
 * @param token 当前 session token，用于日志脱敏。
 * @param hasPublishedReady ready 状态读取器。
 * @param onDiagnostic 诊断回调。
 * @param onDataDiscontinuityThresholdExceeded 断流 warning 达到重启阈值后的回调。
 */
export function attachSidecarStderrDiagnostics(
  child: SidecarChild,
  token: string,
  hasPublishedReady: () => boolean,
  onDiagnostic: ((diagnostic: AecSidecarDiagnostic) => void) | undefined,
  onDataDiscontinuityThresholdExceeded: (() => void) | undefined = undefined
): void {
  if (!child.stderr) {
    return;
  }

  let stderrBuffer = "";
  const dataDiscontinuityState = createDataDiscontinuityState();

  child.stderr.on("data", chunk => {
    stderrBuffer += chunk.toString();
    const lines = stderrBuffer.split(/\r?\n/);
    stderrBuffer = lines.pop() ?? "";

    for (const line of lines) {
      const message = line.trim();
      if (!message) {
        continue;
      }

      const phase = hasPublishedReady() ? "runtime" : "start";
      if (isDataDiscontinuityWarning(message)) {
        handleDataDiscontinuityWarning({
          state: dataDiscontinuityState,
          phase,
          onDiagnostic,
          onThresholdExceeded: onDataDiscontinuityThresholdExceeded,
          nowMs: Date.now()
        });
        continue;
      }

      emitDiagnostic(onDiagnostic, {
        event: "aec-sidecar-error",
        phase,
        message: readSanitizedErrorMessage(message, token)
      });
    }
  });
}

/**
 * emitSidecarOutputDiagnostic 把 sidecar 普通 stdout 行输出为脱敏诊断。
 *
 * @param line stdout 单行输出。
 * @param token 当前 session token。
 * @param phase 当前 sidecar 阶段。
 * @param onDiagnostic 诊断回调。
 */
export function emitSidecarOutputDiagnostic(
  line: string,
  token: string,
  phase: "start" | "runtime",
  onDiagnostic: ((diagnostic: AecSidecarDiagnostic) => void) | undefined
): void {
  const message = line.trim();
  if (!message) {
    return;
  }

  emitDiagnostic(onDiagnostic, {
    event: "aec-sidecar-output",
    phase,
    message: readSanitizedErrorMessage(message, token)
  });
}

/**
 * readSanitizedErrorMessage 从未知错误对象中提取可写入诊断的安全 message。
 *
 * @param error Node child_process error 事件或 stderr 文本。
 * @param token 当前 session token。
 * @returns 不包含 token、spawnargs 或 stack 的诊断文本。
 */
export function readSanitizedErrorMessage(error: unknown, token: string): string {
  const rawMessage =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "AEC sidecar process error.";

  if (!token) {
    return rawMessage;
  }

  return rawMessage.split(token).join("[redacted-token]");
}

/**
 * emitDiagnostic 输出 sidecar 生命周期诊断。
 *
 * @param onDiagnostic 可选外部诊断回调。
 * @param diagnostic 不含敏感字段的诊断对象。
 */
export function emitDiagnostic(
  onDiagnostic: ((diagnostic: AecSidecarDiagnostic) => void) | undefined,
  diagnostic: AecSidecarDiagnostic
): void {
  if (onDiagnostic) {
    onDiagnostic(diagnostic);
    return;
  }

  console.info("[companion-main] aec-sidecar diagnostic", diagnostic);
}

/**
 * createDataDiscontinuityState 创建单个 child 的断流 warning 统计状态。
 *
 * @returns 独立于其他 child 的录音断流统计容器。
 */
function createDataDiscontinuityState(): DataDiscontinuityState {
  return {
    warningTimestamps: [],
    summaryWindowStartedAt: null,
    suppressedWarnings: 0,
    restartRequested: false
  };
}

/**
 * handleDataDiscontinuityWarning 处理 soundcard 录音断流 warning 的分级、节流和阈值检测。
 *
 * @param input 当前 warning 对应的状态、阶段、诊断回调、阈值回调和时间戳。
 */
function handleDataDiscontinuityWarning(input: {
  state: DataDiscontinuityState;
  phase: "start" | "runtime";
  onDiagnostic: ((diagnostic: AecSidecarDiagnostic) => void) | undefined;
  onThresholdExceeded: (() => void) | undefined;
  nowMs: number;
}): void {
  const { state, phase, onDiagnostic, onThresholdExceeded, nowMs } = input;

  state.warningTimestamps = state.warningTimestamps
    .filter(timestamp => nowMs - timestamp < DATA_DISCONTINUITY_RESTART_WINDOW_MS);
  state.warningTimestamps.push(nowMs);

  if (state.summaryWindowStartedAt === null) {
    state.summaryWindowStartedAt = nowMs;
    emitDiagnostic(onDiagnostic, {
      event: "aec-sidecar-warning",
      phase,
      kind: "data_discontinuity",
      message: "Soundcard recording data discontinuity detected."
    });
  } else if (nowMs - state.summaryWindowStartedAt >= DATA_DISCONTINUITY_SUMMARY_MS) {
    state.suppressedWarnings += 1;
    emitDiagnostic(onDiagnostic, {
      event: "aec-sidecar-warning",
      phase,
      kind: "data_discontinuity",
      message: `Soundcard recording data discontinuity repeated ${state.suppressedWarnings} times in the last 10 seconds.`
    });
    state.summaryWindowStartedAt = nowMs;
    state.suppressedWarnings = 0;
  } else {
    state.suppressedWarnings += 1;
  }

  if (
    phase !== "runtime" ||
    state.restartRequested ||
    state.warningTimestamps.length < DATA_DISCONTINUITY_RESTART_THRESHOLD
  ) {
    return;
  }

  state.restartRequested = true;
  onThresholdExceeded?.();
}

/**
 * isDataDiscontinuityWarning 判断 stderr 行是否是 soundcard 录音断流 warning。
 *
 * @param message sidecar stderr 的单行文本。
 * @returns 匹配已知 warning 文本时返回 true。
 */
function isDataDiscontinuityWarning(message: string): boolean {
  return (
    message.includes("SoundcardRuntimeWarning: data discontinuity in recording") ||
    message.includes("warnings.warn(\"data discontinuity in recording\", SoundcardRuntimeWarning)")
  );
}
