import { performance } from "node:perf_hooks";
import type { CapturedEnvironmentSnapshot, EnvironmentCaptureService } from "./contextCapture";
import type { EnvironmentContextClient, EnvironmentSnapshotMetadata } from "./environmentContextClient";

const TEXT_SNAPSHOT_FRESHNESS_MS = 10_000;
const TEXT_SNAPSHOT_OPERATION_TIMEOUT_MS = 500;

export type EnvironmentSnapshotService = {
  prepareTextSnapshot(): void;
  uploadTextSnapshot(): Promise<string | null>;
  captureVoiceSnapshot(metadata: EnvironmentSnapshotMetadata): Promise<void>;
  stop(): void;
};

type EnvironmentSnapshotServiceOptions = {
  capture: EnvironmentCaptureService;
  client: EnvironmentContextClient;
  freshnessMs?: number;
  textOperationTimeoutMs?: number;
  now?: () => number;
  logger?: Pick<Console, "info" | "warn">;
};

/**
 * 创建事件驱动的内存环境快照服务。
 *
 * Args:
 *   options: 单次采集器、上传客户端、新鲜度窗口、时间源与日志入口。
 *
 * Returns:
 *   支持文本预采集、提交冻结和语音 turn 上传的服务。
 */
export function createEnvironmentSnapshotService(
  options: EnvironmentSnapshotServiceOptions
): EnvironmentSnapshotService {
  const freshnessMs = options.freshnessMs ?? TEXT_SNAPSHOT_FRESHNESS_MS;
  const textOperationTimeoutMs = options.textOperationTimeoutMs ?? TEXT_SNAPSHOT_OPERATION_TIMEOUT_MS;
  const now = options.now ?? (() => Date.now());
  const logger = options.logger ?? console;
  let latestSnapshot: CapturedEnvironmentSnapshot | null = null;
  let latestCompletedAt: number | null = null;
  let captureInFlight: Promise<CapturedEnvironmentSnapshot> | null = null;
  let stopped = false;

  /** 共享当前进行中的采集，并在成功后更新文本缓存。 */
  function captureSingleFlight(): Promise<CapturedEnvironmentSnapshot> {
    if (captureInFlight !== null) {
      return captureInFlight;
    }
    captureInFlight = options.capture.captureSnapshot()
      .then(snapshot => {
        if (!stopped) {
          latestSnapshot = snapshot;
          latestCompletedAt = now();
        }
        logger.info("[companion-main] environment snapshot captured", snapshot.timingsMs);
        return snapshot;
      })
      .finally(() => {
        captureInFlight = null;
      });
    return captureInFlight;
  }

  /** 返回仍处于新鲜度窗口内且包含图片的文本预采集结果。 */
  function reusableTextSnapshot(): CapturedEnvironmentSnapshot | null {
    if (!latestSnapshot?.screenshotBytes || latestCompletedAt === null) {
      return null;
    }
    return now() - latestCompletedAt <= freshnessMs ? latestSnapshot : null;
  }

  async function upload(
    snapshot: CapturedEnvironmentSnapshot,
    metadata: EnvironmentSnapshotMetadata,
    signal?: AbortSignal
  ): Promise<string | null> {
    if (stopped) {
      return null;
    }
    const startedAt = performance.now();
    try {
      const snapshotId = await options.client.upload(snapshot, metadata, signal);
      const httpUploadMs = performance.now() - startedAt;
      logger.info("[companion-main] environment snapshot uploaded", {
        reason: metadata.reason,
        httpUploadMs,
        totalElapsedMs: (snapshot.timingsMs.total ?? 0) + httpUploadMs,
        bytes: snapshot.screenshotBytes?.byteLength ?? 0
      });
      return snapshotId;
    } catch (error) {
      logger.warn("[companion-main] environment snapshot upload failed", metadata.reason, error);
      return null;
    }
  }

  return {
    prepareTextSnapshot(): void {
      if (stopped) {
        return;
      }
      void captureSingleFlight().catch(error => {
        logger.warn("[companion-main] environment text preparation failed", error);
      });
    },

    async uploadTextSnapshot(): Promise<string | null> {
      if (stopped) {
        return null;
      }
      const abortController = new AbortController();
      const timeout = setTimeout(() => abortController.abort(), textOperationTimeoutMs);
      try {
        const snapshot = reusableTextSnapshot()
          ?? await waitForAbortableTask(captureSingleFlight(), abortController.signal);
        if (stopped || abortController.signal.aborted) {
          return null;
        }
        return await waitForAbortableTask(
          upload(snapshot, { reason: "chat_submit" }, abortController.signal),
          abortController.signal
        );
      } catch (error) {
        logger.warn("[companion-main] environment text submission degraded", error);
        return null;
      } finally {
        clearTimeout(timeout);
      }
    },

    async captureVoiceSnapshot(metadata): Promise<void> {
      if (stopped) {
        return;
      }
      try {
        const snapshot = await captureSingleFlight();
        if (!stopped) {
          await upload(snapshot, metadata);
        }
      } catch (error) {
        logger.warn("[companion-main] environment voice capture failed", error);
      }
    },

    stop(): void {
      stopped = true;
      latestSnapshot = null;
      latestCompletedAt = null;
    }
  };
}

/**
 * 让不可取消的截图任务在提交 deadline 到达时立即向调用方降级。
 *
 * Args:
 *   task: 仍可在后台自然完成并更新缓存的采集任务。
 *   signal: 文本现场操作的统一取消信号。
 *
 * Returns:
 *   deadline 内完成的任务结果。
 */
function waitForAbortableTask<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) {
    return Promise.reject(new Error("environment_text_operation_timeout"));
  }
  return new Promise<T>((resolve, reject) => {
    const handleAbort = () => reject(new Error("environment_text_operation_timeout"));
    signal.addEventListener("abort", handleAbort, { once: true });
    task.then(
      value => {
        signal.removeEventListener("abort", handleAbort);
        resolve(value);
      },
      error => {
        signal.removeEventListener("abort", handleAbort);
        reject(error);
      }
    );
  });
}
