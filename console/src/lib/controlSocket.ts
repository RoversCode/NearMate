import {
  calculateControlReconnectDelayMs,
  CONTROL_RECONNECT_STABLE_CONNECTION_MS
} from "./controlReconnectBackoff";

type ControlSocketMessage = {
  type: string;
  payload?: unknown;
};

export type ControlSocketHandlers = {
  onMessage: (message: ControlSocketMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (event: Event) => void;
};

export type ControlSocketScope = "runtime" | "status";

export type ControlSocketConnection = {
  close: () => void;
};

/**
 * 建立本地控制面 WebSocket 连接。
 *
 * 核心入参：baseUrl 是控制面 HTTP 根地址，handlers 接收连接生命周期和消息事件。
 * 预期输出：返回可关闭连接的轻量句柄。
 * 边界异常：服务端发送非 JSON 文本时忽略该消息，避免单条坏消息中断后续实时刷新。
 */
export function connectControlSocket(
  baseUrl: string,
  handlers: ControlSocketHandlers,
  scope: ControlSocketScope = "runtime"
): ControlSocketConnection {
  const url = new URL(baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/control";
  url.search = scope === "status" ? "?scope=status" : "";
  url.hash = "";

  let disposed = false;
  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let stableConnectionTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempt = 0;
  let connectionGeneration = 0;

  const clearReconnectTimer = (): void => {
    if (reconnectTimer === null) {
      return;
    }
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  };

  const clearStableConnectionTimer = (): void => {
    if (stableConnectionTimer === null) {
      return;
    }
    clearTimeout(stableConnectionTimer);
    stableConnectionTimer = null;
  };

  const scheduleReconnect = (): void => {
    clearStableConnectionTimer();
    if (disposed || reconnectTimer !== null) {
      return;
    }
    const delayMs = calculateControlReconnectDelayMs(reconnectAttempt);
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delayMs);
    socket?.close();
    socket = null;
  };

  const connect = (): void => {
    if (disposed) {
      return;
    }
    const generation = connectionGeneration + 1;
    connectionGeneration = generation;
    try {
      const nextSocket = new WebSocket(url.toString());
      socket = nextSocket;
      const isCurrentConnection = (): boolean => (
        !disposed
        && connectionGeneration === generation
        && socket === nextSocket
      );
      // 浏览器在 error 之后通常还会补发 close；用标记位保证 onClose 对调用方恰好触发一次。
      let closeNotified = false;
      const notifyClose = (): void => {
        if (closeNotified) {
          return;
        }
        closeNotified = true;
        handlers.onClose?.();
      };
      addSocketListener(nextSocket, "open", () => {
        if (!isCurrentConnection()) {
          return;
        }
        handlers.onOpen?.();
        clearStableConnectionTimer();
        stableConnectionTimer = setTimeout(() => {
          if (isCurrentConnection()) {
            reconnectAttempt = 0;
          }
          stableConnectionTimer = null;
        }, CONTROL_RECONNECT_STABLE_CONNECTION_MS);
      });
      addSocketListener(nextSocket, "close", () => {
        if (!isCurrentConnection()) {
          return;
        }
        socket = null;
        notifyClose();
        scheduleReconnect();
      });
      addSocketListener(nextSocket, "error", (event) => {
        if (!isCurrentConnection()) {
          return;
        }
        handlers.onError?.(event);
        // 随后的 close 事件会被 isCurrentConnection 拦截（重连已把 socket 置空），
        // 必须在这里主动补齐 onClose，否则调用方的断线兜底逻辑永远不会启动。
        notifyClose();
        scheduleReconnect();
      });
      addSocketListener(nextSocket, "message", (event) => {
        if (!isCurrentConnection()) {
          return;
        }
        try {
          // 后端控制事件统一采用 JSON 格式；解析失败说明该消息不符合契约，跳过可保留连接可用性。
          handlers.onMessage(JSON.parse(String(event.data)) as ControlSocketMessage);
        } catch {
          return;
        }
      });
    } catch (error) {
      if (disposed || connectionGeneration !== generation) {
        return;
      }
      handlers.onError?.(error instanceof Event ? error : new Event("error"));
      scheduleReconnect();
    }
  };

  // WebSocket 是控制台后续实时刷新的唯一长连接入口；连接断开后由同一状态机退避重连。
  connect();

  return {
    close: () => {
      // 页面卸载或热更新时主动关闭连接，避免本地后端保留过期连接。
      disposed = true;
      connectionGeneration += 1;
      clearReconnectTimer();
      clearStableConnectionTimer();
      reconnectAttempt = 0;
      socket?.close();
      socket = null;
    }
  };
}

function addSocketListener<K extends keyof WebSocketEventMap>(
  socket: WebSocket,
  type: K,
  listener: (event: WebSocketEventMap[K]) => void
): void {
  if (typeof socket.addEventListener === "function") {
    socket.addEventListener(type, listener as EventListener);
    return;
  }

  // 测试替身和少数精简 WebSocket 实现只提供 onopen/onmessage 形式；这里保持控制通道可用。
  (socket as unknown as Record<string, unknown>)[`on${type}`] = listener;
}
