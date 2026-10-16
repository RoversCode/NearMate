import { createRequire } from "node:module";

type UndiciModule = typeof import("undici");

const loadNodePackage = createRequire(import.meta.url);

export type ControlSettingsSocketHandlers = {
  onMessage(message: string): void;
  onOpen?(): void;
  onClose(): void;
  onError(error: Error): void;
};

export type ControlSettingsSocket = {
  close(): void;
};

type ControlSettingsWebSocket = {
  addEventListener(type: "message", listener: (event: { data: unknown }) => void): void;
  addEventListener(type: "open", listener: () => void): void;
  addEventListener(type: "close", listener: () => void): void;
  addEventListener(type: "error", listener: (event: Event) => void): void;
  close(): void;
};

type ControlSettingsWebSocketConstructor = new (url: string) => ControlSettingsWebSocket;

/**
 * 创建桌面主进程专用的控制设置 WebSocket。
 *
 * @param url 控制面 WebSocket 地址。
 * @param handlers 连接消息、关闭和错误回调。
 * @returns 可关闭底层连接的 socket 句柄。
 */
export function createControlSettingsSocket(
  url: string,
  handlers: ControlSettingsSocketHandlers
): ControlSettingsSocket {
  return createControlSettingsSocketWithConstructor(readUndiciWebSocketConstructor(), url, handlers);
}

/**
 * 使用指定构造器创建控制设置 WebSocket。
 *
 * @param WebSocketConstructor WebSocket 客户端构造器。
 * @param url 控制面 WebSocket 地址。
 * @param handlers 连接消息、关闭和错误回调。
 * @returns 可关闭底层连接的 socket 句柄。
 */
export function createControlSettingsSocketWithConstructor(
  WebSocketConstructor: ControlSettingsWebSocketConstructor,
  url: string,
  handlers: ControlSettingsSocketHandlers
): ControlSettingsSocket {
  const socket = new WebSocketConstructor(url);

  socket.addEventListener("open", () => {
    handlers.onOpen?.();
  });
  socket.addEventListener("message", (event) => {
    handlers.onMessage(readSocketMessage(event.data));
  });
  socket.addEventListener("close", () => {
    handlers.onClose();
  });
  socket.addEventListener("error", (event) => {
    handlers.onError(readSocketError(event));
  });

  return {
    close(): void {
      socket.close();
    }
  };
}

/**
 * 运行时读取 undici WebSocket 构造器，避免 Electron main 入口静态打包大型客户端依赖。
 *
 * @returns undici 提供的 WebSocket 构造器。
 */
function readUndiciWebSocketConstructor(): ControlSettingsWebSocketConstructor {
  return (loadNodePackage("undici") as UndiciModule).WebSocket as unknown as ControlSettingsWebSocketConstructor;
}

/**
 * 把 WebSocket 原始消息统一转成 UTF-8 文本。
 *
 * @param data WebSocket message 事件传出的原始数据。
 * @returns 可交给控制事件解析器的 JSON 文本。
 */
function readSocketMessage(data: unknown): string {
  if (typeof data === "string") {
    return data;
  }

  if (Buffer.isBuffer(data)) {
    return data.toString("utf8");
  }

  if (data instanceof ArrayBuffer) {
    return Buffer.from(data).toString("utf8");
  }

  if (ArrayBuffer.isView(data)) {
    return Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString("utf8");
  }

  return String(data);
}

/**
 * 将 WebSocket error 事件归一成 Error，方便上层日志保留稳定形态。
 *
 * @param event WebSocket error 事件对象。
 * @returns 可直接记录或透传的 Error。
 */
function readSocketError(event: Event): Error {
  const candidate = event as Event & { error?: unknown; message?: unknown };

  if (candidate.error instanceof Error) {
    return candidate.error;
  }

  if (typeof candidate.message === "string" && candidate.message.trim()) {
    return new Error(candidate.message);
  }

  return new Error("control settings websocket error");
}
