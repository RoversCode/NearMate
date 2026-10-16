import { describe, expect, it, vi } from "vitest";
import type { CapturedEnvironmentSnapshot } from "./contextCapture";
import { createEnvironmentSnapshotService } from "./environmentSnapshotService";

describe("createEnvironmentSnapshotService", () => {
  it("聚焦只后台预采集且提交复用 10 秒内成功快照", async () => {
    let nowMs = 0;
    const captureSnapshot = vi.fn(async () => makeSnapshot("focus"));
    const upload = vi.fn(async () => "snapshot-1");
    const service = createService({ captureSnapshot, upload, now: () => nowMs });

    service.prepareTextSnapshot();
    await flushPromises();
    nowMs = 9_999;
    const snapshotId = await service.uploadTextSnapshot();

    expect(snapshotId).toBe("snapshot-1");
    expect(captureSnapshot).toHaveBeenCalledOnce();
    expect(upload).toHaveBeenCalledWith(
      makeSnapshot("focus"),
      { reason: "chat_submit" },
      expect.any(AbortSignal)
    );
  });

  it("超过 10 秒或预采集缺少截图时重新采集", async () => {
    let nowMs = 0;
    const captureSnapshot = vi
      .fn()
      .mockResolvedValueOnce(makeSnapshot("focus"))
      .mockResolvedValueOnce(makeSnapshot("submit"));
    const upload = vi.fn(async () => "snapshot-2");
    const service = createService({ captureSnapshot, upload, now: () => nowMs });

    service.prepareTextSnapshot();
    await flushPromises();
    nowMs = 10_001;
    await service.uploadTextSnapshot();

    expect(captureSnapshot).toHaveBeenCalledTimes(2);
    expect(upload).toHaveBeenCalledWith(
      makeSnapshot("submit"),
      { reason: "chat_submit" },
      expect.any(AbortSignal)
    );
  });

  it("聚焦和语音并发时共享同一次屏幕采集并保留语音元数据", async () => {
    const deferred = createDeferred<CapturedEnvironmentSnapshot>();
    const captureSnapshot = vi.fn(() => deferred.promise);
    const upload = vi.fn(async () => "voice-snapshot");
    const service = createService({ captureSnapshot, upload });

    service.prepareTextSnapshot();
    const voiceTask = service.captureVoiceSnapshot({
      request_id: "request-1",
      turn_id: "turn-1",
      reason: "voice_turn_started"
    });
    expect(captureSnapshot).toHaveBeenCalledOnce();
    deferred.resolve(makeSnapshot("shared"));
    await voiceTask;

    expect(upload).toHaveBeenCalledWith(makeSnapshot("shared"), {
      request_id: "request-1",
      turn_id: "turn-1",
      reason: "voice_turn_started"
    }, undefined);
  });

  it("上传失败返回 null 而不阻止文本提交调用方", async () => {
    const service = createService({
      captureSnapshot: vi.fn(async () => makeSnapshot("failed")),
      upload: vi.fn(async () => { throw new Error("backend unavailable"); })
    });

    await expect(service.uploadTextSnapshot()).resolves.toBeNull();
  });

  it("停止后丢弃迟到采集且不再上传", async () => {
    const deferred = createDeferred<CapturedEnvironmentSnapshot>();
    const upload = vi.fn(async () => "snapshot-late");
    const service = createService({
      captureSnapshot: vi.fn(() => deferred.promise),
      upload
    });

    const voiceTask = service.captureVoiceSnapshot({
      request_id: "request-late",
      turn_id: "turn-late",
      reason: "voice_turn_started"
    });
    service.stop();
    deferred.resolve(makeSnapshot("late"));
    await voiceTask;

    expect(upload).not.toHaveBeenCalled();
    await expect(service.uploadTextSnapshot()).resolves.toBeNull();
  });

  it("采集或上传挂起时在 deadline 后降级为 null", async () => {
    const captureHungService = createService({
      captureSnapshot: vi.fn(() => new Promise<CapturedEnvironmentSnapshot>(() => undefined)),
      upload: vi.fn(async () => "unused"),
      textOperationTimeoutMs: 5
    });
    await expect(captureHungService.uploadTextSnapshot()).resolves.toBeNull();

    const uploadHungService = createService({
      captureSnapshot: vi.fn(async () => makeSnapshot("captured")),
      upload: vi.fn(() => new Promise<string>(() => undefined)),
      textOperationTimeoutMs: 5
    });
    await expect(uploadHungService.uploadTextSnapshot()).resolves.toBeNull();
  });
});

function createService(input: {
  captureSnapshot: () => Promise<CapturedEnvironmentSnapshot>;
  upload: (
    snapshot: CapturedEnvironmentSnapshot,
    metadata: never,
    signal?: AbortSignal
  ) => Promise<string>;
  now?: () => number;
  textOperationTimeoutMs?: number;
}) {
  return createEnvironmentSnapshotService({
    capture: { captureSnapshot: input.captureSnapshot },
    client: { upload: input.upload },
    now: input.now,
    textOperationTimeoutMs: input.textOperationTimeoutMs,
    logger: { info: vi.fn(), warn: vi.fn() }
  });
}

function makeSnapshot(label: string): CapturedEnvironmentSnapshot {
  return {
    capturedAt: label,
    activeWindowTitle: "Code",
    screenshotBytes: Buffer.from(label),
    captureError: null,
    timingsMs: {}
  };
}

async function flushPromises(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(innerResolve => {
    resolve = innerResolve;
  });
  return { promise, resolve };
}
