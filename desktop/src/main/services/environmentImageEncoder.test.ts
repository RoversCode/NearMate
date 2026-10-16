import { beforeEach, describe, expect, it, vi } from "vitest";
import { createEnvironmentImageEncoder } from "./environmentImageEncoder";

const workerMock = vi.hoisted(() => {
  class TestWorker {
    private readonly listeners = new Map<string, Array<(value: unknown) => void>>();

    postMessage = vi.fn((request: { requestId: number }) => {
      const bytes = Uint8Array.from([137, 80, 78, 71]).buffer;
      this.emit("message", { requestId: request.requestId, bytes });
    });
    terminate = vi.fn(async () => 0);

    on(event: string, listener: (value: never) => void): this {
      const listeners = this.listeners.get(event) ?? [];
      listeners.push(listener as (value: unknown) => void);
      this.listeners.set(event, listeners);
      return this;
    }

    private emit(event: string, value: unknown): void {
      for (const listener of this.listeners.get(event) ?? []) {
        listener(value);
      }
    }
  }

  return {
    create: vi.fn(() => new TestWorker())
  };
});

vi.mock("./environmentImageWorker?nodeWorker", () => ({
  default: workerMock.create
}));

describe("createEnvironmentImageEncoder", () => {
  beforeEach(() => {
    workerMock.create.mockClear();
  });

  it("首次编码时才创建 Worker 并返回 PNG 字节", async () => {
    const encoder = createEnvironmentImageEncoder();

    expect(workerMock.create).not.toHaveBeenCalled();
    await expect(encoder.encode({
      bitmap: Buffer.from([0, 0, 255, 255]),
      width: 1,
      height: 1
    })).resolves.toEqual(Buffer.from([137, 80, 78, 71]));
    expect(workerMock.create).toHaveBeenCalledOnce();

    encoder.stop();
    await expect(encoder.encode({
      bitmap: Buffer.alloc(4),
      width: 1,
      height: 1
    })).rejects.toThrow("environment image worker stopped");
  });
});
