import { beforeEach, describe, expect, it, vi } from "vitest";

import { createControlSettingsSocketWithConstructor } from "./controlSettingsSocket";

type ControlSettingsSocketConstructor = Parameters<typeof createControlSettingsSocketWithConstructor>[0];

const webSocketMockState = vi.hoisted(() => ({
  instances: [] as Array<{
    url: string;
    close: ReturnType<typeof vi.fn>;
    emit(type: string, payload?: unknown): void;
    addEventListener(type: string, listener: (payload?: unknown) => void): void;
  }>,
  FakeUndiciWebSocket: class FakeUndiciWebSocket {
    readonly url: string;
    readonly close = vi.fn();
    private readonly listeners = new Map<string, Array<(payload?: unknown) => void>>();

    constructor(url: string) {
      this.url = url;
      webSocketMockState.instances.push(this);
    }

    addEventListener(type: string, listener: (payload?: unknown) => void): void {
      const listeners = this.listeners.get(type) ?? [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    }

    emit(type: string, payload?: unknown): void {
      this.listeners.get(type)?.forEach((listener) => listener(payload));
    }
  }
}));

describe("createControlSettingsSocket", () => {
  beforeEach(() => {
    webSocketMockState.instances.length = 0;
  });

  it("把文本消息转交给 onMessage", () => {
    const handlers = {
      onMessage: vi.fn(),
      onClose: vi.fn(),
      onError: vi.fn()
    };

    createTestSocket(handlers);
    webSocketMockState.instances[0].emit("message", {
      data: "{\"type\":\"settings.changed\"}"
    });

    expect(handlers.onMessage).toHaveBeenCalledWith("{\"type\":\"settings.changed\"}");
  });

  it("把 Buffer 消息按 UTF-8 转成交给 onMessage", () => {
    const handlers = {
      onMessage: vi.fn(),
      onClose: vi.fn(),
      onError: vi.fn()
    };

    createTestSocket(handlers);
    webSocketMockState.instances[0].emit("message", {
      data: Buffer.from("你好", "utf8")
    });

    expect(handlers.onMessage).toHaveBeenCalledWith("你好");
  });

  it("把 ArrayBuffer 与 Buffer 数组消息统一转成交给 onMessage", () => {
    const handlers = {
      onMessage: vi.fn(),
      onClose: vi.fn(),
      onError: vi.fn()
    };
    const arrayBuffer = Uint8Array.from(Buffer.from("数组", "utf8")).buffer;

    createTestSocket(handlers);
    webSocketMockState.instances[0].emit("message", {
      data: arrayBuffer
    });

    expect(handlers.onMessage).toHaveBeenNthCalledWith(1, "数组");
  });

  it("转交 close 和 error 事件", () => {
    const error = new Error("socket failed");
    const handlers = {
      onMessage: vi.fn(),
      onClose: vi.fn(),
      onError: vi.fn()
    };

    createTestSocket(handlers);
    webSocketMockState.instances[0].emit("error", error);
    webSocketMockState.instances[0].emit("close");

    expect(handlers.onError).toHaveBeenCalledWith(error);
    expect(handlers.onClose).toHaveBeenCalledOnce();
  });

  it("close 会关闭底层 socket", () => {
    const handlers = {
      onMessage: vi.fn(),
      onClose: vi.fn(),
      onError: vi.fn()
    };

    const socket = createTestSocket(handlers);
    socket.close();

    expect(webSocketMockState.instances[0].close).toHaveBeenCalledOnce();
  });
});

function createTestSocket(handlers: {
  onMessage(message: string): void;
  onClose(): void;
  onError(error: Error): void;
}) {
  return createControlSettingsSocketWithConstructor(
    webSocketMockState.FakeUndiciWebSocket as ControlSettingsSocketConstructor,
    "ws://127.0.0.1:8765/ws/control",
    handlers
  );
}
