import { beforeEach, describe, expect, it, vi } from "vitest";

describe("createRuntimeContextEventWatcher", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("环境刷新请求触发环境缓存后台更新", async () => {
    const { createRuntimeContextEventWatcher } = await import("./runtimeContextEventWatcher");
    const handlersRef: Array<{ onMessage(message: string): void; onClose(): void; onError(error: Error): void }> = [];
    const captureVoiceSnapshot = vi.fn(async () => undefined);
    const watcher = createRuntimeContextEventWatcher({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: (_url, handlers) => {
        handlersRef.push(handlers);
        return { close: vi.fn() };
      },
      environmentSnapshot: { captureVoiceSnapshot },
      managedAssetStore: { release: vi.fn() },
      materialFeedback: { notifyMaterialPreflightFailures: vi.fn() },
      logger: { warn: vi.fn() }
    });

    await watcher.start();
    handlersRef[0].onMessage(JSON.stringify({
      type: "environment.snapshot_requested",
      payload: { request_id: "request-1", turn_id: "turn-1", reason: "voice_turn_started" }
    }));

    expect(captureVoiceSnapshot).toHaveBeenCalledWith({
      request_id: "request-1",
      turn_id: "turn-1",
      reason: "voice_turn_started"
    });
  });

  it("连接提前断开时清除旧稳定计时器", async () => {
    const { createRuntimeContextEventWatcher } = await import("./runtimeContextEventWatcher");
    const handlersRef: Array<{
      onOpen?(): void;
      onClose(): void;
    }> = [];
    const watcher = createRuntimeContextEventWatcher({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: (_url, handlers) => {
        handlersRef.push(handlers);
        return { close: vi.fn() };
      },
      environmentSnapshot: { captureVoiceSnapshot: vi.fn() },
      managedAssetStore: { release: vi.fn() },
      materialFeedback: { notifyMaterialPreflightFailures: vi.fn() },
      random: () => 0.5,
      logger: { warn: vi.fn() }
    });

    await watcher.start();
    handlersRef[0].onOpen?.();
    handlersRef[0].onClose();

    expect(vi.getTimerCount()).toBe(1);
    watcher.stop();
  });

  it("旧连接迟到关闭不能关闭替代连接或继续创建连接", async () => {
    const { createRuntimeContextEventWatcher } = await import("./runtimeContextEventWatcher");
    const sockets: Array<{
      close: ReturnType<typeof vi.fn>;
      handlers: {
        onClose(): void;
      };
    }> = [];
    const watcher = createRuntimeContextEventWatcher({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: (_url, handlers) => {
        const socket = { close: vi.fn(), handlers };
        sockets.push(socket);
        return { close: socket.close };
      },
      environmentSnapshot: { captureVoiceSnapshot: vi.fn() },
      managedAssetStore: { release: vi.fn() },
      materialFeedback: { notifyMaterialPreflightFailures: vi.fn() },
      reconnectDelayMs: 1000,
      random: () => 0.5,
      logger: { warn: vi.fn() }
    });

    await watcher.start();
    sockets[0].handlers.onClose();
    await vi.advanceTimersByTimeAsync(1000);
    sockets[0].handlers.onClose();

    expect(sockets).toHaveLength(2);
    expect(sockets[1].close).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    watcher.stop();
  });

  it("受管资产释放事件只按资产编号调用仓储", async () => {
    const { createRuntimeContextEventWatcher } = await import("./runtimeContextEventWatcher");
    const release = vi.fn(async () => true);
    const handlersRef: Array<{ onMessage(message: string): void }> = [];
    const watcher = createRuntimeContextEventWatcher({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: (_url, handlers) => {
        handlersRef.push(handlers);
        return { close: vi.fn() };
      },
      environmentSnapshot: { captureVoiceSnapshot: vi.fn() },
      managedAssetStore: { release },
      materialFeedback: { notifyMaterialPreflightFailures: vi.fn() },
      logger: { warn: vi.fn() }
    });

    await watcher.start();
    handlersRef[0].onMessage(JSON.stringify({
      type: "materials.assets_released",
      payload: { managed_asset_ids: ["asset-1", "asset-2", 3] }
    }));
    await Promise.resolve();

    expect(release).toHaveBeenCalledTimes(2);
    expect(release).toHaveBeenNthCalledWith(1, "asset-1");
    expect(release).toHaveBeenNthCalledWith(2, "asset-2");
  });

  it("异步预检失败通过统一反馈总线提示并过滤材料", async () => {
    const { createRuntimeContextEventWatcher } = await import("./runtimeContextEventWatcher");
    const notifyMaterialPreflightFailures = vi.fn();
    const handlersRef: Array<{ onMessage(message: string): void }> = [];
    const watcher = createRuntimeContextEventWatcher({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: (_url, handlers) => {
        handlersRef.push(handlers);
        return { close: vi.fn() };
      },
      environmentSnapshot: { captureVoiceSnapshot: vi.fn() },
      managedAssetStore: { release: vi.fn() },
      materialFeedback: { notifyMaterialPreflightFailures },
      logger: { warn: vi.fn() }
    });

    await watcher.start();
    handlersRef[0].onMessage(JSON.stringify({
      type: "materials.preflight_failed",
      payload: { items: [{ material_id: "material-1", display_name: "损坏.pdf", reason: "unreadable_file" }] }
    }));

    expect(notifyMaterialPreflightFailures).toHaveBeenCalledWith([{
      materialId: "material-1",
      displayName: "损坏.pdf",
      reason: "unreadable_file"
    }]);
  });
});
