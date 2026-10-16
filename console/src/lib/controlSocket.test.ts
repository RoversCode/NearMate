import { afterEach, describe, expect, it, vi } from "vitest";

import { connectControlSocket } from "./controlSocket";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  private listeners = new Map<string, Array<(event: unknown) => void>>();

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {
    this.emit("close", {});
  }

  open(): void {
    this.emit("open", {});
  }

  closeFromServer(): void {
    this.emit("close", {});
  }

  fail(): void {
    this.emit("error", new Event("error"));
  }

  receive(message: object): void {
    this.emit("message", { data: JSON.stringify(message) });
  }

  private emit(type: string, event: unknown): void {
    this.listeners.get(type)?.forEach(listener => listener(event));
  }
}

describe("connectControlSocket", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    FakeWebSocket.instances = [];
  });

  it("断线后按 500ms 首轮退避自动重连", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const connection = connectControlSocket("http://127.0.0.1:8765", {
      onMessage: vi.fn(),
      onClose: vi.fn()
    });
    FakeWebSocket.instances[0].closeFromServer();

    vi.advanceTimersByTime(499);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    connection.close();
  });

  it("连接稳定 10 秒后会把下一次退避恢复到首轮", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const connection = connectControlSocket("http://127.0.0.1:8765", {
      onMessage: vi.fn()
    });
    const first = FakeWebSocket.instances[0];
    first.open();
    vi.advanceTimersByTime(10_000);
    first.closeFromServer();

    vi.advanceTimersByTime(499);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    connection.close();
  });

  it("连接提前断开时清除旧稳定计时器", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const connection = connectControlSocket("http://127.0.0.1:8765", {
      onMessage: vi.fn()
    });
    const first = FakeWebSocket.instances[0];
    first.open();
    first.closeFromServer();

    expect(vi.getTimerCount()).toBe(1);
    connection.close();
  });

  it("异常断开先派生 error 再派生 close 时 onClose 恰好触发一次", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const onClose = vi.fn();
    const onError = vi.fn();

    const connection = connectControlSocket("http://127.0.0.1:8765", {
      onMessage: vi.fn(),
      onClose,
      onError
    });
    const first = FakeWebSocket.instances[0];

    // 浏览器在连接异常时先派生 error 再派生 close；两个事件都只能算一次断开。
    first.fail();
    first.closeFromServer();

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);

    // 断开后仍按既定退避自动重连。
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(2);

    connection.close();
  });

  it("服务端正常关闭时 onClose 恰好触发一次", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const onClose = vi.fn();

    const connection = connectControlSocket("http://127.0.0.1:8765", {
      onMessage: vi.fn(),
      onClose
    });
    FakeWebSocket.instances[0].closeFromServer();

    expect(onClose).toHaveBeenCalledTimes(1);

    connection.close();
  });

  it("旧连接的迟到事件不能污染或关闭新连接", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const onMessage = vi.fn();
    const onOpen = vi.fn();
    const onClose = vi.fn();
    const onError = vi.fn();

    const connection = connectControlSocket("http://127.0.0.1:8765", {
      onMessage,
      onOpen,
      onClose,
      onError
    });
    const first = FakeWebSocket.instances[0];
    first.closeFromServer();
    vi.advanceTimersByTime(500);
    const second = FakeWebSocket.instances[1];
    second.open();
    vi.clearAllMocks();

    first.open();
    first.receive({ type: "settings.changed" });
    first.fail();
    first.closeFromServer();
    vi.advanceTimersByTime(60_000);

    expect(onOpen).not.toHaveBeenCalled();
    expect(onMessage).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(2);

    connection.close();
  });
});
