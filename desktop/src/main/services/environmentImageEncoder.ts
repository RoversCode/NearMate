import createImageWorker from "./environmentImageWorker?nodeWorker";

export type EnvironmentImageEncoder = {
  encode(input: { bitmap: Buffer; width: number; height: number }): Promise<Buffer>;
  stop(): void;
};

type WorkerResponse = {
  requestId: number;
  bytes?: ArrayBuffer;
  error?: string;
};

/**
 * 创建复用同一 Worker Thread 的环境截图编码器。
 *
 * Returns:
 *   可编码原始位图并在应用退出时停止的端口。
 */
export function createEnvironmentImageEncoder(): EnvironmentImageEncoder {
  const pending = new Map<number, {
    resolve(value: Buffer): void;
    reject(error: Error): void;
  }>();
  let worker: ReturnType<typeof createImageWorker> | null = null;
  let nextRequestId = 1;
  let stopped = false;

  function ensureWorker(): ReturnType<typeof createImageWorker> {
    if (stopped) {
      throw new Error("environment image worker stopped");
    }
    if (worker !== null) {
      return worker;
    }
    worker = createImageWorker({});
    worker.on("message", (response: WorkerResponse) => {
      const request = pending.get(response.requestId);
      if (!request) {
        return;
      }
      pending.delete(response.requestId);
      if (response.error || !response.bytes) {
        request.reject(new Error(response.error || "environment image worker returned no bytes"));
        return;
      }
      request.resolve(Buffer.from(response.bytes));
    });
    worker.on("error", error => rejectAll(error));
    worker.on("exit", code => {
      worker = null;
      if (!stopped) {
        rejectAll(new Error(`environment image worker exited with code ${code}`));
      }
    });
    return worker;
  }

  function rejectAll(error: Error): void {
    for (const request of pending.values()) {
      request.reject(error);
    }
    pending.clear();
  }

  return {
    encode(input): Promise<Buffer> {
      let activeWorker: ReturnType<typeof createImageWorker>;
      try {
        activeWorker = ensureWorker();
      } catch (error) {
        return Promise.reject(error);
      }
      const requestId = nextRequestId++;
      const bitmap = new ArrayBuffer(input.bitmap.byteLength);
      new Uint8Array(bitmap).set(input.bitmap);
      return new Promise((resolve, reject) => {
        pending.set(requestId, { resolve, reject });
        try {
          activeWorker.postMessage({
            requestId,
            width: input.width,
            height: input.height,
            bitmap
          }, [bitmap]);
        } catch (error) {
          pending.delete(requestId);
          reject(error);
        }
      });
    },

    stop(): void {
      stopped = true;
      rejectAll(new Error("environment image worker stopped"));
      if (worker !== null) {
        void worker.terminate();
        worker = null;
      }
    }
  };
}
